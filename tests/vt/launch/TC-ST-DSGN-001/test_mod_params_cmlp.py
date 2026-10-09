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
"""TC-ST-DSGN-001 launch test: CMLP preflight (Layer 1) on broken run parameters.

The payload runs the upstream Ingestion/ReferenceDEM/ReferenceGeometry/
Coregistration steps nominally and drives the CMLP unit with one deliberately
broken parameter (``max_past_stable_history``) while
``triggering__validate_run`` stays ``false`` so the EOPF contract (Layer 2)
is bypassed and the Layer-1 preflight engine must raise a single
``PreflightError`` carrying exactly one violation.
"""

import logging
import traceback
from pathlib import Path

import pytest
import yaml

from scripts.run_payload import run_payload

FACILITY_ROOT = Path("/data/SENTINEL1/SURAT")

TARGET_UNIT = "CMLP"
TARGET_STEP = "cmlp"

# MOD parameter carried by the payload (see triggering/TC-ST-DSGN-001/mod_params_cmlp.yaml).
MOD_PARAMS = {
    "max_past_stable_history": 2.5,
}

EXPECTED_VIOLATION_LINE = "  - max_past_stable_history: value 2.5 has an invalid type: expected int, got float"

YAML_PATH = Path(__file__).parents[2] / "triggering" / "TC-ST-DSGN-001" / "mod_params_cmlp.yaml"


@pytest.mark.ard
def test_mod_params_cmlp(capsys: pytest.CaptureFixture, caplog: pytest.LogCaptureFixture):
    """Run the broken-parameter payload and assert the Layer-1 preflight rejects the CMLP step."""
    caplog.set_level(logging.INFO)

    if not FACILITY_ROOT.exists():
        pytest.skip("facility data root not available")

    with open(YAML_PATH, encoding="utf-8") as f:
        payload = yaml.safe_load(f)

    # Sanity: the MOD value must have survived the YAML round-trip.
    step = next(s for s in payload["workflow"] if s["name"] == TARGET_STEP)
    assert payload["general_configuration"]["triggering__validate_run"] is False
    assert step["parameters"] == MOD_PARAMS
    assert "outputs" not in step, "the target step must not declare outputs"

    # The preflight must abort the run before the target step processes anything.
    with pytest.raises(Exception) as excinfo:
        run_payload(payload, dry_run=False)

    exc = excinfo.value
    haystack = str(exc) + "".join(traceback.format_exception(exc)) + capsys.readouterr().out + caplog.text

    # The target unit must have actually started before being rejected.
    assert f"Started : {TARGET_UNIT} Unit" in haystack

    # Layer-1 preflight raised with exactly the one MOD parameter violation.
    assert f"{TARGET_UNIT} preflight failed with 1 violation(s):" in haystack
    assert EXPECTED_VIOLATION_LINE in haystack
