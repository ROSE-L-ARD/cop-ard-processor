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
"""TC-ST-DSGN-002 launch test: Ingestion preflight (Layer 1b) on a broken ETAD product.

The payload wires the optional ``ETAD`` ADF to a MOD-ETAD product (a copy of the
real 2023-07-27 ETAD product with its ``annotation/*.EOF`` files deleted).
``triggering__validate_run`` is ``true`` and the nominal parameters satisfy the
EOPF contract (Layer 2), so execution reaches ``Ingestion.run`` where the
Layer-1 preflight must raise a single ``PreflightError`` reporting that no
``annotation/*.EOF`` file was found.
"""

import logging
import traceback
from pathlib import Path

import pytest
import yaml

from scripts.run_payload import run_payload

FACILITY_ROOT = Path("/data/SENTINEL1/SURAT")

# PLACEHOLDER — confirm/create on facility (base: /data/SENTINEL1/SURAT)
# MOD-ETAD: real 2023-07-27 ETAD product copy with annotation/*.EOF deleted.
MOD_ETAD_PATH = (
    "/data/SENTINEL1/SURAT/MOD_ETAD/S1A_IW_ETA__AXDV_20231031T084128_20231031T084155_051006_062667_9059.SAFE"
)

YAML_PATH = Path(__file__).parents[2] / "triggering" / "TC-ST-DSGN-002" / "mod_etad.yaml"


@pytest.mark.ard
def test_mod_etad(capsys: pytest.CaptureFixture, caplog: pytest.LogCaptureFixture):
    """Run the MOD-ETAD payload and assert the preflight reports the missing EOF annotation."""
    caplog.set_level(logging.INFO)

    if not FACILITY_ROOT.exists():
        pytest.skip("facility data root not available")
    if not Path(MOD_ETAD_PATH).exists():
        pytest.skip(f"MOD-ETAD artifact not available: {MOD_ETAD_PATH}")

    with open(YAML_PATH, encoding="utf-8") as f:
        payload = yaml.safe_load(f)

    # Sanity: the MOD content must have survived the YAML round-trip.
    step = payload["workflow"][0]
    params = step["parameters"]
    assert payload["general_configuration"]["triggering__validate_run"] is True
    assert step["adfs"] == {"REFDB": "REFDB", "ETAD": "ETAD"}
    assert params["indices"] == [0, 1, 27, 28]
    io_adfs = {adf["id"]: adf["path"] for adf in payload["io"]["adfs"]}
    assert io_adfs["ETAD"] == MOD_ETAD_PATH

    # The preflight must abort the run before any processing happens.
    with pytest.raises(Exception) as excinfo:
        run_payload(payload, dry_run=False)

    exc = excinfo.value
    haystack = str(exc) + "".join(traceback.format_exception(exc)) + capsys.readouterr().out + caplog.text

    # The Ingestion unit must have actually started before being rejected.
    assert "Started : Ingestion Unit" in haystack

    # Layer-1 preflight raised pointing at the MOD-ETAD product and its missing EOF annotation.
    assert "Ingestion preflight failed with" in haystack
    assert MOD_ETAD_PATH in haystack
    assert "no annotation/*.EOF file found" in haystack
