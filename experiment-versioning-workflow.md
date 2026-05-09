# Experiment Versioning Workflow: Bidder-ML Original vs Slurm + Free Storage

> Goal: every experiment submission is bound to a specific git commit, so it can be reproduced later.
> This document maps the Bidder-ML mechanism to its equivalent for "remote Slurm cluster + free storage."

---

## 0. Side-by-side overview

| Component | Bidder-ML (original)                              | New project (Slurm + free storage)                |
|---|----------------------------------------------------|---------------------------------------------------|
| Compute scheduler | Kubernetes (GKE)                          | Slurm (remote cluster, submit over SSH)           |
| Runtime container | Docker `gcr.io/.../bidder-ml:<V>`         | Apptainer/Singularity `.sif` *or* uv venv on shared FS |
| Code distribution unit | Python wheel with git SHA baked in   | Same — mechanism reused as-is                     |
| Version number | GCS atomic counter (200001+)                 | `git rev-list --count HEAD` (no external state)   |
| Wheel storage | GCS + private PyPI                            | **GitHub Releases** (primary) / R2 / shared FS    |
| Experiment config storage | GCS `master_configs/<flow_id>.json` | **GitHub Release assets** / R2 / shared FS    |
| Image build | Jenkins (Kaniko)                                | GitHub Actions (build venv tarball or `.sif`)     |
| Submission entry point | Local Python → K8s API directly      | Local Python → SSH login node → `sbatch`          |
| Pulling code onto compute node | pod cmd `gsutil cp` + unpack | sbatch script `gh release download` + `flock` cache |
| Three places version is logged | GCS config / pod label / WandB summary | GitHub asset / `--comment`+env / WandB summary |

---

## 1. Bidder-ML original workflow (reference)

### 1.1 Core idea

Image and code are **decoupled**. Each master commit produces two artifacts identified by the same monotonic integer `BUILD_VERSION`:

1. **Wheel** `bidderml-<V>-py3-none-any.whl` — contains a generated `bidderml/version.py` with the git SHA baked in.
2. **Docker image** `gcr.io/.../bidder-ml:<V>` — runtime only (Python, CUDA, deps), **no application code**.

At submit time, pick `bidder_ml_version=N`: the pod boots `bidder-ml:latest`, then `gsutil cp`s wheel N from GCS, unpacks it, and points `PYTHONPATH` at it. The same docker image can run any historical experiment.

### 1.2 End-to-end flow

| Stage | Tool | Key file / function | Behavior |
|---|---|---|---|
| 1. Reserve version number | GitHub Actions + GCS | `get-build-number.sh` | GCS object + `x-goog-if-generation-match` CAS, monotonic from 200001 |
| 2. Bake SHA | GitHub Actions | `.github/workflows/build-wheel.yaml` | Write `bidderml/version.py`, rewrite `version` in `pyproject.toml` |
| 3. Build wheel | GitHub Actions | `uv build` | Upload to GCS + Artifact Registry |
| 4. Build image | Jenkins (Kaniko) | `ci/Jenkinsfile` | Tag as `:latest` + `:<V>` |
| 5. Submit job | Local Python | `MasterConfigBase.submit()` | Write `bidder_ml_version` into config, serialize to GCS |
| 6. Inject wheel-pull command | `K8SLauncher` | `pull_eggs` in `k8s.py` | Prepend `gsutil cp` + `wheel unpack` to pod startup cmd |
| 7. Log version in three places | — | GCS config / pod label / WandB summary | Audit trail |

### 1.3 Key code snippets

**`pull_eggs` (pod startup command prefix):**
```bash
rm -rf $PYTHONPATH
export BIDDERML_WHEEL_VER=<V>
gsutil cp gs://applovin-hdfs-us-central1/bidderml/build/bidderml-$BIDDERML_WHEEL_VER-py3-none-any.whl .
uv run wheel unpack bidderml-$BIDDERML_WHEEL_VER-py3-none-any.whl
export PYTHONPATH=/app/bidderml-<V>
```

