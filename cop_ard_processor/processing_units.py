import json
import os
from datetime import datetime, timezone

import xarray as xr
from cop_ard_app.area_projection import stack_geometry, stack_mask
from cop_ard_app.cmlp import stack_cmlp
from cop_ard_app.coreg import finalize_coreg, stack_coregistration
from cop_ard_app.dem import import_dem, stack_dem
from cop_ard_app.esd import stack_esd
from cop_ard_app.etad import import_ETAD
from cop_ard_app.gmlp import stack_gmlp
from cop_ard_app.io import Path
from cop_ard_app.matching import estimate_poly_matching, stack_disparity
from cop_ard_app.model import extract_acquisition_dates, extract_orbits, update_coreg, update_gmlp
from cop_ard_app.orbit import import_ORBIT
from cop_ard_app.simulation import stack_simulation, stack_simulation_ml
from cop_ard_app.slc import finalize_import, import_slc_stack
from cop_ard_app.split_spectrum import split_spectrum
from cop_ard_app.utils import build_coreg_poly_dict, get_bbox
from cop_ard_core.logger import logger
from eopf.computing import EOProcessingUnit

from cop_ard_processor.breakpoints import resumable_breakpoint
from cop_ard_processor.utils import (
    _get_slcs_path,
    _is_multiburst,
    _json_safe,
    get_adf,
    get_config,
    get_instrument_mode,
    get_wdir_dynamic,
    get_wdir_static,
    log_processing_parameters,
    log_time,
)


class Ingestion(EOProcessingUnit):
    """Ingestion processing unit for Sentinel-1 and ROSE-L Single Look Complex (SLC) products.

    This unit handles reading input SAFE/SLC data, applying radiometric calibration,
    noise removal, deramping, and precise orbital/ETAD timing corrections. It also
    populates EOPF-compliant `processing_history` metadata.
    """

    @classmethod
    def get_mandatory_adf_list(cls, mode=None, **kwargs):
        """Retrieve the list of mandatory Auxiliary Data Files (ADFs) required for ingestion."""
        return ["REFDB"]

    @log_time("Ingestion Unit", logger)
    @resumable_breakpoint(identifier="Ingestion")
    def run(self, inputs, adfs=None, **kwargs):
        """Execute the SLC ingestion workflow.

        Parameters
        ----------
        inputs : dict
            Dictionary of input products supplied by the workflow orchestrator.
        adfs : dict, optional
            Dictionary mapping ADF types (e.g., 'ETAD', 'ORB', 'REFDB') to their file paths.
        **kwargs : dict
            Execution overrides and configuration settings:
            - input_slcs_files (list of str): Paths to input SLC files.
            - enable_dask (bool): Enable Dask lazy evaluation graph (default: True).
            - recompute (bool): Force re-creation of reference database stack.
            - chunks (dict): Dask chunk dimensions for the radar grid.
            - calibration_type (str): Radiometric calibration target (e.g., 'sigma0').
            - indices (list or int): Sub-swath or burst selection indices.
            - polarization (list or str): Polarizations to process ('VV', 'VH', etc.).
            - deramp (bool): Flag to apply deramping for TOPSAR modes.
            - noise_removal (bool): Flag to enable thermal noise removal.
            - apply_invalid_mask (bool): Flag to mask invalid edge samples.
            - reference_id (str, optional): Target master acquisition identifier.

        Returns
        -------
        dict
            Dictionary containing the output dataset: `{"slcs": slcs}`.

        Raises
        ------
        ValueError
            If `input_slcs_files` is missing or if mandatory `reference_id` is missing
            for non-multiburst acquisitions when `recompute=False`.
        """
        input_slcs_files = kwargs.get("input_slcs_files")
        if input_slcs_files is None:
            raise ValueError("input_slcs_files must be provided via processing parameters (kwargs).")
        logger.debug(f"TEST LOG DEBUG")
        logger.info(f"TEST LOG INFO")
        # 1. Resolve instrument mode and baseline configuration
        mode = get_instrument_mode(input_slcs_files)
        config = get_config(mode)
        multiburst = mode in ["IW", "EW"]

        # 2. Extract execution parameters from kwargs and default configs
        enable_dask = kwargs.get("enable_dask", True)
        recompute = kwargs.get("recompute", config["Global"]["recompute"])
        chunks = kwargs.get("chunks", config["Global"]["chunks_radar_grid"])

        calibration_type = kwargs.get("calibration_type", config["Ingestion"]["calibration_type"])
        indices = kwargs.get("indices", config["Ingestion"]["indices"])
        polarization = kwargs.get("polarization", config["Ingestion"]["polarization"])
        deramp = kwargs.get("deramp", config["Ingestion"]["deramp"])
        noise_removal = kwargs.get("noise_removal", config["Ingestion"]["noise_removal"])
        apply_invalid_mask = kwargs.get("apply_invalid_mask", config["Ingestion"]["apply_invalid_mask"])
        reference_id = kwargs.get("reference_id")

        # Ensure indices parameter is formatted as a list
        if isinstance(indices, int):
            indices = [indices]

        # 3. Construct unit processing parameters dictionary
        params = {
            "mode": mode,
            "multiburst": multiburst,
            "enable_dask": enable_dask,
            "recompute": recompute,
            "reference_id": reference_id,
            "chunks_radar_grid": _json_safe(chunks),
            "polarization": _json_safe(polarization),
            "deramp": deramp,
            "noise_removal": noise_removal,
            "apply_invalid_mask": apply_invalid_mask,
            "measurement_type": calibration_type,
            "indices": _json_safe(indices),
        }

        log_processing_parameters(params, self, logger)

        # Resolve static and dynamic working directories
        working_directory_static, _, storage_options_static = get_wdir_static(adfs)
        working_directory_dynamic, storage_options_dynamic = get_wdir_dynamic()

        # Validate reference acquisition configuration
        if not multiburst and reference_id is None and not recompute:
            raise ValueError(
                f"Missing reference_id. For non-multiburst modes ({mode}), a reference_id "
                "must be explicitly provided unless you are initializing a new reference (recompute=True)."
            )

        slcs_path = _get_slcs_path(working_directory_dynamic)
        do_import = not recompute

        # 4. Import SLC stack and apply radiometric/geometric preprocessing
        slcs = import_slc_stack(
            input_slcs_files,
            slcs_path,
            deramp=deramp,
            multiburst=multiburst,
            noise_removal_flag=noise_removal,
            apply_invalid_mask_flag=apply_invalid_mask,
            polarizations=polarization,
            indices=indices,
            calib_type=calibration_type,
            chunks=chunks,
            storage_options=storage_options_dynamic,
            do_import=do_import,
            enable_dask=enable_dask,
        )

        acquisition_dates = extract_acquisition_dates(slcs, multiburst=multiburst)

        # 5. Apply external ETAD and precise orbit auxiliary data
        adfs_list = []

        if (adfs is not None) and not recompute:
            # Extract ADF paths for ETAD and ORBIT if present in the configuration
            etad_files = [get_adf(v) for k, v in adfs.items() if "ETAD" in k]
            orbit_files = [get_adf(v) for k, v in adfs.items() if "ORB" in k]

            # --- ETAD Processing Step ---
            if etad_files:
                logger.info("Executing ETAD import...")
                slcs, used_etads = import_ETAD(
                    slcs,
                    acquisition_dates,
                    etad_files,
                    wdir_path=working_directory_dynamic,
                    multiburst=multiburst,
                    storage_options=storage_options_dynamic,
                    enable_dask=enable_dask,
                )
                if used_etads:
                    adfs_list.append(used_etads)
            else:
                logger.info("Skipping ETAD step (ADF not provided).")

            # --- External Orbit Processing Step ---
            if orbit_files:
                logger.info("Executing ORBIT import...")
                slcs, used_orbits = import_ORBIT(
                    slcs,
                    acquisition_dates,
                    orbit_files,
                    wdir_path=working_directory_dynamic,
                    multiburst=multiburst,
                    storage_options=storage_options_dynamic,
                    enable_dask=enable_dask,
                )
                if used_orbits:
                    adfs_list.append(used_orbits)
            else:
                logger.info("Skipping external orbit step (ADF not provided).")
        else:
            logger.info("Skipping auxiliary data imports (recompute=True or no ADFs provided).")

        # 6. Finalize stack import and set reference acquisition ID
        slcs, reference_id = finalize_import(
            slcs=slcs,
            working_directory=working_directory_static,
            recompute=recompute,
            acquisition_id=reference_id,
            enable_dask=enable_dask,
        )

        params["reference_id"] = reference_id

        # 7. Register metadata record in EOPF processing history
        current_time = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        facility = kwargs.get("facility", config.get("Global", {}).get("facility", "ESA-Copernicus Ground Segment"))

        ingestion_step = {
            "processor": "COP ARD Ingestion Unit",
            "version": getattr(self, "version", "0.0.1"),
            "facility": facility,
            "time": current_time,
            "inputs": [str(f) for f in input_slcs_files]
            if isinstance(input_slcs_files, list)
            else str(input_slcs_files),
            "outputs": ["slcs"],
            "adfs": adfs_list,
            "execution_parameters": params,
        }

        # Safely append step metadata without overwriting existing Dataset attributes
        slcs.attrs["params"] = [ingestion_step]
        output = {"slcs": slcs}
        return output


