#!/usr/bin/env bash

# Exit immediately if an uninitialized variable is accessed
set -u

# ==============================================================================
# SCRIPT PARAMETERS & SETUP
# ==============================================================================

# Read the target test folder name from the first command-line argument
TEST_NAME="${1:-}"

# Check if the test name argument was supplied
if [ -z "$TEST_NAME" ]; then
    echo "Usage: $0 <TEST_NAME>"
    echo "Example: $0 TC-ST-FUNC-009"
    exit 1
fi

# Set up directory paths relative to the project root
LAUNCH_DIR="tests/vt/launch/$TEST_NAME"
TRIGGER_DIR="tests/vt/triggering/$TEST_NAME"
LOG_DIR="logs/$TEST_NAME"
INPUT_DATA_DIR="/scratch/external/DATA-ROSEL"

# Validate that the requested test folder exists in the launch directory
if [ ! -d "$LAUNCH_DIR" ]; then
    echo "Error: Launch folder '$LAUNCH_DIR' does not exist."
    exit 1
fi

# Ensure output log directory and input scratch directory exist
mkdir -p "$LOG_DIR"
mkdir -p "$INPUT_DATA_DIR"

# ==============================================================================
# 1. BUILD ORDERED TEST LIST
# Requirement: Run REFERENCE first, then remaining tests in chronological order
# ==============================================================================

TEST_LIST=()

# Always append 'REFERENCE' first if REFERENCE.py exists in the launch folder
if [ -f "$LAUNCH_DIR/REFERENCE.py" ]; then
    TEST_LIST+=("REFERENCE")
fi

# Find all remaining .py test scripts, sort them alphabetically/chronologically,
# and append them to the test queue (excluding REFERENCE to avoid duplicate runs)
while IFS= read -r file; do
    [ -z "$file" ] && continue
    base=$(basename "$file" .py)
    if [ "$base" != "REFERENCE" ]; then
        TEST_LIST+=("$base")
    fi
done < <(find "$LAUNCH_DIR" -maxdepth 1 -name "*.py" | sort)

