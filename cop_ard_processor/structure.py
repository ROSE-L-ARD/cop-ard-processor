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

"""
Structure checks module for COP ARD Processor.

Provides functions to recursively validate the internal structure of an
xarray.DataTree against a YAML template specification. Supports bidirectional checks
(detecting both missing required items and unexpected/extra items).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import xarray as xr
import yaml

DEFAULT_LOGGER = logging.getLogger(__name__)


def check_map(keyword: str):
    """Map a reserved template keyword to its corresponding validation function."""
    func_map_dict = {
        "attributes": check_attributes,
        "coords": check_coordinates,
        "groups": check_groups,
        "data_variables": check_data_variables,
        "data": check_data,
        "dimensions": check_dimensions,
    }
    try:
        return func_map_dict[keyword]
    except KeyError:
        raise ValueError(f"Invalid keyword in structure template: {keyword}")


def check_attributes(
    input_data: xr.DataTree | xr.DataArray | dict,
    template: dict,
    path: str = "",
    allow_extra: bool = False,
    logger: Any = DEFAULT_LOGGER,
) -> list[str]:
    """
    Validate attributes bidirectionally against a template specification.
    """
    errors = []
    if isinstance(input_data, (xr.DataTree, xr.DataArray)):
        attrs = input_data.attrs
    else:
        attrs = input_data

    if not isinstance(attrs, dict):
        err = f"Attribute container at '{path}' is not a valid dictionary."
        logger.error(err)
        return [err]

    # 1. Forward check: Template -> Input
    for key, value in template.items():
        if key in ("optional", "size"):
            continue

        current_path = f"{path}/{key}" if path else key
        is_optional = isinstance(value, dict) and value.get("optional", False)

        if key not in attrs:
            if not is_optional:
                err = f"Missing attribute key: {current_path}"
                errors.append(err)
                logger.error(err)
            continue

        # Handle list-indexed attribute inspection (e.g., LIST0 -> attrs[key][0])
        has_list_keys = False
        if isinstance(value, dict):
            for k, v in value.items():
                if k.startswith("LIST"):
                    has_list_keys = True
                    try:
                        counter = int(k[4:])
                        target_elem = attrs[key][counter]
                        errors.extend(
                            check_attributes(
                                target_elem,
                                v,
                                f"{current_path}[{counter}]",
                                allow_extra=allow_extra,
                                logger=logger,
                            )
                        )
                    except (IndexError, KeyError, TypeError) as exc:
                        err = f"Cannot access list index '{k}' for attribute {current_path}: {exc}"
                        errors.append(err)
                        logger.error(err)

        if not has_list_keys and isinstance(value, dict) and value:
            if not isinstance(attrs[key], dict):
                err = (
                    f"Incorrect type for attribute key '{current_path}': expected dict, got {type(attrs[key]).__name__}"
                )
                errors.append(err)
                logger.error(err)
            else:
                errors.extend(
                    check_attributes(
                        attrs[key],
                        value,
                        current_path,
                        allow_extra=allow_extra,
                        logger=logger,
                    )
                )

    # 2. Reverse check: Input -> Template (detect unexpected attributes)
    if not allow_extra:
        for attr_key in attrs:
            if attr_key not in template:
                err = f"Unexpected extra attribute at '{path}': '{attr_key}'"
                errors.append(err)
                logger.error(err)

    return errors


def check_coordinates(
    dt: xr.DataTree | xr.DataArray,
    template: dict,
    path: str = "",
    allow_extra: bool = False,
    logger: Any = DEFAULT_LOGGER,
) -> list[str]:
    """
    Validate coordinates bidirectionally.
    """
    errors = []

    # 1. Forward check: Template -> DataTree
    for coord_name, coord_spec in template.items():
        if coord_name == "optional":
            continue

        is_optional = isinstance(coord_spec, dict) and coord_spec.get("optional", False)

        if coord_name not in dt.coords:
            if not is_optional:
                err = f"Missing coordinate: {path}/{coord_name}"
                errors.append(err)
                logger.error(err)
        else:
            errors.extend(
                check_structure(
                    dt=dt.coords[coord_name],
                    template=coord_spec if isinstance(coord_spec, dict) else {},
                    path=f"{path}/coords/{coord_name}",
                    allow_extra=allow_extra,
                    logger=logger,
                )
            )

    # 2. Reverse check: DataTree -> Template (detect extra coordinates)
    if not allow_extra:
        for coord_name in dt.coords:
            if coord_name not in template:
                err = f"Unexpected extra coordinate at '{path}': '{coord_name}'"
                errors.append(err)
                logger.error(err)

    return errors


def check_groups(
    dt: xr.DataTree,
    template: dict,
    path: str = "",
    allow_extra: bool = False,
    logger: Any = DEFAULT_LOGGER,
) -> list[str]:
    """
    Validate child groups (sub-nodes) bidirectionally.
    """
    errors = []

    # 1. Forward check: Template -> DataTree
    for group_name, group_spec in template.items():
        if group_name == "optional":
            continue

        is_optional = isinstance(group_spec, dict) and group_spec.get("optional", False)

        if group_name not in dt.children:
            if not is_optional:
                err = f"Missing group: {path}/{group_name}"
                errors.append(err)
                logger.error(err)
        else:
            errors.extend(
                check_structure(
                    dt=dt.children[group_name],
                    template=group_spec if isinstance(group_spec, dict) else {},
                    path=f"{path}/groups/{group_name}",
                    allow_extra=allow_extra,
                    logger=logger,
                )
            )

    # 2. Reverse check: DataTree -> Template (detect unexpected groups)
    if not allow_extra:
        for child_name in dt.children:
            if child_name not in template:
                err = f"Unexpected extra group at '{path}': '{child_name}'"
                errors.append(err)
                logger.error(err)

    return errors


def check_data_variables(
    dt: xr.DataTree,
    template: dict,
    path: str = "",
    allow_extra: bool = False,
    logger: Any = DEFAULT_LOGGER,
) -> list[str]:
    """
    Validate data variables bidirectionally.
    """
    errors = []

    # 1. Forward check: Template -> DataTree
    for var_name, var_spec in template.items():
        if var_name == "optional":
            continue

        is_optional = isinstance(var_spec, dict) and var_spec.get("optional", False)

        if var_name not in dt.data_vars:
            if not is_optional:
                err = f"Missing data variable: {path}/{var_name}"
                errors.append(err)
                logger.error(err)
        else:
            errors.extend(
                check_structure(
                    dt=dt.data_vars[var_name],
                    template=var_spec if isinstance(var_spec, dict) else {},
                    path=f"{path}/data_variables/{var_name}",
                    allow_extra=allow_extra,
                    logger=logger,
                )
            )

    # 2. Reverse check: DataTree -> Template (detect unexpected data variables)
    if not allow_extra:
        for var_name in dt.data_vars:
            if var_name not in template:
                err = f"Unexpected extra data variable at '{path}': '{var_name}'"
                errors.append(err)
                logger.error(err)

    return errors


def check_data(
    dt: xr.DataTree,
    template: dict,
    path: str = "",
    allow_extra: bool = False,
    logger: Any = DEFAULT_LOGGER,
) -> list[str]:
    """Validate all immediate child nodes of a DataTree against a shared schema."""
    errors = []
    for child_name, child in dt.children.items():
        errors.extend(
            check_structure(
                dt=child,
                template=template,
                path=f"{path}/{child_name}" if path else child_name,
                allow_extra=allow_extra,
                logger=logger,
            )
        )
    return errors


def check_dimensions(
    dt: xr.DataTree | xr.DataArray,
    dimensions: dict,
    path: str = "",
    allow_extra: bool = False,
    logger: Any = DEFAULT_LOGGER,
) -> list[str]:
    """Validate dimensions and sizes bidirectionally."""
    errors = []

    # 1. Forward check: Template -> DataTree
    for dim_name, dim_spec in dimensions.items():
        if dim_name == "optional":
            continue

        is_optional = isinstance(dim_spec, dict) and dim_spec.get("optional", False)

        if dim_name not in dt.dims:
            if not is_optional:
                err = f"Missing dimension: {path}/{dim_name}"
                errors.append(err)
                logger.error(err)
        else:
            if isinstance(dim_spec, dict) and "size" in dim_spec:
                expected_size = dim_spec["size"]
                actual_size = dt.sizes[dim_name]
                if actual_size != expected_size:
                    err = f"Incorrect size for dimension {path}/{dim_name}: expected {expected_size}, got {actual_size}"
                    errors.append(err)
                    logger.error(err)

            if isinstance(dim_spec, dict) and dim_spec:
                if hasattr(dt, "coords") and dim_name in dt.coords:
                    errors.extend(
                        check_structure(
                            dt[dim_name],
                            dim_spec,
                            f"{path}/dimensions/{dim_name}",
                            allow_extra=allow_extra,
                            logger=logger,
                        )
                    )

    # 2. Reverse check: DataTree -> Template (detect unexpected dimensions)
    if not allow_extra:
        for dim_name in dt.dims:
            if dim_name not in dimensions:
                err = f"Unexpected extra dimension at '{path}': '{dim_name}'"
                errors.append(err)
                logger.error(err)

    return errors


def check_structure(
    dt: xr.DataTree | xr.DataArray,
    template: dict,
    path: str = "",
    allow_extra: bool = False,
    logger: Any = DEFAULT_LOGGER,
) -> list[str]:
    """Main dispatcher evaluating reserved keywords in a template against a target node."""
    reserved_keywords = [
        "data",
        "attributes",
        "dimensions",
        "coords",
        "data_variables",
        "groups",
    ]
    errors = []
    if not isinstance(template, dict):
        return errors

    for keyword in reserved_keywords:
        if keyword in template:
            checker_func = check_map(keyword)
            # Pass allow_extra parameter down the recursive check chain
            try:
                errors.extend(
                    checker_func(
                        dt,
                        template[keyword],
                        path,
                        allow_extra=allow_extra,
                        logger=logger,
                    )
                )
            except TypeError:
                errors.extend(checker_func(dt, template[keyword], path, logger=logger))

    return errors


def check_ARD_outputs(
    dt: xr.DataTree,
    product_id: str,
    allow_extra: bool = False,
    logger: Any = DEFAULT_LOGGER,
) -> None:
    """High-level validation entry point for COP ARD output products (CSLC, GMLP)."""
    parent = Path(__file__).parent.resolve()
    prod_id_lower = product_id.lower()

    if "cslc" in prod_id_lower:
        template_file = parent / "conf" / "COP_ARD_CSLC.yaml"
    elif "gmlp" in prod_id_lower:
        template_file = parent / "conf" / "COP_ARD_GMLP.yaml"
    else:
        raise ValueError(
            f"Unsupported product ID '{product_id}' for ARD output validation. "
            "Expected product ID containing 'cslc' or 'gmlp'."
        )

    if not template_file.exists():
        raise FileNotFoundError(f"Structure template file not found at: {template_file}")

    with open(template_file, "r", encoding="utf-8") as f:
        template = yaml.safe_load(f)

    # Run bidirectional check across the DataTree
    checks = check_structure(dt, template, allow_extra=allow_extra, logger=logger)
    if checks:
        raise ValueError(f"ARD DataTree structure validation failed for '{product_id}':\n" + "\n".join(checks))
