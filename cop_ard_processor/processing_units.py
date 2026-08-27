import re
from cop_ard_app.io import Path
import json
import os
from typing import Union, Any

import xarray as xr
from cop_ard_app.area_projection import stack_geometry, stack_mask
from cop_ard_app.cmlp import stack_cmlp
from cop_ard_app.coreg import stack_coregistration, finalize_coreg
from cop_ard_app.dem import import_dem, stack_dem
from cop_ard_app.etad import import_ETAD
from cop_ard_app.gmlp import stack_gmlp
from cop_ard_app.matching import estimate_poly_matching, stack_disparity
from cop_ard_app.model import extract_acquisition_dates, extract_orbits
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

    @resumable_breakpoint(identifier="Calibration")
    def run(
        self,
        inputs,
        input_slcs_files: list,
        adfs=None,
        calibration_type=None,
        recompute=None,
        reference_id=None,
        indices=None,
        enable_dask=None,
        chunks=None,
        polarization=None,
        deramp=None,
        noise_removal=None,
        apply_invalid_mask=None,
        **kwargs,
    ):
        params = {}
        mode = get_instrument_mode(input_slcs_files)
        config = get_config(mode)
        multiburst = True if mode in ["IW", "EW"] else False
        if reference_id is None and not multiburst:
            raise ValueError("Not possible")
        # Extract processing parameters from kwargs or config file
        enable_dask = enable_dask if enable_dask is not None else True
        recompute = recompute if recompute is not None else config["Global"]["recompute"]
        chunks = chunks if chunks is not None else config["Global"]["chunks_radar_grid"]

        # Parametri di calibrazione
        calibration_type = (
            calibration_type if calibration_type is not None else config["Calibration"]["calibration_type"]
        )
        indices = indices if indices is not None else config["Calibration"]["indices"]
        polarization = polarization if polarization is not None else config["Calibration"]["polarization"]
        deramp = deramp if deramp is not None else config["Calibration"]["deramp"]
        noise_removal = noise_removal if noise_removal is not None else config["Calibration"]["noise_removal"]
        apply_invalid_mask = (
            apply_invalid_mask if apply_invalid_mask is not None else config["Calibration"]["apply_invalid_mask"]
        )
        # Alternative for structured JSON logging
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

        if not multiburst and reference_id is None:
            raise ValueError(f"A reference_id must be provided when multiburst is False.")

        slcs_path = _get_slcs_path(working_directory_dynamic)
        do_import = not (recompute and multiburst)
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
        slcs, reference_id = finalize_import(
            slcs=slcs,
            working_directory=working_directory_static,
            recompute=recompute,
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

    @resumable_breakpoint(identifier="ReferenceDEM")
    def run(
        self,
        inputs,
        adfs,
        recompute=None,
        chunks_geo_grid=None,
        interpolation=None,
        spacing=None,
        epsg=None,
        **kwargs,
    ):
        slcs = inputs["slcs"]
        params = inputs["params"]
        multiburst = _is_multiburst(slcs)
        dem_type = get_adf(adfs["DEM"])
        params["dem"] = dem_type.path
        config = get_config(params["mode"])
        recompute = recompute if recompute is not None else config["Global"]["recompute"]
        chunks = chunks_geo_grid if chunks_geo_grid is not None else config["Global"]["chunks_geo_grid"]
        chunks = {k: v for k, v in chunks.items() if k in ["y", "x"]}
        method = interpolation if interpolation is not None else config["ReferenceDEM"]["interpolation"]
        spacing = spacing if spacing is not None else config["ReferenceDEM"]["spacing"]
        working_directory_static, _, storage_options_static = get_wdir_static(adfs)
        working_directory_dynamic, storage_options_dynamic = get_wdir_dynamic()
        crs = epsg
        params["chunks_geo_grid"] = json.dumps(chunks)
        params["dem:interpolation"] = method
        params["dem:spacing"] = json.dumps(spacing)
        params["dem:crs"] = crs
        logger.info(f"Processing parameters: {params}")
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

    @resumable_breakpoint(identifier="ReferenceGeometry")
    def run(
        self,
        inputs,
        adfs=None,
        recompute: bool | None = None,
        chunks_geo_grid: dict | None = None,
        enable_dask: bool = True,
        **kwargs: Any,
    ):
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
        adfs : Mapping[str, ADF] | None, optional
            Auxiliary Data Files mapping. Expected keys:

            - 'REFERENCE_DB': ADF for persistent storage.
        recompute : bool | None, optional
            Force recomputation of existing files. If None, uses config fallback.
        chunks_geo_grid : dict | None, optional
            Chunking configuration for the geo grid. If None, uses config fallback.
        **kwargs : Any
            Additional keyword arguments.

        Returns
        -------
        Mapping[str, xr.DataTree]
            A dictionary containing the reference simulation:

            - 'simulation_ref': The simulated master geometry (xr.DataTree).

        Raises
        -------
        ValueError
            If mandatory inputs ('slcs', 'dem') are missing.
        TypeError
            If inputs are not of type `xr.DataTree`.
        """
        slcs = inputs["slcs"]
        dem_tree = inputs["dem"]
        params = inputs["params"]
        # Load config and directories
        config = get_config(params["mode"])
        recompute = recompute if recompute is not None else config["Global"]["recompute"]
        working_directory_static, _, storage_options_static = get_wdir_static(adfs)
        working_directory_dynamic, storage_options_dynamic = get_wdir_dynamic()
        chunks = chunks_geo_grid if chunks_geo_grid is not None else config["Global"]["chunks_geo_grid"]
        chunks = {k: v for k, v in chunks.items() if k in ["y", "x"]}
        reference_id = params["reference_id"]
        logger.info(f"Processing parameters: {params}")
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

    @resumable_breakpoint(identifier="Coregistration")
    def run(
        self,
        inputs,
        adfs,
        initialize_current_reference: bool | None = None,
        do_image_matching: bool | None = None,
        do_split_spectrum: bool | None = None,
        recompute: bool | None = None,
        chunks_geo_grid: dict | None = None,
        do_flattening: bool | None = None,
        do_reramp: bool | None = None,
        warp_ov_fact: Any | None = None,
        warp_kernel_shape: Any | None = None,
        warp_kaiser_beta: Any | None = None,
        atbd_link: str | None = None,
        facility: str | None = None,
        product_timeliness: str | None = None,
        product_timeliness_category: str | None = None,
        product_data_access: str | None = None,
        product_pixel_coordinate_convention: str | None = None,
        product_family_specification: str | None = None,
        cslc_status: str | None = None,
        providers: str | None = None,
        enable_dask: bool = True,
        **kwargs: Any,
    ):
        """
        Coregisters the SLC stack to the reference geometry.
        """
        slcs = inputs["slcs"]
        dem_tree = inputs["dem"]
        simulation_tree = inputs["simulation_ref"]
        simulation_ml_tree = inputs["simulation_ml"]
        params = inputs["params"]

        # Load config, directories, and parameters
        config = get_config(params["mode"])

        initialize_current_reference = (
            initialize_current_reference
            if initialize_current_reference is not None
            else config["Coregistration"]["initialize_current_reference"]
        )

        if initialize_current_reference:
            do_image_matching = False
            do_split_spectrum = False
            logger.info(
                "initialize_current_reference is True: forcing do_image_matching and do_split_spectrum to False."
            )
        else:
            do_image_matching = (
                do_image_matching if do_image_matching is not None else config["Coregistration"]["do_image_matching"]
            )
            do_split_spectrum = (
                do_split_spectrum if do_split_spectrum is not None else config["Coregistration"]["do_split_spectrum"]
            )
        # ---------------------------------------------
        recompute = recompute if recompute is not None else config["Global"]["recompute"]
        working_directory_reference_db_static, working_directory_reference_db_dynamic, storage_options_static = (
            get_wdir_static(adfs)
        )
        working_directory_dynamic, storage_options_dynamic = get_wdir_dynamic()

        chunks = chunks_geo_grid if chunks_geo_grid is not None else config["Global"]["chunks_geo_grid"]
        chunks = {k: v for k, v in chunks.items() if k in ["y", "x"]}

        do_flattening = do_flattening if do_flattening is not None else config["Coregistration"]["do_flattening"]
        do_reramp = do_reramp if do_reramp is not None else config["Coregistration"]["do_reramp"]
        ov_fact = warp_ov_fact if warp_ov_fact is not None else config["Coregistration"]["warp_ov_fact"]
        kernel_shape = (
            warp_kernel_shape if warp_kernel_shape is not None else config["Coregistration"]["warp_kernel_shape"]
        )
        kaiser_beta = warp_kaiser_beta if warp_kaiser_beta is not None else config["Coregistration"]["warp_kaiser_beta"]
        reference_id = params["reference_id"]

        params["ov_fact"] = ov_fact
        params["kernel_shape"] = kernel_shape
        params["kaiser_beta"] = kaiser_beta

        metadata_args = {
            "atbd_link": atbd_link,
            "facility": facility,
            "product_timeliness": product_timeliness,
            "product_timeliness_category": product_timeliness_category,
            "product_data_access": product_data_access,
            "product_pixel_coordinate_convention": product_pixel_coordinate_convention,
            "product_family_specification": product_family_specification,
            "cslc_status": cslc_status,
            "providers": providers,
        }

        for name, val in metadata_args.items():
            params[name] = val if val is not None else config["Global"][name]

        logger.info(f"Processing parameters: {params}")

        # Orbit information extraction from the slcs structure
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
            )

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

        slcs = finalize_coreg(
            slcs=slcs,
            working_directory=working_directory_reference_db_dynamic,
            reference_id=reference_id,
            do_image_matching=do_image_matching,
            do_split_spectrum=do_split_spectrum,
        )

        if do_image_matching:
            disparity_map = stack_disparity(
                slcs,
                reference_id,
                enable_dask=enable_dask,
            )
            x_poly, y_poly = estimate_poly_matching(
                slcs,
                disparity_map,
                enable_dask=enable_dask,
            )
            logger.debug(f"x_poly: {x_poly}")
            logger.debug(f"y_poly: {y_poly}")
            if params["mode"] in ["IW", "EW", "SM"]:
                x_poly = None

            slcs = stack_coregistration(
                slcs,
                simulation_tree,
                simulation_ml_tree,
                reference_id,
                simulation_secondary_tree=simulation_secondary_tree,
                multiburst=multiburst,
                working_directory=f"{working_directory_dynamic}/image_matching",
                enable_dask=enable_dask,
                recompute=recompute,
                x_poly=x_poly,
                y_poly=y_poly,
            )

        # TODO: fix structure
        cslcs = slcs
        return {"cslcs": cslcs, "slcs": slcs}


class ReferenceUpdate(EOProcessingUnit):
    @classmethod
    def get_mandatory_input_list(cls, mode=None, **kwargs):
        return ["slcs", "params"]

    @classmethod
    def get_mandatory_adf_list(cls, mode=None, **kwargs):
        return ["REFERENCE_DB"]

    @resumable_breakpoint(identifier="ReferenceUpdate")
    def run(
        self,
        inputs,
        adfs,
        posting: dict | None = None,
        memory_param: float | None = None,
        coherence_threshold: float | None = None,
        stable_interval_days: int | None = None,
        max_past_stable_history: int | None = None,
        enable_dask: bool | None = None,
        **kwargs,
    ):
        slcs = inputs["slcs"]
        params = inputs["params"]
        config = get_config(params["mode"])
        working_directory_reference_db_static, working_directory_reference_db_dynamic, storage_options_static = (
            get_wdir_static(adfs)
        )
        working_directory_dynamic, storage_options_dynamic = get_wdir_dynamic()
        posting = posting if posting is not None else config["ReferenceUpdate"]["posting"]
        memory_param = memory_param if memory_param is not None else config["ReferenceUpdate"]["memory_param"]
        coherence_threshold = (
            coherence_threshold if coherence_threshold is not None else config["ReferenceUpdate"]["coherence_threshold"]
        )
        stable_interval_days = (
            stable_interval_days
            if stable_interval_days is not None
            else config["ReferenceUpdate"]["stable_interval_days"]
        )
        max_past_stable_history = (
            max_past_stable_history
            if max_past_stable_history is not None
            else config["ReferenceUpdate"]["max_past_stable_history"]
        )
        enable_dask = enable_dask if enable_dask is not None else True

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
        )

        logger.debug(f"stable_reference : {stable_references}")
        # TODO: write script to do this externally.
        from cop_ard_app.utils import update_reference_database

        update_reference_database(current_reference, stable_references, working_directory_reference_db_dynamic)
        return {
            "current_reference": current_reference,
            "stable_references": stable_references,
        }


class Geocoding(EOProcessingUnit):
    @classmethod
    def get_mandatory_input_list(cls, mode=None, **kwargs):
        return ["cmlp", "simulation", "simulation_ml"]

    @resumable_breakpoint(identifier="Geocoding")
    def run(
        self,
        inputs,
        adfs,
        mode,
        workdir_path_static,
        workdir_path_dynamic,
        spacing,
        enable_dask,
        **kwargs,
    ):
        cmlp = inputs["cmlp"]
        simulation = inputs["simulation"]
        simulation_ml = inputs["simulation_ml"]

        gmlp = stack_gmlp(
            cmlp_tree=cmlp,
            simulation_tree=simulation,
            simulation_ml_tree=simulation_ml,
            spacing=spacing,
            working_directory_tmp=workdir_path_dynamic,
            enable_dask=enable_dask,
        )
        return {"gmlp": gmlp}
