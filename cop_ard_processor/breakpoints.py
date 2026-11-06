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
"""Breakpoint management and interactive inspection utilities for COP ARD processing units.

Provides the `resumable_breakpoint` decorator to cache intermediate execution states
to disk (e.g., Zarr stores) and allow downstream pipeline stages to resume execution
without re-running expensive upstream processing steps.
"""

import os
from functools import wraps
from typing import Any, Callable

import xarray as xr
from cop_ard_core.logger import logger
from eopf import EOConfiguration
from eopf.common.file_utils import AnyPath
from eopf.computing.breakpoint import TypeFunctionWritersDispatcher
from eopf.exceptions.errors import EOConfigurationError, InvalidConfigurationError


def _inspection_sandbox(breakpoint_data: dict, breakpoint_identifier: str, log_obj: Any = logger):
    """Launch an interactive IPython inspection shell on intermediate breakpoint data.

    Parameters
    ----------
    breakpoint_data : dict
        Dictionary containing intermediate output objects to inspect.
    breakpoint_identifier : str
        Identifier name of the active breakpoint.
    log_obj : Any, default logger
        Logger instance used to emit diagnostic messages.
    """
    try:
        import IPython

        IPython.embed()
    except ImportError:
        log_obj.warning(f"IPython package not available, skipping {breakpoint_identifier} breakpoint inspection")


def resumable_breakpoint(
    identifier: str,
    description: str | None = None,
    expected_outputs: int = 1,
    optional_outputs: list[str] = [],
) -> Callable[..., Any]:
    """Decorator factory configuring resumable disk checkpoints and debugging for processing units.

    Registers breakpoint and inspection parameters in `EOConfiguration`, restores persisted
    outputs when available, and saves newly computed execution outputs to Zarr stores.

    Parameters
    ----------
    identifier : str
        Unique identifier name for the processing breakpoint.
    description : str, optional
        Human-readable description for parameter registration.
    expected_outputs : int, default 1
        Number of output stores expected to consider the breakpoint cache valid and complete.
    optional_outputs : list of str, default []
        List of optional dictionary keys to populate with None if missing from disk.

    Returns
    -------
    Callable
        Decorator wrapping the target execution method.

    Raises
    ------
    InvalidConfigurationError
        If `identifier` is a reserved keyword name.
    EOConfigurationError
        If a breakpoint parameter with the same identifier has already been registered.
    """
    if identifier in ["activate_all", "folder", "persist"]:
        raise InvalidConfigurationError(f"{identifier} is not allowed for breakpoint name")
    filename_prefix = f"breakpoint_{identifier}_"

    breakpoint_id = f"breakpoints__{identifier}"
    if breakpoint_id in EOConfiguration()._registered_params:
        raise EOConfigurationError("Breakpoint already registered")

    EOConfiguration().register_requested_parameter(
        f"breakpoints__{identifier}",
        default_value=False,
        param_is_optional=True,
        description=description,
    )
    EOConfiguration().register_requested_parameter(
        f"inspection__{identifier}",
        default_value=False,
        param_is_optional=True,
        description=description,
    )

    def decorator(func_in: Callable[..., Any]) -> Callable[..., Any]:
        """Wrap the target callable with breakpoint retrieval, execution, and persistence logic.

        Parameters
        ----------
        func_in : Callable
            The target execution callable (e.g. `EOProcessingUnit.run`).

        Returns
        -------
        Callable
            The wrapped callable intercepting execution.
        """
        @wraps(func_in)
        def _method_wrapper(
            *args: Any,
            **kwargs: Any,
        ) -> Any:
            """Execute or resume unit execution and handle breakpoint storage and inspection.

            Parameters
            ----------
            *args : Any
                Positional arguments forwarded to `func_in`.
            **kwargs : Any
                Keyword arguments forwarded to `func_in`.

            Returns
            -------
            Any
                Dictionary containing processed or restored `xr.DataTree` objects.

            Raises
            ------
            ValueError
                If output is not a dictionary containing exclusively `xr.DataTree` values.
            """
            activate_inspection = bool(EOConfiguration().get(f"inspections__{identifier}", default=False))
            activate_inspection |= bool(EOConfiguration().get("inspections__activate_all", default=False))
            activate_breakpoint = bool(EOConfiguration().get(f"breakpoints__{identifier}", default=False))
            activate_breakpoint |= bool(EOConfiguration().get("breakpoints__activate_all", default=False))
            persist_inspection_outputs = bool(
                EOConfiguration().get(f"inspections__persist__{identifier}", default=False)
            )
            persist_inspection_outputs |= bool(EOConfiguration().get("inspections__persist_all", default=False))
            breakpoint_path = EOConfiguration().get("breakpoints__folder", default=None)
            breakpoint_storage_options = EOConfiguration().get("breakpoints__storage_options", default={})
            if not activate_breakpoint:
                logger.debug(f"Breakpoint {identifier} is deactivated")
                obj = func_in(*args, **kwargs)
                if activate_inspection:
                    _inspection_sandbox(obj, identifier, logger)
                return obj
            if breakpoint_path:
                breakpoint_storage_options = (
                    breakpoint_storage_options if breakpoint_storage_options is not None else {}
                )
                breakpoint_folder = AnyPath.cast(f"{breakpoint_path}/{identifier}", **breakpoint_storage_options)
            else:
                breakpoint_folder = AnyPath.cast(os.getcwd()) / "breakpoints" / identifier
            breakpoint_folder.mkdir(exist_ok=True)
            if not breakpoint_folder.islocal():
                (breakpoint_folder / "placeholder.txt").touch()

            matching_files = breakpoint_folder.glob(f"{filename_prefix}*")
            read_from_breakpoints = bool(matching_files) and len(matching_files) == expected_outputs
            if read_from_breakpoints:
                obj = {}
                for file in matching_files:
                    filename = file.basename.removeprefix(filename_prefix)
                    key = filename.removesuffix("_None.zarr")
                    obj[key] = xr.open_datatree(file.to_zarr_store(), chunks={}, engine="zarr")  # type: ignore
                for optional_output in optional_outputs:
                    if optional_output not in obj:
                        obj[optional_output] = None
                logger.info(f"Breakpoint files for {identifier} found, skipping execution")
            else:
                obj = func_in(*args, **kwargs)

            if not isinstance(obj, dict):
                raise ValueError("Only dict breakpoint outputs are supported")
            if any(not isinstance(val, xr.DataTree | None) for val in obj.values()):
                raise ValueError("Only DataTrees allowed as breakpoint dict values")

            if activate_inspection:
                _inspection_sandbox(obj, identifier, logger)

            if not read_from_breakpoints or (activate_inspection and persist_inspection_outputs):
                logger.debug(f"WRITE Breakpoint for {func_in.__name__} in {breakpoint_folder}")  # type: ignore[unresolved-attribute]
                try:
                    TypeFunctionWritersDispatcher.handle_value(obj, breakpoint_folder, filename_prefix)
                except Exception as err:
                    logger.error(f"Failed to write breakpoint {identifier}: {err}")
                    raise
            return obj

        return _method_wrapper

    return decorator