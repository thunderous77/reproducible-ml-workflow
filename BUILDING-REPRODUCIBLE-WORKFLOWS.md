# Building a Commit-Bound Reproducible Experiment Workflow

A general-purpose method for making **every experiment run bound to a specific git commit**, so any historical run can be reproduced byte-for-byte regardless of where `HEAD` has moved since.

This is the pattern distilled from a Slurm + GitHub-Releases design study, a production K8s + GCS implementation that follows the same shape, and the local-execution implementation in *this* repo. The shape is independent of any single deployment.

The goal of this document: give you enough structure that you can port the pattern to any new repo (different scheduler, different storage, different runtime) without re-deriving it from scratch.

---

## 1. The reproducibility contract

A workflow is "commit-bound reproducible" iff it satisfies all four:

1. **Code immutability.** For every released version `V`, the bytes that produce `V`'s behavior live at a durable, content-addressable URL forever. Deleting them breaks reproducibility for `V`'s historical runs.
2. **Code-to-commit binding.** From any artifact `V`, you can recover the exact git commit that produced it (the commit SHA is *baked into* the artifact, not just stamped onto the release).
3. **Run-to-code binding.** Every job execution records, in a durable place, *which* `V` it ran. The record outlives the job.
4. **Visible failure mode.** If reproducibility was broken (someone ran from an unbuilt working tree, or you tried to reproduce a missing/corrupt artifact), this is *visible* in the logs — not silently masked.

If any of these is missing, your "reproducibility" is aspirational, not actual. Most "we just commit before submitting" workflows fail on (1) or (3): the commit gets force-pushed, the wheel gets garbage-collected, the WandB run loses its hash field after a refactor.

---

## 2. The five invariant components

Every implementation of this pattern, regardless of stack, has these five pieces. The choices change; the structure does not.

| Component | Job | Examples |
|---|---|---|
| **(A) Version reservation** | Produce a monotonic identifier for each build | GCS atomic counter / `git rev-list --count HEAD` / DB sequence |
| **(B) SHA-baked artifact** | Build a code artifact whose contents include the git SHA | Python wheel with generated `version.py` / Docker image label / Apptainer SIF |
| **(C) Immutable storage** | Persist the artifact at a per-version URL nothing overwrites | GCS bucket / GitHub Release / R2 / ECR |
| **(D) Bootstrap step** | At job start: pull the artifact for the requested version, prepare runtime, run | K8s pod startup script / sbatch prelude / local subprocess wrapper |
| **(E) Three-place audit trail** | Persist `version` in the run config, the job metadata, and the experiment tracker | GCS config + pod label + WandB summary / Release asset + sbatch `--comment` + WandB / etc |

The **decoupling between (B) and (D)** is the key insight: *one* runtime serves *every* historical version. You don't rebuild the runtime per experiment; you swap the code artifact at startup. This is what makes "run experiment N from 6 months ago" cheap.

---

## 3. Design space — picking each component

### (A) Version reservation

| Option | Pros | Cons |
|---|---|---|
| `git rev-list --count HEAD` | No external state, deterministic, free | Force-push on the build branch moves it backwards (mitigation: tag-collision guard in CI + branch protection) |
| GCS / S3 atomic counter (CAS) | Truly monotonic regardless of git state | Extra service to authenticate against; one more failure mode |
| DB sequence / Redis INCR | Same as above | Same as above |

**Default:** `git rev-list --count HEAD` + tag-collision guard. Simpler is better; force-push is policy-fixable.

### (B) SHA-baked artifact

| Option | When it fits |
|---|---|
| Python wheel with generated `version.py` | Pure Python or Python+native deps where the runtime carries native libs |
| Docker image with build-arg label | Self-contained services / when you can afford rebuild-per-version |
| Apptainer/Singularity `.sif` | HPC environments where docker is unavailable |
| Tarball + manifest | Polyglot / non-Python projects |

**Universal trick:** the artifact bakes a small auto-generated file (e.g. `version.py`, `BUILD_INFO`, `version.json`) containing `git_hash`, `git_branch`, and the build counter. A runtime helper reads it and falls back to a sentinel value (`-1`, `unknown`, `null`) when the file is absent — that's how you get invariant (4), the visible failure mode.