**`git_info.py`:**
```python
def git_hash_version() -> str:
    try:
        from bidderml.version import git_hash   # baked into the wheel
    except ImportError:
        git_hash = "-1"                          # fallback for local bare runs
    return git_hash
```

---

## 2. New project workflow (Slurm + free storage)

### 2.1 Design choices

| Decision | Choice | Rationale |
|---|---|---|
| Primary storage | **GitHub Releases** | Free, no size cap, CI integrated, release tag = version, browsable UI |
| Cache layer | Cluster shared FS (e.g. `/scratch/$USER/wheel_cache/`) | Same version is downloaded once; subsequent job startups become seconds |
| Version number | `git rev-list --count HEAD` (commit count) | Monotonic, deterministic, no external state, PEP 440 compliant (`0.1.123`) |
| Runtime | uv venv on shared FS, cached by version | Slurm clusters typically lack root for docker; apptainer optional |
| Submission method | Local Python → `ssh login sbatch -` | Doesn't depend on slurm REST API; works on every cluster |
| Experiment tracking | WandB free tier (or MLflow) | Aligns with original — `git_hash`/`git_branch` written to run summary |

### 2.2 Core idea (same as original)

- Image/runtime is rebuilt only when deps change → one venv on shared FS is enough
- Code is distributed via **content-addressable wheels** → one `.whl` per commit, SHA baked in
- The "experiment = git commit" guarantee comes from wheel immutability, not pre-submit hooks

### 2.3 End-to-end flow

```
Dev machine                 GitHub                Slurm login node      Slurm compute node
  │                          │                         │                         │
  │ git push main            │                         │                         │
  │─────────────────────────>│                         │                         │
  │                          │ Actions:                │                         │
  │                          │  - compute build_count  │                         │
  │                          │  - write version.py     │                         │
  │                          │  - uv build             │                         │
  │                          │  - create release v0.1.N│                         │
  │                          │  - upload .whl to release                         │
  │                          │                         │                         │
  │ python submit.py \       │                         │                         │
  │   --version 123 ...      │                         │                         │
  │ 1. write config.json     │                         │                         │
  │ 2. gh release upload     │                         │                         │
  │    config to release     │                         │                         │
  │ 3. generate sbatch script│                         │                         │
  │ 4. ssh sbatch -          │────────────────────────>│                         │
  │                          │                         │ sbatch enqueues         │
  │                          │                         │  ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ >│
  │                          │                         │                         │ pull_eggs:
  │                          │<────────────────────────┼─────────────────────────│  gh release dl
  │                          │ (download cached)       │ flock + cache           │  uv venv create
  │                          │                         │                         │  pip install whl
  │                          │                         │                         │  python entry.py
  │                          │                         │                         │  WandB log
```

### 2.4 Implementation skeleton

#### A. `pyproject.toml` setup

```toml
[project]
name = "mypkg"
version = "0.1.0"           # CI rewrites this
requires-python = "==3.11.*"
```

#### B. CI: `.github/workflows/build-wheel.yaml`

```yaml
name: Build & Release Wheel
on:
  push:
    branches: [main]
permissions:
  contents: write             # required to create releases

jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0      # rev-list --count needs full history

      - name: Compute version
        id: ver
        run: |
          COUNT=$(git rev-list --count HEAD)
          SHA=$(git rev-parse HEAD)
          BRANCH="${GITHUB_REF_NAME}"
          echo "build_version=0.1.${COUNT}" >> $GITHUB_OUTPUT
          echo "tag=v0.1.${COUNT}"          >> $GITHUB_OUTPUT
          echo "sha=${SHA}"                 >> $GITHUB_OUTPUT
          echo "branch=${BRANCH}"           >> $GITHUB_OUTPUT

      - name: Bake version.py & pyproject
        run: |
          cat > mypkg/version.py <<EOF
          git_hash = "${{ steps.ver.outputs.sha }}"
          git_branch = "${{ steps.ver.outputs.branch }}"
          build_version = "${{ steps.ver.outputs.build_version }}"
          EOF
          sed -i 's/^version = "[^"]*"/version = "${{ steps.ver.outputs.build_version }}"/' pyproject.toml

      - name: Install uv & build
        run: |
          curl -LsSf https://astral.sh/uv/install.sh | sh
          ~/.local/bin/uv build

      - name: Create release & upload wheel
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        run: |
          gh release create "${{ steps.ver.outputs.tag }}" \
            --target "${{ steps.ver.outputs.sha }}" \
            --title "${{ steps.ver.outputs.tag }}" \
            --notes "Auto-built from ${{ steps.ver.outputs.sha }}" \
            dist/*.whl
```

