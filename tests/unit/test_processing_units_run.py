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
"""Unit tests for the execution (run) methods of the COP ARD processing units.

These tests use unittest.mock to isolate the EOPF processing unit logic from the
underlying algorithmic implementations in cop_ard_app. This ensures we test the
configuration parsing, parameter resolution, and metadata propagation without
requiring actual SAFE archives or triggering massive Dask graphs.
"""

from unittest.mock import MagicMock, patch

import pytest
import xarray as xr
from eopf.computing.types import AuxiliaryDataFile

from cop_ard_processor.processing_units import (
    CMLP,
    Coregistration,
    Geocoding,
    Ingestion,
    ReferenceDEM,
    ReferenceGeometry,
    ReferenceUpdate,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Common Fixtures & Mocks
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_config():
    """Returns a fake configuration dict covering all units."""
    return {
        "Global": {
            "recompute": False,
            "chunks_radar_grid": {"y": 100, "x": 100},
            "chunks_geo_grid": {"y": 50, "x": 50},
            "facility": "Test Facility",
        },
        "Ingestion": {
            "calibration_type": "sigma0",
            "indices": [0],
            "polarization": "VV",
            "deramp": False,
            "noise_removal": True,
            "apply_invalid_mask": True,
        },
        "ReferenceDEM": {
            "interpolation": "bilinear",
            "spacing": [10.0, 10.0],
        },
        "Coregistration": {
            "initialize_current_reference": False,
            "enable_image_matching": False,
            "enable_split_spectrum": False,
            "enable_esd": False,
            "do_flattening": True,
            "do_reramp": True,
            "warp_ov_fact_low_acc": 2,
            "warp_kernel_shape_low_acc": [11, 11],
            "warp_kaiser_beta_low_acc": 5.0,
            "warp_ov_fact_high_acc": 4,
            "warp_kernel_shape_high_acc": [21, 21],
            "warp_kaiser_beta_high_acc": 5.0,
            "image_matching_degree": 2,
            "coherence_threshold_image_matching": 0.3,
            "coherence_threshold_split_spectrum": 0.4,
            "coherence_threshold_esd": 0.5,
            "matching_kernel_shape": [7, 7],
            "wsize": [64, 64],
            "wstep": [32, 32],
            "wsearch": [16, 16],
            "dim": 2,
            "block_size": 1024,
            "bandwidth": 0.3,
            "frequency_offset": 0.1,
            "multilook_split_spectrum": [5, 1],
            "multilook_esd": [5, 1],
            "esd_poly_degree": 2,
            "split_spectrum_poly_degree": 2,
            "height_ionosphere": 300000,
        },
        "ReferenceUpdate": {
            "posting": [20, 20],
            "memory_param": 0.9,
            "coherence_threshold": 0.6,
            "stable_interval_days": 30,
            "max_past_stable_history": 5,
        },
        "Geocoding": {
            "warp_ov_fact": 2,
            "warp_kernel_shape": [11, 11],
            "warp_kaiser_beta": 5.0,
            "spacing": [10.0, 10.0],
        },
    }


@pytest.fixture
def mock_slcs_tree():
    """Creates a dummy DataTree with required metadata for downstream units."""
    tree = xr.DataTree()
    tree.attrs["params"] = [{"execution_parameters": {"mode": "IW", "reference_id": "REF_123"}}]
    return tree


@pytest.fixture
def adfs_mapping():
    """Creates a basic mapping of ADF paths."""
    return {
        "REFDB": AuxiliaryDataFile("REFDB", "/dummy/refdb"),
        "DEM": AuxiliaryDataFile("DEM", "/dummy/dem"),
    }


# ---------------------------------------------------------------------------
# Unit Tests for `run` Methods
# ---------------------------------------------------------------------------


@patch("cop_ard_processor.processing_units.run_preflight", return_value=[])
@patch("cop_ard_processor.processing_units.get_config")
@patch("cop_ard_processor.processing_units.get_instrument_mode", return_value="IW")
@patch("cop_ard_processor.processing_units.import_slc_stack")
@patch("cop_ard_processor.processing_units.extract_acquisition_id", return_value=["REF_123"])
@patch("cop_ard_processor.processing_units.extract_acquisition_dates", return_value=["2023-07-27"])
@patch("cop_ard_processor.processing_units.finalize_import")
@patch("cop_ard_processor.processing_units.get_wdir_dynamic", return_value=("/tmp/dyn", None))
@patch("cop_ard_processor.processing_units.get_wdir_static", return_value=("/tmp/stat", "/tmp/dyn", None))
def test_ingestion_run(
    mock_wdir_static,
    mock_wdir_dyn,
    mock_finalize,
    mock_extract_dates,
    mock_extract_ids,
    mock_import_slc,
    mock_get_mode,
    mock_get_config,
    mock_preflight,
    mock_config,
):
    """Test nominal execution of Ingestion unit run method."""
    mock_get_config.return_value = mock_config

    dummy_tree = xr.DataTree()
    dummy_tree["bursts"] = xr.DataTree()
    mock_import_slc.return_value = dummy_tree
    mock_finalize.return_value = (dummy_tree, "REF_123")

    unit = Ingestion()
    adfs = {"REFDB": AuxiliaryDataFile("REFDB", "/dummy/refdb")}
    kwargs = {
        "input_slcs_files": ["/dummy/S1A_IW_SLC__1SDV_20230727T084125.SAFE"],
        "reference_id": "REF_123",
    }

    result = unit.run(inputs={}, adfs=adfs, **kwargs)

    assert "slcs" in result
    assert isinstance(result["slcs"], xr.DataTree)

    params_history = result["slcs"].attrs.get("params", [])
    assert len(params_history) == 1
    assert params_history[0]["processor"] == "COP ARD Ingestion Unit"
    assert params_history[0]["execution_parameters"]["mode"] == "IW"

    mock_import_slc.assert_called_once()
    mock_finalize.assert_called_once()


@patch("cop_ard_processor.processing_units.run_preflight", return_value=[])
@patch("cop_ard_processor.processing_units.get_config")
@patch("cop_ard_processor.processing_units.import_dem")
@patch("cop_ard_processor.processing_units.stack_dem")
@patch("cop_ard_processor.processing_units.get_bbox", return_value=(0, 0, 10, 10))
@patch("cop_ard_processor.processing_units.get_wdir_dynamic", return_value=("/tmp/dyn", None))
@patch("cop_ard_processor.processing_units.get_wdir_static", return_value=("/tmp/stat", "/tmp/dyn", None))
def test_reference_dem_run(
    mock_wdir_static,
    mock_wdir_dyn,
    mock_get_bbox,
    mock_stack_dem,
    mock_import_dem,
    mock_get_config,
    mock_preflight,
    mock_config,
    mock_slcs_tree,
    adfs_mapping,
):
    """Test nominal execution of ReferenceDEM unit run method."""
    mock_get_config.return_value = mock_config
    mock_import_dem.return_value = MagicMock()
    mock_stack_dem.return_value = xr.DataTree()

    unit = ReferenceDEM()
    inputs = {"slcs": mock_slcs_tree}

    result = unit.run(inputs=inputs, adfs=adfs_mapping)

    assert "dem" in result
    mock_import_dem.assert_called_once()
    mock_stack_dem.assert_called_once()

    history = result["dem"].attrs.get("params", [])
    assert len(history) == 2


@patch("cop_ard_processor.processing_units.run_preflight", return_value=[])
@patch("cop_ard_processor.processing_units.get_config")
@patch("cop_ard_processor.processing_units.extract_orbits")
@patch("cop_ard_processor.processing_units.stack_simulation")
@patch("cop_ard_processor.processing_units.stack_simulation_ml")
@patch("cop_ard_processor.processing_units.stack_geometry")
@patch("cop_ard_processor.processing_units.stack_mask")
@patch("cop_ard_processor.processing_units.get_wdir_dynamic", return_value=("/tmp/dyn", None))
@patch("cop_ard_processor.processing_units.get_wdir_static", return_value=("/tmp/stat", "/tmp/dyn", None))
def test_reference_geometry_run(
    mock_wdir_static,
    mock_wdir_dyn,
    mock_stack_mask,
    mock_stack_geo,
    mock_stack_sim_ml,
    mock_stack_sim,
    mock_extract_orbits,
    mock_get_config,
    mock_preflight,
    mock_config,
    mock_slcs_tree,
    adfs_mapping,
):
    """Test nominal execution of ReferenceGeometry unit run method."""
    mock_get_config.return_value = mock_config
    mock_stack_sim.return_value = xr.DataTree()
    mock_stack_sim_ml.return_value = xr.DataTree()

    unit = ReferenceGeometry()

    dem_tree = xr.DataTree()
    dem_tree.attrs["params"] = list(mock_slcs_tree.attrs["params"])
    inputs = {"slcs": mock_slcs_tree, "dem": dem_tree}

    result = unit.run(inputs=inputs, adfs=adfs_mapping)

    assert "simulation_ref" in result
    assert "simulation_ml" in result

    mock_stack_sim.assert_called_once()
    mock_stack_geo.assert_called_once()
    mock_stack_mask.assert_called_once()


@patch("cop_ard_processor.processing_units.run_preflight", return_value=[])
@patch("cop_ard_processor.processing_units.get_config")
@patch("cop_ard_processor.processing_units.extract_orbits")
@patch("cop_ard_processor.processing_units.stack_coregistration")
@patch("cop_ard_processor.processing_units.update_coreg")
@patch("cop_ard_processor.processing_units.get_wdir_dynamic", return_value=("/tmp/dyn", None))
@patch("cop_ard_processor.processing_units.get_wdir_static", return_value=("/tmp/stat", "/tmp/dyn", None))
def test_coregistration_run_base(
    mock_wdir_static,
    mock_wdir_dyn,
    mock_update_coreg,
    mock_stack_coreg,
    mock_extract_orbits,
    mock_get_config,
    mock_preflight,
    mock_config,
    mock_slcs_tree,
    adfs_mapping,
):
    """Test Coregistration unit bypassing secondary simulation and refinements."""
    mock_get_config.return_value = mock_config

    mock_orbits = MagicMock()
    mock_orbits.where.return_value.size = 0
    mock_extract_orbits.return_value = mock_orbits

    mock_stack_coreg.return_value = xr.DataTree()
    mock_update_coreg.return_value = (xr.DataTree(), xr.DataTree())

    unit = Coregistration()

    sim_tree = xr.DataTree()
    sim_tree.attrs["params"] = list(mock_slcs_tree.attrs["params"])

    inputs = {
        "slcs": mock_slcs_tree,
        "dem": xr.DataTree(),
        "simulation_ref": sim_tree,
        "simulation_ml": xr.DataTree(),
    }

    result = unit.run(inputs=inputs, adfs=adfs_mapping)

    assert "cslcs" in result
    assert "slcs" in result
    mock_stack_coreg.assert_called_once()
    mock_update_coreg.assert_called_once()


@patch("cop_ard_processor.processing_units.run_preflight", return_value=[])
@patch("cop_ard_processor.processing_units.get_config")
@patch("cop_ard_processor.processing_units.extract_orbits")
@patch("cop_ard_processor.processing_units.stack_coregistration")
@patch("cop_ard_processor.processing_units.finalize_coreg")
@patch("cop_ard_processor.processing_units.stack_disparity")
@patch("cop_ard_processor.processing_units.estimate_poly_matching", return_value=(MagicMock(), MagicMock()))
@patch("cop_ard_processor.processing_units.split_spectrum")
@patch("cop_ard_processor.processing_units.stack_esd")
@patch("cop_ard_processor.processing_units.update_coreg")
@patch("cop_ard_processor.processing_units.get_wdir_dynamic", return_value=("/tmp/dyn", None))
@patch("cop_ard_processor.processing_units.get_wdir_static", return_value=("/tmp/stat", "/tmp/dyn", None))
def test_coregistration_run_full(
    mock_wdir_static,
    mock_wdir_dyn,
    mock_update_coreg,
    mock_stack_esd,
    mock_split_spectrum,
    mock_estimate_poly,
    mock_stack_disparity,
    mock_finalize_coreg,
    mock_stack_coreg,
    mock_extract_orbits,
    mock_get_config,
    mock_preflight,
    mock_config,
    mock_slcs_tree,
    adfs_mapping,
):
    """Test Coregistration unit with image matching, split spectrum, and ESD enabled."""
    mock_get_config.return_value = mock_config

    mock_orbits = MagicMock()
    mock_orbits.where.return_value.size = 0
    mock_extract_orbits.return_value = mock_orbits

    mock_tree = xr.DataTree()
    mock_stack_coreg.return_value = mock_tree
    mock_finalize_coreg.return_value = mock_tree
    mock_split_spectrum.return_value = mock_tree
    mock_stack_esd.return_value = mock_tree
    mock_update_coreg.return_value = (mock_tree, mock_tree)

    unit = Coregistration()

    sim_tree = xr.DataTree()
    sim_tree.attrs["params"] = list(mock_slcs_tree.attrs["params"])

    inputs = {
        "slcs": mock_slcs_tree,
        "dem": xr.DataTree(),
        "simulation_ref": sim_tree,
        "simulation_ml": xr.DataTree(),
    }

    kwargs = {
        "enable_image_matching": True,
        "enable_split_spectrum": True,
        "enable_esd": True,
        "initialize_current_reference": False,
    }

    result = unit.run(inputs=inputs, adfs=adfs_mapping, **kwargs)

    assert "cslcs" in result
    assert "slcs" in result
    mock_finalize_coreg.assert_called()
    mock_stack_disparity.assert_called_once()
    mock_split_spectrum.assert_called_once()
    mock_stack_esd.assert_called_once()


@patch("cop_ard_processor.processing_units.run_preflight", return_value=[])
@patch("cop_ard_processor.processing_units.get_config")
@patch("cop_ard_processor.processing_units.stack_cmlp")
@patch("cop_ard_processor.processing_units.get_wdir_dynamic", return_value=("/tmp/dyn", None))
@patch("cop_ard_processor.processing_units.get_wdir_static", return_value=("/tmp/stat", "/tmp/dyn", None))
def test_reference_update_run(
    mock_wdir_static,
    mock_wdir_dyn,
    mock_stack_cmlp,
    mock_get_config,
    mock_preflight,
    mock_config,
    mock_slcs_tree,
    adfs_mapping,
):
    """Test ReferenceUpdate unit execution."""
    mock_get_config.return_value = mock_config
    dummy_ref = xr.DataTree()
    dummy_stable = xr.DataTree()
    mock_stack_cmlp.return_value = (None, dummy_ref, dummy_stable)

    unit = ReferenceUpdate()
    result = unit.run(inputs={"slcs": mock_slcs_tree}, adfs=adfs_mapping)

    assert "current_reference" in result
    assert "stable_references" in result
    mock_stack_cmlp.assert_called_once()


@patch("cop_ard_processor.processing_units.run_preflight", return_value=[])
@patch("cop_ard_processor.processing_units.get_config")
@patch("cop_ard_processor.processing_units.stack_cmlp")
@patch("cop_ard_processor.processing_units.get_wdir_dynamic", return_value=("/tmp/dyn", None))
@patch("cop_ard_processor.processing_units.get_wdir_static", return_value=("/tmp/stat", "/tmp/dyn", None))
def test_cmlp_run(
    mock_wdir_static,
    mock_wdir_dyn,
    mock_stack_cmlp,
    mock_get_config,
    mock_preflight,
    mock_config,
    mock_slcs_tree,
    adfs_mapping,
):
    """Test CMLP unit execution."""
    mock_get_config.return_value = mock_config
    mock_stack_cmlp.return_value = (xr.DataTree(), xr.Dataset(), xr.Dataset())

    unit = CMLP()
    result = unit.run(inputs={"slcs": mock_slcs_tree}, adfs=adfs_mapping)

    assert "cmlp" in result
    assert "current_reference" in result
    assert "stable_references" in result
    mock_stack_cmlp.assert_called_once()


@patch("cop_ard_processor.processing_units.run_preflight", return_value=[])
@patch("cop_ard_processor.processing_units.get_config")
@patch("cop_ard_processor.processing_units.get_simulation_trees")
@patch("cop_ard_processor.processing_units.stack_gmlp")
@patch("cop_ard_processor.processing_units.update_gmlp")
@patch("cop_ard_processor.processing_units.get_wdir_dynamic", return_value=("/tmp/dyn", None))
@patch("cop_ard_processor.processing_units.get_wdir_static", return_value=("/tmp/stat", "/tmp/dyn", None))
def test_geocoding_run(
    mock_wdir_static,
    mock_wdir_dyn,
    mock_update_gmlp,
    mock_stack_gmlp,
    mock_get_sim_trees,
    mock_get_config,
    mock_preflight,
    mock_config,
    mock_slcs_tree,
    adfs_mapping,
):
    """Test Geocoding unit execution."""
    mock_get_config.return_value = mock_config
    mock_get_sim_trees.return_value = (xr.DataTree(), xr.DataTree())
    mock_stack_gmlp.return_value = xr.DataTree()
    mock_update_gmlp.return_value = xr.DataTree()

    unit = Geocoding()

    cmlp_tree = xr.DataTree()
    cmlp_tree.attrs["params"] = list(mock_slcs_tree.attrs["params"])

    inputs = {
        "cmlp": cmlp_tree,
        "slcs": mock_slcs_tree,
        "simulation_ref": xr.DataTree(),
        "simulation_ml": xr.DataTree(),
    }

    result = unit.run(inputs=inputs, adfs=adfs_mapping)

    assert "gmlp" in result
    mock_stack_gmlp.assert_called_once()
    mock_update_gmlp.assert_called_once()
