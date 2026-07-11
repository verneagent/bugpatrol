"""Project-neutral bug intake and triage orchestration."""

import sys

# Fail fast with an actionable message. bugpatrol reads TOML config via the
# stdlib `tomllib`, which only exists on 3.11+. Running on an older interpreter
# (e.g. a runner's default python3 that predates 3.11) otherwise dies deep in an
# import with a cryptic `ModuleNotFoundError: No module named 'tomllib'`. This
# guard runs before any submodule import, since `python -m bugpatrol` executes
# this package init first. `requires-python` in pyproject only guards installs,
# not PYTHONPATH-based invocation, so it does not cover the workflow runner path.
if sys.version_info < (3, 11):
    raise RuntimeError(
        "BugPatrol requires Python >= 3.11 (needs stdlib tomllib); "
        f"got {sys.version_info.major}.{sys.version_info.minor}. "
        "Point the runner at a 3.11+ interpreter."
    )

__all__ = ["__version__"]

__version__ = "0.1.0"

