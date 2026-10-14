<!--
  Copyright 2026 European Union

  Licensed under the Apache License, Version 2.0 (the "License");
  you may not use this file except in compliance with the License.
  You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

  Unless required by applicable law or agreed to in writing, software
  distributed under the License is distributed on an "AS IS" BASIS,
  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
  See the License for the specific language governing permissions and
  limitations under the License.
-->

# COP ARD PROCESSOR

The `cop-ard-processor` repository contains the core processing pipeline for generating Copernicus Analysis Ready Data (ARD) products.

## Project Structure

The repository separates the processing package, its operational tooling, and the formal verification suites:

* `cop_ard_processor/`: Primary Python package source code.
  * `conf/`: Global settings (`CONFIG.json`) and output structures (`COP_ARD_CSLC.yaml`, `COP_ARD_GMLP.yaml`).
  * `processing_units.py`: Core pipeline execution units and workflow steps.
  * `preflight.py`: Input-parameter validation for processing units (`UnitPreflight`, field rules, ETAD-stack and SAFE-XML validators); failures raise `PreflightError` with the collected violations.
  * `models/`: Serialized processing-unit contracts (`processor_contract()`) for `ingestion`, `coregistration`, `geocoding`, `reference_dem`, `reference_geometry`, `reference_update`, `cmlp`; validated against the EOPF model validator by `tests/unit/test_ingestion_model.py` and `tests/unit/test_downstream_models.py`.
  * `structure.py`, `utils.py`, `breakpoints.py`: Structural definitions, helper functions, and breakpoint utilities.
* `tests/`: Comprehensive test suite.
  * `unit/`: Lightweight unit tests (`test_*.py`).
  * `vt/launch/`: System Verification Test (VT) launch scripts, organized by test identifier (`TC-ST-DSGN-*`, `TC-ST-FUNC-*`, `TC-ST-PERF-*`, `TC-ST-RAMS-*`) and by named site dataset (`MURCIA`, `NAPLES_SAOCOM`, `NAPLES_SENTINEL`, `SURAT`, `SURAT_ESD`).
  * `vt/triggering/`: Trigger payloads (`<TC-ID>/<RUN>.yaml`) paired with the launch scripts.
* `scripts/`: Operational helpers.
  * `run_tests.sh`: VT orchestrator (see Test Execution Guidelines).
  * `run_payload.py`: Runs a single EOPF trigger payload through the workflow runner.
* `notebooks/`: CSLC, CSLC-ESD, geolocation, RIWS qualification and a third-party software comparison.
* `docs/`: Rendered qualification reports produced by `make docs`.
* `makefile`: Installation and quality-gate targets (see Development).
* `Dockerfile`, `docker-compose.yml`: Container build and Dask cluster deployment (see Deployment).

## Installation

Install the processor with:

```bash
make install
```

`make install` is the supported installation path. It runs the `proj` target first, then `uv sync`:

* **`proj`** — downloads the EGM2008 geoid grid (`us_nga_egm08_25.tif`) from the PROJ CDN into the `pyproj` data directory reported by the environment, then creates the `Und_min1x1_egm2008_isw=82_WGS84_TideFree` alias symlink required for geoid-corrected heights. The download is skipped when the grid file is already present, so the target is safe to re-run.
* **`uv sync`** — creates and updates the local `.venv` from `pyproject.toml` and the pinned `uv.lock`.

