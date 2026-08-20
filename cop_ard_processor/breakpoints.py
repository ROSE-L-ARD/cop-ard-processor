import logging
import os
from functools import wraps
from typing import Any, Callable

import xarray as xr
from eopf import EOConfiguration
from eopf.common.file_utils import AnyPath
from eopf.computing.breakpoint import TypeFunctionWritersDispatcher
from eopf.exceptions.errors import EOConfigurationError, InvalidConfigurationError


def _inspection_sandbox(breakpoint_data: dict, breakpoint_identifier: str, logger: logging.Logger):
    try:
        import IPython

        IPython.embed()
    except ImportError:
        logger.warning("IPython package not available, skipping {breakpoint_identifier} breakpoint inspection")


def resumable_breakpoint(
    identifier: str,
    description: str | None = None,
    expected_outputs: int = 1,
) -> Callable[..., Any]:
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
        @wraps(func_in)
        def _method_wrapper(
            *args: Any,
            **kwargs: Any,
        ) -> Any:
            logger = logging.getLogger("eopf.breakpoint")
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
                    key = filename.split("_")[0]
                    obj[key] = xr.open_datatree(file.to_zarr_store(), engine="zarr")  # type: ignore
                logger.info(f"Breakpoint files for {identifier} found, skipping execution")
            else:
                obj = func_in(*args, **kwargs)

            if not isinstance(obj, dict):
                raise ValueError("Only dict breakpoint outputs are supported")
            if any(not isinstance(val, xr.DataTree) for val in obj.values()):
                raise ValueError("Only DataTrees allowed as breakpoint dict values")

            if activate_inspection:
                _inspection_sandbox(obj, identifier, logger)

            if not read_from_breakpoints or (activate_inspection and persist_inspection_outputs):
                logger.debug(f"WRITE Breakpoint for {func_in.__name__} in {breakpoint_folder}")  # ty: ignore[unresolved-attribute]
                try:
                    TypeFunctionWritersDispatcher.handle_value(obj, breakpoint_folder, filename_prefix)
                except Exception as err:
                    logger.error(err)
                    raise
            return obj

        return _method_wrapper

    return decorator