class ReferenceDEM(EOProcessingUnit):
    """Reference Digital Elevation Model (DEM) processing unit.

    This unit handles importing, cropping to bounding box, and resampling the target DEM
    to match the spatial radar grid and coordinate system of the input SLC stack.
    """

    @classmethod
    def get_mandatory_input_list(cls, mode=None, **kwargs):
        """Retrieve the list of mandatory input data products required by this unit."""
        return ["slcs"]

    @classmethod
    def get_mandatory_adf_list(cls, mode=None, **kwargs):
        """Retrieve the list of mandatory Auxiliary Data Files (ADFs) required by this unit."""
        return ["REFDB", "DEM"]

    @log_time("ReferenceDEM Unit", logger)
    @resumable_breakpoint(identifier="ReferenceDEM")
    def run(self, inputs, adfs=None, **kwargs):
        """Execute the Reference DEM processing workflow.

        Parameters
        ----------
        inputs : dict
            Dictionary of input products supplied by the workflow orchestrator (must include `"slcs"`).
        adfs : dict, optional
            Dictionary mapping ADF types (`"REFDB"`, `"DEM"`) to their file paths or objects.
        **kwargs : dict
            Execution overrides and configuration settings:
            - recompute (bool): Force re-creation of reference DEM cache.
            - chunks_geo_grid (dict): Spatial chunk sizes for the geographic grid.
            - interpolation (str): Resampling method (e.g., 'bilinear', 'nearest').
            - spacing (tuple or list): Pixel spacing in geographic/projected units.
            - epsg (int or str, optional): Target EPSG projection code.
            - reference_id (str, optional): Reference acquisition identifier.

        Returns
        -------
        dict
            Dictionary containing the resampled DEM tree: `{"DEM": dem_tree}`.

        Raises
        ------
        KeyError
            If mandatory `"slcs"` dataset or mandatory ADFs are missing.
        """
        # 1. Extract base inputs and resolve instrument mode
        slcs = inputs["slcs"]
        all_pararams = slcs.attrs["params"]
        params = all_pararams[0]["execution_parameters"]
        multiburst = _is_multiburst(slcs)

        # Safely determine instrument mode (from kwargs, slcs, or fallback)
        config = get_config(params["mode"])

        # Retrieve DEM auxiliary data object
        dem_type = get_adf(adfs["DEM"])

        # 2. Extract execution parameters with fallback to global/unit config
        recompute = kwargs.get("recompute", config["Global"]["recompute"])
        reference_id = params["reference_id"]

        chunks_geo_grid = kwargs.get("chunks_geo_grid", config["Global"]["chunks_geo_grid"])
        # Filter chunks to keep spatial dimensions only ('y', 'x')
        chunks = {k: v for k, v in chunks_geo_grid.items() if k in ["y", "x"]}

        method = kwargs.get("interpolation", config["ReferenceDEM"]["interpolation"])
        spacing = kwargs.get("spacing", config["ReferenceDEM"]["spacing"])
        crs = kwargs.get("epsg")

        # 3. Setup static and dynamic working directories
        working_directory_static, _, storage_options_static = get_wdir_static(adfs)
        working_directory_dynamic, storage_options_dynamic = get_wdir_dynamic()

        # 4. Construct unit processing parameters dictionary
        execution_parameters = {
            "recompute": recompute,
            "dem_type": str(dem_type.path),
            "chunks_geo_grid": _json_safe(chunks),
            "dem_interpolation": method,
            "dem_spacing": _json_safe(spacing),
            "dem_crs": crs,
        }

        log_processing_parameters(execution_parameters, self, logger)

        # 5. Execute core DEM processing pipeline
        # Compute spatial bounding box of the SLC stack
        bbox = get_bbox(slcs)

        # Import DEM covering the target bounding box
        dem = import_dem(
            bbox=bbox,
            working_dir=working_directory_dynamic,
            dem_type=dem_type.path,
            chunks=chunks,
            storage_options_dem=dem_type.storage_options,
            storage_options=storage_options_dynamic,
        )

        # Resample and stack DEM according to grid CRS and pixel spacing
        dem_tree = stack_dem(
            dem,
            slcs,
            multiburst,
            spacing,
            working_directory_static,
            working_directory_dynamic,
            chunks,
            crs=crs,
            method=method,
            storage_options=storage_options_dynamic,
            storage_options_static=storage_options_static,
            recompute=recompute,
            reference_id=reference_id,
        )

        # 6. Register metadata record in EOPF processing history
        current_time = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        facility = kwargs.get("facility", config.get("Global", {}).get("facility", "ESA-Copernicus Ground Segment"))

        dem_step = {
            "processor": "COP ARD ReferenceDEM Unit",
            "version": getattr(self, "version", "0.0.1"),
            "facility": facility,
            "time": current_time,
            "inputs": ["slcs"],
            "outputs": ["dem"],
            "adfs": [str(v.path) for v in adfs.values()] if adfs else [],
            "execution_parameters": execution_parameters,
        }

        all_pararams.append(dem_step)
        dem_tree.attrs["params"] = all_pararams
        output = {"dem": dem_tree}
        return output


