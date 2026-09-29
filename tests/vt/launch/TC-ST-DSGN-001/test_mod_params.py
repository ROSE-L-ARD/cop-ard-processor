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
"""TC-ST-DSGN-001 launch test: Ingestion preflight (Layer 1) on broken run parameters.

The payload drives the Ingestion unit with three deliberately broken parameters
(``indices``, ``calibration_type``, ``polarization``) while
``triggering__validate_run`` stays ``false`` so the EOPF contract (Layer 2) is
bypassed and the Layer-1 preflight engine must raise a single ``PreflightError``
carrying exactly three violations.
"""

import logging
import traceback
from pathlib import Path

import pytest
import yaml

from scripts.run_payload import run_payload

FACILITY_ROOT = Path("/data/SENTINEL1/SURAT")

# MOD parameters carried by the payload (see triggering/TC-ST-DSGN-001/mod_params.yaml).
MOD_PARAMS = {
    "indices": [-1],
    "calibration_type": "WrongCalibration",
    "polarization": "XX",
}

EXPECTED_VIOLATION_DETAILS = (
    "value -1 at index 0 is not a non-negative integer",
    "value 'WrongCalibration' is not one of the allowed values: ['BetaZero', 'GammaZero', 'SigmaZero']",
    "value 'XX' is not a valid polarization: allowed values are ['VV', 'VH', 'HH', 'HV']",
)

YAML_PATH = Path(__file__).parents[2] / "triggering" / "TC-ST-DSGN-001" / "mod_params.yaml"


@pytest.mark.need_files
def test_mod_params(capsys: pytest.CaptureFixture, caplog: pytest.LogCaptureFixture):
    """Run the broken-parameter payload and assert the Layer-1 preflight rejects it."""
    caplog.set_level(logging.INFO)

    if not FACILITY_ROOT.exists():
        pytest.skip("facility data root not available")

    with open(YAML_PATH, encoding="utf-8") as f:
        payload = yaml.safe_load(f)

    # Sanity: the MOD content must have survived the YAML round-trip.
    params = payload["workflow"][0]["parameters"]
    assert payload["general_configuration"]["triggering__validate_run"] is False
    for key, value in MOD_PARAMS.items():
        assert params[key] == value, f"payload parameter '{key}' does not carry its MOD value"

    # The preflight must abort the run before any processing happens.
    with pytest.raises(Exception) as excinfo:
        run_payload(payload, dry_run=False)

    exc = excinfo.value
    haystack = str(exc) + "".join(traceback.format_exception(exc)) + capsys.readouterr().out + caplog.text

    # The Ingestion unit must have actually started before being rejected.
    assert "Started : Ingestion Unit" in haystack

    # Layer-1 preflight raised with exactly the three MOD parameter violations.
    assert "Ingestion preflight failed with 3 violation(s):" in haystack
    for scope in MOD_PARAMS:
        assert f"{scope}:" in haystack, f"expected preflight violation for '{scope}' not found"
    for detail in EXPECTED_VIOLATION_DETAILS:
        assert detail in haystack, f"expected preflight violation detail not found: {detail}"
