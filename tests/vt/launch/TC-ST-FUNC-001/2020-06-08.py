# Copyright 2026 B-Open
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
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
from cop_ard_core.logger import logger
from structlog.testing import capture_logs

from cop_ard_processor.structure import check_ARD_outputs
from scripts.run_payload import run_payload

RP_BASE_PATH = Path(__file__).parents[2]
yaml_path = f"{RP_BASE_PATH}/triggering/TC-ST-FUNC-001/2020-06-08.yaml"


@pytest.mark.ard
def test_payload():
    """E2E Functional Test TC-ST-FUNC-001: CSLC Full Chain Validation."""
    with open(yaml_path, encoding="utf-8") as f:
        payload = yaml.safe_load(f)

    # 1. Execution & Log Capture Phase
    with capture_logs() as captured:
        output_stores = run_payload(payload, dry_run=False)
        updates = {}

        # 2. Output Validation Phase (Structure & Dual-Polarization)
        for output, output_store in output_stores.items():
            logger.info(f"Checking output store: {output}")
            logger.info(f"Store metadata: {output_store}")

            storage_opts = output_store.get("storage_options", {}) if isinstance(output_store, dict) else {}
            storage_opts = is_local(output_store["path"], storage_opts)

            data = xr.open_datatree(
                output_store["path"],
                engine="zarr",
                chunks={},
                storage_options=storage_opts,
            )

            if output not in ["cslcs", "gmlp"]:
                updates[output] = data
                continue

            for element in data.values():
                check_ARD_outputs(element, output, logger=logger)

                if output == "cslcs":
                    pols = set(element.measurements.cslc.polarization.values.astype(str))
                    assert {"VV", "VH"}.issubset(pols), (
                        f"Pass Criteria Failed: Dual-pol check failed for CSLC product. Found polarizations: {pols}"
                    )

    # Extract event string messages from captured structlog entries
    captured_events = [str(log.get("event", "")) for log in captured]
    captured_logs_text = "\n".join(captured_events)

    # 3. Mandatory Log Events Verification
    mandatory_units = [
        "Ingestion Unit",
        "ReferenceDEM Unit",
        "ReferenceGeometry Unit",
        "Coregistration Unit",
        "ReferenceUpdate Unit",
    ]
    for unit in mandatory_units:
        expected_trace = f"Started : {unit}"
        assert any(expected_trace in event for event in captured_events), (
            f"Pass Criteria Failed: Mandatory log trace '{expected_trace}' was not found in captured execution logs.\n"
            f"Captured events:\n{captured_logs_text}"
        )

    # 4. Optional Steps Bypass Verification
    bypassed_steps = [
        "ETAD",
        "external orbit",
        "image matching",
        "split spectrum",
        "ESD",
    ]
    for step in bypassed_steps:
        expected_skip_log = f"Skipping {step} step"
        assert any(expected_skip_log in event for event in captured_events), (
            f"Pass Criteria Failed: Mandatory skip trace '{expected_skip_log}' was not found in execution logs.\n"
            f"Captured events:\n{captured_logs_text}"
        )