class ReferenceGeometry(EOProcessingUnit):
    """Reference Geometry processing unit for coregistration simulation.

    This unit simulates the master radar geometry using the reference acquisition orbit
    and the input DEM. It also pre-computes geometric layers (e.g., incidence angles)
    and masks (layover, shadow) to be stored in the static working directory.
    """

    @classmethod
    def get_mandatory_input_list(cls, mode=None, **kwargs):
        """Retrieve the list of mandatory input dataset keys required by this unit."""
        return ["slcs", "dem"]

    @classmethod
    def get_mandatory_adf_list(cls, mode=None, **kwargs):
        """Retrieve the list of mandatory Auxiliary Data Files (ADFs) required by this unit."""
        return ["REFDB"]

    @log_time("ReferenceGeometry Unit", logger)
    @resumable_breakpoint(identifier="ReferenceGeometry", expected_outputs=2)
    def run(self, inputs, adfs=None, **kwargs):
        """Generates the reference geometry

        Parameters
        ----------
        inputs : dict
            Input mapping containing:
            - 'slcs': Calibrated SLC stack (xr.DataTree).
            - 'dem': Resampled DEM (xr.DataTree).
        adfs : dict, optional
            Auxiliary Data Files mapping (must contain 'REFDB').
        **kwargs : dict
            Execution parameters (recompute, chunks_geo_grid, enable_dask, facility).

        Returns
        -------
        dict
            Mapping containing geometry simulations:
            - 'simulation_ref': Simulated master geometry (xr.DataTree).
            - 'simulation_ml': Multilooked simulated master geometry (xr.DataTree).

        Raises
        ------
        KeyError
            If mandatory inputs ('slcs', 'dem') are missing.
        """
        # 1. Extract base inputs and preserve exact metadata flow
        slcs = inputs["slcs"]
        dem_tree = inputs["dem"]
        all_params = dem_tree.attrs["params"]
        params = all_params[0]["execution_parameters"]
        # 2. Load configuration
        config = get_config(params["mode"])

        # 3. Extract execution parameters with explicit config/default fallbacks
        recompute = kwargs.get("recompute", config["Global"]["recompute"])
        enable_dask = kwargs.get("enable_dask", True)

        chunks_geo_grid = kwargs.get("chunks_geo_grid", config["Global"]["chunks_geo_grid"])
        chunks = {k: v for k, v in chunks_geo_grid.items() if k in ["y", "x"]}

        reference_id = params["reference_id"]

        # 4. Setup working directories
        working_directory_static, _, storage_options_static = get_wdir_static(adfs)
        working_directory_dynamic, storage_options_dynamic = get_wdir_dynamic()

        # Update internal params dictionary
        execution_parameters = {"enable_dask": enable_dask, "chunks_geo_grid": _json_safe(chunks)}
        log_processing_parameters(execution_parameters, self, logger)

        # 5. Core processing logic (identical to original pipeline calls)
        multiburst = "bursts" in slcs

        orbits = extract_orbits(
            slcs,
            multiburst=multiburst,
            acquisition_id=[reference_id],
        )

        simulation_tree = stack_simulation(
            dem_tree=dem_tree,
            orbits=orbits,
            multiburst=multiburst,
            working_directory=working_directory_static,
            chunks={**chunks, "acquisition_id": 1},
            master_image=True,
            recompute=recompute,
            enable_dask=enable_dask,
            reference_id=reference_id,
        )

        simulation_ml_tree = stack_simulation_ml(
            simulation_tree=simulation_tree,
            reference_id=reference_id,
            multiburst=multiburst,
            working_directory=working_directory_static,
            recompute=recompute,
            enable_dask=enable_dask,
        )

        stack_geometry(
            slcs,
            simulation_tree,
            simulation_ml_tree,
            working_directory_static=working_directory_static,
            working_directory_tmp=working_directory_dynamic,
            reference_id=reference_id,
            chunk_length=max([v for v in chunks.values()]),
            recompute=recompute,
            multiburst=multiburst,
            enable_dask=enable_dask,
        )

        stack_mask(
            slcs,
            simulation_tree,
            simulation_ml_tree,
            working_directory_static=working_directory_static,
            reference_id=reference_id,
            recompute=recompute,
            multiburst=multiburst,
            enable_dask=enable_dask,
        )

        # Preserve internal pipeline params attribute

        # 6. EOPF Processing History registration (using 'execution_parameters')
        current_time = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        facility = kwargs.get("facility", config.get("Global", {}).get("facility", "ESA-Copernicus Ground Segment"))

        geo_step = {
            "processor": "COP ARD ReferenceGeometry Unit",
            "version": getattr(self, "version", "0.0.1"),
            "facility": facility,
            "time": current_time,
            "inputs": ["slcs", "DEM"],
            "outputs": ["simulation_ref", "simulation_ml"],
            "adfs": [str(v.path) for v in adfs.values()] if adfs else [],
            "execution_parameters": execution_parameters,
        }

        all_params.append(geo_step)
        simulation_tree.attrs["params"] = all_params
        return {
            "simulation_ref": simulation_tree,
            "simulation_ml": simulation_ml_tree,
        }


