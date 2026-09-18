#!/bin/bash
# ==============================================================================
# Script: run.sh
# Objective: Orchestrate TC-ST-FUNC-014 (Concurrent & Multi-User Isolation Test)
# Target: cop-ard-processor (ESA Verification Suite)
#
# Requirements Validated:
#   APP-FUNC-038, APP-FUNC-039, APP-OPER-004 to APP-OPER-008, APP-DSGN-042.
#
# Execution Note:
#   Must be run with administrative privileges (sudo) from the repository root.
# ==============================================================================

set -e

# Superuser privileges are required to create OS users and manage workspace permissions
if [ "$EUID" -ne 0 ]; then
  echo "ERROR: Administrative privileges required. Please run this script with sudo."
  exit 1
fi

echo "=========================================================================="
echo " Starting TC-ST-FUNC-014: Concurrent & Isolation Execution               "
echo "=========================================================================="

# ------------------------------------------------------------------------------
# 0. GLOBAL TOOLING & LOCAL ENVIRONMENT PREPARATION
# ------------------------------------------------------------------------------
# Verify that the 'uv' package manager is globally accessible to all system users
echo "[0/6] Ensuring 'uv' is available globally and synchronizing local environment..."

UV_EXEC="/usr/local/bin/uv"

if [ ! -f "$UV_EXEC" ]; then
    echo "      Downloading and installing standalone 'uv' binary to /usr/local/bin..."
    curl -sSfL https://astral.sh/uv/install.sh | env UV_INSTALL_DIR="/usr/local/bin" sh
    chmod 755 "$UV_EXEC"
else
    echo "      Found active 'uv' executable at $UV_EXEC"
fi

CURRENT_DIR=$(pwd)
OUTPUT_BASE="/data/TC-ST-FUNC-014"
TEMPLATE_YAML="$CURRENT_DIR/tests/vt/triggering/TC-ST-FUNC-014/2023-08-08.yaml"

if [ ! -f "$TEMPLATE_YAML" ]; then
    echo "ERROR: Invalid context. Template trigger not found at $TEMPLATE_YAML."
    echo "Please execute this script strictly from the root of cop-ard-processor repository."
    exit 1
fi

# Synchronize local repository dependencies to guarantee the build environment is up to date
echo "      Synchronizing workspace dependencies via 'uv sync --extra tests'..."
$UV_EXEC sync --extra tests

# Adjust directory traversal permissions for the base Python interpreter.
# 'uv' creates virtual environment symlinks pointing to host-managed Python binaries
# (e.g., in /root/.local/share/uv/). Dummy test users need execute permissions along
# the path to resolve and execute the interpreter binaries properly.
echo "      Configuring directory traversal permissions for underlying Python installation..."
REAL_PYTHON=$(readlink -f "$CURRENT_DIR/.venv/bin/python")
PYTHON_INSTALL_DIR=$(dirname $(dirname "$REAL_PYTHON"))

chmod -R a+rX "$PYTHON_INSTALL_DIR" 2>/dev/null || true

DIR=$(dirname "$REAL_PYTHON")
while [ "$DIR" != "/" ]; do
    chmod a+rx "$DIR" 2>/dev/null || true
    DIR=$(dirname "$DIR")
done

# ------------------------------------------------------------------------------
# 1. OS-LEVEL USER PROVISIONING
# ------------------------------------------------------------------------------
# Create distinct unprivileged system users to test process and permission isolation
echo "[1/6] Provisioning test OS users (user_alpha, user_beta)..."
id -u user_alpha &>/dev/null || useradd -m user_alpha
id -u user_beta &>/dev/null || useradd -m user_beta

# ------------------------------------------------------------------------------
# 2. WORKSPACE INITIALIZATION & APP INSTALLATIONS
# ------------------------------------------------------------------------------
# Clean previous execution outputs and set up isolated execution workspace
echo "[2/6] Initializing isolated workspace at $OUTPUT_BASE..."
rm -rf "$OUTPUT_BASE"
mkdir -p "$OUTPUT_BASE"/apps \
         "$OUTPUT_BASE"/outputs/app_v1 \
         "$OUTPUT_BASE"/outputs/app_v2 \
         "$OUTPUT_BASE"/outputs/app_v3 \
         "$OUTPUT_BASE"/logs \
         "$OUTPUT_BASE"/triggers

