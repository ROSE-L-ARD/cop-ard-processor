from cop_ard_app.io import Path
import json
import os

import xarray as xr
from cop_ard_app.area_projection import stack_geometry, stack_mask
from cop_ard_app.cmlp import stack_cmlp
from cop_ard_app.coreg import stack_coregistration, finalize_coreg
from cop_ard_app.dem import import_dem, stack_dem
from cop_ard_app.etad import import_ETAD
from cop_ard_app.gmlp import stack_gmlp
from cop_ard_app.matching import estimate_poly_matching, stack_disparity
from cop_ard_app.model import extract_acquisition_dates, extract_orbits, update_coreg
from cop_ard_app.split_spectrum import split_spectrum
from cop_ard_app.orbit import import_ORBIT
from cop_ard_app.simulation import stack_simulation, stack_simulation_ml
from cop_ard_app.slc import finalize_import, import_slc_stack
from cop_ard_app.utils import get_bbox
from eopf.computing import EOProcessingUnit

from cop_ard_processor.breakpoints import resumable_breakpoint
from cop_ard_processor.utils import (
    add_processing_software,
    get_adf,
    get_config,
    get_instrument_mode,
    get_wdir_dynamic,
    get_wdir_static,
    log_time,
)
from cop_ard_core.logger import logger


def _get_slcs_path(workdir_path_dynamic: str):
    return os.path.join(workdir_path_dynamic, "slcs")


def _is_multiburst(slcs: xr.DataTree):
    return "bursts" in slcs


class Calibration(EOProcessingUnit):
    @classmethod
    def get_mandatory_adf_list(cls, mode=None, **kwargs):
        return ["REFERENCE_DB"]

    @log_time("Calibration Unit", logger)
    @resumable_breakpoint(identifier="Calibration")
    def run(self, inputs, adfs=None, **kwargs):
        input_slcs_files = kwargs.get("input_slcs_files")
        if input_slcs_files is None:
            raise ValueError("input_slcs_files must be provided via processing parameters (kwargs).")

        mode = get_instrument_mode(input_slcs_files)
        config = get_config(mode)
        multiburst = True if mode in ["IW", "EW"] else False

        enable_dask = kwargs.get("enable_dask", True)
        recompute = kwargs.get("recompute", config["Global"]["recompute"])
        chunks = kwargs.get("chunks", config["Global"]["chunks_radar_grid"])

        calibration_type = kwargs.get("calibration_type", config["Calibration"]["calibration_type"])
        indices = kwargs.get("indices", config["Calibration"]["indices"])
        polarization = kwargs.get("polarization", config["Calibration"]["polarization"])
        deramp = kwargs.get("deramp", config["Calibration"]["deramp"])
        noise_removal = kwargs.get("noise_removal", config["Calibration"]["noise_removal"])
        apply_invalid_mask = kwargs.get("apply_invalid_mask", config["Calibration"]["apply_invalid_mask"])

        reference_id = kwargs.get("reference_id")
        params = {
            "mode": mode,
            "chunks_radar_grid": json.dumps(chunks),
            "multiburst": multiburst,
            "polarization": polarization,
            "deramp": deramp,
            "noise_removal": noise_removal,
            "apply_invalid_mask": apply_invalid_mask,
            "measurement_type": calibration_type,
        }

        logger.info(f"Processing parameters: {params}")

        working_directory_static, _, storage_options_static = get_wdir_static(adfs)
        working_directory_dynamic, storage_options_dynamic = get_wdir_dynamic()

        # Ensure indices are a list
        if isinstance(indices, int):
            indices = [indices]

        if not multiburst and reference_id is None and not recompute:
            raise ValueError(
                f"Missing reference_id. For non-multiburst modes ({mode}), a reference_id "
                "must be explicitly provided unless you are initializing a new reference (recompute=True)."
            )

        slcs_path = _get_slcs_path(working_directory_dynamic)
        do_import = not recompute

        # Import the SLC stack, applying calibration and corrections
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

        # Apply precise timing (ETAD) or orbit corrections
        if (adfs is not None) and (recompute is False):
            etad_files = []
            orbit_files = []
            for k, v in adfs.items():
                if "ETAD" in k:
                    etad_files.append(get_adf(v))
                if "ORBIT" in k:
                    orbit_files.append(get_adf(v))
            slcs, used_etads = import_ETAD(
                slcs,
                acquisition_dates,
                etad_files,
                wdir_path=working_directory_dynamic,
                multiburst=multiburst,
                storage_options=storage_options_dynamic,
                enable_dask=enable_dask,
            )
            params["etad"] = used_etads
            slcs, used_orbits = import_ORBIT(
                slcs,
                acquisition_dates,
                orbit_files,
                wdir_path=working_directory_dynamic,
                multiburst=multiburst,
                storage_options=storage_options_dynamic,
                enable_dask=enable_dask,
            )
            params["orbit"] = used_orbits
        else:
            logger.info("Neither ETAD or external orbits have been provided")

        # Extract acquisition dates for matching with correction files
        # Finalize the import process by checking if reference data is available
        logger.debug(f"reference_id : {reference_id}")
        logger.debug(f"do_import: {do_import}")
        logger.debug(f"recompute: {recompute}")

        slcs, reference_id = finalize_import(
            slcs=slcs,
            working_directory=working_directory_static,
            recompute=recompute,
            acquisition_id=reference_id,
            enable_dask=enable_dask,
        )

        params["reference_id"] = reference_id
        output = {"slcs": slcs, "params": params}
        return output