Output:
- Release tag `v0.1.123`, bound to a specific commit SHA
- Asset `mypkg-0.1.123-py3-none-any.whl`
- The wheel's bundled `version.py` has the SHA baked in

#### C. `mypkg/version_utils.py` (equivalent of `git_info.py`)

```python
def git_hash_version() -> str:
    try:
        from mypkg.version import git_hash
    except ImportError:
        return "-1"
    return git_hash

def git_branch() -> str:
    try:
        from mypkg.version import git_branch
    except ImportError:
        return "None"
    return git_branch

def build_version() -> str:
    try:
        from mypkg.version import build_version
    except ImportError:
        return "-1"
    return build_version
```

#### D. Submit side: `scripts/submit.py`

```python
#!/usr/bin/env python3
"""Run locally: build config, upload, submit remote sbatch."""
import argparse, json, subprocess, uuid, tempfile, pathlib

SLURM_HOST = "slurm-login.example.com"
GITHUB_REPO = "youruser/yourrepo"

def latest_release_tag() -> str:
    out = subprocess.check_output(
        ["gh", "release", "list", "--repo", GITHUB_REPO, "--limit", "1",
         "--json", "tagName", "-q", ".[0].tagName"]
    ).decode().strip()
    return out  # e.g. "v0.1.123"

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", help="release tag, e.g. v0.1.123. Default: latest.")
    ap.add_argument("--config", required=True, help="local config yaml/json")
    ap.add_argument("--gpus", type=int, default=1)
    ap.add_argument("--time", default="12:00:00")
    ap.add_argument("--partition", default="gpu")
    args = ap.parse_args()

    tag = args.version or latest_release_tag()
    flow_id = uuid.uuid4().hex[:12]

    # 1) Upload the config as a release asset too (immutable audit trail)
    cfg_path = pathlib.Path(args.config).resolve()
    asset_name = f"experiment_{flow_id}.json"
    cfg_blob = json.loads(cfg_path.read_text())
    cfg_blob["_meta"] = {"flow_id": flow_id, "pkg_version": tag}
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(cfg_blob, f, indent=2)
        tmp = f.name
    subprocess.check_call(
        ["gh", "release", "upload", tag, f"{tmp}#{asset_name}",
         "--repo", GITHUB_REPO, "--clobber"]
    )

    # 2) Generate sbatch script
    sbatch = f"""#!/bin/bash
#SBATCH --job-name=mypkg-{flow_id}
#SBATCH --partition={args.partition}
#SBATCH --gres=gpu:{args.gpus}
#SBATCH --time={args.time}
#SBATCH --comment="pkg_version={tag} flow_id={flow_id}"
#SBATCH --export=ALL,PKG_VERSION={tag},FLOW_ID={flow_id},GH_REPO={GITHUB_REPO},ASSET_NAME={asset_name}
#SBATCH --output=/scratch/$USER/logs/{flow_id}-%j.out

set -euo pipefail
bash $HOME/mypkg-bootstrap/run.sh
"""
    # 3) Submit remotely
    p = subprocess.run(
        ["ssh", SLURM_HOST, "sbatch", "--parsable"],
        input=sbatch, text=True, capture_output=True, check=True,
    )
    job_id = p.stdout.strip().split(";")[0]
    print(f"flow_id={flow_id}  pkg_version={tag}  slurm_job_id={job_id}")

if __name__ == "__main__":
    main()
```