Prerequisites: [`uv`](https://docs.astral.sh/uv/) on `PATH` and network access for the grid download. The project requires Python 3.13 (`>=3.13, <3.14`); `uv` provisions it automatically.

Running `uv sync` directly is not sufficient: it skips the PROJ grid setup, and geoid-dependent processing fails at runtime.

## Dependency Management

Dependencies and environment builds are managed via `uv`:

* `pyproject.toml`: Project metadata, build rules, and package dependencies.
* `uv.lock`: Deterministic lockfile containing pinned versions for all dependencies.

## Test Execution Guidelines

### Standard Unit & Integration Tests

Lightweight unit and integration tests can be run locally using `pytest`:

```bash
uv run --extra tests pytest tests/
```

### End-to-End & Verification Tests (`tests/vt/launch/`)

> **Execution Platform Note:** End-to-End (E2E) and System Verification Tests are **not** intended to run on automated CI/CD pipelines due to heavy resource, storage, and execution constraints. Whenever possible, these tests must be executed directly on the **CEMPAL** infrastructure (or an equivalent dedicated processing environment).

#### Mandatory Sequential Execution Order

When executing verification test suites under `tests/vt/launch/<TC-ID>/`, tests **must strictly follow a two-step chronological ordering**:

1. **`REFERENCE.py` Initialization:** Must always be executed **first** to build the required reference database.
2. **Chronological Acquisition Runs:** Date-named acquisition scripts (`YYYY-MM-DD.py`) must be executed in **strict chronological order**.

*Example sequence for `TC-ST-FUNC-003`:*

```bash
# 1. Initialize reference baseline (ALWAYS FIRST)
uv run --extra tests pytest tests/vt/launch/TC-ST-FUNC-003/REFERENCE.py

# 2. Process acquisitions in chronological order
uv run --extra tests pytest tests/vt/launch/TC-ST-FUNC-003/2023-07-27.py
uv run --extra tests pytest tests/vt/launch/TC-ST-FUNC-003/2023-08-08.py
```

#### Recommended Runner: `scripts/run_tests.sh`

`scripts/run_tests.sh <TC-ID>` enforces the ordering above automatically: it queues `REFERENCE.py` first, then every remaining launch script in chronological order, runs each through `uv run --extra tests pytest -s -vv -m ard`, and writes per-run logs to `logs/<TC-ID>/<RUN>.log`. Before each run it stages the trigger's input datasets from S3 with `rclone` into `/data/scratch/external/DATA-ROSEL`, using the payload under `tests/vt/triggering/<TC-ID>/<RUN>.yaml`.

```bash
bash scripts/run_tests.sh TC-ST-FUNC-003                  # REFERENCE first, then all acquisitions
bash scripts/run_tests.sh TC-ST-FUNC-009 --skip-reference # reuse the existing reference database
```

The manual sequence above remains valid when a single acquisition must be re-run in isolation.

#### Multi-User & System Isolation Verification (`TC-ST-FUNC-014`)

Test cases validating multi-tenant isolation, distinct OS user privileges (`user_alpha`, `user_beta`), and concurrent execution bypass standard `pytest` invocations and must be launched using their dedicated Bash orchestrator:

```bash
sudo -E bash tests/vt/launch/TC-ST-FUNC-014/run.sh
```

## Updating Dependencies

To update workflow dependencies (such as `cop-ard-app`) in `uv.lock`:

```bash
uv lock -P cop-ard-app
```

To sync your local virtual environment with the lockfile:

```bash
uv sync --extra tests
```

## Development

Quality gates are driven by the `makefile`. The gate targets (`format`, `lint`, `security`, `complexity`, `typecheck`) run through `uv run --frozen` with the matching optional-dependency extra, so `make install` must have run first; `docs` requires a standalone Quarto install.

| Target | Action |
| --- | --- |
| `make all` | Default goal: `format`, then `check` |
| `make format` | `ruff format` over `cop_ard_processor/`, `tests/`, `notebooks/`, plus import sorting (`ruff check --select I --fix`) |
| `make lint` | `ruff check` over `cop_ard_processor/` and `tests/` |
| `make security` | `bandit` over `cop_ard_processor/` using `bandit.yml` |
| `make complexity` | `xenon` report over `cop_ard_processor/` (`--max-average b --max-modules c --max-absolute d`) |
| `make typecheck` | `mypy` over `cop_ard_processor/` (`--allow-redefinition`) |
| `make check` | `lint` + `security` + `complexity` + `typecheck` |
| `make docs` | `quarto render` of the four rendered qualification notebooks (comparison, CSLC, CSLC-ESD, geolocation) into `docs/` |
| `make cslc-qualification-notebook` | Builds the separate Python 3.12 `.venv-qual` environment from `notebooks/cslc_qualification.txt` |

## Deployment

`Dockerfile` is a two-stage build: a `uv` builder stage syncs the pinned environment with `--extra cluster-plugin` (private GitHub dependencies are fetched over HTTPS using a BuildKit `secret` mount of `.netrc`) and downloads the PROJ geoid grid into the image's `pyproj` data directory; a `python:3.13-slim` runtime stage installs the C libraries required by the EOPF readers, copies the built tree with `.venv` prepended to `PATH`, and drops privileges to a non-root `appuser` (uid/gid `10001`).

```bash
docker build --secret id=netrc,src="$HOME/.netrc" -t localhost/cop-ard-processor:latest .
```

`docker-compose.yml` runs the processing cluster on that image: a `dask scheduler` service (scheduler port `8786` and dashboard `8787` bound to `127.0.0.1`, TCP healthcheck) plus `worker-1` / `worker-2` (`dask worker tcp://scheduler:8786 --nworkers 4 --nthreads 1 --memory-limit 8GB`, `32G` memory and `4` CPU limits, `shm_size: 2gb` for GDAL/PyArrow workloads), all running as `user: root` and mounting the Sentinel-1 input volume (`/data/SENTINEL1/SURAT`).
