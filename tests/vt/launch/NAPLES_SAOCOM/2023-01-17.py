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
yaml_path = RP_BASE_PATH / "triggering" / "NAPLES_SAOCOM" / "2023-01-17.yaml"


@pytest.mark.ard
def test_payload():
    with open(yaml_path, encoding="utf-8") as f:
        payload = yaml.safe_load(f)

    with capture_logs() as captured:
        output_stores = run_payload(payload, dry_run=False)
        updates = {}

        for output, output_store in output_stores.items():
            logger.info(f"Checking output store: {output}")
            logger.info(f"Store metadata: {output_store}")

            storage_opts = output_store.get("storage_options", {}) if isinstance(output_store, dict) else {}
            storage_opts = is_local(output_store["path"], storage_opts)

            data = xr.open_datatree(output_store["path"], engine="zarr", chunks={}, storage_options=storage_opts)

            if output not in ["cslcs", "gmlp"]:
                updates[output] = data
                continue

            for element in data.values():
                check_ARD_outputs(element, output, logger=logger)

    captured_events = [str(log.get("event", "")) for log in captured]

    mandatory_units = ["Ingestion Unit", "ReferenceDEM Unit", "ReferenceGeometry Unit", "Coregistration Unit"]
    for unit in mandatory_units:
        assert any(f"Started : {unit}" in event for event in captured_events), (
            f"Mandatory trace 'Started : {unit}' missing."
        )

    # 1. Feature Verifications (ETAD)
    assert any("Skipping ETAD step" in event for event in captured_events), "ETAD should be skipped."

    # 2. Check that ESD is skipped
    assert any("Skipping ESD step" in event or "Skipping ESD" in event for event in captured_events), (
        "Skip trace for 'ESD' missing."
    )

    # 3. Check that Image Matching and Split Spectrum were DONE (NOT skipped)
    assert not any("Skipping image matching" in event for event in captured_events), (
        "Image matching was unexpectedly skipped."
    )
    assert not any("Skipping split spectrum" in event for event in captured_events), (
        "Split spectrum was unexpectedly skipped."
    )

    # 4. Check external orbit bypass
    assert any("Skipping external orbit" in event for event in captured_events), (
        "Skip trace for 'external orbit' missing."
    )

    # 5. Reference Database Persistence
    if "current_reference" in output_stores and "stable_references" in output_stores:
        logger.debug("SAVING CURRENT REFERENCE")

        refdb_path = None
        for adf in payload.get("io", {}).get("adfs", []):
            if adf.get("id") == "REFDB":
                refdb_path = adf.get("path")
                break

        reference_id = None
        for step in payload.get("workflow", []):
            if step.get("name") == "ingestion":
                reference_id = step.get("parameters", {}).get("reference_id")
                break

        update_reference_database(
            updates["current_reference"],
            updates["stable_references"],
            f"{refdb_path}/reference_db_dynamic",
            reference_id=reference_id,
        )
