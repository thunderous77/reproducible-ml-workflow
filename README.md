# reproducible-ml-workflow

A minimal demo of a **commit-bound, reproducible experiment runner**.

Every experiment submission is bound to a specific git commit, so any run
can be reproduced later — even after the working tree has moved on.

This is a **local-execution variant** of the Slurm + GitHub-Releases pattern
described in [`experiment-versioning-workflow.md`](./experiment-versioning-workflow.md).
The reproducibility contract is identical; the only swap is that
`submit.py` invokes `run.sh` as a local subprocess instead of `ssh sbatch`.

For the general method (and how to port this pattern to a new project),
see **[BUILDING-REPRODUCIBLE-WORKFLOWS.md](./BUILDING-REPRODUCIBLE-WORKFLOWS.md)**.

---

## How it works

```
Dev machine                     GitHub                       Local job
   │                              │                             │
   │ git push main ──────────────>│                             │
   │                              │ Actions:                    │
   │                              │  • build_version=0.1.<count>│
   │                              │  • bake version.py (SHA)    │
   │                              │  • uv build wheel           │
   │                              │  • create release v0.1.<N>  │
   │                              │  • upload .whl              │
   │                              │                             │
   │ python scripts/submit.py \   │                             │
   │   --config examples/sample.json                            │
   │ 1. resolve release tag       │                             │
   │ 2. stamp config with flow_id │                             │
   │ 3. gh release upload config ─┼────>(immutable audit trail) │
   │ 4. exec scripts/run.sh ──────┼─────────────────────────────│
   │                              │                             │ run.sh:
   │                              │<── gh release download ─────│  • pull wheel
   │                              │     wheel + config          │  • mkdir-locked
   │                              │                             │    venv cache
   │                              │                             │  • python -m
   │                              │                             │    mypkg.entry
   │                              │                             │  • write
   │                              │                             │    results.json
```

The key invariant: **the wheel for `v0.1.N` is immutable**, and its bundled
`mypkg/version.py` carries the git SHA. Any later run of `v0.1.N` recovers
the same code, with the same hash, regardless of HEAD.

---

## Quick start

### Prerequisites

```bash
# 1. uv (https://docs.astral.sh/uv/)
curl -LsSf https://astral.sh/uv/install.sh | sh

# 2. gh CLI authed against the repo
gh auth login
```

### Trigger the first build

```bash
git push origin main
```

GitHub Actions will compute the build version, bake the SHA, build the
wheel, and publish release `v0.1.<N>` with the `.whl` attached. Watch:

```bash
gh run watch
gh release list --limit 5
```

### Submit a run

```bash
python scripts/submit.py --config examples/sample.json
```

Output:

```
flow_id=ab12cd34ef56 pkg_version=v0.1.1 config=.../sample.json
[run.sh] downloading wheel for v0.1.1 ...
[run.sh] building venv for v0.1.1 at ~/.cache/mypkg/venvs/...
[run.sh] launching mypkg.entry ...
============================================================
mypkg.entry starting
  git_hash: 6f3a...
  git_branch: main
  build_version: 0.1.1
  flow_id: ab12cd34ef56
  ...
============================================================
wrote examples/results_ab12cd34ef56.json
done. flow_id=ab12cd34ef56 pkg_version=v0.1.1
```

Subsequent runs reuse the cached wheel and venv (sub-second startup).

### Pin to a historical commit

```bash
python scripts/submit.py --version v0.1.3 --config examples/sample.json
```

That run **always** gets the code from commit count = 3, regardless of
where `main` is now.

### Offline / local-only run

```bash
python scripts/submit.py --no-upload --config examples/sample.json
```

Skips the config upload (no audit trail, but the wheel is still bound to
the chosen release). Useful for iterating before pushing.

---

## Repository layout

```
reproducible-ml-workflow/
├── .github/workflows/
│   └── build-wheel.yml          # CI: version → bake → build → release
├── pyproject.toml
├── src/mypkg/
│   ├── __init__.py
│   ├── version.py               # CI-generated; gitignored
│   ├── version_utils.py         # safe imports of baked metadata
│   └── entry.py                 # the actual job (stub: prints + sleeps)
├── scripts/
│   ├── submit.py                # local equivalent of `ssh sbatch -`
│   └── run.sh                   # local equivalent of `pull_eggs`
├── examples/
│   └── sample.json
├── README.md
└── BUILDING-REPRODUCIBLE-WORKFLOWS.md
```

---

## What "reproducible" means here

A WandB run with `git_hash == "-1"` is the visible **"not reproducible"**
marker — it tells you the run was launched from a working tree without a
built wheel, so the bytes that produced this run no longer exist anywhere
durable.

If `git_hash != "-1"`, you can recover the exact code with:

```bash
gh release download v0.1.<N> --pattern '*.whl'
uv pip install mypkg-0.1.<N>-py3-none-any.whl
# inspect / re-run
```

---

## Differences from the Slurm variant

| Step | Slurm variant | This (local) variant |
|---|---|---|
| Wheel storage | GitHub Releases | GitHub Releases (same) |
| Submission | `ssh login sbatch -` | `subprocess.run(["bash", "run.sh"])` |
| Job env | `--export PKG_VERSION=...` | parent process env |
| Concurrency lock | `flock` (Linux) | `mkdir`-based (portable) |
| Cache root | `/scratch/$USER/...` | `~/.cache/mypkg/...` |

Everything between "release exists" and "entry.py runs" is byte-for-byte
the same contract.
