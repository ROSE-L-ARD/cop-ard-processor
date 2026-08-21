from cop_ard_app.area_projection import stack_geometry, stack_mask
import cop_ard_processor
import os

import xarray as xr
from cop_ard_app.cmlp import stack_cmlp
from cop_ard_app.coreg import stack_coregistration
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


def _get_slcs_path(
    workdir_path_static: str,
    workdir_path_dynamic: str,
):
    return os.path.join(workdir_path_dynamic, "slcs")


def _is_multiburst(slcs: xr.DataTree):
    return "bursts" in slcs


def _do_recompute(mode: str):
    return mode == "reference"


def _get_calibration_type(calibration_type: str, mode: str):
    return "BetaZero" if mode == "reference" else calibration_type


class ImportSLC(EOProcessingUnit):
    PROCESSOR_NAME = "CopArd"
    PROCESSOR_VERSION = cop_ard_processor.__version__
    @resumable_breakpoint(identifier="ImportSLC")
    def run(
        self,
        inputs,
        adfs,
        mode,
        input_safe_files,
        calibration_type,
        multiburst,
        indices,
        workdir_path_static,
        workdir_path_dynamic,
        enable_dask,
        chunks,
        **kwargs,
    ):
        slcs_path = _get_slcs_path(workdir_path_static, workdir_path_dynamic)
        recompute = _do_recompute(mode)
        do_import = not (recompute and multiburst)

        slcs = import_slc_stack(
            input_safe_files,
            slcs_path,
            deramp=True,
            multiburst=multiburst,
            noise_removal_flag=True,
            apply_invalid_mask_flag=True,
            indices=indices,
            calib_type=calibration_type,
            chunks=chunks,
            do_import=do_import,
            enable_dask=enable_dask,
        )

        return {"slcs": slcs}


class ImportOrbits(EOProcessingUnit):
    PROCESSOR_NAME = "CopArd"
    PROCESSOR_VERSION = cop_ard_processor.__version__
    @classmethod
    def get_mandatory_input_list(cls, mode=None, **kwargs):
        return ["slcs"]

    @resumable_breakpoint(identifier="ImportOrbits")
    def run(
        self,
        inputs,
        adfs,
        mode,
        orbit_files,
        workdir_path_static,
        workdir_path_dynamic,
        enable_dask,
        **kwargs,
    ):
        slcs = inputs["slcs"]
        multiburst = _is_multiburst(slcs)
        acquisition_dates = extract_acquisition_dates(slcs, multiburst)

        slcs, used_orbits = import_ORBIT(
            slcs,
            acquisition_dates,
            orbit_files,
            multiburst=multiburst,
            wdir_path=workdir_path_dynamic,
            enable_dask=enable_dask,
        )

        return {"slcs": slcs}


class ImportETAD(EOProcessingUnit):
    PROCESSOR_NAME = "CopArd"
    PROCESSOR_VERSION = cop_ard_processor.__version__
    @classmethod
    def get_mandatory_input_list(cls, mode=None, **kwargs):
        return ["slcs"]

    @resumable_breakpoint(identifier="ImportETAD")
    def run(
        self,
        inputs,
        adfs,
        mode,
        etad_files,
        workdir_path_static,
        workdir_path_dynamic,
        enable_dask,
        **kwargs,
    ):
        slcs = inputs["slcs"]
        multiburst = _is_multiburst(slcs)
        acquisition_dates = extract_acquisition_dates(slcs, multiburst)

        slcs, used_etad = import_ETAD(
            slcs,
            acquisition_dates,
            etad_files,
            multiburst=multiburst,
            wdir_path=workdir_path_dynamic,
            enable_dask=enable_dask,
        )

        return {"slcs": slcs}


class FinalizeImport(EOProcessingUnit):
    PROCESSOR_NAME = "CopArd"
    PROCESSOR_VERSION = cop_ard_processor.__version__
    @classmethod
    def get_mandatory_input_list(cls, mode=None, **kwargs):
        return ["slcs"]

    @resumable_breakpoint(identifier="FinalizeImport")
    def run(
        self,
        inputs,
        adfs,
        mode,
        etad_files,
        workdir_path_static,
        workdir_path_dynamic,
        enable_dask,
        **kwargs,
    ):
        slcs = inputs["slcs"]
        recompute = _do_recompute(mode)

        slcs, reference_id = finalize_import(
            slcs=slcs,
            working_directory=workdir_path_static,
            recompute=recompute,
            enable_dask=enable_dask,
        )
        slcs.attrs["ref_id"] = reference_id  # monkey patch: maybe fetch as a cop-ard-app util function?
        return {"slcs": slcs}