#### E. Slurm bootstrap script: `$HOME/mypkg-bootstrap/run.sh` (lives on the cluster)

This is the equivalent of `pull_eggs`. **Key point: `flock` ensures the wheel/venv for a given version is installed only once, even with concurrent jobs.**

```bash
#!/bin/bash
set -euo pipefail

: "${PKG_VERSION:?missing}"
: "${FLOW_ID:?missing}"
: "${GH_REPO:?missing}"
: "${ASSET_NAME:?missing}"

# ── Cache directory on shared FS ─────────────────────────────
SHARED="/scratch/$USER/mypkg_cache"
WHEEL_DIR="$SHARED/wheels"
VENV_DIR="$SHARED/venvs/$PKG_VERSION"
CFG_DIR="$SHARED/configs"
mkdir -p "$WHEEL_DIR" "$CFG_DIR" "$(dirname "$VENV_DIR")"

# ── Pull wheel (locked to avoid concurrent-download races) ───
WHEEL_GLOB="$WHEEL_DIR/mypkg-${PKG_VERSION#v}-py3-none-any.whl"
(
  flock -x 9
  if ! ls $WHEEL_GLOB 2>/dev/null; then
    cd "$WHEEL_DIR"
    gh release download "$PKG_VERSION" --repo "$GH_REPO" \
       --pattern "mypkg-*.whl" --clobber
  fi
) 9>"$WHEEL_DIR/.lock"

WHEEL=$(ls $WHEEL_GLOB | head -1)

# ── Create / reuse venv (also locked) ────────────────────────
(
  flock -x 9
  if [[ ! -f "$VENV_DIR/.ready" ]]; then
    rm -rf "$VENV_DIR"
    uv venv "$VENV_DIR" --python 3.11
    uv pip install --python "$VENV_DIR/bin/python" "$WHEEL"
    touch "$VENV_DIR/.ready"
  fi
) 9>"$VENV_DIR.lock"

source "$VENV_DIR/bin/activate"

# ── Pull config ──────────────────────────────────────────────
CFG="$CFG_DIR/$FLOW_ID.json"
if [[ ! -f "$CFG" ]]; then
  gh release download "$PKG_VERSION" --repo "$GH_REPO" \
     --pattern "$ASSET_NAME" --dir "$CFG_DIR"
  mv "$CFG_DIR/$ASSET_NAME" "$CFG"
fi

# ── Run the actual entry point ───────────────────────────────
python -m mypkg.entry --config "$CFG" --flow-id "$FLOW_ID"
```

#### F. WandB logging in the training entry point

```python
import wandb
from mypkg.version_utils import git_hash_version, git_branch, build_version

wandb.init(project="myproject", name=f"run-{flow_id}")
wandb.run.summary.update({
    "git_hash": git_hash_version(),
    "git_branch": git_branch(),
    "build_version": build_version(),
    "flow_id": flow_id,
    "slurm_job_id": os.environ.get("SLURM_JOB_ID"),
})
```

---

## 3. Component-by-component mapping

| Function | Bidder-ML implementation | New project implementation |
|---|---|---|
| Reserve a monotonic version | `get-build-number.sh` (GCS CAS counter) | `git rev-list --count HEAD` (no external state) |
| Bake git SHA | CI writes `bidderml/version.py` | CI writes `mypkg/version.py` |
| Immutable wheel storage | GCS bucket | **GitHub Release assets** |
| Fetch wheel by version | `gsutil cp .../bidderml-<V>.whl` | `gh release download v0.1.<N> -p '*.whl'` |
| Runtime container | Docker image (Kaniko build) | uv venv on shared FS, cached per version |
| Equivalent of `imagePullPolicy: Always` | — | `flock` + check `.ready` marker, concurrency-safe |
| Persist experiment config | `mc.to_gcs(flow_id)` | `gh release upload <tag> experiment_<flow_id>.json` |
| Enqueue on scheduler | K8s Job API | `ssh login sbatch -` |
| Pod label / job metadata | `bidder_ml_version` label | `--comment` + `--export PKG_VERSION,FLOW_ID` |
| WandB linkage | `git_info.git_hash_version()` | Same-named helper, reused as-is |
| Local bare-run fallback | `version.py` import fails → `"-1"` | Same |

