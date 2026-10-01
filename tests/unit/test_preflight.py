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
"""Offline unit tests for the Layer-1 (preflight) validation engine.

Covers the resolved-parameter sanity checks, the ETAD product integrity
checks and the SAFE archive XML checks implemented in
:mod:`cop_ard_processor.preflight`, plus the ``Ingestion.preflight`` wiring
in the processing unit. Every fixture is written under ``tmp_path`` so the
tests stay fully offline.
"""

import numpy as np
import pytest
import xarray as xr

from cop_ard_processor.preflight import (
    EtadStackValidator,
    PreflightError,
    SafeXmlValidator,
    Violation,
    run_preflight,
)
from cop_ard_processor.processing_units import Ingestion

pytestmark = pytest.mark.unit

SAFE_PRODUCT = "S1A_IW_SLC__1SDV_20230727T084125_20230727T084152_049606_05F712_E111.SAFE"
NOMINAL_SAFE_FILE = "/data/SENTINEL1/SURAT/SAFE/" + SAFE_PRODUCT

NOMINAL_KWARGS = {
    "input_slcs_files": [NOMINAL_SAFE_FILE],
    "indices": [0, 1, 27, 28],
}


# ---------------------------------------------------------------------------
# Resolved-parameter checks
# ---------------------------------------------------------------------------


def test_nominal_kwargs_pass_preflight():
    assert run_preflight({}, None, **NOMINAL_KWARGS) == []


def test_only_mandatory_parameter_passes_preflight():
    violations = run_preflight({}, None, input_slcs_files=NOMINAL_SAFE_FILE)
    assert violations == []


@pytest.mark.parametrize(
    ("parameter", "value"),
    [
        ("indices", [-1]),
        ("indices", [0, "1"]),
        ("calibration_type", "Wrong"),
        ("deramp", "yes"),
        ("enable_dask", 1),
        ("reference_id", ""),
        ("input_slcs_files", []),
        ("polarization", "XX"),
        ("chunks", {"azimuth_time": 0}),
    ],
)
def test_single_bad_parameter_is_reported(parameter, value):
    kwargs = {**NOMINAL_KWARGS, parameter: value}
    violations = run_preflight({}, None, **kwargs)
    assert violations
    assert violations[0].scope == parameter


def test_bad_parameters_report_exact_details():
    violations = run_preflight(
        {},
        None,
        input_slcs_files=[NOMINAL_SAFE_FILE],
        indices=-1,
        calibration_type="Wrong",
        deramp="yes",
    )
    details = {v.scope: v.detail for v in violations}
    assert details["calibration_type"] == (
        "value 'Wrong' is not one of the allowed values: ['BetaZero', 'GammaZero', 'SigmaZero']"
    )
    assert details["deramp"] == "value 'yes' has an invalid type: expected bool, got str"
    assert details["indices"] == "value -1 is not a non-negative integer"


def test_run_preflight_never_raises_for_data_problems(tmp_path):
    violations = run_preflight(
        {},
        {"ETAD_IW": str(tmp_path / "ETAD_IW")},
        input_slcs_files=[],
        indices=-1,
    )
    scopes = {v.scope for v in violations}
    assert "input_slcs_files" in scopes
    assert "indices" in scopes
    assert str(tmp_path / "ETAD_IW") in scopes


# ---------------------------------------------------------------------------
# Ingestion.preflight wiring
# ---------------------------------------------------------------------------


def test_preflight_classmethod_raises_valueerror_with_all_violations():
    kwargs = {**NOMINAL_KWARGS, "indices": [-1], "deramp": "yes"}
    with pytest.raises(PreflightError) as excinfo:
        Ingestion.preflight({}, None, **kwargs)

    assert isinstance(excinfo.value, ValueError)
    assert {v.scope for v in excinfo.value.violations} == {"indices", "deramp"}
    assert str(excinfo.value).startswith("Ingestion preflight failed with 2 violation(s):")


def test_preflight_error_message_format_is_exact():
    violations = [
        Violation(
            "calibration_type",
            "value 'Wrong' is not one of the allowed values: ['BetaZero', 'GammaZero', 'SigmaZero']",
        ),
        Violation("indices", "value -1 is not a non-negative integer"),
    ]
    error = PreflightError(violations)
    expected = (
        "Ingestion preflight failed with 2 violation(s):\n"
        "  - calibration_type: value 'Wrong' is not one of the allowed values: "
        "['BetaZero', 'GammaZero', 'SigmaZero']\n"
        "  - indices: value -1 is not a non-negative integer"
    )
    assert str(error) == expected


