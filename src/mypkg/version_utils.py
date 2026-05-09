"""Read git/build metadata baked into the wheel by CI.

When running from a built wheel, ``mypkg.version`` exists and contains the
three values written by ``.github/workflows/build-wheel.yml``. When running
from a working tree without a build, the import fails and we surface that
fact rather than silently substituting fresh values — a run with
``git_hash == "-1"`` is the visible "not reproducible" marker.
"""


def git_hash() -> str:
    try:
        from mypkg.version import git_hash as v
    except ImportError:
        return "-1"
    return v


def git_branch() -> str:
    try:
        from mypkg.version import git_branch as v
    except ImportError:
        return "None"
    return v


def build_version() -> str:
    try:
        from mypkg.version import build_version as v
    except ImportError:
        return "-1"
    return v