class Coregistration(EOProcessingUnit):
    """Coregistration processing unit for aligning SLC stack to reference geometry.

    This unit performs geometrical warping, image matching refinement, split-spectrum
    ionospheric phase estimation, and Enhanced Spectral Diversity (ESD) corrections.
    """

    @classmethod
    def get_mandatory_input_list(cls, mode=None, **kwargs):
        """Retrieve the list of mandatory input datasets required by this unit."""
        return ["slcs", "dem", "simulation_ref", "simulation_ml"]

    @classmethod
    def get_mandatory_adf_list(cls, mode=None, **kwargs):
        """Retrieve the list of mandatory Auxiliary Data Files (ADFs) required by this unit."""
        return ["REFDB"]

    @log_time("Coregistration Unit", logger)
    @resumable_breakpoint(identifier="Coregistration", expected_outputs=2)
    def run(self, inputs, adfs=None, **kwargs):
        """Coregisters the SLC stack to the reference geometry.

        Parameters
        ----------
        inputs : dict
            Input mapping containing:
            - 'slcs': Calibrated SLC stack (xr.DataTree).
            - 'dem': Resampled DEM (xr.DataTree).
            - 'simulation_ref': Master geometry simulation (xr.DataTree).
            - 'simulation_ml': Multilooked geometry simulation (xr.DataTree).
        adfs : dict, optional
            Auxiliary Data Files mapping (must include 'REFDB', optionally 'IONO').
        **kwargs : dict
            Execution overrides and module configurations.

        Returns
        -------
        dict
            Dictionary containing output datasets: `{"cslcs": cslcs, "slcs": slcs}`.

        Raises
        ------
        KeyError
            If mandatory inputs or configurations are missing.
        """
        # 1. Extract base inputs and retain upstream pipeline parameters
        slcs = inputs["slcs"]
        dem_tree = inputs["dem"]
        simulation_tree = inputs["simulation_ref"]
        simulation_ml_tree = inputs["simulation_ml"]
        all_params = simulation_tree.attrs["params"]
        params = all_params[0]["execution_parameters"]

        # 2. Load instrument-specific configuration
        config = get_config(params["mode"])

        # 3. Extract execution control flags
        initialize_current_reference = kwargs.get(
            "initialize_current_reference", config["Coregistration"]["initialize_current_reference"]
        )

        if initialize_current_reference:
            do_image_matching = False
            do_split_spectrum = False
            do_esd = False
            logger.info(
                "initialize_current_reference is True: forcing do_image_matching, do_split_spectrum and do_esd to False."
            )
        else:
            do_image_matching = kwargs.get("enable_image_matching", config["Coregistration"]["enable_image_matching"])
            do_split_spectrum = kwargs.get("enable_split_spectrum", config["Coregistration"]["enable_split_spectrum"])
            do_esd = kwargs.get("enable_esd", config["Coregistration"]["enable_esd"])

        recompute = kwargs.get("recompute", config["Global"]["recompute"])
        enable_dask = kwargs.get("enable_dask", True)

        # Extract grid chunks and filter spatial dimensions ('y', 'x')
        chunks_geo_grid = kwargs.get("chunks_geo_grid", config["Global"]["chunks_geo_grid"])
        chunks = {k: v for k, v in chunks_geo_grid.items() if k in ["y", "x"]}

        # 4. Extract coregistration specific parameters
        do_flattening = kwargs.get("do_flattening", config["Coregistration"]["do_flattening"])
        do_reramp = kwargs.get("do_reramp", config["Coregistration"]["do_reramp"])
        ov_fact_low_acc = kwargs.get("warp_ov_fact_low_acc", config["Coregistration"]["warp_ov_fact_low_acc"])
        kernel_shape_low_acc = kwargs.get(
            "warp_kernel_shape_low_acc", config["Coregistration"]["warp_kernel_shape_low_acc"]
        )
        kaiser_beta_low_acc = kwargs.get(
            "warp_kaiser_beta_low_acc", config["Coregistration"]["warp_kaiser_beta_low_acc"]
        )
        ov_fact_high_acc = kwargs.get("warp_ov_fact_high_acc", config["Coregistration"]["warp_ov_fact_high_acc"])
        kernel_shape_high_acc = kwargs.get(
            "warp_kernel_shape_high_acc", config["Coregistration"]["warp_kernel_shape_high_acc"]
        )
        kaiser_beta_high_acc = kwargs.get(
            "warp_kaiser_beta_high_acc", config["Coregistration"]["warp_kaiser_beta_high_acc"]
        )
        image_matching_degree = kwargs.get("image_matching_degree", config["Coregistration"]["image_matching_degree"])
        coherence_threshold_image_matching = kwargs.get(
            "coherence_threshold_image_matching", config["Coregistration"]["coherence_threshold_image_matching"]
        )
        coherence_threshold_split_spectrum = kwargs.get(
            "coherence_threshold_split_spectrum", config["Coregistration"]["coherence_threshold_split_spectrum"]
        )
        matching_kernel_shape = kwargs.get("matching_kernel_shape", config["Coregistration"]["matching_kernel_shape"])
        wsize = kwargs.get("wsize", config["Coregistration"]["wsize"])
        wstep = kwargs.get("wstep", config["Coregistration"]["wstep"])
        wsearch = kwargs.get("wsearch", config["Coregistration"]["wsearch"])
        dim = kwargs.get("dim", config["Coregistration"]["dim"])
        block_size = kwargs.get("block_size", config["Coregistration"]["block_size"])
        bandwidth = kwargs.get("bandwidth", config["Coregistration"]["bandwidth"])
        frequency_offset = kwargs.get("frequency_offset", config["Coregistration"]["frequency_offset"])
        multilook_split_spectrum = kwargs.get(
            "multilook_split_spectrum", config["Coregistration"]["multilook_split_spectrum"]
        )
        esd_poly_degree = kwargs.get("esd_poly_degree", config["Coregistration"]["esd_poly_degree"])
        split_spectrum_poly_degree = kwargs.get(
            "split_spectrum_poly_degree", config["Coregistration"]["split_spectrum_poly_degree"]
        )
        h_iono = kwargs.get("height_ionosphere", config["Coregistration"]["height_ionosphere"])
        reference_id = params["reference_id"]

        logger.debug(f"frequency offset: {frequency_offset}")
        logger.debug(f"bandwidth: {bandwidth}")

        # 5. Setup working directories and ADFs
        working_directory_reference_db_static, working_directory_reference_db_dynamic, storage_options_static = (
            get_wdir_static(adfs)
        )
        working_directory_dynamic, storage_options_dynamic = get_wdir_dynamic()
        if adfs is not None and "IONO" in adfs:
            dbfile = get_adf(adfs["IONO"], ensure_local=True)
        else:
            dbfile = None

        # 6. Populate unit parameters dictionary with sanitized values
        execution_parameters = {
            "initialize_current_reference": initialize_current_reference,
            "enable_image_matching": do_image_matching,
            "enable_split_spectrum": do_split_spectrum,
            "enable_esd": do_esd,
            "recompute": recompute,
            "enable_dask": enable_dask,
            "chunks_geo_grid": _json_safe(chunks),
            "do_flattening": do_flattening,
            "do_reramp": do_reramp,
            "ov_fact_low_acc": ov_fact_low_acc,
            "kernel_shape_low_acc": _json_safe(kernel_shape_low_acc),
            "kaiser_beta_low_acc": kaiser_beta_low_acc,
            "ov_fact_high_acc": ov_fact_high_acc,
            "kernel_shape_high_acc": _json_safe(kernel_shape_high_acc),
            "kaiser_beta_high_acc": kaiser_beta_high_acc,
            "image_matching_degree": image_matching_degree,
            "coherence_threshold_image_matching": coherence_threshold_image_matching,
            "coherence_threshold_split_spectrum": coherence_threshold_split_spectrum,
            "matching_kernel_shape": _json_safe(matching_kernel_shape),
            "wsize": _json_safe(wsize),
            "wstep": _json_safe(wstep),
            "wsearch": _json_safe(wsearch),
            "dim": dim,
            "block_size": block_size,
            "bandwidth": bandwidth,
            "frequency_offset": frequency_offset,
            "multilook_split_spectrum": _json_safe(multilook_split_spectrum),
            "esd_poly_degree": esd_poly_degree,
            "split_spectrum_poly_degree": split_spectrum_poly_degree,
            "height_ionosphere": h_iono,
            "reference_id": reference_id,
        }

        metadata_keys = [
            "atbd_link",
            "facility",
            "product_timeliness",
            "product_timeliness_category",
            "product_data_access",
            "product_pixel_coordinate_convention",
            "product_family_specification",
            "cslc_status",
            "providers",
        ]

        for name in metadata_keys:
            execution_parameters[name] = kwargs.get(name, config["Global"].get(name))

        log_processing_parameters(execution_parameters, self, logger)

        # 7. Orbit information extraction and secondary simulation
        multiburst = "bursts" in slcs
        sec_orbits = extract_orbits(slcs, multiburst=multiburst)
        simulation_secondary_tree = None

        sec_orbits = sec_orbits.where(sec_orbits.acquisition_id != reference_id, drop=True)
        if sec_orbits.size != 0:
            simulation_secondary_tree = stack_simulation(
                dem_tree=dem_tree,
                orbits=sec_orbits,
                multiburst=multiburst,
                working_directory=working_directory_dynamic,
                chunks={**chunks, "acquisition_id": 1},
                master_image=False,
                recompute=recompute,
                enable_dask=enable_dask,
                reference_id=reference_id,
            )

        # 8. Primary coregistration execution
        slcs = stack_coregistration(
            slcs=slcs,
            simulation_reference_tree=simulation_tree,
            simulation_secondary_tree=simulation_secondary_tree,
            simulation_ml_tree=simulation_ml_tree,
            reference_id=reference_id,
            multiburst=multiburst,
            working_directory=f"{working_directory_dynamic}/geometric",
            warp_ov_fact=ov_fact_low_acc,
            warp_kernel_shape=kernel_shape_low_acc,
            warp_kaiser_beta=kaiser_beta_low_acc,
            storage_options=storage_options_dynamic,
            do_reramp=do_reramp,
            do_flattening=do_flattening,
            recompute=recompute,
        )

        x_poly = None
        y_poly = None

        # 9. Image matching refinement block
        if do_image_matching:
            logger.info("Executing image matching step...")
            slcs = finalize_coreg(
                slcs=slcs,
                working_directory=working_directory_reference_db_dynamic,
                reference_id=reference_id,
                do_image_matching=do_image_matching,
                do_split_spectrum=do_split_spectrum,
                do_esd=do_esd,
            )
            disparity_map = stack_disparity(
                slcs=slcs,
                reference_id=reference_id,
                wsize=wsize,
                wstep=wstep,
                wsearch=wsearch,
                dim=dim,
                block_size=block_size,
                enable_dask=enable_dask,
            )
            logger.debug(f"disparity map : {disparity_map}")

            x_poly, y_poly = estimate_poly_matching(
                slcs=slcs,
                disparity_map=disparity_map,
                degree=image_matching_degree,
                cohe_th=coherence_threshold_image_matching,
                kernel_shape=matching_kernel_shape,
                enable_dask=enable_dask,
            )

            # Reset x_poly for TOPSAR modes
            if params["mode"] in ["IW", "EW"]:
                x_poly = None

            # Re-run coregistration with matching polynomials applied
            slcs = stack_coregistration(
                slcs=slcs,
                simulation_reference_tree=simulation_tree,
                simulation_ml_tree=simulation_ml_tree,
                reference_id=reference_id,
                simulation_secondary_tree=simulation_secondary_tree,
                multiburst=multiburst,
                working_directory=f"{working_directory_dynamic}/image_matching",
                warp_ov_fact=ov_fact_high_acc,
                warp_kernel_shape=kernel_shape_high_acc,
                warp_kaiser_beta=kaiser_beta_high_acc,
                storage_options=storage_options_dynamic,
                do_reramp=do_reramp,
                do_flattening=do_flattening,
                recompute=recompute,
                enable_dask=enable_dask,
                x_poly=x_poly,
                y_poly=y_poly,
            )

            # Safe attribute construction to prevent UnboundLocalError
            if (x_poly is not None) or (y_poly is not None):
                attrs = {
                    "image_matching": build_coreg_poly_dict(
                        description="Image Matching",
                        y_poly=y_poly,
                        x_poly=x_poly,
                    )
                }
                slcs.attrs.setdefault("coregistration_polynomials", {}).update(attrs)
            else:
                slcs.attrs.setdefault("coregistration_polynomials", {})["image_matching"] = None
        else:
            logger.info("Skipping image matching step (disabled in configuration).")
            slcs.attrs.setdefault("coregistration_polynomials", {})["image_matching"] = None

        # 10. Split spectrum block
        if do_split_spectrum:
            logger.info("Executing split-spectrum step...")
            slcs = finalize_coreg(
                slcs=slcs,
                working_directory=working_directory_reference_db_dynamic,
                reference_id=reference_id,
                do_image_matching=do_image_matching,
                do_split_spectrum=do_split_spectrum,
                do_esd=do_esd,
            )

            slcs = split_spectrum(
                slcs,
                multilook=multilook_split_spectrum,
                frequency_offset=frequency_offset,
                bandwidth=bandwidth,
                cohe_th=coherence_threshold_split_spectrum,
                h_iono=h_iono,
                wdir_path=f"{working_directory_dynamic}/split_spectrum",
                esd_poly_degree=esd_poly_degree,
                split_spectrum_poly_degree=split_spectrum_poly_degree,
                enable_dask=enable_dask,
                multiburst=multiburst,
                reference_id=reference_id,
                dbfile=dbfile,
                x_poly=x_poly,
            )
        else:
            logger.info("Skipping split spectrum step (disabled in configuration).")
            slcs.attrs.setdefault("coregistration_polynomials", {})["split_spectrum"] = None

        # 11. Enhanced Spectral Diversity (ESD) block
        if do_esd:
            logger.info("Executing ESD step...")
            slcs = finalize_coreg(
                slcs=slcs,
                working_directory=working_directory_reference_db_dynamic,
                reference_id=reference_id,
                do_image_matching=do_image_matching,
                do_split_spectrum=do_split_spectrum,
                do_esd=do_esd,
            )
            slcs = stack_esd(
                slcs,
                wdir_path=f"{working_directory_dynamic}/esd",
                esd_poly_degree=esd_poly_degree,
                enable_dask=enable_dask,
                multiburst=multiburst,
                reference_id=reference_id,
            )
        else:
            logger.info("Skipping ESD step (disabled in configuration).")
            slcs.attrs.setdefault("coregistration_polynomials", {})["esd"] = None

        # 13. Register step metadata in EOPF processing history
        current_time = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        facility = kwargs.get("facility", config.get("Global", {}).get("facility", "ESA-Copernicus Ground Segment"))

        execution_parameters["coregistration_polynomials"] = slcs.attrs["coregistration_polynomials"]
        coreg_step = {
            "processor": "COP ARD Coregistration Unit",
            "version": getattr(self, "version", "0.0.1"),
            "facility": facility,
            "time": current_time,
            "inputs": ["slcs", "dem", "simulation_ref", "simulation_ml"],
            "outputs": ["cslcs", "slcs"],
            "adfs": [str(v.path) for v in adfs.values()] if adfs else [],
            "execution_parameters": execution_parameters,
        }

        all_params.append(coreg_step)
        slcs.attrs["params"] = all_params
        # 12. Update final coregistered products
        cslcs, slcs = update_coreg(
            slcs,
            wdir_path_static=working_directory_reference_db_static,
            reference_id=reference_id,
            external_metadata=all_params,
            storage_options=storage_options_static,
        )

        return {"cslcs": cslcs, "slcs": slcs}