class ReferenceDEM(EOProcessingUnit):
    @classmethod
    def get_mandatory_input_list(cls, mode=None, **kwargs):
        return ["slcs", "params"]

    @classmethod
    def get_mandatory_adf_list(cls, mode=None, **kwargs):
        return ["REFERENCE_DB", "DEM"]

    @log_time("ReferenceDEM Unit", logger)
    @resumable_breakpoint(identifier="ReferenceDEM")
    def run(self, inputs, adfs=None, **kwargs):
        # 1. Extract base inputs and load the configuration
        slcs = inputs["slcs"]
        params = inputs["params"]
        multiburst = _is_multiburst(slcs)
        dem_type = get_adf(adfs["DEM"])
        params["dem"] = dem_type.path
        config = get_config(params["mode"])

        # 2. Extract parameters from kwargs using the config as an explicit fallback
        recompute = kwargs.get("recompute", config["Global"]["recompute"])
        reference_id = params["reference_id"]

        chunks_geo_grid = kwargs.get("chunks_geo_grid", config["Global"]["chunks_geo_grid"])
        # Filter chunks to keep only spatial dimensions
        chunks = {k: v for k, v in chunks_geo_grid.items() if k in ["y", "x"]}

        method = kwargs.get("interpolation", config["ReferenceDEM"]["interpolation"])
        spacing = kwargs.get("spacing", config["ReferenceDEM"]["spacing"])

        # epsg does not have a config fallback in the original code
        crs = kwargs.get("epsg")

        # 3. Setup working directories
        working_directory_static, _, storage_options_static = get_wdir_static(adfs)
        working_directory_dynamic, storage_options_dynamic = get_wdir_dynamic()

        # 4. Update the params dictionary for metadata/logging
        params["chunks_geo_grid"] = json.dumps(chunks)
        params["dem:interpolation"] = method
        params["dem:spacing"] = json.dumps(spacing)
        params["dem:crs"] = crs

        logger.info(f"Processing parameters: {params}")

        # 5. Execute core processing
        # Get the bounding box of the SLC stack
        bbox = get_bbox(slcs)

        # Import the DEM for the specified bounding box
        dem = import_dem(
            bbox=bbox,
            working_dir=working_directory_dynamic,
            dem_type=dem_type.path,
            chunks=chunks,
            storage_options_dem=dem_type.storage_options,
            storage_options=storage_options_dynamic,
        )

        # DEM resampling with the requested CRS and spacing
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

        output = {"DEM": dem_tree, "params": params}
        return output


