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
from cop_ard_core.logger import logger
from structlog.testing import capture_logs
from scripts.run_payload import run_payload

RP_BASE_PATH = Path(__file__).parents[2]
yaml_path = RP_BASE_PATH / "triggering" / "SURAT_ESD" / "REFERENCE.yaml"

@pytest.mark.ard
def test_payload():
    with open(yaml_path, encoding="utf-8") as f:
        payload = yaml.safe_load(f)

    with capture_logs() as captured:
        run_payload(payload, dry_run=False)

    captured_events = [str(log.get("event", "")) for log in captured]

    mandatory_units = ["Ingestion Unit", "ReferenceDEM Unit", "ReferenceGeometry Unit"]
    for unit in mandatory_units:
        assert any(f"Started : {unit}" in event for event in captured_events), f"Mandatory log trace 'Started : {unit}' missing."