echo "      Deploying 3 independent app installations (including pre-built .venv)..."
for i in 1 2 3; do
    INSTALL_DIR="$OUTPUT_BASE/apps/app_v${i}"
    mkdir -p "$INSTALL_DIR"
    
    # Copy source code and sync'd .venv to eliminate git operations or SSH authentication at runtime
    rsync -av --exclude '.git' --exclude '__pycache__' "$CURRENT_DIR"/ "$INSTALL_DIR"/ > /dev/null
done

# Mutate installation-level default chunking configurations directly in CONFIG.json.
# We use an inline Python snippet to safely traverse and modify multi-line nested JSON structures.
echo "      Injecting distinct installation-level chunking defaults into CONFIG.json..."
python3 - <<EOF
import json

def update_chunks(config_path, chunk_size):
    with open(config_path, "r") as f:
        data = json.load(f)
    
    def walk(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k == "chunks_radar_grid" and isinstance(v, dict):
                    v["azimuth_time"] = chunk_size
                    v["slant_range_time"] = chunk_size
                else:
                    walk(v)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    walk(data)
    with open(config_path, "w") as f:
        json.dump(data, f, indent=2)

update_chunks("$OUTPUT_BASE/apps/app_v1/cop_ard_processor/conf/CONFIG.json", 1000)
update_chunks("$OUTPUT_BASE/apps/app_v2/cop_ard_processor/conf/CONFIG.json", 2000)
update_chunks("$OUTPUT_BASE/apps/app_v3/cop_ard_processor/conf/CONFIG.json", 3000)
EOF

# Grant read, write, and execute rights across the workspace to unprivileged test users
chmod -R 777 "$OUTPUT_BASE"

# ------------------------------------------------------------------------------
# 3. DYNAMIC JOB ORDER GENERATION
# ------------------------------------------------------------------------------
# Construct runtime trigger files. Requirement APP-FUNC-038/039 mandates that Job Orders
# strictly omit chunking options, forcing the application to use installation-level defaults.
echo "[3/6] Generating runtime Job Order YAMLs from template 2023-08-08.yaml..."

python3 - <<EOF
import yaml
import copy

with open("$TEMPLATE_YAML", "r") as f:
    base = yaml.safe_load(f)

def strip_chunks(data):
    """Recursively purge explicit chunking settings to force fallback to installation defaults."""
    if isinstance(data, dict):
        data.pop("chunks_radar_grid", None)
        for v in data.values():
            strip_chunks(v)
    elif isinstance(data, list):
        for item in data:
            strip_chunks(item)

def build_trigger(instance_id, polarizations, output_path, tmp_folder):
    cfg = copy.deepcopy(base)
    strip_chunks(cfg)
    
    # Assign isolated temporary working folders to prevent concurrent write collisions
    cfg["general_configuration"]["temporary__folder"] = tmp_folder
    
    # Update target output Zarr store locations
    for prod in cfg.get("io", {}).get("output_products", []):
        if prod.get("id") == "cslcs":
            prod["path"] = output_path
            
    # Set requested polarization parameters across ingestion/coregistration steps
    for step in cfg.get("workflow", []):
        if step.get("name") in ["ingestion", "coregistration"]:
            params = step.setdefault("parameters", {})
            if polarizations:
                params["polarization"] = polarizations
            else:
                params.pop("polarization", None)
                
    out_file = f"$OUTPUT_BASE/triggers/jo_v{instance_id}.yaml"
    with open(out_file, "w") as f:
        yaml.dump(cfg, f, default_flow_style=False)

# Instance 1 (user_alpha): Process dual-pol (VV + VH)
build_trigger(1, ["VV", "VH"], "$OUTPUT_BASE/outputs/app_v1/CSLC/cslcs_2023-08-08.zarr", "$OUTPUT_BASE/tmp_v1")

# Instance 2 (user_alpha): Process single-pol (VV)
build_trigger(2, ["VV"], "$OUTPUT_BASE/outputs/app_v2/CSLC/cslcs_2023-08-08.zarr", "$OUTPUT_BASE/tmp_v2")

# Instance 3 (user_beta): Process single-pol (VH)
build_trigger(3, ["VH"], "$OUTPUT_BASE/outputs/app_v3/CSLC/cslcs_2023-08-08.zarr", "$OUTPUT_BASE/tmp_v3")
EOF

chmod -R 777 "$OUTPUT_BASE"

JO_V1="$OUTPUT_BASE/triggers/jo_v1.yaml"
JO_V2="$OUTPUT_BASE/triggers/jo_v2.yaml"
JO_V3="$OUTPUT_BASE/triggers/jo_v3.yaml"

# ------------------------------------------------------------------------------
# 4. CONCURRENT UNATTENDED PROCESS EXECUTION
# ------------------------------------------------------------------------------
# Launch 3 simultaneous processor runs across 2 separate OS users in the background.
# '--no-sync' forces 'uv' to execute directly against the copied virtual environments.
echo "[4/6] Spawning 3 concurrent background runs via EOPF CLI..."
set +e

# Run Instance 1: Executed by user_alpha (VV + VH, Target chunking: 1000)
sudo -u user_alpha bash -c "cd $OUTPUT_BASE/apps/app_v1 && PYTHONPATH=. $UV_EXEC run --no-sync eopf trigger local $JO_V1" > "$OUTPUT_BASE"/logs/app_v1.log 2>&1 &
PID1=$!

# Run Instance 2: Executed concurrently by user_alpha (VV, Target chunking: 2000)
sudo -u user_alpha bash -c "cd $OUTPUT_BASE/apps/app_v2 && PYTHONPATH=. $UV_EXEC run --no-sync eopf trigger local $JO_V2" > "$OUTPUT_BASE"/logs/app_v2.log 2>&1 &
PID2=$!

# Run Instance 3: Executed concurrently by user_beta (VH, Target chunking: 3000)
sudo -u user_beta bash -c "cd $OUTPUT_BASE/apps/app_v3 && PYTHONPATH=. $UV_EXEC run --no-sync eopf trigger local $JO_V3" > "$OUTPUT_BASE"/logs/app_v3.log 2>&1 &
PID3=$!

echo "      Execution initialized. Tracking active process PIDs: ($PID1, $PID2, $PID3)..."

# Wait for all background instances to complete and collect exit status codes
wait $PID1; EXIT1=$?
wait $PID2; EXIT2=$?
wait $PID3; EXIT3=$?

echo "      Execution finished. Process exit status -> app_v1: $EXIT1, app_v2: $EXIT2, app_v3: $EXIT3"

# Verify that all concurrent processes completed successfully
if [ $EXIT1 -ne 0 ] || [ $EXIT2 -ne 0 ] || [ $EXIT3 -ne 0 ]; then
    echo "ERROR: One or more concurrent processor instances failed. Inspect logs at $OUTPUT_BASE/logs/"
    exit 1
fi

set -e

# ------------------------------------------------------------------------------
# 5. FORMAL VALIDATION PHASE (PYTEST)
# ------------------------------------------------------------------------------
# Run Pytest suite to validate Zarr dataset outputs, internal chunking, and execution logs
echo "[5/6] Running Pytest verification script..."
cd "$CURRENT_DIR"
PYTHONPATH=. $UV_EXEC run --no-sync pytest tests/vt/launch/TC-ST-FUNC-014/validate.py -v --tb=short

# ------------------------------------------------------------------------------
# 6. TEARDOWN & CLEANUP
# ------------------------------------------------------------------------------
# Remove temporary test OS users and clean test workspace
echo "[6/6] Cleaning up test OS users and removing workspace artifacts..."
userdel -r user_alpha || true
userdel -r user_beta || true
rm -rf "$OUTPUT_BASE"

echo "=========================================================================="
echo " TC-ST-FUNC-014 Execution and Formal Verification Passed Successfully!    "
echo "=========================================================================="
