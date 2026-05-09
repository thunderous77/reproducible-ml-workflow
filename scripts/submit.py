#!/usr/bin/env python3
"""Local-execution submitter.

Equivalent to the SSH+sbatch submitter in the Slurm variant, with the
"submit to remote scheduler" step replaced by a local subprocess call. The
reproducibility contract is unchanged: every run is bound to a specific
release tag, and the experiment config is uploaded as an immutable release
asset before the job starts.

Flow:
    1. Resolve the package version (release tag, default = latest).
    2. Build a flow_id and stamp it into the config.
    3. Upload the stamped config as a release asset (audit trail).
    4. Invoke scripts/run.sh as a subprocess with PKG_VERSION/FLOW_ID/etc.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import uuid

# Override via env if you fork this repo or rename the package.
GITHUB_REPO = os.environ.get("GH_REPO", "thunderous77/reproducible-ml-workflow")
PKG_NAME = os.environ.get("PKG_NAME", "mypkg")

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
RUN_SH = REPO_ROOT / "scripts" / "run.sh"


def latest_release_tag() -> str:
    out = subprocess.check_output(
        [
            "gh", "release", "list",
            "--repo", GITHUB_REPO,
            "--limit", "1",
            "--json", "tagName",
            "-q", ".[0].tagName",
        ],
        text=True,
    ).strip()
    if not out:
        sys.exit(
            f"No releases found on {GITHUB_REPO}. Push a commit to main and "
            "wait for the build-wheel workflow to publish the first release."
        )
    return out


def upload_config(tag: str, flow_id: str, config_path: pathlib.Path) -> str:
    """Stamp the config with metadata and upload as a release asset.

    Returns the asset name (used by the consumer to download it later).
    """
    cfg_blob = json.loads(config_path.read_text())
    cfg_blob["_meta"] = {"flow_id": flow_id, "pkg_version": tag}
    asset_name = f"experiment_{flow_id}.json"

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(cfg_blob, f, indent=2)
        tmp_path = f.name

    try:
        subprocess.check_call([
            "gh", "release", "upload", tag,
            f"{tmp_path}#{asset_name}",
            "--repo", GITHUB_REPO,
            "--clobber",
        ])
    finally:
        os.unlink(tmp_path)

    return asset_name


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--version", help="Release tag, e.g. v0.1.123. Default: latest.")
    ap.add_argument("--config", required=True, help="Local config JSON path.")
    ap.add_argument(
        "--output-dir",
        default=str(REPO_ROOT / "examples"),
        help="Where the entry script writes its results JSON.",
    )
    ap.add_argument(
        "--no-upload",
        action="store_true",
        help="Skip uploading the config as a release asset (offline / dry run).",
    )
    args = ap.parse_args()

    tag = args.version or latest_release_tag()
    flow_id = uuid.uuid4().hex[:12]
    config_path = pathlib.Path(args.config).resolve()
    if not config_path.is_file():
        sys.exit(f"Config not found: {config_path}")

    print(f"flow_id={flow_id} pkg_version={tag} config={config_path}")

    if args.no_upload:
        # Audit trail is bypassed; entry will still receive the local config.
        asset_name = config_path.name
        local_config_for_run = str(config_path)
    else:
        asset_name = upload_config(tag, flow_id, config_path)
        local_config_for_run = ""  # run.sh will fetch from the release

    env = {
        **os.environ,
        "PKG_VERSION": tag,
        "FLOW_ID": flow_id,
        "GH_REPO": GITHUB_REPO,
        "PKG_NAME": PKG_NAME,
        "ASSET_NAME": asset_name,
        "OUTPUT_DIR": args.output_dir,
        # If set, run.sh will skip the gh-release-download for the config and
        # use this local file instead — used by --no-upload.
        "LOCAL_CONFIG_PATH": local_config_for_run,
    }

    print(f"running: bash {RUN_SH}")
    proc = subprocess.run(["bash", str(RUN_SH)], env=env)
    if proc.returncode != 0:
        sys.exit(proc.returncode)
    print(f"done. flow_id={flow_id} pkg_version={tag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
