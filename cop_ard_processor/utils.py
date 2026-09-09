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
import cop_ard_processor
import copy
import functools
import json
import logging
import pathlib
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from typing import Any, Mapping

import eopf
import numpy as np
import xarray as xr
from eopf.common.file_utils import AnyPath
from eopf.common.temp_utils import EOTemporaryFolder
from eopf.computing.breakpoint import declare_as_breakpoint
from eopf.computing.types import ADF
from eopf.config.config import EOConfiguration
from eopf.exceptions.errors import CriticalException
from eopf.triggering.general_utils import resolve_storage_options
from cop_ard_app.io import Path, makedirs, normpath, read

import cop_ard_processor

CONFIG = f"{pathlib.Path(__file__).parent.resolve()}/conf/CONFIG.json"
logger = logging.getLogger(__name__)


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


@contextmanager
def logging_level(level: int, logger: logging.Logger | None = None) -> Generator[Any, Any, Any]:
    logger = logger or logging.getLogger()
    old_level = logger.level
    logger.setLevel(level)
    try:
        yield
    finally:
        logger.setLevel(old_level)


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


def get_instrument_mode(in_files: list[xr.DataTree | str]):
    data = in_files[0]
    modes = ["IW", "EW"]
    # Extract the string we need to search through
    if isinstance(data, str):
        search_string = data
    else:
        search_string = next(iter(data.keys()))
    mode = next((m for m in modes if m in search_string), "SM")

    return mode


def breakpoints(products: list, names: list, unit: str):
    for k, v in zip(names, products):
        if isinstance(v, dict):
            v_copy = copy.deepcopy(v)
            for key, value in v_copy.items():
                v_copy[key] = json.dumps(value)
            v = xr.DataTree.from_dict({k: xr.Dataset(v_copy)})
        elif v is None:
            continue
        brkp_trigger = declare_as_breakpoint(v, unit, filename_prefix=f"{k}_")
        brkp_trigger()


def add_processing_software(output: xr.DataTree) -> xr.DataTree:

    eopf_version = eopf.__version__
    s1_ard_version = cop_ard_processor.__version__

    # Group the root node and its immediate children (1st level only) into a single list
    nodes_to_update = [output] + list(output.values())

    for node in nodes_to_update:
        # 1. This will intentionally throw a KeyError if the exact structure doesn't exist
        stac = node.attrs["stac_discovery"]

        # 2. Update the nested dictionary
        stac["properties"]["processing:software"]["EOPF"] = eopf_version
        stac["properties"]["processing:software"]["Sentinel-1 ARD"] = s1_ard_version

        # 3. Explicitly reassign the top-level attribute so xarray registers the change
        node.attrs["stac_discovery"] = stac

    return output
