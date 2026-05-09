"""Entry point invoked inside the cached venv (or docker container).

Tiny ML toy — proves the pipeline is genuinely reproducible:
  1. Loads sklearn's iris dataset.
  2. Trains a LogisticRegression with hyperparams from the experiment config.
  3. Outputs accuracy + a SHA256 of the trained coefficients.

Two runs of the same wheel + same config should yield the *same* hash.
That's the empirical proof that "experiment = git commit" actually holds.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import socket
import sys
import time
from pathlib import Path

# Pin numerical-library threading before importing numpy / sklearn.
# Multi-threaded BLAS reductions are non-deterministic across thread counts;
# a single thread guarantees byte-identical results on the same backend.
for var in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
            "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"):
    os.environ.setdefault(var, "1")

import numpy as np  # noqa: E402
from sklearn.datasets import load_iris  # noqa: E402
from sklearn.linear_model import LogisticRegression  # noqa: E402
from sklearn.model_selection import train_test_split  # noqa: E402

from mypkg.version_utils import build_version, git_branch, git_hash


def _coef_hash(model: LogisticRegression) -> str:
    """SHA256 of the trained model's parameters.

    Hashes the bit-pattern of (coefficients || intercepts || classes), so
    any change in trained weights — even a 1-ULP floating-point drift —
    flips the hash.
    """
    h = hashlib.sha256()
    for arr in (model.coef_, model.intercept_, model.classes_):
        h.update(np.ascontiguousarray(arr).tobytes())
    return h.hexdigest()


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
        "numpy": np.__version__,
        "sklearn_version": __import__("sklearn").__version__,
        "slurm_job_id": os.environ.get("SLURM_JOB_ID"),
        "in_docker": Path("/.dockerenv").exists(),
    }

    print("=" * 60)
    print("mypkg.entry starting")
    for k, v in metadata.items():
        print(f"  {k}: {v}")
    print(f"  config: {cfg_path}")
    print("=" * 60)

    # ---- Train the model ----
    seed = int(cfg.get("seed", 42))
    test_size = float(cfg.get("test_size", 0.2))
    C = float(cfg.get("C", 1.0))
    max_iter = int(cfg.get("max_iter", 1000))
    solver = cfg.get("solver", "liblinear")  # single-threaded → deterministic

    X, y = load_iris(return_X_y=True)
    X_train, X_test, y_train, y_test = train_test_split(
        X, y, test_size=test_size, random_state=seed, stratify=y,
    )

    t0 = time.perf_counter()
    model = LogisticRegression(
        C=C, max_iter=max_iter, solver=solver, random_state=seed,
    )
    model.fit(X_train, y_train)
    elapsed = time.perf_counter() - t0

    train_acc = float(model.score(X_train, y_train))
    test_acc = float(model.score(X_test, y_test))
    coef_hash = _coef_hash(model)

    print(f"  trained in {elapsed*1000:.1f} ms")
    print(f"  train_accuracy: {train_acc:.6f}")
    print(f"  test_accuracy:  {test_acc:.6f}")
    print(f"  coef_hash:      {coef_hash[:16]}...")
    print("=" * 60)

    result = {
        "metadata": metadata,
        "config": cfg,
        "metrics": {
            "train_accuracy": train_acc,
            "test_accuracy": test_acc,
            "coef_hash": coef_hash,
            "train_seconds": elapsed,
            "n_train": int(len(X_train)),
            "n_test": int(len(X_test)),
        },
    }
    out_file = out_dir / f"results_{args.flow_id}.json"
    out_file.write_text(json.dumps(result, indent=2))
    print(f"wrote {out_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