class ReferenceUpdate(EOProcessingUnit):
    """Reference Database Update processing unit.

    This unit updates the dynamic reference
    database with newly identified stable acquisitions without outputting a full CMLP product tree.
    """

    @classmethod
    def get_mandatory_input_list(cls, mode=None, **kwargs):
        """Retrieve the list of mandatory input datasets required by this unit."""
        return ["slcs"]

    @classmethod
    def get_mandatory_adf_list(cls, mode=None, **kwargs):
        """Retrieve the list of mandatory Auxiliary Data Files (ADFs) required by this unit."""
        return ["REFDB"]

    @log_time("ReferenceUpdate Unit", logger)
    @resumable_breakpoint(identifier="ReferenceUpdate", expected_outputs=2)
    def run(self, inputs, adfs=None, **kwargs):
        """Executes reference update stack calculations and persists updated reference data.

        Parameters
        ----------
        inputs : dict
            Input mapping containing:
            - 'slcs': Coregistered SLC stack (xr.DataTree).
        adfs : dict, optional
            Auxiliary Data Files mapping (must include 'REFDB').
        **kwargs : dict
            Execution overrides (posting, memory_param, coherence_threshold,
            stable_interval_days, max_past_stable_history, enable_dask, facility).

        Returns
        -------
        dict
            Dictionary containing updated reference objects:
            - 'current_reference': Current reference dataset object.
            - 'stable_references': Identified stable reference dataset collection.

        Raises
        ------
        KeyError
            If mandatory inputs or configuration attributes are missing.
        """
        # 1. Extract base inputs and retain upstream pipeline parameters
        slcs = inputs["slcs"]
        all_params = slcs.attrs["params"]
        params = all_params[0]["execution_parameters"]

        # 2. Load instrument-specific configuration
        config = get_config(params["mode"])

        # 3. Setup static and dynamic working directories
        working_directory_reference_db_static, working_directory_reference_db_dynamic, storage_options_static = (
            get_wdir_static(adfs)
        )
        working_directory_dynamic, storage_options_dynamic = get_wdir_dynamic()

        # 4. Extract execution parameters with explicit config fallbacks
        reference_id = params["reference_id"]
        posting = kwargs.get("posting", config["ReferenceUpdate"]["posting"])
        memory_param = kwargs.get("memory_param", config["ReferenceUpdate"]["memory_param"])
        coherence_threshold = kwargs.get("coherence_threshold", config["ReferenceUpdate"]["coherence_threshold"])
        stable_interval_days = kwargs.get("stable_interval_days", config["ReferenceUpdate"]["stable_interval_days"])
        max_past_stable_history = kwargs.get(
            "max_past_stable_history", config["ReferenceUpdate"]["max_past_stable_history"]
        )
        enable_dask = kwargs.get("enable_dask", True)

        # 5. Populate unit execution parameters dictionary
        execution_parameters = {
            "posting": _json_safe(posting),
            "memory_param": memory_param,
            "coherence_threshold": coherence_threshold,
            "stable_interval_days": stable_interval_days,
            "max_past_stable_history": max_past_stable_history,
            "enable_dask": enable_dask,
        }

        log_processing_parameters(execution_parameters, self, logger)

        # 6. Execute CMLP stack calculation without saving metrics
        _, current_reference, stable_references = stack_cmlp(
            slcs=slcs,
            posting=posting,
            working_directory=Path(
                working_directory_dynamic,
                storage_options=storage_options_dynamic,
            ),
            stable_interval_days=stable_interval_days,
            reference_database_path=Path(
                working_directory_reference_db_dynamic,
                storage_options=storage_options_static,
            ),
            enable_dask=enable_dask,
            memory_param=memory_param,
            coherence_threshold=coherence_threshold,
            max_past_stable_history=max_past_stable_history,
            save_metrics=False,
            reference_id=reference_id,
        )

        # 8. Register step metadata in EOPF processing history
        current_time = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        facility = kwargs.get("facility", config.get("Global", {}).get("facility", "ESA-Copernicus Ground Segment"))

        ref_update_step = {
            "processor": "COP ARD ReferenceUpdate Unit",
            "version": getattr(self, "version", "0.0.1"),
            "facility": facility,
            "time": current_time,
            "inputs": ["slcs"],
            "outputs": ["current_reference", "stable_references"],
            "adfs": [str(v.path) if hasattr(v, "path") else str(v) for v in adfs.values()] if adfs else [],
            "execution_parameters": execution_parameters,
        }

        all_params.append(ref_update_step)

        current_reference.attrs["processing_history"] = all_params
        return {
            "current_reference": current_reference,
            "stable_references": stable_references,
        }