### (C) Immutable storage

| Option | Cost | Notes |
|---|---|---|
| **GitHub Releases** | Free, public or private | 2 GB/file cap. Works great for wheels (1–50 MB). Tag = version, browsable UI. |
| GCS / S3 / R2 | Cheap | Per-tenant quota; need to manage credentials on every consumer |
| Private PyPI (Artifact Registry / Nexus / Verdaccio) | Varies | Useful when you want `pip install` to work transparently |
| Shared filesystem | Free | Only viable when *all* consumers see the same FS |

**Default for new repos:** GitHub Releases. Side effect: PR-able auth model (consumers use `gh auth login` once), no separate storage bill.

**Rule:** never delete old artifacts. If you must clean up, mirror to cold storage first. Deleting one entry breaks reproducibility for every run that used it.

### (D) Bootstrap

This is the variable-shaped piece — what changes most across deployments.

| Scheduler | Bootstrap shape |
|---|---|
| Kubernetes | Pod startup command prefix (`pull_eggs`-style string) |
| Slurm | `sbatch` script that runs the bootstrap script before the entry point |
| Local (this repo) | Subprocess invocation of the same bootstrap script |
| Ray | Runtime env / working-directory upload |
| Pure docker | `ENTRYPOINT` script |

**The bootstrap is identical across schedulers in spirit:**

```
1. resolve PKG_VERSION
2. pull artifact for PKG_VERSION  (idempotent, lock-protected)
3. prepare runtime               (cached if (PKG_VERSION, host arch, py) seen before)
4. pull experiment config        (per-run, by flow_id)
5. exec entry point with config + flow_id in env
```

**Concurrency:** the `pull` and `prepare` steps must be idempotent under concurrent jobs on the same host. Linux has `flock`; macOS doesn't. Use `mkdir`-based locks for portability — `mkdir` is atomic on POSIX, no exotic deps:

```bash
acquire_lock() {
  local lockdir="$1"
  while ! mkdir "$lockdir" 2>/dev/null; do sleep 0.2; done
}
```

**Cache key:** when the runtime is shared across hosts (cluster shared FS, NFS), the cache key must encode `(arch, OS, Python version)` — otherwise an x86 host can hit a venv built for ARM. Path template: `cache/venvs/{arch}-{os}-py{X.Y}/{PKG_VERSION}/`.

### (E) Three-place audit trail

The same `PKG_VERSION` (and `flow_id`) must end up in three places:

1. **Inside the experiment config** — stamped into the config blob before storage, so the config-as-stored knows which code produced it.
2. **In the scheduler/job metadata** — `--comment`, K8s labels, env vars on the worker.
3. **In the experiment tracker** — WandB summary, MLflow tags, etc. This is what lets you grep for "all runs of v0.1.42" later.

Drop any of the three and you'll regret it during a postmortem.

---

## 4. Porting checklist

When applying this pattern to a new repo, work through these in order:

- [ ] **Naming.** Pick `{PKG_NAME}` and `{TAG_PREFIX}` (`v0.1.` is fine). Decide where the cache lives: `~/.cache/{PKG_NAME}/` for local, `/scratch/$USER/{PKG_NAME}/` for cluster.
- [ ] **Layout.** `src/{PKG_NAME}/` (src layout), `pyproject.toml` with `[tool.hatch.build]` (or your build backend's equivalent). **Crucial:** if `version.py` is gitignored, declare it as a build artifact (`artifacts = ["src/{PKG_NAME}/version.py"]` for hatch, equivalent for setuptools/poetry) — otherwise your build backend silently filters it out by `.gitignore` and you ship a wheel without the SHA.
- [ ] **Version helper.** `version_utils.py` with `git_hash()`, `git_branch()`, `build_version()` — each wraps the import in `try/except ImportError` and returns a sentinel on miss.
- [ ] **CI.** A `build-wheel.yml` (or equivalent) that:
  - Computes version from `git rev-list --count HEAD`.
  - Guards against tag collision (`git ls-remote --tags`) before doing any work.
  - Bakes `version.py` and rewrites `pyproject.toml`'s version string.
  - Builds the artifact.
  - Creates a release with `--target <SHA>` (binds the tag to the SHA explicitly).
  - Uploads the artifact.
- [ ] **Submit script.** Resolves the version (default = latest release), generates a `flow_id`, stamps the config with `(flow_id, pkg_version)`, uploads the stamped config as an immutable asset, then invokes the bootstrap (subprocess / SSH+sbatch / K8s API).
- [ ] **Bootstrap script.** Pull artifact (lock-protected, cached), prepare runtime (lock-protected, cached, with arch+OS+Python in cache key), pull config, exec entry.
- [ ] **Entry point.** Reads version metadata via the helpers; logs to your tracker (WandB/MLflow). A run with `git_hash == "-1"` should be visible — don't filter it out.
- [ ] **Branch protection.** Disable force-push on the build branch, or accept that you'll occasionally hit the tag-collision guard.
- [ ] **First-build smoke test.** Inspect the wheel to confirm `version.py` is inside it. *(See §6 below — this is the bug everyone hits.)*

---

## 5. Mapping table — pick your variant

Examples drawn from real systems. Mix and match by row.

| Component | Production K8s+GCS variant | Slurm variant (design doc) | Local variant (this repo) |
|---|---|---|---|
| (A) Version reservation | Object-store atomic counter | `git rev-list --count HEAD` | `git rev-list --count HEAD` |
| (B) Artifact | Wheel (PYTHONPATH-based) + Docker runtime image | Wheel (pip-installed) | Wheel (pip-installed) |
| (C) Storage | GCS + private PyPI | GitHub Releases | GitHub Releases |
| (D) Bootstrap | K8s pod startup `gsutil cp` | `ssh login sbatch -` | `subprocess.run(["bash", "run.sh"])` |
| Concurrency lock | implicit (k8s image cache) | `flock` | `mkdir`-based |
| Cache root | image layers | `/scratch/$USER/...` | `~/.cache/{pkg}/...` |
| (E) Audit places | object-store config + pod label + WandB | Release asset + `--comment` + WandB | Release asset + env vars + WandB |

---

## 6. Gotchas — collected from actually building this

These are the bugs you will hit. Knowing them in advance saves a build cycle each.

**1. Build backend filters by `.gitignore`.**
Hatch and setuptools-scm both default to "honor `.gitignore`" when picking files for the wheel. Since `version.py` is gitignored (CI generates it per-build), the very file that makes the artifact reproducible gets filtered out of the artifact.
**Fix:** declare it as a build artifact. For hatch:
```toml
[tool.hatch.build]
artifacts = ["src/{PKG_NAME}/version.py"]
```
**Test:** `unzip -l dist/*.whl | grep version.py` — if it's missing, your reproducibility is broken.

**2. `gh release upload <path>#<displayName>` is brittle.**
The `#displayName` rename syntax silently dropped the displayName in our setup, leaving the asset on the release with the temp basename. Robust alternative: write the temp file with the final name, then `gh release upload <tag> /tmp/.../experiment_<flow_id>.json --clobber`.

**3. `flock` doesn't exist on macOS.**
The original Slurm-side bootstrap uses `flock`; on macOS it's not present (and `brew install util-linux` puts it in a non-default path). Use `mkdir`-based locks if you want one bootstrap script to work everywhere:
```bash
while ! mkdir "$lockdir" 2>/dev/null; do sleep 0.2; done
trap "rmdir '$lockdir'" EXIT
```

**4. Force-push on the build branch breaks `git rev-list --count`.**
After a force-push, the count can move backwards and collide with an existing tag, producing two artifacts under the same version (one of them garbage). Either disable force-push, or add this CI guard:
```bash
if git ls-remote --tags origin "refs/tags/${TAG}" | grep -q .; then
  echo "::error::Tag ${TAG} already exists. Force-push detected." && exit 1
fi
```

**5. Cross-arch venv cache poisoning.**
On a shared filesystem, an ARM build's venv gets reused by an x86 host and crashes mysteriously. Encode `(arch, OS, Python)` in the cache path: `venvs/{arch}-{os}-py{X.Y}/{version}/`.

**6. Local working-tree runs masquerade as reproducible.**
If `version.py` is gitignored *and* present locally (forgotten leftover from a prior run), the helper returns a stale SHA — the run looks reproducible but actually isn't. Either: never write `version.py` outside CI, or treat `git_hash` non-matching `git rev-parse HEAD` as a warning at import time.

**7. Stdout buffering masks the order of `submit.py` and bootstrap output.**
Cosmetic, but confusing. Add `flush=True` to your submit-side prints — otherwise Python buffers them and they appear after the subprocess output, making timing impossible to follow in logs.

**8. Releases live and die with the repo.**
GitHub Releases are gone if the repo is deleted. If you care about long-term reproducibility, mirror wheels to a separate bucket (R2, B2, HF Hub) as a CI side-effect. Cost is near-zero; durability dramatically improves.

**9. Single-file bind mounts hang Docker Desktop on Mac.**
`docker run -v /host/path/file.json:/container/path/file.json` leaves the container stuck in `Created` state on Docker Desktop for Mac (observed on Docker 29.4 / Apple Silicon). Mount the *parent directory* and reference the file inside it instead — directory mounts work fine. This took two minutes of "why is the container hung" before I diffed against a working `docker run`.

**10. CI image build is single-arch by default.**
`docker/build-push-action@v5` only builds the runner's native arch (amd64) unless you set `platforms:`. M-series Macs pulling that image get `no matching manifest for linux/arm64/v8`. Fix: add `docker/setup-qemu-action@v3` and set `platforms: linux/amd64,linux/arm64`. arm64 will be QEMU-emulated, so build is slower (5–10 min for a heavy image) — acceptable given image rebuilds are rare.

**11. Cross-runtime determinism is a separate problem.**
Even with `OMP_NUM_THREADS=1`, two runs of the same wheel + same config can produce *different* trained model bytes when run under different BLAS backends — e.g. macOS-native venv (Accelerate/OpenBLAS-mac) vs Linux-in-container (OpenBLAS-linux). Accuracy to 6 decimals will match; raw coefficient bits will not.

This is *not* a workflow bug — it's a numerical-library reality. If you need bit-identical model parity across runtimes, you must pick *one* runtime as authoritative (typically the docker image) and forbid running the workflow outside it for results-of-record. Within a single runtime, the workflow is reproducible.

---

## 7. Validating your workflow is actually reproducible

A useful smoke test, run after the first deploy:

1. **The basic run-and-recover loop.** Push a commit, wait for the release, run an experiment with the latest version. Confirm `git_hash` in the results matches the commit SHA.
2. **The history-pin test.** Push a second commit, wait for its release. Now run with `--version v0.1.<N>` (the *first* release). Confirm the run reports the *first* commit's SHA — not the latest.
3. **The audit-trail test.** Inspect the release after a submission. Confirm the experiment config asset exists with `_meta.flow_id` and `_meta.pkg_version` set correctly.
4. **The visible-failure test.** Run the entry script directly from the working tree (no built wheel). Confirm `git_hash == "-1"` shows up in the logs/tracker — i.e., the failure mode is *visible*, not silent.
5. **The force-push test (optional, dangerous).** Force-push the build branch and re-trigger CI. Confirm the tag-collision guard fires. (Reset the branch afterwards.)

If all five pass, your workflow is genuinely reproducible — not just hoping it is.

---

## 8. When *not* to use this pattern

- **One-shot research runs no one will revisit.** A jupyter notebook in a git repo is fine.
- **Stateless services with continuous deployment.** Use the standard CI/CD pipeline; there's no "experiment" to reproduce.
- **You can't afford one extra CI step.** This pattern adds ~30s per push. If your CI is already minutes-long this is invisible; if you're optimizing for sub-second feedback, it's an obstacle.

For everything else — anything where you'll later be asked "what *exactly* did the version of the code that produced this number look like" — build this pattern in from day one. Retrofitting it after a year of experiments is approximately impossible.
