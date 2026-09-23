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

import json
from pathlib import Path

import pytest
import rioxarray
import xarray as xr
import yaml
from cop_ard_app.io import is_local
from cop_ard_app.utils import update_reference_database
from cop_ard_core.logger import logger
from pyproj import CRS
from structlog.testing import capture_logs

from cop_ard_processor.structure import check_ARD_outputs
from scripts.run_payload import run_payload

RP_BASE_PATH = Path(__file__).parents[2]
yaml_path = RP_BASE_PATH / "triggering" / "TC-ST-FUNC-011" / "2000-01-01.yaml"


@pytest.mark.ard
def test_payload():
    """E2E Functional Test TC-ST-FUNC-011"""
    with open(yaml_path, encoding="utf-8") as f:
        payload = yaml.safe_load(f)

    # 1. Execution & Log Capture Phase
    with capture_logs() as captured:
        output_stores = run_payload(payload, dry_run=False)
        updates = {}

        # 2. Output Validation Phase
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
    captured_events = [str(log.get("event", "")) for log in captured]
    captured_logs_text = "\n".join(captured_events)

    # 3. Mandatory Processing Units Verification for GMLP Pipeline
    mandatory_units = [
        "Ingestion Unit",
        "ReferenceDEM Unit",
        "ReferenceGeometry Unit",
        "Coregistration Unit",
        "CMLP Unit",
        "Geocoding Unit",
    ]
    for unit in mandatory_units:
        expected_trace = f"Started : {unit}"
        assert any(expected_trace in event for event in captured_events), (
            f"Pass Criteria Failed: Mandatory log trace '{expected_trace}' was not found in captured execution logs.\n"
            f"Captured events:\n{captured_logs_text}"
        )

    # 6. Optional Steps Bypass Verification
    bypassed_steps = [
        "external orbit",
        "image matching",
        "split spectrum",
    ]
    for step in bypassed_steps:
        expected_skip_log = f"Skipping {step} step"
        assert any(expected_skip_log in event for event in captured_events), (
            f"Pass Criteria Failed: Mandatory skip trace '{expected_skip_log}' was not found in execution logs.\n"
            f"Captured events:\n{captured_logs_text}"
        )
    # Verify Enhanced Spectral Diversity (ESD) Execution
    assert any("Executing ESD step..." in event for event in captured_events), (
        f"Pass Criteria Failed: The ESD algorithm execution log was not found.\nCaptured events:\n{captured_logs_text}"
    )
    assert not any("Skipping ESD step" in event for event in captured_events), (
        "Pass Criteria Failed: The ESD step was skipped instead of being executed."
    )
