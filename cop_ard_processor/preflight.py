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
"""Layer-1 ("preflight") input validation for the COP ARD processing units.

This module implements an always-on, fail-safe validation engine that checks
the parameters handed to a processing unit's ``run`` method before any
processing takes place (plus, for Ingestion, the ETAD auxiliary data products
and SAFE archives).

The engine is purely cumulative: it never fails fast and never raises for
data problems. Every problem it finds is reported as a :class:`Violation` and
the caller (the per-unit ``preflight`` classmethod) raises
:class:`PreflightError` when the list of violations is non-empty.

Public entry point
------------------
``run_preflight(inputs, adfs, unit_name, **kwargs)`` runs, for the selected
unit (see `UNIT_SPECS`):

1. Resolved-parameter sanity (always on).
2. ETAD product integrity (Ingestion only, when an ETAD ADF is provided).
3. SAFE XML checks (Ingestion only, opt-in, only when the ``schema_validation``
   kwarg is exactly ``True``): well-formedness of every XML document in the
   archive plus validation against the XSD schemas bundled in the archive's
   ``support`` directory.
"""

import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import xarray as xr
from cop_ard_app.io import Path as Path_io
from lxml import etree

from cop_ard_processor.utils import get_instrument_mode

CONFIG_PATH = Path(__file__).parent / "conf" / "CONFIG.json"

_MISSING = object()


@dataclass(frozen=True)
class Violation:
    """A single preflight violation.

    Attributes
    ----------
    scope:
        The parameter, ADF key or path the violation refers to.
    detail:
        Human readable explanation of the problem. Rule violations always
        include the offending value.
    """

    scope: str
    detail: str


class PreflightError(ValueError):
    """Raised when a unit's preflight validation finds violations.

    The exception message has a fixed format (one line per violation, in
    order):

    <unit> preflight failed with <N> violation(s):
      - <scope>: <detail>
    """

    def __init__(self, violations: Sequence[Violation], unit: str = "Ingestion") -> None:
        self.violations = list(violations)
        self.unit = unit
        lines = [f"{unit} preflight failed with {len(self.violations)} violation(s):"]
        lines.extend(f"  - {violation.scope}: {violation.detail}" for violation in self.violations)
        self.message = "\n".join(lines)
        super().__init__(self.message)


# ---------------------------------------------------------------------------
# Field rules
# ---------------------------------------------------------------------------

CALIBRATION_TYPES = ("BetaZero", "GammaZero", "SigmaZero")
POLARIZATIONS = ("VV", "VH", "HH", "HV")


def _check_input_slcs_files(value: Any) -> list[str]:
    """Detail messages for `input_slcs_files` values of an accepted type."""
    details = []
    if isinstance(value, str):
        if not value:
            details.append(f"value {value!r} must be a non-empty SAFE file path")
    elif isinstance(value, list):
        if not value:
            details.append(f"value {value!r} is an empty list; at least one SAFE file is required")
        else:
            for index, item in enumerate(value):
                if not isinstance(item, str) or not item:
                    details.append(f"value {item!r} at index {index} must be a non-empty SAFE file path")
    return details


def _check_indices(value: Any) -> list[str]:
    """Detail messages for `indices`: non-negative ints, or a list thereof."""
    if isinstance(value, list):
        return [
            f"value {item!r} at index {index} is not a non-negative integer"
            for index, item in enumerate(value)
            if isinstance(item, bool) or not isinstance(item, int) or item < 0
        ]
    if isinstance(value, bool) or value < 0:
        return [f"value {value!r} is not a non-negative integer"]
    return []


def _check_polarization(value: Any) -> list[str]:
    """Detail messages for `polarization`: member(s) of POLARIZATIONS."""
    if isinstance(value, list):
        return [
            f"value {item!r} at index {index} is not a valid polarization: allowed values are {list(POLARIZATIONS)}"
            for index, item in enumerate(value)
            if not isinstance(item, str) or item not in POLARIZATIONS
        ]
    if not isinstance(value, str) or value not in POLARIZATIONS:
        return [f"value {value!r} is not a valid polarization: allowed values are {list(POLARIZATIONS)}"]
    return []


