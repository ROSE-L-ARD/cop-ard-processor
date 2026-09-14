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
import yaml
from cop_ard_app.io import read
from cop_ard_core.logger import logger

from cop_ard_processor.structure import check_ARD_outputs
from cop_ard_app.utils import update_reference_database
from scripts.run_payload import run_payload

RP_BASE_PATH = Path(__file__).parents[2]
yaml_path = f"{RP_BASE_PATH}/triggering/TC-ST-FUNC-001/2023-08-08.yaml"


@pytest.mark.ard
def test_payload():
    with open(yaml_path) as f:
        payload = yaml.safe_load(f)

    output_stores = run_payload(payload, dry_run=False)

    for output, output_store in output_stores.items():
        logger.info(f"Checking : {output}")
        logger.info(f"Checking : {output_store}")
        storage_opts = output_store.get("storage_options", {}) if isinstance(output_store, dict) else {}
        data = read(output_store["path"], storage_options=storage_opts)
        for element in data.values():
            check_ARD_outputs(element, output, logger=logger)

    # Persist updated reference datasets to reference database if reference outputs exist
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
            output_stores["current_reference"],
            output_stores["stable_references"],
            refdb_path,
            reference_id=reference_id,
        )
