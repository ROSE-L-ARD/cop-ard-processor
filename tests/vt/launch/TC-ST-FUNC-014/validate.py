import os

import pytest
import xarray as xr

BASE_PATH = "/data/TC-ST-FUNC-014"
ALL_POLARIZATIONS = {"VV", "VH"}

TEST_CASES = [
    {
        "id": "app_v1",
        "zarr_path": f"{BASE_PATH}/outputs/app_v1/CSLC/cslcs_2023-08-08.zarr",
        "expected_pols": {"VV", "VH"},
        "expected_chunks": {"azimuth_time": 1000, "slant_range_time": 1000},
        "log_path": f"{BASE_PATH}/logs/app_v1.log",
    },
    {
        "id": "app_v2",
        "zarr_path": f"{BASE_PATH}/outputs/app_v2/CSLC/cslcs_2023-08-08.zarr",
        "expected_pols": {"VV"},
        "expected_chunks": {"azimuth_time": 2000, "slant_range_time": 2000},
        "log_path": f"{BASE_PATH}/logs/app_v2.log",
    },
    {
        "id": "app_v3",
        "zarr_path": f"{BASE_PATH}/outputs/app_v3/CSLC/cslcs_2023-08-08.zarr",
        "expected_pols": {"VH"},
        "expected_chunks": {"azimuth_time": 3000, "slant_range_time": 3000},
        "log_path": f"{BASE_PATH}/logs/app_v3.log",
    },
]


@pytest.mark.parametrize("cfg", TEST_CASES, ids=lambda c: c["id"])
def test_zarr_output_and_chunking(cfg):
    """Verify that Zarr stores exist, contain exact polarizations, and match hacked chunking defaults."""
    assert os.path.exists(cfg["zarr_path"]), f"Output Zarr missing: {cfg['zarr_path']}"

    dt = xr.open_datatree(cfg["zarr_path"], engine="zarr", chunks={})

    for _, v in dt.items():
        measurements = getattr(v, "measurements", None)
        ds = measurements.to_dataset() if measurements is not None else v.to_dataset()

        # 1. Positive check: expected polarizations must exist
        for pol in cfg["expected_pols"]:
            assert any([pol.lower() in item.lower() for item in ds.polarization.values]), (
                f"Expected polarization '{pol}' missing in {cfg['id']}"
            )

        # 2. Negative check: excluded polarizations must NOT be present
        excluded_pols = ALL_POLARIZATIONS - cfg["expected_pols"]
        for pol in excluded_pols:
            assert not any([pol.lower() in item.lower() for item in ds.polarization.values]), (
                f"Excluded polarization '{pol}' unexpectedly found in {cfg['id']}"
            )

        # 3. Verify chunking matches installation-specific defaults
        sample_var = list(ds.data_vars.keys())[0]
        encoding_chunks = ds[sample_var].encoding.get("chunks", ())
        dim_names = ds[sample_var].dims
        chunk_map = dict(zip(dim_names, encoding_chunks))

        print(chunk_map)
        for dim, expected_size in cfg["expected_chunks"].items():
            if dim in chunk_map:
                print(chunk_map[dim], expected_size)
                if expected_size < ds[dim].size:
                    assert chunk_map[dim] == expected_size, (
                        f"Chunk mismatch in {cfg['id']} for '{dim}': expected {expected_size}, got {chunk_map[dim]}"
                    )


@pytest.mark.parametrize("cfg", TEST_CASES, ids=lambda c: f"log_{c['id']}")
def test_execution_logs(cfg):
    """Ensure concurrent runs experienced no file-locking issues or unhandled exceptions."""
    assert os.path.exists(cfg["log_path"]), f"Log file missing: {cfg['log_path']}"

    with open(cfg["log_path"], "r") as f:
        log_text = f.read()

    assert "PermissionError" not in log_text, f"Permission error detected in {cfg['id']}"
    assert "FileExistsError" not in log_text, f"File-locking collision detected in {cfg['id']}"
    assert "CRITICAL" not in log_text and "ERROR" not in log_text, f"Errors present in {cfg['id']} log"