def _check_chunks(value: Any) -> list[str]:
    """Detail messages for `chunks`: a flat dict of positive ints."""
    return [
        f"value {item!r} for dimension {dimension!r} is not a positive integer"
        for dimension, item in value.items()
        if isinstance(item, bool) or not isinstance(item, int) or item <= 0
    ]


def _check_spacing(value: Any) -> list[str]:
    """Detail messages for `spacing`: a dict or sequence of positive numbers."""
    if isinstance(value, dict):
        items = list(value.values())
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        return []
    return [
        f"value {item!r} is not a positive number"
        for item in items
        if isinstance(item, bool) or not isinstance(item, (int, float)) or item <= 0
    ]


def _check_number_sequence(value: Any) -> list[str]:
    """Detail messages for list/tuple parameters: every item must be a number."""
    if not isinstance(value, (list, tuple)):
        return []
    return [
        f"value {item!r} at index {index} is not a number"
        for index, item in enumerate(value)
        if isinstance(item, bool) or not isinstance(item, (int, float))
    ]


@dataclass(frozen=True)
class FieldRule:
    """Validation rule for one resolved parameter.

    Attributes
    ----------
    name:
        Parameter name; also used as the violation scope.
    required:
        Whether the parameter must be present (and not None).
    allowed_types:
        Tuple of accepted types. Typing is strict: `bool` only matches real
        booleans and `int` never matches booleans.
    allowed_values:
        Optional whitelist of accepted values.
    min_value, max_value:
        Optional inclusive numeric bounds.
    pattern:
        Optional regex the string value must fully match.
    checker:
        Optional callable `value -> list[str]` returning extra violation
        details for values of an accepted type.
    message:
        Optional custom detail used when a required parameter is missing.
    """

    name: str
    required: bool = False
    allowed_types: tuple[type, ...] | None = None
    allowed_values: tuple[Any, ...] | None = None
    min_value: int | float | None = None
    max_value: int | float | None = None
    pattern: str | None = None
    checker: Callable[[Any], list[str]] | None = None
    message: str | None = None


def _type_matches(value: Any, types: tuple[type, ...]) -> bool:
    """Strict type test: `bool` requires a real bool, `int` rejects bools."""
    for type_ in types:
        if type_ is bool:
            if isinstance(value, bool):
                return True
        elif type_ is int:
            if isinstance(value, int) and not isinstance(value, bool):
                return True
        elif isinstance(value, type_):
            return True
    return False


def apply_rules(
    rules: Sequence[FieldRule],
    params: dict[str, Any],
    violations: list[Violation],
) -> None:
    """Apply a list of rules to a resolved-parameter dict.

    Every check of every rule is evaluated (never fail-fast) and all problems
    are appended to `violations` as `Violation(rule.name, detail)`.
    """
    for rule in rules:
        value = params.get(rule.name, _MISSING)
        if value is _MISSING or value is None:
            if rule.required:
                detail = rule.message or f"parameter '{rule.name}' is required but is missing"
                violations.append(Violation(rule.name, detail))
            continue

        if rule.allowed_types is not None and not _type_matches(value, rule.allowed_types):
            expected = " or ".join(type_.__name__ for type_ in rule.allowed_types)
            violations.append(
                Violation(
                    rule.name,
                    f"value {value!r} has an invalid type: expected {expected}, got {type(value).__name__}",
                )
            )
            continue

        if rule.allowed_values is not None and value not in rule.allowed_values:
            violations.append(
                Violation(rule.name, f"value {value!r} is not one of the allowed values: {list(rule.allowed_values)}")
            )

        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if rule.min_value is not None and value < rule.min_value:
                violations.append(Violation(rule.name, f"value {value!r} is below the minimum {rule.min_value}"))
            if rule.max_value is not None and value > rule.max_value:
                violations.append(Violation(rule.name, f"value {value!r} is above the maximum {rule.max_value}"))

        if rule.pattern is not None:
            if not isinstance(value, str):
                violations.append(
                    Violation(rule.name, f"value {value!r} is not a string and cannot match pattern {rule.pattern!r}")
                )
            elif re.fullmatch(rule.pattern, value) is None:
                violations.append(Violation(rule.name, f"value {value!r} does not match pattern {rule.pattern!r}"))

        if rule.checker is not None:
            violations.extend(Violation(rule.name, detail) for detail in rule.checker(value))