class ImportDEM(EOProcessingUnit):
    PROCESSOR_NAME = "CopArd"
    PROCESSOR_VERSION = cop_ard_processor.__version__
    @classmethod
    def get_mandatory_input_list(cls, mode=None, **kwargs):
        return ["slcs"]

    @classmethod
    def get_mandatory_adf_list(cls, mode=None, **kwargs):
        return ["dem"]

    @resumable_breakpoint(identifier="ImportDEM")
    def run(
        self,
        inputs,
        adfs,
        mode,
        workdir_path_static,
        workdir_path_dynamic,
        spacing,
        enable_dask,
        chunks,
        **kwargs,
    ):
        slcs = inputs["slcs"]
        multiburst = _is_multiburst(slcs)

        bbox = get_bbox(slcs)
        dem_selection = kwargs.get(
            "dem",  # override other options
            "Copernicus" if mode == "reference" else adfs["dem"]
        )
        dem_path = dem_selection.path if hasattr(dem_selection, "path") else dem_selection
        dem_storage_options = dem_selection.adf_params if hasattr(dem_selection, "adf_params") else None


        dem = import_dem(
            bbox=bbox,
            dem_type=dem_path,
            working_dir=workdir_path_dynamic,
            chunks=chunks,
            storage_options_dem=dem_storage_options,
        )
        dem = stack_dem(
            dem,
            slcs,
            multiburst,
            spacing,
            workdir_path_static,
            workdir_path_dynamic,
            chunks,
            enable_dask=enable_dask,
        )

        return {"dem": dem}


class Simulation(EOProcessingUnit):
    PROCESSOR_NAME = "CopArd"
    PROCESSOR_VERSION = cop_ard_processor.__version__
    @classmethod
    def get_mandatory_input_list(cls, mode=None, **kwargs):
        return ["slcs", "dem"]

    @resumable_breakpoint(identifier="Simulation")
    def run(
        self,
        inputs,
        adfs,
        mode,
        workdir_path_static,
        workdir_path_dynamic,
        enable_dask,
        chunks,
        **kwargs,
    ):
        slcs = inputs["slcs"]
        dem = inputs["dem"]
        recompute = _do_recompute(mode)
        reference_id = slcs.attrs["ref_id"]
        multiburst = _is_multiburst(slcs)

        orbits = extract_orbits(
            slcs,
            multiburst=multiburst,
            acquisition_id=[reference_id],
        )

        simulation_tree = stack_simulation(
            dem_tree=dem,
            orbits=orbits,
            multiburst=multiburst,
            working_directory=workdir_path_static,
            chunks={**chunks, "acquisition_date": 1},
            master_image=True,
            recompute=recompute,
            enable_dask=enable_dask,
        )

        simulation_ml_tree = stack_simulation_ml(
            simulation_tree=simulation_tree,
            reference_id=reference_id,
            multiburst=multiburst,
            working_directory=workdir_path_static,
            recompute=recompute,
            enable_dask=enable_dask,
        )
        simulation_secondary = None
        if mode == "secondary":
            sec_orbits = extract_orbits(
                slcs,
                multiburst=multiburst,
            )
            sec_orbits = sec_orbits.where(sec_orbits.acquisition_id != reference_id, drop=True)
            if sec_orbits.size != 0:
                simulation_secondary = stack_simulation(
                    dem_tree=dem,
                    orbits=sec_orbits,
                    multiburst=multiburst,
                    working_directory=workdir_path_dynamic,
                    chunks={**chunks, "acquisition_date": 1},
                    master_image=False,
                    recompute=recompute,
                    enable_dask=enable_dask,
                )

        return {
            "simulation": simulation_tree,
            "simulation_ml": simulation_ml_tree,
            "simulation_secondary": simulation_secondary,
        }


class Geometry(EOProcessingUnit):
    PROCESSOR_NAME = "CopArd"
    PROCESSOR_VERSION = cop_ard_processor.__version__
    @classmethod
    def get_mandatory_input_list(cls, mode=None, **kwargs):
        return ["slcs", "simulation", "simulation_ml"]

    @resumable_breakpoint(identifier="Geometry")
    def run(
        self,
        inputs,
        adfs,
        mode,
        workdir_path_static,
        workdir_path_dynamic,
        chunks,
        enable_dask,
        **kwargs,
    ):
        slcs = inputs["slcs"]
        multiburst = _is_multiburst(slcs)
        reference_id = slcs.attrs["ref_id"]
        simulation = inputs["simulation"]
        simulation_ml = inputs["simulation_ml"]
        recompute = _do_recompute(mode)
        
        chunk_length = max([v for v in chunks.values()])

        stack_geometry(  # side effects, no output but static dir modified
            slcs,
            simulation,
            simulation_ml,
            working_directory_static=workdir_path_static,
            working_directory_tmp=workdir_path_dynamic,
            reference_id=reference_id,
            chunk_length=chunk_length,
            recompute=recompute,
            multiburst=multiburst,
            enable_dask=enable_dask,
        )

        return {"slcs": slcs}  # maybe not needed? likely required for stop/resume breakpoints