class CMLP(EOProcessingUnit):
    """Coregistered Multi-Looked Phase (CMLP) processing unit.

    This unit computes cmlp, identifies stable reference
    acquisitions, updates the reference database, and generates updated stack products.
    """

    @classmethod
    def get_mandatory_input_list(cls, mode=None, **kwargs):
        """Retrieve the list of mandatory input datasets required by this unit."""
        return ["slcs"]

    @classmethod
    def get_mandatory_adf_list(cls, mode=None, **kwargs):
        """Retrieve the list of mandatory Auxiliary Data Files (ADFs) required by this unit."""
        return ["REFDB"]

    @log_time("CMLP Unit", logger)
    @resumable_breakpoint(identifier="CMLP", expected_outputs=3)
    def run(self, inputs, adfs=None, **kwargs):
        """Executes the CMLP stack processing and updates the reference database.

        Parameters
        ----------
        inputs : dict
            Input mapping containing:
            - 'slcs': Coregistered SLC stack (xr.DataTree).
        adfs : dict, optional
            Auxiliary Data Files mapping (must include 'REFDB').
        **kwargs : dict
            Execution overrides (posting, memory_param, coherence_threshold,
            stable_interval_days, max_past_stable_history, enable_dask).

        Returns
        -------
        dict
            Dictionary containing processed output datasets:
            - 'cmlp': Output CMLP data tree (xr.DataTree).
            - 'current_reference': Current reference dataset object.
            - 'stable_references': Identified stable reference dataset collection.

        Raises
        ------
        KeyError
            If mandatory inputs or configuration attributes are missing.
        """
        # 1. Extract base inputs and retain upstream pipeline parameters
        slcs = inputs["slcs"]
        all_params = slcs.attrs["params"]
        params = all_params[0]["execution_parameters"]

        # 2. Load instrument-specific configuration
        config = get_config(params["mode"])

        # 3. Setup working directories
        working_directory_reference_db_static, working_directory_reference_db_dynamic, storage_options_static = (
            get_wdir_static(adfs)
        )
        working_directory_dynamic, storage_options_dynamic = get_wdir_dynamic()

        # 4. Extract execution parameters with explicit config fallbacks
        reference_id = params["reference_id"]
        posting = kwargs.get("posting", config["ReferenceUpdate"]["posting"])
        memory_param = kwargs.get("memory_param", config["ReferenceUpdate"]["memory_param"])
        coherence_threshold = kwargs.get("coherence_threshold", config["ReferenceUpdate"]["coherence_threshold"])
        stable_interval_days = kwargs.get("stable_interval_days", config["ReferenceUpdate"]["stable_interval_days"])
        max_past_stable_history = kwargs.get(
            "max_past_stable_history", config["ReferenceUpdate"]["max_past_stable_history"]
        )
        enable_dask = kwargs.get("enable_dask", True)

        # 5. Populate unit execution parameters dictionary
        execution_parameters = {
            "reference_id": reference_id,
            "posting": _json_safe(posting),
            "memory_param": memory_param,
            "coherence_threshold": coherence_threshold,
            "stable_interval_days": stable_interval_days,
            "max_past_stable_history": max_past_stable_history,
            "enable_dask": enable_dask,
        }

        log_processing_parameters(execution_parameters, self, logger)

        # 6. Execute CMLP stack processing
        cmlp_tree, current_reference, stable_references = stack_cmlp(
            slcs=slcs,
            posting=posting,
            working_directory=Path(
                working_directory_dynamic,
                storage_options=storage_options_dynamic,
            ),
            stable_interval_days=stable_interval_days,
            reference_database_path=Path(
                working_directory_reference_db_dynamic,
                storage_options=storage_options_static,
            ),
            enable_dask=enable_dask,
            memory_param=memory_param,
            coherence_threshold=coherence_threshold,
            max_past_stable_history=max_past_stable_history,
            save_metrics=True,
            reference_id=reference_id,
        )

        logger.debug(f"stable_reference: {stable_references}")

        # 8. Register step metadata in EOPF processing history
        current_time = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        facility = kwargs.get("facility", config.get("Global", {}).get("facility", "ESA-Copernicus Ground Segment"))

        cmlp_step = {
            "processor": "COP ARD CMLP Unit",
            "version": getattr(self, "version", "0.0.1"),
            "facility": facility,
            "time": current_time,
            "inputs": ["slcs"],
            "outputs": ["cmlp", "current_reference", "stable_references"],
            "adfs": [str(v.path) if hasattr(v, "path") else str(v) for v in adfs.values()] if adfs else [],
            "execution_parameters": execution_parameters,
        }

        all_params.append(cmlp_step)
        cmlp_tree.attrs["params"] = all_params

        return {
            "cmlp": cmlp_tree,
            "current_reference": current_reference,
            "stable_references": stable_references,
        }