RULES: tuple[FieldRule, ...] = (
    FieldRule(
        name="input_slcs_files",
        required=True,
        allowed_types=(list, str),
        checker=_check_input_slcs_files,
        message="parameter 'input_slcs_files' is required: at least one SAFE file path must be provided",
    ),
    FieldRule(name="calibration_type", allowed_types=(str,), allowed_values=CALIBRATION_TYPES),
    FieldRule(name="deramp", allowed_types=(bool,)),
    FieldRule(name="noise_removal", allowed_types=(bool,)),
    FieldRule(name="apply_invalid_mask", allowed_types=(bool,)),
    FieldRule(name="recompute", allowed_types=(bool,)),
    FieldRule(name="enable_dask", allowed_types=(bool,)),
    FieldRule(name="schema_validation", allowed_types=(bool,)),
    FieldRule(name="indices", allowed_types=(int, list), checker=_check_indices),
    FieldRule(name="polarization", allowed_types=(str, list), checker=_check_polarization),
    FieldRule(name="chunks", allowed_types=(dict,), checker=_check_chunks),
    FieldRule(name="reference_id", allowed_types=(str,), pattern=".+"),
    FieldRule(name="facility", allowed_types=(str,), pattern=".+"),
)


# ---------------------------------------------------------------------------
# Parameter resolution
# ---------------------------------------------------------------------------

# Resolution source for each parameter: (CONFIG.json section, CONFIG.json key,
# hard default). A parameter is resolved as: kwargs -> CONFIG.json default ->
# hard default. A None section/key means "no config source".
_PARAM_SOURCES: dict[str, tuple[str | None, str | None, Any]] = {
    "input_slcs_files": (None, None, None),
    "calibration_type": ("Ingestion", "calibration_type", "BetaZero"),
    "deramp": ("Ingestion", "deramp", True),
    "noise_removal": ("Ingestion", "noise_removal", True),
    "apply_invalid_mask": ("Ingestion", "apply_invalid_mask", True),
    "recompute": ("Global", "recompute", False),
    "enable_dask": (None, None, True),
    "schema_validation": (None, None, False),
    "indices": ("Ingestion", "indices", None),
    "polarization": ("Ingestion", "polarization", None),
    "chunks": ("Global", "chunks_radar_grid", None),
    "reference_id": (None, None, None),
    "facility": (None, None, None),
}


# ---------------------------------------------------------------------------
# Per-unit preflight specifications
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UnitPreflight:
    """Layer-1 preflight specification of one processing unit.

    Attributes
    ----------
    rules:
        Field rules applied to the resolved parameters.
    sources:
        Resolution source of each parameter: (CONFIG.json section, CONFIG.json
        key, hard default). A None section/key means "no config source".
    mode_source:
        How the instrument mode is resolved: ``"filename"`` (from the
        ``input_slcs_files`` kwarg, Ingestion only) or ``"input"`` (best-effort
        read from the ``slcs`` input product).
    check_etad:
        Whether the ETAD product integrity checks run for this unit.
    check_safe:
        Whether the SAFE archive XML checks run for this unit (still opt-in
        via the ``schema_validation`` kwarg).
    """

    rules: tuple[FieldRule, ...]
    sources: dict[str, tuple[str | None, str | None, Any]]
    mode_source: str
    check_etad: bool
    check_safe: bool


# ReferenceUpdate and CMLP read the exact same parameters from the same
# `ReferenceUpdate` config section, so they share rules and sources.
_REFERENCE_UPDATE_RULES: tuple[FieldRule, ...] = (
    FieldRule(name="posting", allowed_types=(dict,)),
    FieldRule(name="memory_param", allowed_types=(int, float)),
    FieldRule(name="coherence_threshold", allowed_types=(int, float), min_value=0, max_value=1),
    FieldRule(name="stable_interval_days", allowed_types=(int,)),
    FieldRule(name="max_past_stable_history", allowed_types=(int,)),
    FieldRule(name="enable_dask", allowed_types=(bool,)),
    FieldRule(name="facility", allowed_types=(str,)),
)

