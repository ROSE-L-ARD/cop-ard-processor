# Copyright 2026 B-Open
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from pathlib import Path

import pytest
import xarray as xr
import yaml
from cop_ard_app.io import is_local
from cop_ard_app.utils import update_reference_database
from cop_ard_core.logger import logger
from structlog.testing import capture_logs

from cop_ard_processor.structure import check_ARD_outputs
from scripts.run_payload import run_payload

RP_BASE_PATH = Path(__file__).parents[2]
yaml_path = RP_BASE_PATH / "triggering" / "TC-ST-FUNC-006" / "2021-07-01.yaml"


@pytest.mark.ard
def test_payload():
    """E2E Functional Test TC-ST-FUNC-006: CSLC SM mode acquisition (2021-07-01)."""
    with open(yaml_path, encoding="utf-8") as f:
        payload = yaml.safe_load(f)

    updates = {}

    # 1. Execution & Log Capture Phase
    with capture_logs() as captured:
        output_stores = run_payload(payload, dry_run=False)

        # 2. Output Validation Phase
        for output, output_store in output_stores.items():
            storage_opts = output_store.get("storage_options", {}) if isinstance(output_store, dict) else {}
            storage_opts = is_local(output_store["path"], storage_opts)

            data = xr.open_datatree(output_store["path"], engine="zarr", chunks={}, storage_options=storage_opts)
            updates[output] = data

            if output not in ["cslcs", "gmlp"]:
                continue

            for element in data.values():
                check_ARD_outputs(element, output, logger=logger)

                if output == "cslcs":
                    pols = set(element.measurements.cslc.polarization.values.astype(str))
                    assert pols == {"VV"}, (
                        f"Single-polarization check failed. Expected strictly {{'VV'}}, found: {pols}"
                    )

    captured_events = [str(log.get("event", "")) for log in captured]
    captured_logs_text = "\n".join(captured_events)

    # 3. Mandatory Processing Units Verification
    mandatory_units = [
        "Ingestion Unit",
        "ReferenceDEM Unit",
        "ReferenceGeometry Unit",
        "Coregistration Unit",
        "ReferenceUpdate Unit",
    ]
    for unit in mandatory_units:
        assert any(f"Started : {unit}" in event for event in captured_events), (
            f"Pass Criteria Failed: Mandatory trace 'Started : {unit}' missing."
        )

    # 4. Stripmap (SM) Mode Execution Verification
    assert any("SM" in str(log) for log in captured), (
        f"Pass Criteria Failed: Stripmap (SM) mode trace was not found in execution logs.\n"
        f"Captured logs:\n{captured_logs_text}"
    )

    # 5. Optional Steps Bypass Verification
    bypassed_steps = ["ETAD", "external orbit", "image matching", "split spectrum", "ESD"]
    for step in bypassed_steps:
        assert any(f"Skipping {step} step" in event for event in captured_events), f"Skip trace for '{step}' missing."