class Mask(EOProcessingUnit):
    PROCESSOR_NAME = "CopArd"
    PROCESSOR_VERSION = cop_ard_processor.__version__
    @classmethod
    def get_mandatory_input_list(cls, mode=None, **kwargs):
        return ["slcs", "simulation", "simulation_ml"]

    @resumable_breakpoint(identifier="Mask")
    def run(
        self,
        inputs,
        adfs,
        mode,
        workdir_path_static,
        workdir_path_dynamic,
        enable_dask,
        **kwargs,
    ):
        slcs = inputs["slcs"]
        multiburst = _is_multiburst(slcs)
        reference_id = slcs.attrs["ref_id"]
        simulation = inputs["simulation"]
        simulation_ml = inputs["simulation_ml"]
        recompute = _do_recompute(mode)
        
        stack_mask(
            slcs,
            simulation,
            simulation_ml,
            working_directory_static=workdir_path_static,
            reference_id=reference_id,
            recompute=recompute,
            multiburst=multiburst,
            enable_dask=enable_dask,
        )

        return {"slcs": slcs}  # maybe not needed? likely required for stop/resume breakpoints


class Coregistration(EOProcessingUnit):
    PROCESSOR_NAME = "CopArd"
    PROCESSOR_VERSION = cop_ard_processor.__version__
    @classmethod
    def get_mandatory_input_list(cls, mode=None, **kwargs):
        return ["slcs", "simulation", "simulation_ml"]

    @resumable_breakpoint(identifier="Coregistration")
    def run(
        self,
        inputs,
        adfs,
        mode,
        workdir_path_static,
        workdir_path_dynamic,
        enable_dask,
        **kwargs,
    ):
        slcs = inputs["slcs"]
        simulation = inputs["simulation"]
        simulation_ml = inputs["simulation_ml"]
        simulation_secondary = inputs.get("simulation_secondary")
        reference_id = slcs.attrs["ref_id"]
        multiburst = _is_multiburst(slcs)
        recompute = _do_recompute(mode)

        slcs = stack_coregistration(
            slcs,
            simulation,
            simulation_ml,
            reference_id,
            simulation_secondary_tree=simulation_secondary,
            multiburst=multiburst,
            working_directory=workdir_path_dynamic,
            enable_dask=enable_dask,
            recompute=recompute,
        )

        return {"slcs": slcs}


class ImageMatching(EOProcessingUnit):
    PROCESSOR_NAME = "CopArd"
    PROCESSOR_VERSION = cop_ard_processor.__version__
    @classmethod
    def get_mandatory_input_list(cls, mode=None, **kwargs):
        return ["slcs"]

    @resumable_breakpoint(identifier="ImageMatching")
    def run(
        self,
        inputs,
        adfs,
        mode,
        workdir_path_static,
        workdir_path_dynamic,
        enable_dask,
        **kwargs,
    ):
        slcs = inputs["slcs"]
        reference_id = slcs.attrs["ref_id"]

        disparity_map = stack_disparity(
            slcs,
            reference_id,
            enable_dask=enable_dask,
        )
        _ = estimate_poly_matching(
            slcs,
            disparity_map,
            enable_dask=False,
        )

        return {
            "slcs": slcs,  # currently broken? values overwritten on disk at hardcoded location
            "disparity_map": disparity_map,
        }


class CMLP(EOProcessingUnit):
    PROCESSOR_NAME = "CopArd"
    PROCESSOR_VERSION = cop_ard_processor.__version__
    @classmethod
    def get_mandatory_input_list(cls, mode=None, **kwargs):
        return ["slcs"]

    @resumable_breakpoint(identifier="CMLP")
    def run(
        self,
        inputs,
        adfs,
        mode,
        workdir_path_static,
        workdir_path_dynamic,
        posting,
        stable_interval_days,
        reference_database_path,
        enable_dask,
        **kwargs,
    ):
        slcs = inputs["slcs"]

        cmlp, current_reference, stable_references = stack_cmlp(
            slcs=slcs,
            posting=posting,
            working_directory=workdir_path_dynamic,
            stable_interval_days=stable_interval_days,
            reference_database_path=reference_database_path,
            enable_dask=enable_dask,
        )

        return {
            "cmlp": cmlp,
            "current_reference": current_reference,
            "stable_references": stable_references,
        }


class GMLP(EOProcessingUnit):
    PROCESSOR_NAME = "CopArd"
    PROCESSOR_VERSION = cop_ard_processor.__version__
    @classmethod
    def get_mandatory_input_list(cls, mode=None, **kwargs):
        return ["cmlp", "simulation", "simulation_ml"]

    @resumable_breakpoint(identifier="GMLP")
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
