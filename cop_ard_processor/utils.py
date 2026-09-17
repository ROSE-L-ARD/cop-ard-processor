# Copyright 2026 B-Open
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import copy
import functools
import json
import logging
import os
import pathlib
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from typing import Any, Mapping

import eopf
import numpy as np
import xarray as xr
from cop_ard_app.io import Path, makedirs, normpath, read
from cop_ard_core.logger import logger
from eopf.common.file_utils import AnyPath
from eopf.common.temp_utils import EOTemporaryFolder
from eopf.computing.breakpoint import declare_as_breakpoint
from eopf.computing.types import ADF
from eopf.config.config import EOConfiguration
from eopf.exceptions.errors import CriticalException
from eopf.triggering.general_utils import resolve_storage_options

import cop_ard_processor

CONFIG = f"{pathlib.Path(__file__).parent.resolve()}/conf/CONFIG.json"


def log_time(block_id: str, logger: logging.Logger):
    def decorator(method: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(method)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            start_time = time.perf_counter()
            logger.info(f"Started : {block_id}")
            output = method(*args, **kwargs)
            total_time = time.perf_counter() - start_time
            logger.info(f"Completed : {block_id} in {total_time:.4f} seconds")
            return output

        return wrapper

    return decorator


def get_wdir_dynamic(folder: str = "dynamic") -> tuple[str, Any]:
    """
    Retrieves the dynamic (temporary) working directory and storage options.

    This directory is typically used for intermediate processing artifacts that
    do not need to be persisted long-term.

    Returns
    -------
    tuple[str, dict[str, Any]]
        A tuple containing:
        - The path to the dynamic working directory.
        - A dictionary of storage options (e.g., for S3 access).
    """
    # Instantiate the configuration once to avoid repeated lookups
    config = EOConfiguration()
    # Get the S3 secret alias from configuration
    alias = config.get("temporary__folder_s3_secret")
    # Extract the unique temporary folder name
    temporary_dir: AnyPath = EOTemporaryFolder().get()
    # Construct the full working directory path
    working_dir = normpath((temporary_dir / folder).url)
    # Retrieve the secret storage options securely
    # If alias is None, this safely defaults to an empty dict
    storage_options = resolve_storage_options({"s3_secret_alias": alias}).get("storage_options", None)
    # Ensure the directory exists
    makedirs(working_dir, exist_ok=True, storage_options=storage_options)

    return working_dir, storage_options


def get_wdir_static(adfs: Mapping[str, ADF] | None) -> tuple[str, str, dict[str, Any]]:
    """
    Retrieves the static working directory and storage options from ADFs.

    This directory is used for persistent data which can be reused across runs.
    If a single-shot run is requested, it falls back to a dynamic directory.

    Parameters
    ----------
    adfs : Mapping[str, Any]
        A mapping of Auxiliary Data Files (ADFs), which must contain the key
        'REFDB'.
    single_shot_run : bool
        If True, overrides the static directory and uses a dynamic one instead.

    Returns
    -------
    tuple[str, dict[str, Any]]
        A tuple containing:
        - The path to the working directory.
        - A dictionary of storage options.

    Raises
    ------
    KeyError
        If 'REFDB' is not found in the provided `adfs`.
    """
    import posixpath

    if adfs is None:
        raise ValueError("Expected mapping of adfs, got None")
    if "REFDB" not in adfs:
        raise KeyError("The 'REFDB' key is missing from the provided ADFs.")

    path = get_adf(adfs["REFDB"])
    working_dir, storage_options = path.path, path.storage_options
    reference_db_path_static = posixpath.join(working_dir, "reference_db_static")
    reference_db_path_dynamic = posixpath.join(working_dir, "reference_db_dynamic")

    return reference_db_path_static, reference_db_path_dynamic, storage_options


def get_adf(adf, ensure_local: bool = False):
    storage_options = None
    path = adf.path
    params = adf.adf_params
    if params is not None:
        storage_options = params.get("storage_options", None)
    adf_path = Path(path=path, storage_options=storage_options)
    if ensure_local:
        adf_path = adf_path.ensure_local()
    return adf_path


def get_config(mode: str) -> dict:
    """Loads the main JSON configuration file from the ADFs.

    Parameters
    ----------
    adfs : Mapping[str, ADF] | None, optional
        A mapping of Auxiliary Data Files. Must contain 'CONFIG' ADF.

    Returns
    -------
    dict
        The loaded JSON configuration as a dictionary.

    Raises
    ------
    Exception
        If the 'CONFIG' ADF is missing from `adfs`.
    """
    # Open and load the JSON file
    with open(CONFIG, "r") as file:
        config = json.load(file)[mode]
    return config


def read_input_from_kwargs(kwargs: dict):
    """
    Process the 'slcs_path' from kwargs, reading non-SAFE files into memory.

    This function takes a kwargs dictionary and extracts 'slcs_path'.
    'slcs_path' can be a single string or a list of strings.
    Each string in the list is checked:
    - If it contains ".SAFE", it's assumed to be a raw SAFE directory path
      and is kept as a string.
    - If it does not contain ".SAFE", it's assumed to be a path to a
      pre-processed data store (like Zarr) and is read using the `read`
      function into an xarray object (e.g., DataTree).

    Parameters
    ----------
    kwargs : dict
        A dictionary of keyword arguments, expected to contain 'slcs_path'.
    storage_options : dict
        Storage options to be passed to the `read` function for accessing
        data (e.g., credentials for S3).

    Returns
    -------
    list
        A list containing a mix of strings (for .SAFE paths) and
        xarray objects (for pre-processed data).
    """
    # Get the 'slcs_path' value from the kwargs dictionary
    in_files = kwargs["slcs_path"]
    storage_options = kwargs.get("storage_options_slc", None)

    # Ensure in_files is a list, even if a single string was provided
    if isinstance(in_files, str):
        in_files = [in_files]

    # Use a list comprehension to process each item in in_files
    # If ".SAFE" is in the string, keep the string path
    # Otherwise, call the `read` function to load the data
    in_files = [
        (f if ".SAFE" in f else read(f, consolidate_metadata=False, storage_options=storage_options)) for f in in_files
    ]

    # Return the processed list of file paths and/or loaded data objects
    return in_files


def check_output(output: xr.DataTree) -> None:
    """
    Validate and log potential issues in the output data structure.

    This function iterates through an xr.DataTree,
    applies masking rules to specific variables, and checks for the
    presence of NaN values. If NaNs are detected, a log message is
    emitted with the hierarchical path of the variable.

    Parameters
    ----------
    output : xr.DataTree
    """
    for name_data, data in output.items():
        for group_name, group in data.items():
            for var_name, var in group.items():
                if var_name in ["orbit", "phase_demodulation", "ETAD", "gamma0"]:
                    continue
                if np.isnan(np.abs(var)).any():
                    # NOTE: Inform of possible presence of nan values in the output data.
                    logger.critical(f"{name_data}/{group_name}/{var_name} has nan values")


def get_instrument_mode(in_files: list[str]):
    """Identify and validate that all input files belong to the same instrument mode/sensor.

    Parameters
    ----------
    in_files : list of str.
        List of input file paths.

    Returns
    -------
    str
        Validated acquisition mode or sensor type ('IW', 'EW', 'SM', 'SAOCOM', 'ROSEL').

    Raises
    ------
    ValueError
        If `in_files` is empty, if any file mode cannot be identified,
        or if input files contain a mix of different modes/sensors.
    """
    if not in_files:
        raise ValueError("The input list 'in_files' cannot be empty.")

    def _extract_single_mode(item: str) -> str:
        """Extract acquisition mode or sensor from a single file path or DataTree."""
        search_string = item

        search_upper = search_string.upper()

        # Check SAOCOM pattern (e.g., S1A_OPER_SAR_EOSSP... or .xemt extension)
        if any(token in search_upper for token in ["SAOCOM", "EOSSP", ".XEMT"]):
            return "SAOCOM"

        # Check RoseL pattern
        if "ROSEL" in search_upper:
            return "ROSEL"

        # Check for standard modes (IW, EW)
        for mode in ["IW", "EW"]:
            if f"_{mode}_" in search_upper:
                return mode
        
        # Check for StripMap (SM) modes, which use beam identifiers S1 through S6
        stripmap_beams = ["S1", "S2", "S3", "S4", "S5", "S6"]
        if any(f"_{beam}_" in search_upper for beam in stripmap_beams):
            return "SM"

        raise ValueError(f"Unable to identify a valid instrument mode or sensor from input: '{search_string}'")

    # 1. Identify mode for every file in the input list
    detected_modes = [_extract_single_mode(f) for f in in_files]

    # 2. Verify that all files belong to the exact same mode/sensor
    unique_modes = set(detected_modes)
    if len(unique_modes) > 1:
        raise ValueError(
            f"Mixed instrument modes detected in input files: {unique_modes}. "
            "All input files must belong to the exact same mode/sensor."
        )

    return detected_modes[0]


def _get_slcs_path(workdir_path_dynamic: str):
    return os.path.join(workdir_path_dynamic, "slcs")


def _is_multiburst(slcs: xr.DataTree):
    return "bursts" in slcs


def _json_safe(val):
    """Convert complex Python data structures into JSON-serializable strings.

    Parameters
    ----------
    val : Any
        Input value to be sanitized (dict, list, tuple, or scalar).

    Returns
    -------
    Any
        JSON string representation if `val` is a collection, otherwise the original primitive.
    """
    if isinstance(val, (dict, list, tuple)):
        return json.dumps(val)
    return val


def log_processing_parameters(
    params: dict[str, Any], unit: Any, logger: Any, show_global_metadata: bool = False
) -> None:
    """Log unit processing parameters in a clean, human-readable multiline format.

    Parameters
    ----------
    params : dict
        Dictionary containing processing parameters.
    unit : Any
        The processing unit instance (`self`) or name string.
    logger : Any
        Structlog or standard logger instance.
    show_global_metadata : bool, default False
        If False, filters out static global metadata (e.g., ATBD links, providers)
        to keep the debug log focused exclusively on execution settings.
    """
    unit_name = unit.__class__.__name__ if hasattr(unit, "__class__") and not isinstance(unit, str) else str(unit)

    # Static metadata keys to optionally suppress from immediate debug view
    metadata_keys = {
        "atbd_link",
        "facility",
        "product_timeliness",
        "product_timeliness_category",
        "product_data_access",
        "product_pixel_coordinate_convention",
        "product_family_specification",
        "cslc_status",
        "providers",
    }

    display_dict = {}
    for key, val in params.items():
        if not show_global_metadata and key in metadata_keys:
            continue

        # Temporarily decode serialized JSON strings back to Python objects for clean display
        if isinstance(val, str) and val.startswith(("{", "[")):
            try:
                display_dict[key] = json.loads(val)
                continue
            except (json.JSONDecodeError, TypeError):
                pass

        display_dict[key] = val

    # Render formatted multiline string
    formatted_json = json.dumps(display_dict, indent=2, default=str)
    logger.debug(f"[{unit_name}] Execution parameters:\n{formatted_json}")
