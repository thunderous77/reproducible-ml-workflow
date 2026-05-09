#!/usr/bin/env bash
# Bootstrap script — equivalent to `pull_eggs` in the Bidder-ML original.
#
# Pulls the wheel for $PKG_VERSION from a GitHub Release, creates (or
# reuses) a cached venv keyed on (arch, OS, Python, version), pulls the
# experiment config, and runs the entry point.
#
# Locking is mkdir-based for portability — flock is Linux-only and the
# local-execution variant must work on macOS too.

set -euo pipefail

# ── Required environment from submit.py ───────────────────────────
: "${PKG_VERSION:?missing}"     # release tag, e.g. v0.1.123
: "${FLOW_ID:?missing}"
: "${GH_REPO:?missing}"
: "${PKG_NAME:=mypkg}"
: "${ASSET_NAME:?missing}"      # config asset filename on the release
: "${LOCAL_CONFIG_PATH:=}"      # if set, used instead of downloading
: "${OUTPUT_DIR:=}"

# ── Cache directory layout ─────────────────────────────────────────
CACHE_ROOT="${MYPKG_CACHE_DIR:-$HOME/.cache/${PKG_NAME}}"
WHEEL_DIR="$CACHE_ROOT/wheels"
CFG_DIR="$CACHE_ROOT/configs"

# Venv path encodes (arch, OS, Python) so caches don't collide across hosts.
PY_VER="${PY_VER:-3.11}"
ARCH="$(uname -m)"
OS="$(uname -s | tr '[:upper:]' '[:lower:]')"
VENV_ROOT="$CACHE_ROOT/venvs/${ARCH}-${OS}-py${PY_VER}"
VENV_DIR="$VENV_ROOT/${PKG_VERSION}"

mkdir -p "$WHEEL_DIR" "$CFG_DIR" "$VENV_ROOT"

# ── Portable lock helper (mkdir is atomic on POSIX) ───────────────
acquire_lock() {
    local lockdir="$1" elapsed=0 timeout=600
    while ! mkdir "$lockdir" 2>/dev/null; do
        sleep 0.2
        elapsed=$((elapsed + 1))
        if (( elapsed > timeout * 5 )); then
            echo "ERROR: lock timeout on $lockdir" >&2
            return 1
        fi
    done
}

release_lock() {
    rmdir "$1" 2>/dev/null || true
}

# ── Resolve uv binary ─────────────────────────────────────────────
if command -v uv >/dev/null 2>&1; then
    UV="uv"
elif [[ -x "$HOME/.local/bin/uv" ]]; then
    UV="$HOME/.local/bin/uv"
else
    echo "ERROR: uv not found. Install: curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
    exit 1
fi

# ── Stage 1: pull the wheel ──────────────────────────────────────
WHEEL_LOCK="$WHEEL_DIR/.lock-${PKG_VERSION}"
acquire_lock "$WHEEL_LOCK"
trap "release_lock '$WHEEL_LOCK'" EXIT

WHEEL_GLOB="$WHEEL_DIR/${PKG_NAME}-${PKG_VERSION#v}-py3-none-any.whl"
if ! ls $WHEEL_GLOB >/dev/null 2>&1; then
    echo "[run.sh] downloading wheel for $PKG_VERSION ..."
    gh release download "$PKG_VERSION" \
        --repo "$GH_REPO" \
        --pattern "${PKG_NAME}-*.whl" \
        --dir "$WHEEL_DIR" \
        --clobber
else
    echo "[run.sh] wheel cache hit: $WHEEL_GLOB"
fi
release_lock "$WHEEL_LOCK"
trap - EXIT

WHEEL=$(ls $WHEEL_GLOB | head -1)

# ── Stage 2: create / reuse the venv ──────────────────────────────
VENV_LOCK="$VENV_ROOT/.lock-${PKG_VERSION}"
acquire_lock "$VENV_LOCK"
trap "release_lock '$VENV_LOCK'" EXIT

if [[ ! -f "$VENV_DIR/.ready" ]]; then
    echo "[run.sh] building venv for $PKG_VERSION at $VENV_DIR ..."
    rm -rf "$VENV_DIR"
    "$UV" venv "$VENV_DIR" --python "$PY_VER"
    "$UV" pip install --python "$VENV_DIR/bin/python" "$WHEEL"
    touch "$VENV_DIR/.ready"
else
    echo "[run.sh] venv cache hit: $VENV_DIR"
fi
release_lock "$VENV_LOCK"
trap - EXIT

# Activate
source "$VENV_DIR/bin/activate"
echo "[run.sh] using python: $(which python) ($(python --version))"

# ── Stage 3: pull the config ──────────────────────────────────────
CFG="$CFG_DIR/${FLOW_ID}.json"
if [[ -n "$LOCAL_CONFIG_PATH" && -f "$LOCAL_CONFIG_PATH" ]]; then
    echo "[run.sh] using local config (--no-upload): $LOCAL_CONFIG_PATH"
    cp "$LOCAL_CONFIG_PATH" "$CFG"
else
    echo "[run.sh] downloading config asset $ASSET_NAME from $PKG_VERSION ..."
    gh release download "$PKG_VERSION" \
        --repo "$GH_REPO" \
        --pattern "$ASSET_NAME" \
        --dir "$CFG_DIR" \
        --clobber
    mv "$CFG_DIR/$ASSET_NAME" "$CFG"
fi

# ── Stage 4: run entry point ──────────────────────────────────────
EXTRA=()
if [[ -n "$OUTPUT_DIR" ]]; then
    EXTRA+=("--output-dir" "$OUTPUT_DIR")
fi

echo "[run.sh] launching ${PKG_NAME}.entry ..."
python -m "${PKG_NAME}.entry" --config "$CFG" --flow-id "$FLOW_ID" "${EXTRA[@]}"