---

## 4. Improvements over the original

1. **No external counter required.** `git rev-list --count HEAD` is deterministic and monotonic — drops an entire GCS CAS script. Trade-off: rebases / force-pushes can move the count backwards. Build only on `main`, which shouldn't be force-pushed; assert it in CI to be safe.
2. **Storage shares a stack with CI.** No GCP credentials needed; `GITHUB_TOKEN` is auto-injected by Actions. The submit-side machine needs `gh auth login` once.
3. **Release tag is the version.** Browsable UI, each release auto-links to its commit — much more readable than a bare integer.
4. **Shared-FS caching makes job startup far faster than K8s `imagePullPolicy: Always`.** Only the first job on a new version pays the download/install cost (~30s); subsequent jobs `source venv` in <1s.
5. **`flock` replaces K8s's implicit image-layer cache concurrency model.** Explicit but simple.

---

## 5. Gotchas & operational notes

- **GitHub Release size:** public-repo per-file cap is 2GB, plenty for wheels (typically 1–50 MB). If your dependency closure is huge, ship only the app wheel via `--pattern '*.whl'` and let the cached venv carry the heavy deps.
- **Private repo releases:** the cluster nodes need `gh auth login` once. Recommended: a fine-grained PAT with **read-only** `Contents: read` scoped to this single repo, written to `~/.config/gh/hosts.yml`.
- **No root on Slurm clusters:** don't try docker. Use uv venv or apptainer. This document defaults to venv.
- **Force-push to main:** moves `rev-list --count` backwards and risks colliding with an existing version. Add a CI guard `git tag --list "v0.1.${COUNT}" | grep -q . && exit 1`, or disable force-push on `main` in repo settings.
- **Local dev with no built wheel:** `from mypkg.version import git_hash` fails to import; the helper returns `"-1"`. A WandB run with `git_hash == -1` is your visible "not reproducible" marker — don't filter it out silently.
- **Durability:** GitHub Releases live and die with your repo. If you're paranoid, mirror wheels to R2 or HF Hub as a CI side-effect (`aws s3 cp` step).
- **Cross-node venv compatibility on shared FS:** different CPU architectures (x86 vs ARM) or different CUDA versions need separate cache paths. Add the architecture as a prefix: `venvs/$(uname -m)-cuda${CUDA_MAJOR}/$PKG_VERSION`.
- **Deleting old releases / wheels:** **don't.** Deleting one breaks reproducibility for that experiment. If you must clean up, set a retention policy and dump to cold storage before deletion.

---

## 6. Optional enhancements

- **Apptainer instead of venv**: more robust when dependencies bring weird native binaries. Add a CI step `apptainer build mypkg-${V}.sif Apptainer.def` that uploads a `.sif` to the same release. The sbatch script becomes `apptainer exec ... python -m mypkg.entry`. Both modes can coexist.
- **Slurm REST API**: if `slurmrestd` is available, replace `ssh sbatch` with an HTTP POST. `submit.py` then has no SSH dependency.
- **MLflow instead of WandB**: a self-hosted SQLite-backed MLflow server on shared FS is zero-cost and works on offline clusters.
- **Pre-submit clean-tree check** (the original Bidder-ML doesn't do this, but you can be stricter): have `submit.py` reject the combination of `--version <X>` + working tree differing from that commit, to prevent "I thought I was running this commit" mistakes.