# ---------------------------------------------------------------------------
# ETAD product integrity checks
# ---------------------------------------------------------------------------


def _write_etad_product(
    path,
    *,
    missing_root_attr=None,
    missing_burst_attr=None,
    missing_burst_var=None,
    with_measurement=True,
    with_annotation=True,
):
    """Write a minimal, structurally valid ETAD product below `path`."""
    measurement_dir = path / "measurement"
    annotation_dir = path / "annotation"
    measurement_dir.mkdir(parents=True, exist_ok=True)
    annotation_dir.mkdir(parents=True, exist_ok=True)

    burst = xr.DataTree(name="B1")
    for attr in EtadStackValidator.REQUIRED_BURST_ATTRS:
        if attr != missing_burst_attr:
            burst.attrs[attr] = f"{attr}-value"
    for var in EtadStackValidator.REQUIRED_BURST_VARS:
        if var != missing_burst_var:
            burst[var] = xr.DataArray(np.zeros(2, dtype=np.float64), dims="time")
    swath = xr.DataTree(name="IW1")
    swath["B1"] = burst
    root = xr.DataTree()
    for attr in EtadStackValidator.REQUIRED_ROOT_ATTRS:
        if attr != missing_root_attr:
            root.attrs[attr] = "2023-07-27T08:41:25"
    root["IW1"] = swath

    if with_measurement:
        root.to_netcdf(str(measurement_dir / "ETAD.nc"), engine="h5netcdf")
    if with_annotation:
        (annotation_dir / "ETAD_EOF.EOF").write_text("annotation payload\n", encoding="utf-8")
    return path


def test_etad_valid_product_passes(tmp_path):
    product = _write_etad_product(tmp_path / "ETAD_IW")
    violations = []
    EtadStackValidator().validate(str(product), violations)
    assert violations == []


def test_etad_missing_directory_reports_violation(tmp_path):
    missing = tmp_path / "ETAD_IW"
    violations = []
    EtadStackValidator().validate(str(missing), violations)
    assert len(violations) == 1
    assert violations[0].scope == str(missing)
    assert violations[0].detail == f"ETAD product directory does not exist: {missing}"


@pytest.mark.parametrize(
    ("product_kwargs", "detail"),
    [
        ({"with_annotation": False}, "no annotation/*.EOF file found"),
        ({"with_measurement": False}, "no measurement/*.nc file found"),
        ({"missing_root_attr": "rangeTimeMin"}, "root missing attribute 'rangeTimeMin'"),
        ({"missing_burst_attr": "burstId"}, "burst 'B1' missing attribute 'burstId'"),
        ({"missing_burst_var": "sumOfCorrectionsAz"}, "burst 'B1' missing variable 'sumOfCorrectionsAz'"),
    ],
)
def test_etad_corruptions_are_detected(tmp_path, product_kwargs, detail):
    product = _write_etad_product(tmp_path / "ETAD_IW", **product_kwargs)
    violations = []
    EtadStackValidator().validate(str(product), violations)
    assert detail in [v.detail for v in violations]


def test_run_preflight_checks_etad_adf(tmp_path):
    product = _write_etad_product(tmp_path / "ETAD_IW", with_annotation=False)
    violations = run_preflight({}, {"ETAD_IW": str(product)}, **NOMINAL_KWARGS)
    assert len(violations) == 1
    assert violations[0].scope == str(product)
    assert violations[0].detail == "no annotation/*.EOF file found"


# ---------------------------------------------------------------------------
# SAFE archive XML checks
# ---------------------------------------------------------------------------

PRODUCT_XSD = """<?xml version="1.0" encoding="UTF-8"?>
<xsd:schema xmlns:xsd="http://www.w3.org/2001/XMLSchema">
  <xsd:element name="product" type="productType"/>
  <xsd:complexType name="productType">
    <xsd:sequence>
      <xsd:element name="adsHeader" type="xsd:string"/>
      <xsd:element name="s1SARPayload" type="xsd:string"/>
    </xsd:sequence>
  </xsd:complexType>
</xsd:schema>
"""

VALID_PRODUCT_XML = """<?xml version="1.0" encoding="UTF-8"?>
<product>
  <adsHeader></adsHeader>
  <s1SARPayload></s1SARPayload>
</product>
"""

