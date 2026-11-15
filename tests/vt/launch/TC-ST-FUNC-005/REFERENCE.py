# Copyright 2026 B-Open
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from pathlib import Path

import pytest
import rioxarray  # noqa: F401
import xarray as xr
import yaml
from cop_ard_core.logger import logger
from structlog.testing import capture_logs

from scripts.run_payload import run_payload

RP_BASE_PATH = Path(__file__).parents[2]
yaml_path = RP_BASE_PATH / "triggering" / "TC-ST-FUNC-005" / "REFERENCE.yaml"


def _get_zarr_dem_resolution(dem_path: Path) -> tuple[float, float]:
    """Get DEM resolution."""
    if not dem_path.exists():
        raise FileNotFoundError(f"Input DEM Zarr path not found: {dem_path}")

    ds = xr.open_zarr(dem_path)

    if not ds.data_vars:
        dt = xr.open_datatree(dem_path, engine="zarr")
        for node in dt.subtree:
            if node.ds is not None and node.ds.data_vars:
                ds = node.ds
                break

    x_res, y_res = ds.rio.resolution()
    return abs(float(x_res)), abs(float(y_res))


@pytest.mark.ard
def test_payload():
    """E2E Functional Test TC-ST-FUNC-005: Reference stack creation & DEM interpolation validation."""
    with open(yaml_path, encoding="utf-8") as f:
        payload = yaml.safe_load(f)

    adfs = {adf["id"]: Path(adf["path"]) for adf in payload["io"]["adfs"]}
    dem_path = adfs["DEM"]

    _, y_res = _get_zarr_dem_resolution(dem_path)
    expected_glo90_res = 3.0 / 3600.0  # GLO-90 = 3 arcsec (~90 m); GLO-30 = 1/3600 deg

    assert y_res == pytest.approx(expected_glo90_res, rel=1e-3), (
        f"Pass Criteria Failed: Input DEM Zarr at '{dem_path}' has latitude resolution {y_res:.8f} deg, "
        f"expected GLO-90 resolution ~{expected_glo90_res:.8f} deg (3/3600 deg)."
    )

    # 2. Execute reference stack creation with log capture
    with capture_logs() as captured:
        run_payload(payload, dry_run=False)

    captured_events = [str(log.get("event", "")) for log in captured]
    captured_logs_text = "\n".join(captured_events)

    # 2. Mandatory Processing Units Verification
    mandatory_units = [
        "Ingestion Unit",
        "ReferenceDEM Unit",
        "ReferenceGeometry Unit",
    ]
    for unit in mandatory_units:
        expected_trace = f"Started : {unit}"
        assert any(expected_trace in event for event in captured_events), (
            f"Pass Criteria Failed: Mandatory log trace '{expected_trace}' was not found in execution logs.\n"
            f"Captured events:\n{captured_logs_text}"
        )

    # 4. Verify 'dem_interpolation' parameter in execution parameters dictionary
    interpolation_found = False
    for log in captured:
        # Check structured key directly if available in log payload
        if "dem_interpolation" in log:
            val = str(log["dem_interpolation"]).lower()
            assert "linear" in val, f"Pass Criteria Failed: Expected 'dem_interpolation' to be 'linear', found '{val}'."
            interpolation_found = True
            break
        # Fallback check on string representation of event/log dictionary
        elif "dem_interpolation" in str(log):
            log_str = str(log).lower()
            assert "linear" in log_str, (
                f"Pass Criteria Failed: 'linear' interpolation was not found in log entry: {log}"
            )
            interpolation_found = True
            break

    assert interpolation_found, (
        f"Pass Criteria Failed: The 'dem_interpolation' parameter was not found in logged execution parameters.\n"
        f"Captured logs:\n{captured_logs_text}"
    )
