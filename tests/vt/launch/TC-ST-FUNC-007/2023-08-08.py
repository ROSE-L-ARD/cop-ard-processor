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
from pyproj import CRS
from structlog.testing import capture_logs

from cop_ard_processor.structure import check_ARD_outputs
from scripts.run_payload import run_payload

RP_BASE_PATH = Path(__file__).parents[2]
yaml_part1_path = RP_BASE_PATH / "triggering" / "TC-ST-FUNC-007" / "2023-08-08-part1.yaml"
yaml_part2_path = RP_BASE_PATH / "triggering" / "TC-ST-FUNC-007" / "2023-08-08-part2.yaml"


@pytest.mark.ard
def test_payload():
    """E2E Functional Test TC-ST-FUNC-007: GMLP IW mode (Surat) with specific posting and spacing dicts."""
    with open(yaml_part1_path, encoding="utf-8") as f:
        payload_part1 = yaml.safe_load(f)

    with open(yaml_part2_path, encoding="utf-8") as f:
        payload_part2 = yaml.safe_load(f)

    updates = {}

    # 1. Execution & Log Capture Phase (Sequential execution of part1 then part2)
    with capture_logs() as captured:
        output_stores_1 = run_payload(payload_part1, dry_run=False)
        output_stores_2 = run_payload(payload_part2, dry_run=False)

    # Merge output stores from both parts
    output_stores = {**output_stores_1, **output_stores_2}

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

            # Verify single-polarization isolation for GMLP Phase measurement
            if output == "gmlp":
                pols = set(element.measurements.phase.polarization.values.astype(str))
                assert pols == {"VV"}, (
                    f"Pass Criteria Failed: Single-polarization check failed for GMLP product. "
                    f"Expected strictly {{'VV'}}, but found: {pols}"
                )
                assert CRS(element.measurements.to_dataset().phase.rio.crs).to_2d().to_string() == "EPSG:32755"

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

    # 4. Extract and verify specific pipeline parameters (Posting, Spacing, CRS)
    posting_found = False
    spacing_found = False

    for log in captured:
        # Uniform the string representation to handle spaces and quotes cleanly
        log_str = str(log).replace(" ", "").replace("'", '"')

        # Check posting dictionary -> expected keys azimuth_time and slant_range_time
        if '"posting":' in log_str and '"azimuth_time":' in log_str and '"slant_range_time":' in log_str:
            posting_found = True

        # Check spacing dictionary -> expected keys x and y with value 50
        if '"spacing":' in log_str and '"x":50' in log_str and '"y":50' in log_str:
            spacing_found = True

    assert posting_found, (
        f"Pass Criteria Failed: The required 'posting' dictionary (with azimuth_time and slant_range_time) was not found in logs.\n"
        f"Captured logs:\n{captured_logs_text}"
    )

    assert spacing_found, (
        f"Pass Criteria Failed: The required 'spacing' dictionary (with x=50, y=50) was not found in logs.\n"
        f"Captured logs:\n{captured_logs_text}"
    )

    # 5. Optional Steps Bypass Verification
    bypassed_steps = [
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

    # 6. Reference Database Persistence (if applicable for reference updates)
    if "current_reference" in updates and "stable_references" in updates:
        logger.debug("SAVING CURRENT REFERENCE")
        refdb_path = None
        for p in (payload_part1, payload_part2):
            for adf in p.get("io", {}).get("adfs", []):
                if adf.get("id") == "REFDB":
                    refdb_path = adf.get("path")
                    break
            if refdb_path:
                break

        reference_id = None
        for step in payload_part1.get("workflow", []):
            if step.get("name") == "ingestion":
                reference_id = step.get("parameters", {}).get("reference_id")
                break

        update_reference_database(
            updates["current_reference"],
            updates["stable_references"],
            f"{refdb_path}/reference_db_dynamic",
            reference_id=reference_id,
        )