class ReferenceGeometry(EOProcessingUnit):
    @classmethod
    def get_mandatory_input_list(cls, mode=None, **kwargs):
        return ["slcs", "dem", "params"]

    @classmethod
    def get_mandatory_adf_list(cls, mode=None, **kwargs):
        return ["REFERENCE_DB"]

    @log_time("ReferenceGeometry Unit", logger)
    @resumable_breakpoint(identifier="ReferenceGeometry")
    def run(self, inputs, adfs=None, **kwargs):
        """
        Generates the reference geometry for coregistration.

        This step uses the orbit of a single "master" acquisition (defined by
        `reference_date`) and the DEM to simulate the master radar geometry.
        It also pre-computes and saves geometric layers (e.g., incidence
        angles) and masks (layover, shadow) to the static directory for reuse.

        Parameters
        ----------
        inputs : Mapping[str, xr.DataTree]
            Input mapping. Expected keys:

            - 'slcs': The calibrated SLC stack (xr.DataTree).
            - 'dem': The resampled DEM (xr.DataTree).
            - 'params': Processing parameters dictionary.
        adfs : Mapping[str, ADF] | None, optional
            Auxiliary Data Files mapping. Expected keys:

            - 'REFERENCE_DB': ADF for persistent storage.
        **kwargs : Any
            Additional keyword arguments (e.g., recompute, chunks_geo_grid, enable_dask).

        Returns
        -------
        Mapping[str, xr.DataTree]
            A dictionary containing the reference simulation:

            - 'simulation_ref': The simulated master geometry (xr.DataTree).
            - 'simulation_ml': The multilooked simulated master geometry (xr.DataTree).

        Raises
        -------
        ValueError
            If mandatory inputs ('slcs', 'dem') are missing.
        TypeError
            If inputs are not of type `xr.DataTree`.
        """
        # 1. Extract base inputs
        slcs = inputs["slcs"]
        dem_tree = inputs["dem"]
        params = inputs["params"]

        # 2. Load configuration
        config = get_config(params["mode"])

        # 3. Extract parameters from kwargs with explicit config/default fallbacks
        recompute = kwargs.get("recompute", config["Global"]["recompute"])
        enable_dask = kwargs.get("enable_dask", True)

        chunks_geo_grid = kwargs.get("chunks_geo_grid", config["Global"]["chunks_geo_grid"])
        chunks = {k: v for k, v in chunks_geo_grid.items() if k in ["y", "x"]}

        reference_id = params["reference_id"]

        # 4. Setup directories
        working_directory_static, _, storage_options_static = get_wdir_static(adfs)
        working_directory_dynamic, storage_options_dynamic = get_wdir_dynamic()

        logger.info(f"Processing parameters: {params}")

        # 5. Core processing logic
        # check if slcs is a multiburst type data (IW or EW)
        multiburst = True if "bursts" in slcs else False

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

        return {
            "simulation_ref": simulation_tree,
            "simulation_ml": simulation_ml_tree,
        }


