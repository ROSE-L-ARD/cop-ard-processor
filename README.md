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

The codebase is organized into core processing modules and formal verification test suites:

* `cop_ard_processor/`: Primary Python package source code.
  * `conf/`: Global settings (`CONFIG.json`) and output structures (`COP_ARD_CSLC.yaml`, `COP_ARD_GMLP.yaml`).
  * `processing_units.py`: Core pipeline execution units and workflow steps.
  * `structure.py`, `utils.py`, `breakpoints.py`: Structural definitions, helper functions, and breakpoint utilities.
* `tests/`: Comprehensive test suite.
  * `vt/launch/`: System Verification Test (VT) cases organized by test identifier (e.g., `TC-ST-FUNC-*`, `TC-ST-PERF-*`).

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