_REFERENCE_UPDATE_SOURCES: dict[str, tuple[str | None, str | None, Any]] = {
    "posting": ("ReferenceUpdate", "posting", None),
    "memory_param": ("ReferenceUpdate", "memory_param", 0.95),
    "coherence_threshold": ("ReferenceUpdate", "coherence_threshold", 0.1),
    "stable_interval_days": ("ReferenceUpdate", "stable_interval_days", 365),
    "max_past_stable_history": ("ReferenceUpdate", "max_past_stable_history", 10),
    "enable_dask": (None, None, True),
    "facility": ("Global", "facility", "ESA-Copernicus Ground Segment"),
}

UNIT_SPECS: dict[str, UnitPreflight] = {
    "Ingestion": UnitPreflight(
        rules=RULES,
        sources=_PARAM_SOURCES,
        mode_source="filename",
        check_etad=True,
        check_safe=True,
    ),
    "ReferenceDEM": UnitPreflight(
        rules=(
            FieldRule(name="recompute", allowed_types=(bool,)),
            FieldRule(name="chunks_geo_grid", allowed_types=(dict,), checker=_check_chunks),
            FieldRule(name="interpolation", allowed_types=(str,)),
            FieldRule(name="spacing", allowed_types=(dict, list, tuple), checker=_check_spacing),
            FieldRule(name="epsg", allowed_types=(int, str)),
            FieldRule(name="facility", allowed_types=(str,)),
        ),
        sources={
            "recompute": ("Global", "recompute", False),
            "chunks_geo_grid": ("Global", "chunks_geo_grid", None),
            "interpolation": ("ReferenceDEM", "interpolation", "cubic"),
            "spacing": ("ReferenceDEM", "spacing", None),
            "epsg": (None, None, None),
            "facility": ("Global", "facility", "ESA-Copernicus Ground Segment"),
        },
        mode_source="input",
        check_etad=False,
        check_safe=False,
    ),
    "ReferenceGeometry": UnitPreflight(
        rules=(
            FieldRule(name="recompute", allowed_types=(bool,)),
            FieldRule(name="enable_dask", allowed_types=(bool,)),
            FieldRule(name="chunks_geo_grid", allowed_types=(dict,), checker=_check_chunks),
            FieldRule(name="facility", allowed_types=(str,)),
        ),
        sources={
            "recompute": ("Global", "recompute", False),
            "enable_dask": (None, None, True),
            "chunks_geo_grid": ("Global", "chunks_geo_grid", None),
            "facility": ("Global", "facility", "ESA-Copernicus Ground Segment"),
        },
        mode_source="input",
        check_etad=False,
        check_safe=False,
    ),
    "Coregistration": UnitPreflight(
        rules=(
            FieldRule(name="initialize_current_reference", allowed_types=(bool,)),
            FieldRule(name="enable_image_matching", allowed_types=(bool,)),
            FieldRule(name="enable_split_spectrum", allowed_types=(bool,)),
            FieldRule(name="enable_esd", allowed_types=(bool,)),
            FieldRule(name="do_flattening", allowed_types=(bool,)),
            FieldRule(name="do_reramp", allowed_types=(bool,)),
            FieldRule(name="recompute", allowed_types=(bool,)),
            FieldRule(name="enable_dask", allowed_types=(bool,)),
            FieldRule(name="chunks_geo_grid", allowed_types=(dict,), checker=_check_chunks),
            FieldRule(name="warp_ov_fact_low_acc", allowed_types=(int, float)),
            FieldRule(name="warp_kaiser_beta_low_acc", allowed_types=(int, float)),
            FieldRule(name="warp_ov_fact_high_acc", allowed_types=(int, float)),
            FieldRule(name="warp_kaiser_beta_high_acc", allowed_types=(int, float)),
            FieldRule(name="bandwidth", allowed_types=(int, float)),
            FieldRule(name="frequency_offset", allowed_types=(int, float)),
            FieldRule(name="coherence_threshold_image_matching", allowed_types=(int, float), min_value=0, max_value=1),
            FieldRule(name="coherence_threshold_split_spectrum", allowed_types=(int, float), min_value=0, max_value=1),
            FieldRule(name="image_matching_degree", allowed_types=(int,)),
            FieldRule(name="block_size", allowed_types=(int,)),
            FieldRule(name="esd_poly_degree", allowed_types=(int,)),
            FieldRule(name="split_spectrum_poly_degree", allowed_types=(int,)),
            FieldRule(name="height_ionosphere", allowed_types=(int,)),
            FieldRule(name="warp_kernel_shape_low_acc", allowed_types=(list, tuple), checker=_check_number_sequence),
            FieldRule(name="warp_kernel_shape_high_acc", allowed_types=(list, tuple), checker=_check_number_sequence),
            FieldRule(name="matching_kernel_shape", allowed_types=(list, tuple), checker=_check_number_sequence),
            FieldRule(name="wsize", allowed_types=(list, tuple), checker=_check_number_sequence),
            FieldRule(name="wstep", allowed_types=(list, tuple), checker=_check_number_sequence),
            FieldRule(name="wsearch", allowed_types=(list, tuple), checker=_check_number_sequence),
            FieldRule(name="dim", allowed_types=(list, tuple), checker=_check_number_sequence),
            FieldRule(name="multilook_split_spectrum", allowed_types=(dict, list, tuple, int, float)),
            FieldRule(name="facility", allowed_types=(str,)),
        ),
        sources={
            "initialize_current_reference": ("Coregistration", "initialize_current_reference", True),
            "enable_image_matching": ("Coregistration", "enable_image_matching", True),
            "enable_split_spectrum": ("Coregistration", "enable_split_spectrum", True),
            "enable_esd": ("Coregistration", "enable_esd", True),
            "do_flattening": ("Coregistration", "do_flattening", True),
            "do_reramp": ("Coregistration", "do_reramp", True),
            "recompute": ("Global", "recompute", False),
            "enable_dask": (None, None, True),
            "chunks_geo_grid": ("Global", "chunks_geo_grid", None),
            "warp_ov_fact_low_acc": ("Coregistration", "warp_ov_fact_low_acc", 10000),
            "warp_kaiser_beta_low_acc": ("Coregistration", "warp_kaiser_beta_low_acc", 6),
            "warp_ov_fact_high_acc": ("Coregistration", "warp_ov_fact_high_acc", 100000),
            "warp_kaiser_beta_high_acc": ("Coregistration", "warp_kaiser_beta_high_acc", 6),
            "bandwidth": ("Coregistration", "bandwidth", 16000000.0),
            "frequency_offset": ("Coregistration", "frequency_offset", 10000000.0),
            "coherence_threshold_image_matching": ("Coregistration", "coherence_threshold_image_matching", 0.3),
            "coherence_threshold_split_spectrum": ("Coregistration", "coherence_threshold_split_spectrum", 0.3),
            "image_matching_degree": ("Coregistration", "image_matching_degree", 2),
            "block_size": ("Coregistration", "block_size", 10),
            "esd_poly_degree": ("Coregistration", "esd_poly_degree", 2),
            "split_spectrum_poly_degree": ("Coregistration", "split_spectrum_poly_degree", 2),
            "height_ionosphere": ("Coregistration", "height_ionosphere", None),
            "warp_kernel_shape_low_acc": ("Coregistration", "warp_kernel_shape_low_acc", [11, 11]),
            "warp_kernel_shape_high_acc": ("Coregistration", "warp_kernel_shape_high_acc", [11, 11]),
            "matching_kernel_shape": ("Coregistration", "matching_kernel_shape", [15, 15]),
            "wsize": ("Coregistration", "wsize", [256, 256]),
            "wstep": ("Coregistration", "wstep", [256, 256]),
            "wsearch": ("Coregistration", "wsearch", [5, 5]),
            "dim": ("Coregistration", "dim", [1, 1]),
            "multilook_split_spectrum": (
                "Coregistration",
                "multilook_split_spectrum",
                {"azimuth_time": 16, "slant_range_time": 64},
            ),
            "facility": ("Global", "facility", "ESA-Copernicus Ground Segment"),
        },
        mode_source="input",
        check_etad=False,
        check_safe=False,
    ),
    "ReferenceUpdate": UnitPreflight(
        rules=_REFERENCE_UPDATE_RULES,
        sources=_REFERENCE_UPDATE_SOURCES,
        mode_source="input",
        check_etad=False,
        check_safe=False,
    ),
    "CMLP": UnitPreflight(
        # CMLP reads the same `ReferenceUpdate` config section as ReferenceUpdate.
        rules=_REFERENCE_UPDATE_RULES,
        sources=_REFERENCE_UPDATE_SOURCES,
        mode_source="input",
        check_etad=False,
        check_safe=False,
    ),
    "Geocoding": UnitPreflight(
        rules=(
            FieldRule(name="warp_ov_fact", allowed_types=(int, float)),
            FieldRule(name="warp_kernel_shape", allowed_types=(list, tuple), checker=_check_number_sequence),
            FieldRule(name="warp_kaiser_beta", allowed_types=(int, float)),
            FieldRule(name="spacing", allowed_types=(dict, list, tuple), checker=_check_spacing),
            FieldRule(name="chunks_geo_grid", allowed_types=(dict,), checker=_check_chunks),
            FieldRule(name="enable_dask", allowed_types=(bool,)),
            FieldRule(name="facility", allowed_types=(str,)),
        ),
        sources={
            "warp_ov_fact": ("Geocoding", "warp_ov_fact", 100000),
            "warp_kernel_shape": ("Geocoding", "warp_kernel_shape", None),
            "warp_kaiser_beta": ("Geocoding", "warp_kaiser_beta", 6),
            "spacing": ("Geocoding", "spacing", None),
            "chunks_geo_grid": ("Global", "chunks_geo_grid", None),
            "enable_dask": (None, None, True),
            "facility": ("Global", "facility", "ESA-Copernicus Ground Segment"),
        },
        mode_source="input",
        check_etad=False,
        check_safe=False,
    ),
}