# Fail early if no Python test files were detected
if [ ${#TEST_LIST[@]} -eq 0 ]; then
    echo "Error: No .py test files found in $LAUNCH_DIR"
    exit 1
fi

echo "=========================================="
echo "Running test suite for: $TEST_NAME"
echo "Execution queue (via pytest & uv run):"
for t in "${TEST_LIST[@]}"; do
    echo "  - $t"
done
echo "=========================================="

# ==============================================================================
# 2. MAIN TEST EXECUTION LOOP
# Process each test file sequentially
# ==============================================================================

for TEST_BASE in "${TEST_LIST[@]}"; do
    PY_FILE="$LAUNCH_DIR/${TEST_BASE}.py"
    YAML_FILE="$TRIGGER_DIR/${TEST_BASE}.yaml"
    LOG_FILE="$LOG_DIR/${TEST_BASE}.log"

    echo ""
    echo ">>> Starting execution: $TEST_BASE"

    PARSE_OUT=""
    HAS_CHANGES="False"

    if [ -f "$YAML_FILE" ]; then
        # Create a backup copy of the original YAML config file
        cp "$YAML_FILE" "${YAML_FILE}.bak"

        # Execute inline Python script via 'uv run' to parse YAML paths:
        # 1. Checks if any input/output path actually starts with s3://
        # 2. ONLY modifies keys that exist and contain s3:// URIs
        # 3. ONLY overwrites the YAML file if changes were required
        # 4. Outputs JSON metadata with 'has_changes' boolean
        PARSE_OUT=$(uv run python - "$YAML_FILE" "$INPUT_DATA_DIR" << 'EOF'
import sys, os, yaml, json

yaml_path = sys.argv[1]
input_base_dir = sys.argv[2]

def s3_to_rclone(s3_url):
    """Converts s3://bucket/path into bucket:bucket/path for rclone remote syntax."""
    if isinstance(s3_url, str) and s3_url.startswith('s3://'):
        path_without_scheme = s3_url[5:]
        parts = path_without_scheme.split('/', 1)
        bucket = parts[0]
        return f"{bucket}:{path_without_scheme}"
    return s3_url

# Load YAML configuration content safely
with open(yaml_path, 'r') as f:
    data = yaml.safe_load(f)

gen_config = data.get('general_configuration', {})
tmp_folder = gen_config.get('temporary__folder', '/scratch/external/TMP')

inputs_map = []
outputs_map = []
has_changes = False

# --------------------------------------------------------------------------
# Process Input SLCS files
# Rewrite S3 sources to local destination ONLY if input_slcs_files exists
# and actually contains s3:// paths
# --------------------------------------------------------------------------
workflow = data.get('workflow', [])
if isinstance(workflow, list):
    for step in workflow:
        if isinstance(step, dict):
            params = step.get('parameters', {})
            if isinstance(params, dict) and 'input_slcs_files' in params:
                slcs = params['input_slcs_files']
                if isinstance(slcs, list):
                    new_slcs = []
                    step_has_s3 = False
                    for item in slcs:
                        if isinstance(item, str) and item.startswith('s3://'):
                            step_has_s3 = True
                            base_name = os.path.basename(item.rstrip('/'))
                            local_path = os.path.join(input_base_dir, base_name)
                            rclone_src = s3_to_rclone(item)
                            inputs_map.append({'s3': item, 'rclone': rclone_src, 'local': local_path})
                            new_slcs.append(local_path)
                        else:
                            new_slcs.append(item)
                    if step_has_s3:
                        params['input_slcs_files'] = new_slcs
                        has_changes = True

# --------------------------------------------------------------------------
# Process Output Products
# Rewrite S3 destinations to local path ONLY if path starts with s3://
# --------------------------------------------------------------------------
io_config = data.get('io', {})
if isinstance(io_config, dict):
    outputs = io_config.get('output_products', [])
    if isinstance(outputs, list):
        for out in outputs:
            if isinstance(out, dict):
                s3_path = out.get('path', '')
                if isinstance(s3_path, str) and s3_path.startswith('s3://'):
                    base_name = os.path.basename(s3_path.rstrip('/'))
                    local_path = os.path.join(tmp_folder, base_name)
                    rclone_dst = s3_to_rclone(s3_path)
                    outputs_map.append({'local': local_path, 's3': s3_path, 'rclone': rclone_dst})
                    out['path'] = local_path
                    has_changes = True

# ONLY overwrite the YAML file on disk if S3 paths were actually detected & modified
if has_changes:
    with open(yaml_path, 'w') as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)

# Output JSON metadata string
print(json.dumps({
    'has_changes': has_changes,
    'inputs': inputs_map,
    'outputs': outputs_map,
    'tmp_folder': tmp_folder
}))
EOF
        )

        # Check if S3 changes were detected
        if [ -n "$PARSE_OUT" ]; then
            HAS_CHANGES=$(echo "$PARSE_OUT" | uv run python -c 'import sys, json; print(json.load(sys.stdin).get("has_changes", False))')
        fi

        # Download input files from S3 to /scratch/external/DATA-ROSEL using 'rclone copy -P'
        if [ "$HAS_CHANGES" = "True" ]; then
            echo "$PARSE_OUT" | uv run python -c '
import sys, json, subprocess

data = json.load(sys.stdin)
for item in data.get("inputs", []):
    rclone_src = item["rclone"]
    local_dst = item["local"]
    print(f"[RCLONE DOWNLOAD] Downloading {rclone_src} -> {local_dst}")
    subprocess.run(["rclone", "copy", "-P", rclone_src, local_dst], check=True)
'
        else
            # Remove redundant backup file if no changes were needed
            rm -f "${YAML_FILE}.bak"
        fi
    fi

    # ==========================================================================
    # 3. RUN PYTHON TEST SCRIPT VIA PYTEST
    # Redirect both stdout and stderr (2>&1) into logs/<TEST_NAME>/<TEST_BASE>.log
    # ==========================================================================
    echo "Launching $PY_FILE via 'uv run pytest' (log output -> $LOG_FILE)..."
    uv run --extra tests pytest -s -vv -m ard "$PY_FILE" > "$LOG_FILE" 2>&1
    EXIT_CODE=$?

    if [ $EXIT_CODE -eq 0 ]; then
        echo "✓ Test $TEST_BASE completed successfully."
    else
        echo "✗ ERROR: Test $TEST_BASE failed with exit code $EXIT_CODE. Check log: $LOG_FILE"
    fi

    # ==========================================================================
    # 4. POST-TEST UPLOAD, LOCAL CLEANUP, AND YAML RESTORATION
    # ==========================================================================
    if [ -f "$YAML_FILE" ] && [ "$HAS_CHANGES" = "True" ]; then
        echo "$PARSE_OUT" | uv run python -c '
import sys, json, subprocess, os, shutil

data = json.load(sys.stdin)

# Step 4a: Upload generated output files back to original S3 locations using rclone copy -P and remove local copies
for item in data.get("outputs", []):
    local_src = item["local"]
    rclone_dst = item["rclone"]
    if os.path.exists(local_src):
        print(f"[RCLONE UPLOAD] Uploading {local_src} -> {rclone_dst}")
        subprocess.run(["rclone", "copy", "-P", local_src, rclone_dst], check=True)
        print(f"[CLEANUP OUTPUT] Removing local copy: {local_src}")
        if os.path.isdir(local_src):
            shutil.rmtree(local_src)
        else:
            os.remove(local_src)
    else:
        print(f"[WARNING] Local output product {local_src} not found. Skipping upload.")

# Step 4b: Delete downloaded input files from /scratch/external/DATA-ROSEL
for item in data.get("inputs", []):
    local_input = item["local"]
    if os.path.exists(local_input):
        print(f"[CLEANUP INPUT] Removing local copy: {local_input}")
        if os.path.isdir(local_input):
            shutil.rmtree(local_input)
        else:
            os.remove(local_input)
'
        # Step 4c: Restore the original untouched YAML configuration file from backup
        if [ -f "${YAML_FILE}.bak" ]; then
            mv "${YAML_FILE}.bak" "$YAML_FILE"
        fi
    fi

    echo "----------------------------------------"
done

echo "All tests for $TEST_NAME have finished processing."
