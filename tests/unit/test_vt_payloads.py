# Copyright 2026 B-Open
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Offline unit tests for the three Ingestion preflight payload YAMLs.

For every payload under ``tests/vt/triggering`` this module checks, without
shelling out and without starting any dask cluster:

1. the YAML parses with ``yaml.safe_load``;
2. the Ingestion workflow step carries its MOD markers (the
   ``validate_run`` flag and the MOD parameters / ADF values);
3. the EOPF workflow plan builds offline and the mandatory ADF-key check
   (``_preprocess_mandatory``) passes: the 1-step no-output workflow
   requests only the mandatory ``REFDB`` ADF and no inputs/outputs.
"""

from pathlib import Path

import pytest
import yaml

from eopf.config.config import EOConfiguration
from eopf.triggering.parsers import (
    EOIOSectionRaw,
    WorkflowSectionRaw,
    build_io,
    build_workflow,
    load_section,
)

pytestmark = pytest.mark.unit

TRIGGERING_DIR = Path(__file__).parents[2] / "tests" / "vt" / "triggering"

SAFE_NAME = "S1A_IW_SLC__1SDV_20230727T084125_20230727T084152_049606_05F712_E111.SAFE"
SAFE_PRODUCT = f"/data/SENTINEL1/SURAT/SAFE/{SAFE_NAME}"
MOD_SLC_PATH = f"/data/SENTINEL1/SURAT/SAFE/{SAFE_NAME[:-5]}_MODSLC.SAFE"
MOD_ETAD_PATH = "/data/SENTINEL1/SURAT/WDIR/MOD-ETAD-20230727"
REFDB_PATH = "/data/SENTINEL1/SURAT/WDIR/REFDB"

# (relative YAML path, expected validate_run, expected step adfs, expected MOD/nominal parameters, expected io.adfs)
CASES = [
    pytest.param(
        "TC-ST-DSGN-001/mod_params.yaml",
        False,
        {"REFDB": "REFDB"},
        {
            "input_slcs_files": [SAFE_PRODUCT],
            "indices": [-1],
            "calibration_type": "WrongCalibration",
            "polarization": "XX",
        },
        {"REFDB": REFDB_PATH},
        id="dsgn-001-mod-params",
    ),
    pytest.param(
        "TC-ST-RAMS-001/mod_slc.yaml",
        True,
        {"REFDB": "REFDB"},
        {
            "input_slcs_files": [MOD_SLC_PATH],
            "indices": [0, 1, 27, 28],
            "schema_validation": True,
        },
        {"REFDB": REFDB_PATH},
        id="rams-001-mod-slc",
    ),
    pytest.param(
        "TC-ST-DSGN-002/mod_etad.yaml",
        True,
        {"REFDB": "REFDB", "ETAD": "ETAD"},
        {
            "input_slcs_files": [SAFE_PRODUCT],
            "indices": [0, 1, 27, 28],
        },
        {"REFDB": REFDB_PATH, "ETAD": MOD_ETAD_PATH},
        id="dsgn-002-mod-etad",
    ),
]

PAYLOAD_FILES = [pytest.param(case.values[0], id=case.id) for case in CASES]


def _load_payload(rel_path: str) -> dict:
    with open(TRIGGERING_DIR / rel_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


@pytest.mark.parametrize("rel_path, validate_run, step_adfs, step_params, io_adfs", CASES)
def test_payload_yaml_parses_and_carries_mod_markers(rel_path, validate_run, step_adfs, step_params, io_adfs):
    """The payload YAML parses and its Ingestion step carries the MOD markers."""
    payload = _load_payload(rel_path)

    step = payload["workflow"][0]
    assert step["name"] == "ingestion"
    assert step["module"] == "cop_ard_processor.processing_units"
    assert step["processing_unit"] == "Ingestion"

    assert payload["general_configuration"]["triggering__validate_run"] is validate_run
    assert step["adfs"] == step_adfs
    for key, value in step_params.items():
        assert step["parameters"][key] == value, f"step parameter '{key}' does not carry the expected value"
    assert {adf["id"]: adf["path"] for adf in payload["io"]["adfs"]} == io_adfs


def _bootstrap_eo_configuration(payload: dict) -> dict:
    """Mirror ``EORunner._load_eo_configuration`` without importing the runner.

    ``triggering__error_policy`` is normally registered at
    ``eopf.triggering.runner`` import time; since this test only imports the
    parsers, the value is set explicitly when absent.
    """
    general_config = dict(payload["general_configuration"])
    EOConfiguration().load_dict(general_config)
    if EOConfiguration().get("triggering__load_default_configuration", True):
        EOConfiguration().load_default_file(policy="old")
    if not EOConfiguration().has_value("triggering__error_policy"):
        EOConfiguration()["triggering__error_policy"] = "FAIL_FAST"
    return general_config


@pytest.mark.parametrize("rel_path", PAYLOAD_FILES)
def test_workflow_plan_builds_offline(rel_path: str):
    """The 1-step no-output plan builds offline and the mandatory ADF-key check passes."""
    payload = _load_payload(rel_path)
    _bootstrap_eo_configuration(payload)

    workflow = build_workflow(load_section(payload, WorkflowSectionRaw).workflow)
    plan = workflow.plan

    # A single active Ingestion unit requesting only the mandatory REFDB ADF.
    assert [unit.identifier for unit in plan.workflow if unit.active] == ["ingestion"]
    assert plan.requested_io_adfs == {"REFDB": ["ingestion.REFDB"]}
    assert plan.requested_io_outputs == {}
    assert plan.requested_io_inputs == {}

    # The mandatory ADF-key check must pass against the payload's io.adfs entries.
    io_config = build_io(load_section(payload, EOIOSectionRaw).io)
    workflow._validator.validate(io_config.adfs, io_config.input_products, io_config.output_products)