def _load_mode_config(mode: str | None) -> dict:
    """Load the per-mode section of CONFIG.json directly.

    The file is read directly (never through ``utils.get_config``) so that
    modes without a configuration entry (e.g. SAOCOM, ROSEL) simply get an
    empty mapping instead of raising.
    """
    if mode is None:
        return {}
    try:
        with open(CONFIG_PATH, encoding="utf-8") as handle:
            config = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {}
    section = config.get(mode, {})
    return section if isinstance(section, dict) else {}


def _resolve_mode(inputs: dict, kwargs: dict, mode_source: str) -> tuple[str | None, list[Violation]]:
    """Best-effort resolution of the instrument mode for a preflight run.

    With ``mode_source="filename"`` (Ingestion) the mode is derived from the
    ``input_slcs_files`` kwarg and a failure is reported as a violation. With
    ``mode_source="input"`` (downstream units) the mode is read from the
    ``slcs`` input product and any problem simply degrades to ``None``.
    """
    if mode_source == "filename":
        slcs_files = kwargs.get("input_slcs_files")
        if slcs_files is not None:
            files = [slcs_files] if isinstance(slcs_files, str) else slcs_files
            if isinstance(files, list) and files and all(isinstance(item, str) for item in files):
                try:
                    return get_instrument_mode(files), []
                except Exception as exc:  # report any data problem, never crash the preflight
                    return None, [Violation("input_slcs_files", f"could not determine the instrument mode: {exc}")]
        return None, []

    try:
        slcs = inputs["slcs"]
        return slcs.attrs["params"][0]["execution_parameters"]["mode"], []
    except Exception:  # degrade gracefully, never report a violation for this
        return None, []


