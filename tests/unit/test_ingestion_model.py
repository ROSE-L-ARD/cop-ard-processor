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
"""Offline unit tests for the Ingestion processing-model contract (Layer 2)."""

import json
from pathlib import Path

import pytest

from cop_ard_processor.processing_units import Ingestion
from eopf.computing.types import AuxiliaryDataFile
from eopf.computing.validation import validate_run_parameters
from eopf.computing.validation_model import EOProcessingModel
from eopf.exceptions.errors import EOComputingModelError

pytestmark = pytest.mark.unit

MODEL_FILE = Path(__file__).parents[2] / "cop_ard_processor" / "models" / "ingestion_0.0.1.json"

SAFE_PRODUCT = "S1A_IW_SLC__1SDV_20230727T084125_20230727T084152_049606_05F712_E111.SAFE"
NOMINAL_KWARGS = {
    "input_slcs_files": ["/data/SENTINEL1/SURAT/SAFE/" + SAFE_PRODUCT],
    "indices": [0, 1, 27, 28],
}

REFDB_PATH = "/data/SENTINEL1/SURAT/WDIR/REFDB"
ETAD_PATH = "/data/SENTINEL1/SURAT/WDIR/ETAD_IW"
ORB_PATH = "/data/SENTINEL1/SURAT/WDIR/ORBIT"


def _adf(name, path):
    return AuxiliaryDataFile(name=name, path=path)


def _contract():
    model = Ingestion.processor_contract()
    assert model is not None
    return model


def test_processor_identity():
    assert Ingestion.PROCESSOR_NAME == "Ingestion"
    assert Ingestion.PROCESSOR_VERSION == "0.0.1"


def test_model_file_matches_processor_identity():
    assert MODEL_FILE.is_file()
    with open(MODEL_FILE, encoding="utf-8") as handle:
        data = json.load(handle)

    model = EOProcessingModel(**data)
    assert model.available_modes == ["default"]
    assert model.default_mode == "default"

    default = model.modes_config["default"]
    assert default.inputs == {}
    assert default.outputs == {}
    assert list(default.adfs) == ["REFDB", ".*ETAD.*", ".*ORB.*"]
    assert default.adfs["REFDB"].min_occurs == 1
    assert default.adfs[".*ETAD.*"].min_occurs == 0
    assert default.adfs[".*ORB.*"].min_occurs == 0

    parameters = default.parameters
    assert parameters["input_slcs_files"].required is True
    for name, spec in parameters.items():
        if name != "input_slcs_files":
            assert spec.required is False, name


def test_nominal_call_passes_contract():
    adfs = {"REFDB": _adf("REFDB", REFDB_PATH)}
    anomalies = validate_run_parameters(
        _contract(), {}, mode=None, adfs=adfs, validate_product_model=False, raises=True, **NOMINAL_KWARGS
    )
    assert anomalies == []


def test_all_adf_kinds_pass_contract():
    adfs = {
        "REFDB": _adf("REFDB", REFDB_PATH),
        "ETAD_IW": _adf("ETAD_IW", ETAD_PATH),
        "ORB": _adf("ORB", ORB_PATH),
    }
    anomalies = validate_run_parameters(
        _contract(), {}, mode=None, adfs=adfs, validate_product_model=False, raises=True, **NOMINAL_KWARGS
    )
    assert anomalies == []


def test_bad_calibration_type_raises():
    adfs = {"REFDB": _adf("REFDB", REFDB_PATH)}
    kwargs = {**NOMINAL_KWARGS, "calibration_type": "Wrong"}
    with pytest.raises(EOComputingModelError):
        validate_run_parameters(
            _contract(), {}, mode=None, adfs=adfs, validate_product_model=False, raises=True, **kwargs
        )


def test_undeclared_parameter_raises():
    adfs = {"REFDB": _adf("REFDB", REFDB_PATH)}
    kwargs = {**NOMINAL_KWARGS, "bogus_param": 1}
    with pytest.raises(EOComputingModelError):
        validate_run_parameters(
            _contract(), {}, mode=None, adfs=adfs, validate_product_model=False, raises=True, **kwargs
        )


def test_missing_refdb_raises():
    with pytest.raises(EOComputingModelError):
        validate_run_parameters(
            _contract(), {}, mode=None, adfs={}, validate_product_model=False, raises=True, **NOMINAL_KWARGS
        )
