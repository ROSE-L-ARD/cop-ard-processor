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
"""Offline unit tests for the downstream processing-model contracts (Layer 2).

Covers the six processing units that consume Ingestion output: ReferenceDEM,
ReferenceGeometry, Coregistration, ReferenceUpdate, CMLP and Geocoding.
"""

import json
from pathlib import Path

import pytest
import xarray as xr
from eopf.common.functions_utils import camel_to_snake
from eopf.computing.types import AuxiliaryDataFile
from eopf.computing.validation import validate_run_parameters
from eopf.computing.validation_model import EOProcessingModel
from eopf.exceptions.errors import EOComputingModelError

from cop_ard_processor.processing_units import (
    CMLP,
    Coregistration,
    Geocoding,
    ReferenceDEM,
    ReferenceGeometry,
    ReferenceUpdate,
)

pytestmark = pytest.mark.unit

MODELS_DIR = Path(__file__).parents[2] / "cop_ard_processor" / "models"

# (unit, expected input min_occurs, expected ADF min_occurs, nominal kwargs, bad kwargs)
CASES = [
    (ReferenceDEM, {"slcs": 1}, {"REFDB": 1, "DEM": 1}, {}, {"recompute": "yes"}),
    (ReferenceGeometry, {"slcs": 1, "dem": 1}, {"REFDB": 1}, {}, {"enable_dask": "no"}),
    (
        Coregistration,
        {"slcs": 1, "dem": 1, "simulation_ref": 1, "simulation_ml": 1},
        {"REFDB": 1},
        {"initialize_current_reference": True},
        {"coherence_threshold_image_matching": 2.5},
    ),
    (
        ReferenceUpdate,
        {"slcs": 1, "dem": 0, "simulation_ref": 0, "simulation_ml": 0},
        {"REFDB": 1},
        {},
        {"stable_interval_days": "365"},
    ),
    (CMLP, {"slcs": 1}, {"REFDB": 1}, {}, {"max_past_stable_history": 2.5}),
    (
        Geocoding,
        {"cmlp": 1, "slcs": 1, "simulation_ref": 1, "simulation_ml": 1},
        {"REFDB": 1},
        {},
        {"enable_dask": "no"},
    ),
]
CASE_IDS = ["reference-dem", "reference-geometry", "coregistration", "reference-update", "cmlp", "geocoding"]

UNITS = [case[0] for case in CASES]


def _adf(name, path):
    return AuxiliaryDataFile(name=name, path=path)


def _model_file_name(unit):
    return f"{camel_to_snake(unit.PROCESSOR_NAME)}_{camel_to_snake(unit.PROCESSOR_VERSION)}.json"


def _contract(unit):
    model = unit.processor_contract()
    assert model is not None
    return model


def _inputs(expected_inputs):
    return {name: xr.DataTree() for name in expected_inputs}


def _adfs(expected_adfs):
    return {name: _adf(name, f"/data/SENTINEL1/SURAT/WDIR/{name}") for name in expected_adfs}


@pytest.mark.parametrize("unit", UNITS, ids=CASE_IDS)
def test_processor_identity(unit):
    assert unit.PROCESSOR_NAME == unit.__name__
    assert unit.PROCESSOR_VERSION == "0.0.1"


@pytest.mark.parametrize("unit", UNITS, ids=CASE_IDS)
def test_processor_contract_loaded(unit):
    contract = unit.processor_contract()
    assert contract is not None
    assert isinstance(contract, EOProcessingModel)


@pytest.mark.parametrize("unit, expected_inputs, expected_adfs, nominal_kwargs, bad_kwargs", CASES, ids=CASE_IDS)
def test_model_file_matches_processor_identity(unit, expected_inputs, expected_adfs, nominal_kwargs, bad_kwargs):
    model_file = MODELS_DIR / _model_file_name(unit)
    assert model_file.is_file()
    with open(model_file, encoding="utf-8") as handle:
        data = json.load(handle)

    model = EOProcessingModel(**data)
    assert model.available_modes == ["default"]
    assert model.default_mode == "default"

    default = model.modes_config["default"]
    assert default.outputs == {}
    assert {name: spec.min_occurs for name, spec in default.inputs.items()} == expected_inputs
    assert {name: spec.min_occurs for name, spec in default.adfs.items()} == expected_adfs
    for spec in default.parameters.values():
        assert spec.required is False


@pytest.mark.parametrize("unit, expected_inputs, expected_adfs, nominal_kwargs, bad_kwargs", CASES, ids=CASE_IDS)
def test_nominal_call_passes_contract(unit, expected_inputs, expected_adfs, nominal_kwargs, bad_kwargs):
    anomalies = validate_run_parameters(
        _contract(unit),
        _inputs(expected_inputs),
        mode=None,
        adfs=_adfs(expected_adfs),
        validate_product_model=False,
        raises=True,
        **nominal_kwargs,
    )
    assert anomalies == []


@pytest.mark.parametrize("unit, expected_inputs, expected_adfs, nominal_kwargs, bad_kwargs", CASES, ids=CASE_IDS)
def test_bad_parameter_raises(unit, expected_inputs, expected_adfs, nominal_kwargs, bad_kwargs):
    with pytest.raises(EOComputingModelError):
        validate_run_parameters(
            _contract(unit),
            _inputs(expected_inputs),
            mode=None,
            adfs=_adfs(expected_adfs),
            validate_product_model=False,
            raises=True,
            **bad_kwargs,
        )


@pytest.mark.parametrize("unit, expected_inputs, expected_adfs, nominal_kwargs, bad_kwargs", CASES, ids=CASE_IDS)
def test_undeclared_parameter_raises(unit, expected_inputs, expected_adfs, nominal_kwargs, bad_kwargs):
    with pytest.raises(EOComputingModelError):
        validate_run_parameters(
            _contract(unit),
            _inputs(expected_inputs),
            mode=None,
            adfs=_adfs(expected_adfs),
            validate_product_model=False,
            raises=True,
            not_a_parameter="x",
        )


@pytest.mark.parametrize("unit, expected_inputs, expected_adfs, nominal_kwargs, bad_kwargs", CASES, ids=CASE_IDS)
def test_missing_refdb_raises(unit, expected_inputs, expected_adfs, nominal_kwargs, bad_kwargs):
    with pytest.raises(EOComputingModelError):
        validate_run_parameters(
            _contract(unit),
            _inputs(expected_inputs),
            mode=None,
            adfs={},
            validate_product_model=False,
            raises=True,
        )