def _resolve_parameters(
    kwargs: dict,
    mode_config: dict,
    sources: dict[str, tuple[str | None, str | None, Any]],
) -> dict[str, Any]:
    """Resolve every known parameter: kwargs -> CONFIG.json default -> hard default."""
    resolved = {}
    for name, (section_name, section_key, hard_default) in sources.items():
        if name in kwargs:
            resolved[name] = kwargs[name]
        elif section_name is not None and section_key in mode_config.get(section_name, {}):
            resolved[name] = mode_config[section_name][section_key]
        else:
            resolved[name] = hard_default
    return resolved


# ---------------------------------------------------------------------------
# ETAD product validation
# ---------------------------------------------------------------------------


class EtadStackValidator:
    """Structural validator for a single ETAD product directory.

    An ETAD product is a directory holding at least one netCDF file under
    ``measurement/`` and one EOF file under ``annotation/``. The netCDF file
    is a DataTree with root-level timing attributes and one group per swath,
    each swath containing one group per burst.
    """

    REQUIRED_ROOT_ATTRS = ("azimuthTimeMin", "azimuthTimeMax", "rangeTimeMin")
    REQUIRED_BURST_ATTRS = (
        "burstId",
        "gridStartAzimuthTime",
        "gridStartRangeTime",
        "gridSamplingAzimuth",
        "gridSamplingRange",
    )
    REQUIRED_BURST_VARS = (
        "sumOfCorrectionsAz",
        "sumOfCorrectionsRg",
    )

    def validate(self, value: Any, violations: list[Violation]) -> None:
        """Validate one ETAD product directory and append violations found."""
        path = getattr(value, "path", value)
        scope = str(path)
        product_dir = Path_io(path)

        if not product_dir.exists():
            violations.append(Violation(scope, f"ETAD product directory does not exist: {path}"))
            return

        measurement_files = sorted([str(path) for path in product_dir.glob("measurement/*.nc")])
        if not measurement_files:
            violations.append(Violation(scope, "no measurement/*.nc file found"))
        if not sorted([str(path) for path in product_dir.glob("annotation/*.EOF")]):
            violations.append(Violation(scope, "no annotation/*.EOF file found"))
        if not measurement_files:
            return

        nc_path = measurement_files[0]
        try:
            tree = xr.open_datatree(str(nc_path), engine="h5netcdf", chunks={}, cache=False)
        except Exception as exc:
            violations.append(Violation(scope, f"failed to open {nc_path}: {exc}"))
            return

        try:
            for attr in self.REQUIRED_ROOT_ATTRS:
                if attr not in tree.attrs:
                    violations.append(Violation(scope, f"root missing attribute '{attr}'"))

            swath_groups = list(tree.children.values())
            if not swath_groups:
                violations.append(Violation(scope, "no swath group found"))
                return

            for swath in swath_groups:
                for burst in swath.children.values():
                    for attr in self.REQUIRED_BURST_ATTRS:
                        if attr not in burst.attrs:
                            violations.append(Violation(scope, f"burst '{burst.name}' missing attribute '{attr}'"))
                    for var in self.REQUIRED_BURST_VARS:
                        if var not in burst.data_vars:
                            violations.append(Violation(scope, f"burst '{burst.name}' missing variable '{var}'"))
        finally:
            tree.close()


