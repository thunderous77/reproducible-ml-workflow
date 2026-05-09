"""Entry point invoked inside the cached venv (or docker container).

Plumbing-only stub: reads the experiment config, captures version metadata
that proves which commit produced this run, simulates a few seconds of
"work", and writes a results JSON next to the config.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import socket
import sys
import time
from pathlib import Path

from mypkg.version_utils import build_version, git_branch, git_hash


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--flow-id", required=True)
    ap.add_argument("--output-dir", default=None)
    args = ap.parse_args()

    cfg_path = Path(args.config)
    cfg = json.loads(cfg_path.read_text())

    out_dir = Path(args.output_dir) if args.output_dir else cfg_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    metadata = {
        "git_hash": git_hash(),
        "git_branch": git_branch(),
        "build_version": build_version(),
        "flow_id": args.flow_id,
        "hostname": socket.gethostname(),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "slurm_job_id": os.environ.get("SLURM_JOB_ID"),
    }

    print("=" * 60)
    print("mypkg.entry starting")
    for k, v in metadata.items():
        print(f"  {k}: {v}")
    print(f"  config: {cfg_path}")
    print("=" * 60)

    sleep_s = float(cfg.get("sleep_seconds", 1.5))
    print(f"simulating work for {sleep_s}s ...")
    time.sleep(sleep_s)

    result = {
        "metadata": metadata,
        "config": cfg,
        "result": {
            "score": cfg.get("score", 0.42),
            "elapsed_seconds": sleep_s,
        },
    }
    out_file = out_dir / f"results_{args.flow_id}.json"
    out_file.write_text(json.dumps(result, indent=2))
    print(f"wrote {out_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
