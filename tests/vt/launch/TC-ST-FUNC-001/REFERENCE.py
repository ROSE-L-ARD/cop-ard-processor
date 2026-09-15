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

import logging
from pathlib import Path

import pytest
import xarray as xr
import yaml
from cop_ard_app.io import is_local
from cop_ard_app.utils import update_reference_database
from cop_ard_core.logger import logger

from cop_ard_processor.structure import check_ARD_outputs
from scripts.run_payload import run_payload

RP_BASE_PATH = Path(__file__).parents[2]
yaml_path = f"{RP_BASE_PATH}/triggering/TC-ST-FUNC-001/REFERENCE.yaml"


@pytest.mark.ard
def test_payload(caplog: pytest.LogCaptureFixture):
    # Set logging level to capture INFO logs in memory
    caplog.set_level(logging.INFO)

    with open(yaml_path, encoding="utf-8") as f:
        payload = yaml.safe_load(f)

    # 1. Execute full CSLC chain
    output_stores = run_payload(payload, dry_run=False)
    updates = {}

    # 2. Inspect output stores using xarray.open_datatree & verify dual-polarization
    for output, output_store in output_stores.items():
        logger.info(f"Checking : {output}")
        logger.info(f"Checking : {output_store}")
        storage_opts = output_store.get("storage_options", {}) if isinstance(output_store, dict) else {}
        storage_opts = is_local(output_store["path"], storage_opts)
        
        data = xr.open_datatree(output_store["path"], engine="zarr", chunks={}, storage_options=storage_opts)
        
        if output not in ["cslcs", "gmlp"]:
            updates[output] = data
            continue
            
        for element in data.values():
            check_ARD_outputs(element, output, logger=logger)
            
            # Confirm concurrent processing of dual-polarization (VV + VH)
            if output == "cslcs":
                pols = set(element.measurements.cslc.polarization.values.astype(str))
                assert {"VV", "VH"}.issubset(pols), f"Dual-pol check failed. Found polarizations: {pols}"

    # 3. Monitor and verify mandatory log trace events
    captured_logs = caplog.text

    mandatory_units = [
        "Ingestion Unit",
        "ReferenceDEM Unit",
        "ReferenceGeometry Unit",
        "Coregistration Unit",
        "ReferenceUpdate Unit",
    ]
    for unit in mandatory_units:
        expected_trace = f"Started : {unit}"
        assert expected_trace in captured_logs, (
            f"Pass Criteria Failed: Mandatory log trace '{expected_trace}' was not found in captured execution logs."
        )

    # 4. Verify optional steps bypass/skip traces
    bypassed_steps = [
        "ETAD",
        "external orbit",
        "image matching",
        "split-spectrum",
        "ESD",
    ]
    for step in bypassed_steps:
        expected_skip_log = f"Skipping {step} step"
        assert expected_skip_log in captured_logs, (
            f"Pass Criteria Failed: Mandatory skip trace '{expected_skip_log}' was not found in execution logs."
        )

    # 5. Persist updated reference datasets to reference database if reference outputs exist
    if "current_reference" in output_stores and "stable_references" in output_stores:
        logger.debug("SAVING CURRENT REFERENCE")

        # Dynamically retrieve REFDB path from payload IO definition
        refdb_path = None
        for adf in payload.get("io", {}).get("adfs", []):
            if adf.get("id") == "REFDB":
                refdb_path = adf.get("path")
                break

        # Dynamically retrieve reference_id from ingestion step parameters
        reference_id = None
        for step in payload.get("workflow", []):
            if step.get("name") == "ingestion":
                reference_id = step.get("parameters", {}).get("reference_id")
                break

        update_reference_database(
            updates["current_reference"],
            updates["stable_references"],
            refdb_path,
            reference_id=reference_id,
        )