class Coregistration(EOProcessingUnit):
    @classmethod
    def get_mandatory_input_list(cls, mode=None, **kwargs):
        return ["slcs", "dem", "simulation_ref", "simulation_ml", "params"]

    @classmethod
    def get_mandatory_adf_list(cls, mode=None, **kwargs):
        return ["REFERENCE_DB"]

    @log_time("Coregistration Unit", logger)
    @resumable_breakpoint(identifier="Coregistration")
    def run(self, inputs, adfs=None, **kwargs):
        """
        Coregisters the SLC stack to the reference geometry.
        """
        # 1. Extract base inputs
        slcs = inputs["slcs"]
        dem_tree = inputs["dem"]
        simulation_tree = inputs["simulation_ref"]
        simulation_ml_tree = inputs["simulation_ml"]
        params = inputs["params"]

        # 2. Load configuration based on the instrument mode
        config = get_config(params["mode"])

        # 3. Extract logic-controlling parameters
        initialize_current_reference = kwargs.get(
            "initialize_current_reference", config["Coregistration"]["initialize_current_reference"]
        )

        if initialize_current_reference:
            do_image_matching = False
            do_split_spectrum = False
            logger.info(
                "initialize_current_reference is True: forcing do_image_matching and do_split_spectrum to False."
            )
        else:
            do_image_matching = kwargs.get("do_image_matching", config["Coregistration"]["do_image_matching"])
            do_split_spectrum = kwargs.get("do_split_spectrum", config["Coregistration"]["do_split_spectrum"])

        recompute = kwargs.get("recompute", config["Global"]["recompute"])
        enable_dask = kwargs.get("enable_dask", True)

        # Extract grid chunks and filter them
        chunks_geo_grid = kwargs.get("chunks_geo_grid", config["Global"]["chunks_geo_grid"])
        chunks = {k: v for k, v in chunks_geo_grid.items() if k in ["y", "x"]}

        # 4. Extract coregistration specific parameters
        do_flattening = kwargs.get("do_flattening", config["Coregistration"]["do_flattening"])
        do_reramp = kwargs.get("do_reramp", config["Coregistration"]["do_reramp"])
        ov_fact = kwargs.get("warp_ov_fact", config["Coregistration"]["warp_ov_fact"])
        kernel_shape = kwargs.get("warp_kernel_shape", config["Coregistration"]["warp_kernel_shape"])
        kaiser_beta = kwargs.get("warp_kaiser_beta", config["Coregistration"]["warp_kaiser_beta"])
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

        # 5. Setup directories
        working_directory_reference_db_static, working_directory_reference_db_dynamic, storage_options_static = (
            get_wdir_static(adfs)
        )
        working_directory_dynamic, storage_options_dynamic = get_wdir_dynamic()

        # 6. Populate parameters dictionary for metadata/logging
        params["ov_fact"] = ov_fact
        params["kernel_shape"] = kernel_shape
        params["kaiser_beta"] = kaiser_beta

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

        # Dynamically fetch metadata arguments from kwargs or Global config
        for name in metadata_keys:
            params[name] = kwargs.get(name, config["Global"].get(name))

        logger.info(f"Processing parameters: {params}")

        # 7. Orbit information extraction and simulation for secondary images
        multiburst = True if "bursts" in slcs else False
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

        # 8. Execute Coregistration stack
        slcs = stack_coregistration(
            slcs=slcs,
            simulation_reference_tree=simulation_tree,
            simulation_secondary_tree=simulation_secondary_tree,
            simulation_ml_tree=simulation_ml_tree,
            reference_id=reference_id,
            multiburst=multiburst,
            working_directory=working_directory_dynamic,
            warp_ov_fact=ov_fact,
            warp_kernel_shape=kernel_shape,
            warp_kaiser_beta=kaiser_beta,
            storage_options=storage_options_dynamic,
            do_reramp=do_reramp,
            do_flattening=do_flattening,
            recompute=recompute,
        )

        logger.debug(f"reference_id : {reference_id}")
        logger.debug(f"init current ref: {initialize_current_reference}")
        logger.debug(f"image_matching: {do_image_matching}")
        logger.debug(f"split spectrum : {do_split_spectrum}")

        # 9. Image matching refinement block
        if do_image_matching:
            slcs = finalize_coreg(
                slcs=slcs,
                working_directory=working_directory_reference_db_dynamic,
                reference_id=reference_id,
                do_image_matching=do_image_matching,
                do_split_spectrum=do_split_spectrum,
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
            logger.debug(f"x_poly: {x_poly}")
            logger.debug(f"y_poly: {y_poly}")

            # Reset x_poly for specific modes
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
                warp_ov_fact=ov_fact,
                warp_kernel_shape=kernel_shape,
                warp_kaiser_beta=kaiser_beta,
                storage_options=storage_options_dynamic,
                do_reramp=do_reramp,
                do_flattening=do_flattening,
                recompute=recompute,
                enable_dask=enable_dask,
                x_poly=x_poly,
                y_poly=y_poly,
            )

        if do_split_spectrum:
            slcs = finalize_coreg(
                slcs=slcs,
                working_directory=working_directory_reference_db_dynamic,
                reference_id=reference_id,
                do_image_matching=do_image_matching,
                do_split_spectrum=do_split_spectrum,
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
            )

        cslcs, slcs = update_coreg(
            slcs,
            wdir_path_static=working_directory_reference_db_static,
            reference_id=reference_id,
            external_metadata=params,
            storage_options=storage_options_static,
        )
        return {"cslcs": cslcs, "slcs": slcs}


class ReferenceUpdate(EOProcessingUnit):
    @classmethod
    def get_mandatory_input_list(cls, mode=None, **kwargs):
        return ["slcs", "params"]

    @classmethod
    def get_mandatory_adf_list(cls, mode=None, **kwargs):
        return ["REFERENCE_DB"]

    @log_time("ReferenceUpdate Unit", logger)
    @resumable_breakpoint(identifier="ReferenceUpdate")
    def run(self, inputs, adfs=None, **kwargs):
        # 1. Extract base inputs
        slcs = inputs["slcs"]
        params = inputs["params"]

        # 2. Load configuration based on the instrument mode
        config = get_config(params["mode"])

        # 3. Setup directories
        working_directory_reference_db_static, working_directory_reference_db_dynamic, storage_options_static = (
            get_wdir_static(adfs)
        )
        working_directory_dynamic, storage_options_dynamic = get_wdir_dynamic()

        # 4. Extract parameters from kwargs with explicit config fallbacks
        reference_id = params["reference_id"]
        posting = kwargs.get("posting", config["ReferenceUpdate"]["posting"])
        memory_param = kwargs.get("memory_param", config["ReferenceUpdate"]["memory_param"])
        coherence_threshold = kwargs.get("coherence_threshold", config["ReferenceUpdate"]["coherence_threshold"])
        stable_interval_days = kwargs.get("stable_interval_days", config["ReferenceUpdate"]["stable_interval_days"])
        max_past_stable_history = kwargs.get(
            "max_past_stable_history", config["ReferenceUpdate"]["max_past_stable_history"]
        )

        enable_dask = kwargs.get("enable_dask", True)

        # 5. Execute CMLP stack
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

        logger.debug(f"stable_reference : {stable_references}")

        # 6. Update Reference Database
        # TODO: write script to do this externally.
        from cop_ard_app.utils import update_reference_database

        logger.debug("SAVING CURRENT REFERENCE")
        update_reference_database(
            current_reference,
            stable_references,
            working_directory_reference_db_dynamic,
            reference_id=reference_id,
        )

        return {
            "current_reference": current_reference,
            "stable_references": stable_references,
        }


