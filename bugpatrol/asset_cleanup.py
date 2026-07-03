"""Cleanup helpers for materialized issue assets."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AssetCleanupResult:
    scanned: int
    matched: int
    deleted: int
    paths: tuple[str, ...]


def cleanup_asset_repo(
    *,
    checkout_path: Path,
    base_path: str,
    message_id_prefix: str = "",
    delete: bool = False,
    push: bool = False,
    branch: str = "main",
    remote_url: str = "origin",
    git: str = "git",
) -> AssetCleanupResult:
    base = checkout_path.expanduser() / base_path.strip("/")
    if not base.exists():
        return AssetCleanupResult(scanned=0, matched=0, deleted=0, paths=())
    scanned_paths = sorted(base.iterdir())
    paths = [path for path in scanned_paths if _matches(path=path, prefix=message_id_prefix)]
    deleted = 0
    if delete:
        for path in paths:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
            deleted += 1
        if push and deleted:
            _commit_and_push(
                checkout_path=checkout_path.expanduser(),
                branch=branch,
                remote_url=remote_url or "origin",
                git=git,
            )
    return AssetCleanupResult(
        scanned=len(scanned_paths),
        matched=len(paths),
        deleted=deleted,
        paths=tuple(str(path) for path in paths),
    )


def _matches(*, path: Path, prefix: str) -> bool:
    if not prefix:
        return True
    return path.name.startswith(prefix)


def _commit_and_push(*, checkout_path: Path, branch: str, remote_url: str, git: str) -> None:
    _run([git, "-C", str(checkout_path), "add", "-A"])
    _run(
        [
            git,
            "-C",
            str(checkout_path),
            "commit",
            "--no-verify",
            "-m",
            "cleanup: bugpatrol assets",
        ],
        allow_no_changes=True,
    )
    _run([git, "-C", str(checkout_path), "push", "--no-verify", remote_url, branch])


def _run(args: list[str], *, allow_no_changes: bool = False) -> None:
    completed = subprocess.run(args, capture_output=True, text=True, check=False)
    if completed.returncode == 0:
        return
    combined = f"{completed.stdout}\n{completed.stderr}"
    if allow_no_changes and "nothing to commit" in combined:
        return
    raise RuntimeError(f"{' '.join(args)} failed: {completed.stderr.strip()}")
