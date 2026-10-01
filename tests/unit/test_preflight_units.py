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
"""Offline unit tests for the generalized Layer-1 (preflight) validation engine.

Covers the per-unit preflight specifications of the seven downstream
processing units (ReferenceDEM, ReferenceGeometry, Coregistration,
ReferenceUpdate, CMLP, Geocoding) in :mod:`cop_ard_processor.preflight`, the
instrument-mode resolution from the ``slcs`` input product, and the
``preflight`` classmethod wiring in :mod:`cop_ard_processor.processing_units`.

All inputs are synthetic ``xr.DataTree`` objects built in memory; the tests
stay fully offline and never touch the network or facility data.
"""

import pytest
import xarray as xr

from cop_ard_processor.preflight import PreflightError, _resolve_mode, run_preflight
from cop_ard_processor.processing_units import Coregistration, Geocoding, ReferenceDEM

pytestmark = pytest.mark.unit

DOWNSTREAM_UNITS = (
    "ReferenceDEM",
    "ReferenceGeometry",
    "Coregistration",
    "ReferenceUpdate",
    "CMLP",
    "Geocoding",
)


def make_slcs_input() -> xr.DataTree:
    """Build a synthetic ``slcs`` input product with nominal execution parameters."""
    tree = xr.DataTree()
    tree.attrs["params"] = [{"execution_parameters": {"mode": "IW", "reference_id": "REF"}}]
    return tree


def make_inputs() -> dict:
    """Build the inputs mapping expected by the downstream units."""
    return {"slcs": make_slcs_input()}


def _slcs_without_mode() -> xr.DataTree:
    """Build a synthetic ``slcs`` input product whose params lack the mode."""
    tree = xr.DataTree()
    tree.attrs["params"] = [{"execution_parameters": {"reference_id": "REF"}}]
    return tree


def nominal_kwargs_for(unit_name: str) -> dict:
    """Nominal execution overrides for a unit.

    Coregistration mirrors the nominal TC-ST-FUNC-001 payload, every other
    unit runs with no overrides at all.
    """
    if unit_name == "Coregistration":
        return {"initialize_current_reference": True}
    return {}


# ---------------------------------------------------------------------------
# Nominal cases: the real facility nominal run must not be blocked
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("unit_name", DOWNSTREAM_UNITS)
def test_nominal_kwargs_pass_preflight(unit_name):
    violations = run_preflight(make_inputs(), None, unit_name=unit_name, **nominal_kwargs_for(unit_name))
    assert violations == []


# ---------------------------------------------------------------------------
# Instrument-mode resolution from the `slcs` input
# ---------------------------------------------------------------------------


def test_resolve_mode_reads_mode_from_slcs_input():
    mode, violations = _resolve_mode(make_inputs(), {}, "input")
    assert mode == "IW"
    assert violations == []


@pytest.mark.parametrize(
    "inputs",
    [
        {},
        {"slcs": xr.DataTree()},
        {"slcs": "not-a-tree"},
        {"slcs": _slcs_without_mode()},
    ],
)
def test_resolve_mode_degrades_without_violations(inputs):
    mode, violations = _resolve_mode(inputs, {}, "input")
    assert mode is None
    assert violations == []


def test_unreadable_slcs_input_degrades_gracefully():
    assert run_preflight({}, None, unit_name="ReferenceDEM") == []
    assert run_preflight({"slcs": "not-a-tree"}, None, unit_name="Geocoding") == []


# ---------------------------------------------------------------------------
# Targeted bad-parameter tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("unit_name", "parameter", "value"),
    [
        ("ReferenceDEM", "recompute", "yes"),
        ("ReferenceDEM", "epsg", 12.5),
        ("ReferenceGeometry", "recompute", 1),
        ("ReferenceGeometry", "enable_dask", "no"),
        ("Coregistration", "enable_esd", "true"),
        ("Coregistration", "coherence_threshold_image_matching", 2.5),
        ("ReferenceUpdate", "memory_param", "high"),
        ("ReferenceUpdate", "stable_interval_days", "365"),
        ("CMLP", "max_past_stable_history", 2.5),
        ("CMLP", "coherence_threshold", -0.5),
        ("Geocoding", "warp_kaiser_beta", "six"),
        ("Geocoding", "spacing", [0]),
    ],
)
def test_bad_parameter_is_reported(unit_name, parameter, value):
    violations = run_preflight(make_inputs(), None, unit_name=unit_name, **{parameter: value})
    assert any(v.scope == parameter for v in violations)


def test_spacing_checker_reports_exact_details():
    violations = run_preflight(make_inputs(), None, unit_name="Geocoding", spacing={"x": 0, "y": "50"})
    details = [v.detail for v in violations if v.scope == "spacing"]
    assert details == ["value 0 is not a positive number", "value '50' is not a positive number"]


def test_number_sequence_checker_reports_exact_details():
    violations = run_preflight(make_inputs(), None, unit_name="Geocoding", warp_kernel_shape=[11, "11", True])
    details = [v.detail for v in violations if v.scope == "warp_kernel_shape"]
    assert details == ["value '11' at index 1 is not a number", "value True at index 2 is not a number"]


# ---------------------------------------------------------------------------
# Preflight classmethod wiring
# ---------------------------------------------------------------------------


def test_reference_dem_preflight_classmethod_raises_with_unit_name():
    with pytest.raises(PreflightError) as excinfo:
        ReferenceDEM.preflight(make_inputs(), None, recompute="yes")
    error = excinfo.value
    assert isinstance(error, ValueError)
    assert error.unit == "ReferenceDEM"
    assert {v.scope for v in error.violations} == {"recompute"}
    assert str(error).startswith("ReferenceDEM preflight failed with")


def test_coregistration_preflight_classmethod_raises_with_unit_name():
    with pytest.raises(PreflightError) as excinfo:
        Coregistration.preflight(make_inputs(), None, enable_esd="true")
    error = excinfo.value
    assert isinstance(error, ValueError)
    assert error.unit == "Coregistration"
    assert {v.scope for v in error.violations} == {"enable_esd"}
    assert str(error).startswith("Coregistration preflight failed with")


def test_geocoding_preflight_classmethod_raises_with_unit_name():
    with pytest.raises(PreflightError) as excinfo:
        Geocoding.preflight(make_inputs(), None, warp_kaiser_beta="six")
    error = excinfo.value
    assert isinstance(error, ValueError)
    assert error.unit == "Geocoding"
    assert {v.scope for v in error.violations} == {"warp_kaiser_beta"}
    assert str(error).startswith("Geocoding preflight failed with")
