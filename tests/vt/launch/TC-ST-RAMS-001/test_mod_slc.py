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
"""TC-ST-RAMS-001 launch test: Ingestion preflight (Layer 1c) on a broken SAFE archive.

The payload points ``input_slcs_files`` at a MOD-SLC SAFE archive in which the
first SLC product annotation XML has had its required ``<swathTiming>``
element removed. The Layer-1 preflight validates every XML document against
the XSD schemas bundled in the archive's own ``support`` directory, so
execution reaches ``Ingestion.run`` and the preflight must raise a single
``PreflightError`` reporting the schema violation against
``s1-level-1-product.xsd``.

To build the MOD-SLC artifact on the facility (base: /data/SENTINEL1/SURAT):

    cp -r SAFE/S1A_IW_SLC__1SDV_20230727T084125_20230727T084152_049606_05F712_E111.SAFE \
        SAFE/S1A_IW_SLC__1SDV_20230727T084125_20230727T084152_049606_05F712_E111_MODSLC.SAFE
    python3 - <<'PY'
from lxml import etree
path = (
    "/data/SENTINEL1/SURAT/SAFE/"
    "S1A_IW_SLC__1SDV_20230727T084125_20230727T084152_049606_05F712_E111_MODSLC.SAFE"
    "/annotation/s1a-iw1-slc-vh-20230727t084127-20230727t084152-049606-05f712-001.xml"
)
tree = etree.parse(path)
for el in [c for c in tree.getroot() if c.tag.split("}")[-1] == "swathTiming"]:
    tree.getroot().remove(el)
tree.write(path, xml_declaration=True, encoding="UTF-8")
PY
"""

import logging
import traceback
from pathlib import Path

import pytest
import yaml

from scripts.run_payload import run_payload

FACILITY_ROOT = Path("/data/SENTINEL1/SURAT")

# PLACEHOLDER — confirm/create on facility (base: /data/SENTINEL1/SURAT)
# MOD-SLC: real SAFE copy with the required <swathTiming> element removed from
# the first product annotation XML (see module docstring for the recipe).
MOD_SLC_PATH = "/data/SENTINEL1/SURAT/SAFE/S1A_IW_SLC__1SDV_20230727T084125_20230727T084152_049606_05F712_E111_MODSLC.SAFE"

YAML_PATH = Path(__file__).parents[2] / "triggering" / "TC-ST-RAMS-001" / "mod_slc.yaml"


@pytest.mark.need_files
def test_mod_slc(capsys: pytest.CaptureFixture, caplog: pytest.LogCaptureFixture):
    """Run the MOD-SLC payload and assert the preflight reports the schema violation."""
    caplog.set_level(logging.INFO)

    if not FACILITY_ROOT.exists():
        pytest.skip("facility data root not available")
    if not Path(MOD_SLC_PATH).exists():
        pytest.skip(f"MOD-SLC artifact not available: {MOD_SLC_PATH}")

    with open(YAML_PATH, encoding="utf-8") as f:
        payload = yaml.safe_load(f)

    # Sanity: the MOD content must have survived the YAML round-trip.
    params = payload["workflow"][0]["parameters"]
    assert payload["general_configuration"]["triggering__validate_run"] is True
    assert params["input_slcs_files"] == [MOD_SLC_PATH]
    assert params["schema_validation"] is True

    # The preflight must abort the run before any processing happens.
    with pytest.raises(Exception) as excinfo:
        run_payload(payload, dry_run=False)

    exc = excinfo.value
    haystack = str(exc) + "".join(traceback.format_exception(exc)) + capsys.readouterr().out + caplog.text

    # The Ingestion unit must have actually started before being rejected.
    assert "Started : Ingestion Unit" in haystack

    # Layer-1 preflight raised pointing at the MOD-SLC product XML, the bundled
    # schema that rejected it and the missing element.
    assert "Ingestion preflight failed with" in haystack
    assert MOD_SLC_PATH in haystack
    assert "s1-level-1-product.xsd" in haystack
    assert "swathTiming" in haystack