# ---------------------------------------------------------------------------
# SAFE archive XML validation
# ---------------------------------------------------------------------------


class SafeXmlValidator:
    """Well-formedness and XML-schema validator for SAFE archives.

    Every XML document inside the SAFE directory (all ``*.xml`` files plus
    the ``manifest.safe`` root file) must be well-formed.  In addition, every
    document whose root element is declared by one of the XSD schemas bundled
    in the archive's own ``support`` directory is validated against that
    schema.  The bundled schemas are the Sentinel-1 Level-1 product schemas
    shipped inside the product itself (``s1-level-1-product.xsd``,
    ``s1-level-1-calibration.xsd``, ...), so no external schema source is
    needed.  Documents without a matching bundled schema (e.g. the CCSDS XFDU
    manifest) are only checked for well-formedness, as are archives that
    carry no ``support`` directory at all.  Non-XML preview files
    (``*.html``, ``*.kml``) are intentionally ignored.
    """

    MANIFEST_NAME = "manifest.safe"
    SUPPORT_DIR = "support"

    def validate(self, value: Any, violations: list[Violation]) -> None:
        """Validate one SAFE product directory and append violations found."""
        safe_dir = Path(str(getattr(value, "path", value)))
        scope = str(safe_dir)
        if not safe_dir.is_dir():
            violations.append(Violation(scope, f"SAFE product directory does not exist: {safe_dir}"))
            return

        schemas = self._load_bundled_schemas(safe_dir)
        for xml_path in sorted(safe_dir.rglob("*.xml")) + [safe_dir / self.MANIFEST_NAME]:
            if not xml_path.is_file():
                continue
            file_scope = str(xml_path.resolve())
            try:
                doc = etree.parse(str(xml_path))
            except etree.XMLSyntaxError as exc:
                violations.append(Violation(file_scope, f"malformed XML: {exc}"))
                continue

            entry = schemas.get(self._split_tag(str(doc.getroot().tag))[1])
            if entry is None:
                continue
            schema, schema_file = entry
            try:
                schema.assertValid(doc)
            except etree.DocumentInvalid as exc:
                detail = " ".join(str(exc).split())
                violations.append(Violation(file_scope, f"XML validation against {schema_file} failed: {detail}"))

    def _load_bundled_schemas(self, safe_dir: Path) -> dict[str, tuple[etree.XMLSchema, str]]:
        """Compile every XSD in the SAFE's ``support`` directory.

        Returns a map from the local name of each globally declared element
        to the compiled schema and its file name.  Schemas that fail to
        compile are skipped; a missing ``support`` directory means no schema
        validation is possible.
        """
        schemas: dict[str, tuple[etree.XMLSchema, str]] = {}
        support_dir = safe_dir / self.SUPPORT_DIR
        if not support_dir.is_dir():
            return schemas
        for xsd_path in sorted(support_dir.glob("*.xsd")):
            try:
                schema = etree.XMLSchema(etree.parse(str(xsd_path)))
            except etree.LxmlError:
                continue
            for name in self._global_element_names(xsd_path):
                schemas[name] = (schema, xsd_path.name)
        return schemas

    @staticmethod
    def _global_element_names(xsd_path: Path) -> list[str]:
        """Names of the elements declared directly under ``<xsd:schema>``."""
        root = etree.parse(str(xsd_path)).getroot()
        return [
            child.get("name")  # type: ignore
            for child in root
            if isinstance(child.tag, str)
            and SafeXmlValidator._split_tag(child.tag)[1] == "element"
            and child.get("name")
        ]

    @staticmethod
    def _split_tag(tag: str) -> tuple[str | None, str]:
        """Split an XML tag into (namespace, local name)."""
        if tag.startswith("{"):
            namespace, _, local_name = tag[1:].partition("}")
            return namespace, local_name
        return None, tag


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def _safe_paths(kwargs: dict) -> list[str]:
    """The resolved `input_slcs_files` entries that point to a .SAFE product."""
    value = kwargs.get("input_slcs_files")
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return [path for path in value if isinstance(path, str) and Path(path).suffix.lower() == ".safe"]


