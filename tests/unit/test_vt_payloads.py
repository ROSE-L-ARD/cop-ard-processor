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
"""Offline unit tests for the TC-ST-DSGN-001/002 and TC-ST-RAMS-001 preflight payloads.

For the three 1-step Ingestion payloads under ``tests/vt/triggering`` this
module checks, without shelling out and without starting any dask cluster:

1. the YAML parses with ``yaml.safe_load``;
2. the Ingestion workflow step carries its MOD markers (the
   ``validate_run`` flag and the MOD parameters / ADF values);
3. the EOPF workflow plan builds offline and the mandatory ADF-key check
   (``_preprocess_mandatory``) passes: the 1-step no-output workflow
   requests only the mandatory ``REFDB`` ADF and no inputs/outputs.

The six downstream-unit preflight scenarios (also part of TC-ST-DSGN-001)
are checked the same way, generalized to multi-step workflows: the target
step carries exactly one MOD parameter and no ``outputs`` block, every
upstream step keeps its nominal TC-ST-FUNC-001 parameters, and the plan
requests the mandatory ADFs of every step in the chain.
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
MOD_ETAD_PATH = (
    "/data/SENTINEL1/SURAT/MOD_ETAD/S1A_IW_ETA__AXDV_20231031T084128_20231031T084155_051006_062667_9059.SAFE"
)
REFDB_PATH = "/data/SENTINEL1/SURAT/WDIR/REFDB"
DEM_PATH = "/data/SENTINEL1/SURAT/dem"

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

# Downstream-unit preflight scenarios, all part of the existing TC-ST-DSGN-001
# design case: (relative YAML path, target step, expected active steps in
# order, MOD parameter, MOD value, expected plan requested_io_outputs).
DOWNSTREAM_CASES = [
    pytest.param(
        "TC-ST-DSGN-001/mod_params_reference_dem.yaml",
        "reference_dem",
        ["ingestion", "reference_dem"],
        "recompute",
        "yes",
        {},
        id="dsgn-001-reference-dem",
    ),
    pytest.param(
        "TC-ST-DSGN-001/mod_params_reference_geometry.yaml",
        "reference_geometry",
        ["ingestion", "reference_dem", "reference_geometry"],
        "enable_dask",
        "no",
        {},
        id="dsgn-001-reference-geometry",
    ),
    pytest.param(
        "TC-ST-DSGN-001/mod_params_coregistration.yaml",
        "coregistration",
        ["ingestion", "reference_dem", "reference_geometry", "coregistration"],
        "coherence_threshold_image_matching",
        2.5,
        {},
        id="dsgn-001-coregistration",
    ),
    pytest.param(
        "TC-ST-DSGN-001/mod_params_reference_update.yaml",
        "reference_update",
        ["ingestion", "reference_dem", "reference_geometry", "coregistration", "reference_update"],
        "stable_interval_days",
        "365",
        {"cslcs": ["coregistration.cslcs"]},
        id="dsgn-001-reference-update",
    ),
    pytest.param(
        "TC-ST-DSGN-001/mod_params_cmlp.yaml",
        "cmlp",
        ["ingestion", "reference_dem", "reference_geometry", "coregistration", "cmlp"],
        "max_past_stable_history",
        2.5,
        {"cslcs": ["coregistration.cslcs"]},
        id="dsgn-001-cmlp",
    ),
    pytest.param(
        "TC-ST-DSGN-001/mod_params_geocoding.yaml",
        "geocoding",
        ["ingestion", "reference_dem", "reference_geometry", "coregistration", "cmlp", "geocoding"],
        "spacing",
        [0],
        {
            "cslcs": ["coregistration.cslcs"],
            "current_reference": ["cmlp.current_reference"],
            "stable_references": ["cmlp.stable_references"],
        },
        id="dsgn-001-geocoding",
    ),
]

DOWNSTREAM_FILES = [pytest.param(*case.values, id=case.id) for case in DOWNSTREAM_CASES]

# Nominal per-step parameters of the TC-ST-FUNC-001 wiring, for the steps the
# downstream scenarios keep upstream of their target.
NOMINAL_STEP_PARAMS = {
    "ingestion": {"input_slcs_files": [SAFE_PRODUCT], "indices": [0, 1, 27, 28]},
    "reference_dem": {},
    "reference_geometry": {},
    "coregistration": {"initialize_current_reference": True},
    "cmlp": {},
}


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


@pytest.mark.parametrize("path, target, steps, param, value, outputs", DOWNSTREAM_FILES)
def test_downstream_payload_carries_mod_markers(path, target, steps, param, value, outputs):
    """Each downstream payload wires the nominal chain and breaks only its target step."""
    payload = _load_payload(path)

    assert [step["name"] for step in payload["workflow"]] == steps
    assert payload["general_configuration"]["triggering__validate_run"] is False

    for step in payload["workflow"]:
        if step["name"] == target:
            assert step["parameters"] == {param: value}
            assert "outputs" not in step
        else:
            assert step.get("parameters", {}) == NOMINAL_STEP_PARAMS[step["name"]]

    # Per-scenario scratch space: REFDB under the scenario subdir, shared DEM.
    assert {adf["id"]: adf["path"] for adf in payload["io"]["adfs"]} == {
        "REFDB": f"/data/SENTINEL1/SURAT/WDIR/TC-ST-DSGN-001/{target}/REFDB",
        "DEM": DEM_PATH,
    }


@pytest.mark.parametrize("path, target, steps, param, value, outputs", DOWNSTREAM_FILES)
def test_downstream_workflow_plan_builds_offline(path, target, steps, param, value, outputs):
    """Each downstream payload builds a valid workflow plan offline and passes the ADF check."""
    payload = _load_payload(path)
    _bootstrap_eo_configuration(payload)

    workflow = build_workflow(load_section(payload, WorkflowSectionRaw).workflow)
    plan = workflow.plan

    assert [unit.identifier for unit in plan.workflow if unit.active] == steps
    assert plan.requested_io_adfs == {
        "REFDB": [f"{step}.REFDB" for step in steps],
        "DEM": ["reference_dem.DEM"],
    }
    assert plan.requested_io_outputs == outputs
    assert plan.requested_io_inputs == {}

    # The mandatory ADF-key check must pass against the payload's io.adfs entries.
    io_config = build_io(load_section(payload, EOIOSectionRaw).io)
    workflow._validator.validate(io_config.adfs, io_config.input_products, io_config.output_products)