class CMLP(EOProcessingUnit):
    @classmethod
    def get_mandatory_input_list(cls, mode=None, **kwargs):
        return ["slcs", "params"]

    @classmethod
    def get_mandatory_adf_list(cls, mode=None, **kwargs):
        return ["REFERENCE_DB"]

    @log_time("CMLP Unit", logger)
    @resumable_breakpoint(identifier="CMLP")
    def run(self, inputs, adfs=None, **kwargs):
        # 1. Extract base inputs
        slcs = inputs["slcs"]
        params = inputs["params"]

        # 2. Load configuration based on the instrument mode
        config = get_config(params["mode"])

        # 3. Setup directories
        working_directory_reference_db_static, working_directory_reference_db_dynamic, storage_options_static = (
            get_wdir_static(adfs)
        )
        working_directory_dynamic, storage_options_dynamic = get_wdir_dynamic()

        # 4. Extract parameters from kwargs with explicit config fallbacks
        reference_id = params["reference_id"]
        posting = kwargs.get("posting", config["ReferenceUpdate"]["posting"])
        memory_param = kwargs.get("memory_param", config["ReferenceUpdate"]["memory_param"])
        coherence_threshold = kwargs.get("coherence_threshold", config["ReferenceUpdate"]["coherence_threshold"])
        stable_interval_days = kwargs.get("stable_interval_days", config["ReferenceUpdate"]["stable_interval_days"])
        max_past_stable_history = kwargs.get(
            "max_past_stable_history", config["ReferenceUpdate"]["max_past_stable_history"]
        )

        enable_dask = kwargs.get("enable_dask", True)

        # 5. Execute CMLP stack
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

        logger.debug(f"stable_reference : {stable_references}")

        # 6. Update Reference Database
        # TODO: write script to do this externally.
        from cop_ard_app.utils import update_reference_database

        logger.debug("NOT SAVING CURRENT REFERENCE")
        # update_reference_database(
        #     current_reference,
        #     stable_references,
        #     working_directory_reference_db_dynamic,
        #     reference_id=reference_id,
        # )

        return {
            "cmlp": cmlp_tree,
            "current_reference": current_reference,
            "stable_references": stable_references,
        }


class Geocoding(EOProcessingUnit):
    @classmethod
    def get_mandatory_input_list(cls, mode=None, **kwargs):
        return ["cmlp", "simulation_ref", "simulation_ml", "params"]

    @log_time("Geocoding Unit", logger)
    @resumable_breakpoint(identifier="Geocoding")
    def run(self, inputs, adfs, **kwargs):
        cmlp = inputs["cmlp"]
        simulation_tree = inputs["simulation_ref"]
        simulation_ml_tree = inputs["simulation_ml"]
        params = inputs["params"]

        config = get_config(params["mode"])

        # 3. Setup directories
        working_directory_dynamic, storage_options_dynamic = get_wdir_dynamic()

        ov_fact = kwargs.get("warp_ov_fact", config["Geocoding"]["warp_ov_fact"])
        kernel_shape = kwargs.get("warp_kernel_shape", config["Geocoding"]["warp_kernel_shape"])
        kaiser_beta = kwargs.get("warp_kaiser_beta", config["Geocoding"]["warp_kaiser_beta"])
        spacing = kwargs.get("spacing", config["Geocoding"]["spacing"])
        chunks_geo_grid = kwargs.get("chunks_geo_grid", config["Global"]["chunks_geo_grid"])
        enable_dask = kwargs.get("enable_dask", True)

        gmlp = stack_gmlp(
            cmlp_tree=cmlp,
            simulation_tree=simulation_tree,
            simulation_ml_tree=simulation_ml_tree,
            spacing=spacing,
            working_directory_tmp=working_directory_dynamic,
            enable_dask=enable_dask,
            warp_ov_fact=ov_fact,
            warp_kernel_shape=kernel_shape,
            warp_kaiser_beta=kaiser_beta,
            chunks=chunks_geo_grid,
        )
        return {"gmlp": gmlp}
