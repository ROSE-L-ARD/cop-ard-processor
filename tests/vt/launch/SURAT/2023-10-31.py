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
from cop_ard_core.logger import logger
from structlog.testing import capture_logs

from cop_ard_processor.structure import check_ARD_outputs
from scripts.run_payload import run_payload

RP_BASE_PATH = Path(__file__).parents[2]
yaml_path = RP_BASE_PATH / "triggering" / "SURAT" / "2023-10-31.yaml"


@pytest.mark.ard
def test_payload():
    with open(yaml_path, encoding="utf-8") as f:
        payload = yaml.safe_load(f)

    with capture_logs() as captured:
        output_stores = run_payload(payload, dry_run=False)

        for output, output_store in output_stores.items():
            storage_opts = output_store.get("storage_options", {}) if isinstance(output_store, dict) else {}
            storage_opts = is_local(output_store["path"], storage_opts)

            data = xr.open_datatree(output_store["path"], engine="zarr", chunks={}, storage_options=storage_opts)

            if output not in ["cslcs", "gmlp"]:
                continue

            for element in data.values():
                check_ARD_outputs(element, output, logger=logger)

    captured_events = [str(log.get("event", "")) for log in captured]
    mandatory_units = ["Ingestion Unit", "ReferenceDEM Unit", "ReferenceGeometry Unit", "Coregistration Unit"]
    for unit in mandatory_units:
        assert any(f"Started : {unit}" in event for event in captured_events), (
            f"Mandatory trace 'Started : {unit}' missing."
        )

    # Feature Verifications
    assert not any("Skipping ETAD step" in event for event in captured_events), "ETAD was skipped."
    assert any("ETAD" in event for event in captured_events), "ETAD execution trace missing."

    bypassed_steps = ["external orbit", "image matching", "split spectrum", "ESD"]
    for step in bypassed_steps:
        assert any(f"Skipping {step} step" in event for event in captured_events), f"Skip trace for '{step}' missing."
