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
import os
import re
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
YAML_4C_PATH = RP_BASE_PATH / "triggering" / "TC-ST-PERF-002" / "2023-08-08_4c.yaml"
YAML_8C_PATH = RP_BASE_PATH / "triggering" / "TC-ST-PERF-002" / "2023-08-08_8c.yaml"

CSLC_UNITS = [
    "Ingestion Unit",
    "ReferenceDEM Unit",
    "ReferenceGeometry Unit",
    "Coregistration Unit",
]

GMLP_UNITS = [
    "CMLP Unit",
    "Geocoding Unit",
]

ALL_UNITS = CSLC_UNITS + GMLP_UNITS


def validate_and_extract_times(output_stores: dict, captured: list) -> tuple[float, float]:
    """
    Validates Zarr output stores and extracts internal execution times from logs.
    Returns a tuple: (cslc_time, gmlp_time).
    """
    # 1. Zarr Products Validation
    for output, output_store in output_stores.items():
        logger.info(f"Checking output store: {output}")
        storage_opts = output_store.get("storage_options", {}) if isinstance(output_store, dict) else {}
        storage_opts = is_local(output_store["path"], storage_opts)

        data = xr.open_datatree(
            output_store["path"],
            engine="zarr",
            chunks={},
            storage_options=storage_opts,
        )

        if output in ["cslcs", "gmlp"]:
            for element in data.values():
                check_ARD_outputs(element, output, logger=logger)

    # 2. Extract logs and perform mandatory traces & time extraction
    captured_events = [str(log.get("event", "")) for log in captured]
    captured_logs_text = "\n".join(captured_events)

    extracted_times = {}

    for unit in ALL_UNITS:
        # Check if unit started
        expected_start = f"Started : {unit}"
        assert any(expected_start in event for event in captured_events), (
            f"Pass Criteria Failed: Mandatory start trace '{expected_start}' was not found.\n"
        )

        # Extract execution time
        completion_pattern = rf"Completed : {unit} in ([\d\.]+) seconds"
        unit_time = None

        for event in captured_events:
            match = re.search(completion_pattern, event)
            if match:
                unit_time = float(match.group(1))
                break

        assert unit_time is not None, (
            f"Pass Criteria Failed: Completion log trace with time for '{unit}' was not found."
        )

        extracted_times[unit] = unit_time
        logger.info(f"Extracted time for {unit}: {unit_time:.4f} seconds")

    # Sum times according to workflow segments
    cslc_total_time = sum(extracted_times[unit] for unit in CSLC_UNITS)
    gmlp_total_time = sum(extracted_times[unit] for unit in GMLP_UNITS)

    logger.info(f"Total Internal CSLC Time (Ingestion -> Coregistration): {cslc_total_time:.2f}s")
    logger.info(f"Total Internal GMLP Time (CMLP -> Geocoding): {gmlp_total_time:.2f}s")

    return cslc_total_time, gmlp_total_time


@pytest.mark.ard
def test_cslc_scalability_perf_002_007():
    """E2E Performance & Scalability Test TC-ST-PERF-002 and TC-ST-PERF-007."""
    assert YAML_4C_PATH.exists(), f"Missing 4-core trigger YAML at {YAML_4C_PATH}"
    assert YAML_8C_PATH.exists(), f"Missing 8-core trigger YAML at {YAML_8C_PATH}"

    # Verify the host has enough cores to run the test
    total_cores = os.cpu_count()
    assert total_cores is not None and total_cores >= 8, (
        f"Insufficient CPU cores to run 8-core scalability test. Found: {total_cores}"
    )

    # --------------------------------------------------------------------------
    # STEP 1: Baseline Execution (4 Cores / Workers)
    # --------------------------------------------------------------------------
    with open(YAML_4C_PATH, encoding="utf-8") as f:
        payload_4c = yaml.safe_load(f)

    logger.info("Setting CPU affinity to 4 cores (0-3)...")
    os.sched_setaffinity(0, set(range(4)))

    logger.info("Starting Execution: 4-core baseline...")
    with capture_logs() as captured_4c:
        output_stores_4c = run_payload(payload_4c, dry_run=False)

    cslc_4c, gmlp_4c = validate_and_extract_times(output_stores_4c, captured_4c)
    logger.info(f"4-Core Internal Execution Times -> CSLC: {cslc_4c:.2f}s | GMLP: {gmlp_4c:.2f}s")

    # --------------------------------------------------------------------------
    # STEP 2: Scalability Execution (8 Cores / Workers)
    # --------------------------------------------------------------------------
    with open(YAML_8C_PATH, encoding="utf-8") as f:
        payload_8c = yaml.safe_load(f)

    logger.info("Setting CPU affinity to 8 cores (0-7)...")
    os.sched_setaffinity(0, set(range(8)))

    logger.info("Starting Execution: 8-core run...")
    with capture_logs() as captured_8c:
        output_stores_8c = run_payload(payload_8c, dry_run=False)

    cslc_8c, gmlp_8c = validate_and_extract_times(output_stores_8c, captured_8c)
    logger.info(f"8-Core Internal Execution Times -> CSLC: {cslc_8c:.2f}s | GMLP: {gmlp_8c:.2f}s")

    # Reset affinity back to all available cores after execution
    os.sched_setaffinity(0, set(range(total_cores)))

    # --------------------------------------------------------------------------
    # STEP 3: Ratio R Calculation and ESA Acceptance Criteria Verification
    # --------------------------------------------------------------------------

    # TC-ST-PERF-002: CSLC Scalability
    ratio_cslc = cslc_8c / cslc_4c
    logger.info(
        f"TC-ST-PERF-002: CSLC Scalability Ratio R = T8c / T4c = {cslc_8c:.2f} / {cslc_4c:.2f} = {ratio_cslc:.4f}"
    )

    assert ratio_cslc <= 0.75, (
        f"Pass Criteria Failed (TC-ST-PERF-002): CSLC workflow scalability requirement not met.\n"
        f"Expected ratio R = (T8c / T4c) <= 0.75, but achieved R = {ratio_cslc:.4f}.\n"
        f"T4c (4-core): {cslc_4c:.2f}s | T8c (8-core): {cslc_8c:.2f}s"
    )

    # TC-ST-PERF-007: GMLP Scalability
    ratio_gmlp = gmlp_8c / gmlp_4c
    logger.info(
        f"TC-ST-PERF-007: GMLP Scalability Ratio R = T8c / T4c = {gmlp_8c:.2f} / {gmlp_4c:.2f} = {ratio_gmlp:.4f}"
    )

    assert ratio_gmlp <= 0.75, (
        f"Pass Criteria Failed (TC-ST-PERF-007): GMLP workflow scalability requirement not met.\n"
        f"Expected ratio R = (T8c / T4c) <= 0.75, but achieved R = {ratio_gmlp:.4f}.\n"
        f"T4c (4-core): {gmlp_4c:.2f}s | T8c (8-core): {gmlp_8c:.2f}s"
    )

    logger.info("All scalability requirements (CSLC and GMLP) passed successfully.")