class Geocoding(EOProcessingUnit):
    """Geocoding processing unit for projecting radar-grid data into geographic coordinates.

    This unit resamples and warps the  CMLP stack onto the target geographic grid using specified resampling kernels and pixel spacing.
    """

    @classmethod
    def get_mandatory_input_list(cls, mode=None, **kwargs):
        """Retrieve the list of mandatory input datasets required by this unit."""
        return ["cmlp", "slcs", "simulation_ref", "simulation_ml"]

    @classmethod
    def get_mandatory_adf_list(cls, mode=None, **kwargs):
        """Retrieve the list of mandatory Auxiliary Data Files (ADFs) required by this unit."""
        return ["REFDB"]

    @log_time("Geocoding Unit", logger)
    @resumable_breakpoint(identifier="Geocoding")
    def run(self, inputs, adfs, **kwargs):
        """Executes the geocoding processing pipeline.

        Parameters
        ----------
        inputs : dict
            Input mapping containing:
            - 'cmlp': Output from CMLP processing (xr.DataTree).
            - 'slcs': Coregistered SLC stack (xr.DataTree).
            - 'simulation_ref': Master geometry simulation (xr.DataTree).
            - 'simulation_ml': Multilooked geometry simulation (xr.DataTree).
        adfs : dict, optional
            Auxiliary Data Files mapping (must include 'REFDB').
        **kwargs : dict
            Execution overrides (warp_ov_fact, warp_kernel_shape, warp_kaiser_beta,
            spacing, chunks_geo_grid, enable_dask, facility).

        Returns
        -------
        dict
            Dictionary containing geocoded output products:
            - 'gmlp': Updated geocoded product stack (xr.DataTree).

        Raises
        ------
        KeyError
            If mandatory inputs or configuration attributes are missing.
        """
        # 1. Extract base inputs and retain upstream pipeline parameters
        cmlp_tree = inputs["cmlp"]
        slcs = inputs["slcs"]
        simulation_tree = inputs["simulation_ref"]
        simulation_ml_tree = inputs["simulation_ml"]

        all_params = cmlp_tree.attrs["params"]
        params = all_params[0]["execution_parameters"]

        # 2. Load instrument-specific configuration
        config = get_config(params["mode"])
        reference_id = params["reference_id"]

        # 3. Setup static and dynamic working directories
        working_directory_reference_db_static, _, storage_options_static = get_wdir_static(adfs)
        working_directory_dynamic, storage_options_dynamic = get_wdir_dynamic()

        # 4. Extract execution parameters with explicit config fallbacks
        ov_fact = kwargs.get("warp_ov_fact", config["Geocoding"]["warp_ov_fact"])
        kernel_shape = kwargs.get("warp_kernel_shape", config["Geocoding"]["warp_kernel_shape"])
        kaiser_beta = kwargs.get("warp_kaiser_beta", config["Geocoding"]["warp_kaiser_beta"])
        spacing = kwargs.get("spacing", config["Geocoding"]["spacing"])
        chunks_geo_grid = kwargs.get("chunks_geo_grid", config["Global"]["chunks_geo_grid"])
        enable_dask = kwargs.get("enable_dask", True)

        # 5. Populate unit execution parameters dictionary
        execution_parameters = {
            "warp_ov_fact": ov_fact,
            "warp_kernel_shape": _json_safe(kernel_shape),
            "warp_kaiser_beta": kaiser_beta,
            "spacing": _json_safe(spacing),
            "chunks_geo_grid": _json_safe(chunks_geo_grid),
            "enable_dask": enable_dask,
        }

        metadata_keys = [
            "atbd_link",
            "facility",
            "product_timeliness",
            "product_timeliness_category",
            "product_data_access",
            "product_pixel_coordinate_convention",
            "product_family_specification",
            "gmlp_status",
            "providers",
        ]

        for name in metadata_keys:
            execution_parameters[name] = kwargs.get(name, config["Global"].get(name))

        log_processing_parameters(execution_parameters, self, logger)

        # 6. Core geocoding stack processing
        gmlp = stack_gmlp(
            cmlp_tree=cmlp_tree,
            slcs=slcs,
            simulation_tree=simulation_tree,
            simulation_ml_tree=simulation_ml_tree,
            spacing=spacing,
            reference_id=reference_id,
            working_directory_static=working_directory_reference_db_static,
            working_directory_tmp=working_directory_dynamic,
            enable_dask=enable_dask,
            warp_ov_fact=ov_fact,
            warp_kernel_shape=kernel_shape,
            warp_kaiser_beta=kaiser_beta,
            chunks=chunks_geo_grid,
        )

        # 7. Register step metadata in EOPF processing history
        current_time = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        facility = kwargs.get("facility", config.get("Global", {}).get("facility", "ESA-Copernicus Ground Segment"))

        geocoding_step = {
            "processor": "COP ARD Geocoding Unit",
            "version": getattr(self, "version", "0.0.1"),
            "facility": facility,
            "time": current_time,
            "inputs": ["cmlp", "slcs", "simulation_ref", "simulation_ml"],
            "outputs": ["gmlp"],
            "adfs": [str(v.path) if hasattr(v, "path") else str(v) for v in adfs.values()] if adfs else [],
            "execution_parameters": execution_parameters,
        }

        all_params.append(geocoding_step)

        gmlp_update = update_gmlp(gmlp, slcs, all_params)
        return {"gmlp": gmlp_update}