def run_preflight(
    inputs: dict,
    adfs: dict | None = None,
    unit_name: str = "Ingestion",
    **kwargs,
) -> list[Violation]:
    """Run the full preflight for a processing unit and return every violation found.

    The unit to validate is selected via `unit_name` (a key of `UNIT_SPECS`,
    defaulting to ``"Ingestion"``). This function never raises for data
    problems; every problem is reported as a :class:`Violation` and the
    returned list is empty when everything is fine.
    """
    violations: list[Violation] = []
    adfs = adfs or {}
    spec = UNIT_SPECS[unit_name]

    # (a) Resolved-parameter sanity (always on).
    mode, mode_violations = _resolve_mode(inputs, kwargs, spec.mode_source)
    violations.extend(mode_violations)
    resolved = _resolve_parameters(kwargs, _load_mode_config(mode), spec.sources)
    apply_rules(spec.rules, resolved, violations)

    # (b) ETAD integrity (only for units that consume ETAD ADFs, and only
    # when an ETAD ADF is provided).
    if spec.check_etad:
        for key, value in adfs.items():
            if "ETAD" in key:
                EtadStackValidator().validate(value, violations)

    # (c) SAFE XML checks (opt-in, only when the resolved flag is strictly True).
    if spec.check_safe and kwargs.get("schema_validation", False) is True:
        for safe_path in _safe_paths(kwargs):
            SafeXmlValidator().validate(safe_path, violations)

    return violations
