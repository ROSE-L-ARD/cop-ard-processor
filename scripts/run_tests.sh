#!/usr/bin/env bash

# Exit immediately if an uninitialized variable is accessed
set -u

# ==============================================================================
# SCRIPT PARAMETERS & ARGUMENT PARSING
# ==============================================================================

TEST_NAME=""
RUN_REFERENCE=true  # Default behavior: run REFERENCE if present

# Parse flags and positional arguments
while [[ $# -gt 0 ]]; do
    case "$1" in
        --skip-reference|--no-ref|--skip-ref)
            RUN_REFERENCE=false
            shift
            ;;
        --with-reference|--ref)
            RUN_REFERENCE=true
            shift
            ;;
        -h|--help)
            echo "Usage: $0 <TEST_NAME> [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  --skip-reference, --no-ref    Skip executing REFERENCE.py if present (default: run it)"
            echo "  --help, -h                    Show this help message"
            echo ""
            echo "Examples:"
            echo "  $0 TC-ST-FUNC-009"
            echo "  $0 TC-ST-FUNC-009 --skip-reference"
            exit 0
            ;;
        *)
            if [ -z "$TEST_NAME" ]; then
                TEST_NAME="$1"
            else
                echo "Error: Unknown argument or multiple test names supplied: '$1'"
                exit 1
            fi
            shift
            ;;
    esac
done

# Check if the test name argument was supplied
if [ -z "$TEST_NAME" ]; then
    echo "Usage: $0 <TEST_NAME> [--skip-reference]"
    echo "Example: $0 TC-ST-FUNC-009"
    exit 1
fi

# Set up directory paths relative to the project root
LAUNCH_DIR="tests/vt/launch/$TEST_NAME"
TRIGGER_DIR="tests/vt/triggering/$TEST_NAME"
LOG_DIR="logs/$TEST_NAME"
INPUT_DATA_DIR="/data/scratch/external/DATA-ROSEL"

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
# Requirement: Run REFERENCE first (if enabled), then remaining tests chronologically
# ==============================================================================

TEST_LIST=()

# Append 'REFERENCE' first ONLY if enabled by flag AND REFERENCE.py exists
if [ "$RUN_REFERENCE" = true ] && [ -f "$LAUNCH_DIR/REFERENCE.py" ]; then
    TEST_LIST+=("REFERENCE")
elif [ "$RUN_REFERENCE" = false ] && [ -f "$LAUNCH_DIR/REFERENCE.py" ]; then
    echo "ℹ REFERENCE.py found but skipped due to --skip-reference flag."
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