# Product document with the required `<s1SARPayload>` child removed, mirroring
# the MOD-SLC artifact used by TC-ST-RAMS-001.
MOD_PRODUCT_XML = """<?xml version="1.0" encoding="UTF-8"?>
<product>
  <adsHeader></adsHeader>
</product>
"""

EXTRA_ANNOTATION = """<?xml version="1.0" encoding="UTF-8"?>
<annotation>
  <ok>true</ok>
</annotation>
"""

# The real SAFE manifest is a CCSDS XFDU document; no bundled schema covers
# it, so the preflight only checks it for well-formedness.
VALID_MANIFEST = """<?xml version="1.0" encoding="UTF-8"?>
<xfdu:XFDU xmlns:xfdu="urn:ccsds:schema:xfdu:1">
  <xfdu:informationPackageMap/>
  <xfdu:metadataSection/>
  <xfdu:dataObjectSection/>
</xfdu:XFDU>
"""

MALFORMED_MANIFEST = """<xfdu:XFDU xmlns:xfdu="urn:ccsds:schema:xfdu:1">
  <xfdu:informationPackageMap>
</xfdu:XFDU>
"""


def _write_safe_product(safe_dir, manifest=VALID_MANIFEST, product=VALID_PRODUCT_XML, with_support=True):
    """Write a minimal SAFE archive layout under `safe_dir`.

    Mirrors the layout of a real Sentinel-1 SLC SAFE: a ``manifest.safe``
    root file, product annotation XML under ``annotation/`` and the bundled
    product schema under ``support/``.
    """
    safe_dir.mkdir(parents=True, exist_ok=True)
    annotation_dir = safe_dir / "annotation"
    annotation_dir.mkdir(parents=True, exist_ok=True)
    (safe_dir / "manifest.safe").write_text(manifest, encoding="utf-8")
    (annotation_dir / "s1a-product-001.xml").write_text(product, encoding="utf-8")
    (annotation_dir / "extra.xml").write_text(EXTRA_ANNOTATION, encoding="utf-8")
    if with_support:
        support_dir = safe_dir / "support"
        support_dir.mkdir(parents=True)
        (support_dir / "s1-product.xsd").write_text(PRODUCT_XSD, encoding="utf-8")
    return safe_dir


def test_safe_xml_valid_product_passes(tmp_path):
    safe_dir = _write_safe_product(tmp_path / SAFE_PRODUCT)
    violations = run_preflight({}, None, input_slcs_files=[str(safe_dir)], schema_validation=True)
    assert violations == []


def test_safe_xml_bundled_schema_violation(tmp_path):
    safe_dir = _write_safe_product(tmp_path / SAFE_PRODUCT, product=MOD_PRODUCT_XML)

    assert run_preflight({}, None, input_slcs_files=[str(safe_dir)]) == []
    assert run_preflight({}, None, input_slcs_files=[str(safe_dir)], schema_validation=False) == []

    violations = run_preflight({}, None, input_slcs_files=[str(safe_dir)], schema_validation=True)
    assert len(violations) == 1
    assert violations[0].scope.endswith("annotation/s1a-product-001.xml")
    assert violations[0].detail.startswith("XML validation against s1-product.xsd failed: ")
    assert "s1SARPayload" in violations[0].detail


def test_safe_xml_no_support_dir_only_checks_well_formedness(tmp_path):
    # Without a support/ directory no schema is available: the MOD product is
    # still well-formed, so no violation is reported.
    safe_dir = _write_safe_product(tmp_path / SAFE_PRODUCT, product=MOD_PRODUCT_XML, with_support=False)
    violations = run_preflight({}, None, input_slcs_files=[str(safe_dir)], schema_validation=True)
    assert violations == []


def test_safe_xml_malformed_file_reports_violation(tmp_path):
    safe_dir = _write_safe_product(tmp_path / SAFE_PRODUCT, manifest=MALFORMED_MANIFEST)
    violations = run_preflight({}, None, input_slcs_files=[str(safe_dir)], schema_validation=True)
    assert len(violations) == 1
    assert violations[0].scope.endswith("manifest.safe")
    assert violations[0].detail.startswith("malformed XML: ")


def test_safe_xml_missing_directory_reports_violation(tmp_path):
    missing = tmp_path / SAFE_PRODUCT
    violations = []
    SafeXmlValidator().validate(str(missing), violations)
    assert len(violations) == 1
    assert violations[0].scope == str(missing)
    assert violations[0].detail == f"SAFE product directory does not exist: {missing}"