# Fail early if no Python test files were detected in the queue
if [ ${#TEST_LIST[@]} -eq 0 ]; then
    echo "Error: No .py test files selected for execution in $LAUNCH_DIR"
    exit 1
fi

echo "=========================================="
echo "Running test suite for: $TEST_NAME"
echo "Run REFERENCE: $RUN_REFERENCE"
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
    ALL_OUTPUTS_EXIST="False"

    if [ -f "$YAML_FILE" ]; then
        # Create a backup copy of the original YAML config file
        cp "$YAML_FILE" "${YAML_FILE}.bak"

        # Execute inline Python script via 'uv run' to parse YAML paths & check S3 existence
        PARSE_OUT=$(uv run python - "$YAML_FILE" "$INPUT_DATA_DIR" << 'EOF'
import sys, os, yaml, json, subprocess

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

def check_s3_exists(rclone_dst):
    """Checks if an S3 target (file or directory) exists using rclone lsf."""
    try:
        res = subprocess.run(
            ["rclone", "lsf", rclone_dst],
            capture_output=True,
            text=True,
            check=False
        )
        return res.returncode == 0 and len(res.stdout.strip()) > 0
    except Exception:
        return False

def parse_input_item(item, input_base_dir):
    """Handles both dataset directories (.SAFE) and files inside product folders (.xemt, .xml, etc.)."""
    clean_item = item.rstrip('/')
    ext = os.path.splitext(clean_item)[1].lower()

    # If it is a file inside a product folder (like .xemt for SAOCOM), download the entire parent directory
    if ext in ['.xemt', '.xml', '.hdr', '.nc']:
        s3_parent = os.path.dirname(clean_item)
        parent_name = os.path.basename(s3_parent)
        file_name = os.path.basename(clean_item)

        local_parent = os.path.join(input_base_dir, parent_name)
        local_file = os.path.join(local_parent, file_name)

        return {
            's3': item,
            'rclone_src': s3_to_rclone(s3_parent),
            'rclone_dst': local_parent,
            'local_yaml_path': local_file,
            'local_cleanup_path': local_parent
        }
    else:
        # Standard dataset folder (like Sentinel .SAFE or .zarr)
        base_name = os.path.basename(clean_item)
        local_path = os.path.join(input_base_dir, base_name)
        return {
            's3': item,
            'rclone_src': s3_to_rclone(item),
            'rclone_dst': local_path,
            'local_yaml_path': local_path,
            'local_cleanup_path': local_path
        }

# Load YAML configuration content safely
with open(yaml_path, 'r') as f:
    data = yaml.safe_load(f)

gen_config = data.get('general_configuration', {})
tmp_folder = gen_config.get('temporary__folder', '/data/scratch/external/TMP')

inputs_map = []
outputs_map = []
has_changes = False

# --------------------------------------------------------------------------
# Process Input SLCS files
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
                            parsed = parse_input_item(item, input_base_dir)
                            inputs_map.append(parsed)
                            new_slcs.append(parsed['local_yaml_path'])
                        else:
                            new_slcs.append(item)
                    if step_has_s3:
                        params['input_slcs_files'] = new_slcs
                        has_changes = True

# --------------------------------------------------------------------------
# Process Output Products
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

# Check if all output products already exist on S3
all_outputs_exist = False
if outputs_map:
    all_outputs_exist = all(check_s3_exists(out['rclone']) for out in outputs_map)

# ONLY overwrite the YAML file on disk if S3 paths were modified AND outputs don't already exist
if has_changes and not all_outputs_exist:
    with open(yaml_path, 'w') as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)

# Output JSON metadata string
print(json.dumps({
    'has_changes': has_changes,
    'all_outputs_exist': all_outputs_exist,
    'inputs': inputs_map,
    'outputs': outputs_map,
    'tmp_folder': tmp_folder
}))
EOF
        )

        # Extract parsed metadata booleans
        if [ -n "$PARSE_OUT" ]; then
            HAS_CHANGES=$(echo "$PARSE_OUT" | uv run python -c 'import sys, json; print(json.load(sys.stdin).get("has_changes", False))')
            ALL_OUTPUTS_EXIST=$(echo "$PARSE_OUT" | uv run python -c 'import sys, json; print(json.load(sys.stdin).get("all_outputs_exist", False))')
        fi

        # Skip execution if all output products already exist on S3
        if [ "$ALL_OUTPUTS_EXIST" = "True" ]; then
            echo "⏭ Output products already exist on S3 for $TEST_BASE. Skipping test."
            if [ -f "${YAML_FILE}.bak" ]; then
                mv "${YAML_FILE}.bak" "$YAML_FILE"
            fi
            echo "----------------------------------------"
            continue
        fi

        # Download input files/folders from S3 using 'rclone copy -P' and auto-unzip .zip archives
        if [ "$HAS_CHANGES" = "True" ]; then
            echo "$PARSE_OUT" | uv run python -c '
import sys, json, subprocess, os, zipfile

data = json.load(sys.stdin)
for item in data.get("inputs", []):
    rclone_src = item["rclone_src"]
    rclone_dst = item["rclone_dst"]
    print(f"[RCLONE DOWNLOAD] Downloading {rclone_src} -> {rclone_dst}")
    subprocess.run(["rclone", "copy", "-P", rclone_src, rclone_dst], check=True)

    # Search for any .zip files in the downloaded directory and extract them
    search_dir = rclone_dst if os.path.isdir(rclone_dst) else os.path.dirname(rclone_dst)
    if os.path.exists(search_dir):
        for root, _, files in os.walk(search_dir):
            for f in files:
                if f.lower().endswith(".zip"):
                    zip_path = os.path.join(root, f)
                    print(f"[UNZIP] Extracting {zip_path} -> {search_dir}")
                    try:
                        with zipfile.ZipFile(zip_path, "r") as zip_ref:
                            zip_ref.extractall(search_dir)
                        os.remove(zip_path)
                        print(f"[UNZIP] Successfully extracted and removed archive: {zip_path}")
                    except Exception as e:
                        print(f"[WARNING] Failed to extract {zip_path}: {e}")
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
    # 4. POST-TEST UPLOAD, TEMP DIR CLEANUP, AND YAML RESTORATION
    # ==========================================================================
    if [ -f "$YAML_FILE" ] && [ "$HAS_CHANGES" = "True" ]; then
        echo "$PARSE_OUT" | uv run python -c '
import sys, json, subprocess, os, shutil

data = json.load(sys.stdin)

# Step 4a: Upload generated output files back to S3 ONLY if the test passed
exit_code = int(sys.argv[1]) if len(sys.argv) > 1 else 0

for item in data.get("outputs", []):
    local_src = item["local"]
    rclone_dst = item["rclone"]
    if os.path.exists(local_src):
        if exit_code == 0:
            print(f"[RCLONE UPLOAD] Uploading {local_src} -> {rclone_dst}")
            subprocess.run(["rclone", "copy", "-P", local_src, rclone_dst], check=True)
        else:
            print(f"[SKIP UPLOAD] Test failed (exit code {exit_code}). Skipping upload for {local_src}")

        print(f"[CLEANUP OUTPUT] Removing local copy: {local_src}")
        if os.path.isdir(local_src):
            shutil.rmtree(local_src)
        else:
            os.remove(local_src)

# Step 4b: Delete downloaded input files/folders from /data/scratch/external/DATA-ROSEL
for item in data.get("inputs", []):
    cleanup_path = item["local_cleanup_path"]
    if os.path.exists(cleanup_path):
        print(f"[CLEANUP INPUT] Removing local copy: {cleanup_path}")
        if os.path.isdir(cleanup_path):
            shutil.rmtree(cleanup_path)
        else:
            os.remove(cleanup_path)

# Step 4c: Clean up all remaining temporary files and folders left by eopf in temporary__folder
tmp_folder = data.get("tmp_folder")
forbidden_dirs = ["/", "/tmp", "/var", "/home", "/data/scratch", "/data/scratch/external", "/data"]
if tmp_folder and os.path.exists(tmp_folder) and os.path.abspath(tmp_folder) not in forbidden_dirs:
    print(f"[CLEANUP TEMP DIR] Clearing temporary directory contents: {tmp_folder}")
    for entry in os.listdir(tmp_folder):
        entry_path = os.path.join(tmp_folder, entry)
        try:
            if os.path.isdir(entry_path):
                shutil.rmtree(entry_path)
            else:
                os.remove(entry_path)
        except Exception as e:
            print(f"[WARNING] Failed to remove temporary item {entry_path}: {e}")
' "$EXIT_CODE"

        # Step 4d: Restore the original untouched YAML configuration file from backup
        if [ -f "${YAML_FILE}.bak" ]; then
            mv "${YAML_FILE}.bak" "$YAML_FILE"
        fi
    fi

    # ==========================================================================
    # 5. IMMEDIATE EXIT ON FAILURE
    # ==========================================================================
    if [ $EXIT_CODE -ne 0 ]; then
        echo "=========================================="
        echo "ABORTING: Script exiting immediately due to failure in $TEST_BASE."
        echo "=========================================="
        exit $EXIT_CODE
    fi

    echo "----------------------------------------"
done

echo "All tests for $TEST_NAME have finished processing."