"""Ephemeral git worktree isolation and branch resolution for triage runs.

A triage run analyzes the codebase at the branch the reporting topic group
declared (not one triage infers). Each run gets its own detached worktree off
the resolved ref so concurrent runs on a shared checkout never clobber one
another's working tree or index.

The branch may have been merged into main and deleted by the time triage runs,
so resolution is best-effort and falls back to main with a caveat rather than
failing.
"""

from __future__ import annotations

import subprocess
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Protocol
from uuid import uuid4


@dataclass(frozen=True)
class BranchResolution:
    # What we actually analyze ("main" or the feature branch name).
    analyzed_branch: str
    # The branch the topic group declared.
    declared_branch: str
    # Git ref/SHA to check the worktree out at.
    ref: str
    # Human-facing note for the triage comment / Lark message. Empty for main.
    note: str
    # One of: main, branch, merged, tip, deleted_fallback.
    status: str
    # True when the branch was deleted and we could not confirm it merged, so
    # the analysis base is uncertain.
    needs_info: bool = False


class GitDriver(Protocol):
    def fetch_branch(self, branch: str) -> None:
        """Best-effort fetch of a branch's ref; tolerate a missing branch."""

    def remote_branch_exists(self, branch: str) -> bool: ...

    def is_ancestor(self, sha: str, ref: str) -> bool: ...

    def commit_exists(self, sha: str) -> bool: ...


def resolve_triage_branch(
    driver: GitDriver,
    *,
    target_branch: str,
    branch_tip_sha: str = "",
) -> BranchResolution:
    """Resolve the ref a triage run should analyze for a declared branch.

    Cases (see TODO "Branch per topic group"):
      1. Branch exists on remote -> analyze it.
      2. Branch gone but its recorded tip is an ancestor of main -> merged;
         analyze main, annotate.
      3. Branch gone, tip still fetchable -> analyze the recorded commit.
      4. Branch gone, tip unknown/unconfirmed -> fall back to main with a
         strong caveat and flag needs-info.
      5. target_branch main/absent -> analyze main (same code path).
    """
    if not target_branch or target_branch == "main":
        return BranchResolution(
            analyzed_branch="main",
            declared_branch="main",
            ref="origin/main",
            note="",
            status="main",
        )

    driver.fetch_branch(target_branch)
    if driver.remote_branch_exists(target_branch):
        return BranchResolution(
            analyzed_branch=target_branch,
            declared_branch=target_branch,
            ref=f"origin/{target_branch}",
            note=f"分析分支：`{target_branch}`",
            status="branch",
        )

    if branch_tip_sha and driver.is_ancestor(branch_tip_sha, "origin/main"):
        return BranchResolution(
            analyzed_branch="main",
            declared_branch=target_branch,
            ref="origin/main",
            note=f"分支 `{target_branch}` 已合并入 main，按 main 分析",
            status="merged",
        )

    if branch_tip_sha and driver.commit_exists(branch_tip_sha):
        return BranchResolution(
            analyzed_branch=target_branch,
            declared_branch=target_branch,
            ref=branch_tip_sha,
            note=(
                f"分支 `{target_branch}` 已删除，按上报时记录的提交 "
                f"`{branch_tip_sha[:12]}` 分析"
            ),
            status="tip",
        )

    return BranchResolution(
        analyzed_branch="main",
        declared_branch=target_branch,
        ref="origin/main",
        note=(
            f"分支 `{target_branch}` 已删除且无法确认是否合并，暂按 main 分析"
            "（结论可能不准确）"
        ),
        status="deleted_fallback",
        needs_info=True,
    )


class SubprocessGitDriver:
    """GitDriver backed by `git` on a real checkout."""

    def __init__(self, repo_path: Path) -> None:
        self._repo_path = repo_path

    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(self._repo_path), *args],
            check=check,
            capture_output=True,
            text=True,
        )

    def fetch_branch(self, branch: str) -> None:
        # Best-effort: a merged+deleted branch simply won't update any ref.
        self._git("fetch", "origin", branch, check=False)

    def remote_branch_exists(self, branch: str) -> bool:
        result = self._git("ls-remote", "--heads", "origin", branch, check=False)
        return result.returncode == 0 and bool(result.stdout.strip())

    def is_ancestor(self, sha: str, ref: str) -> bool:
        result = self._git("merge-base", "--is-ancestor", sha, ref, check=False)
        return result.returncode == 0

    def commit_exists(self, sha: str) -> bool:
        result = self._git("cat-file", "-e", f"{sha}^{{commit}}", check=False)
        return result.returncode == 0


@contextmanager
def triage_worktree(*, base_repo: Path, ref: str) -> Iterator[Path]:
    """Yield a fresh detached worktree of `base_repo` checked out at `ref`.

    Shares the object DB (cheap) but gives the run its own working tree and
    index. Always removed afterwards; stale worktrees are pruned first.
    """
    subprocess.run(
        ["git", "-C", str(base_repo), "worktree", "prune"],
        check=False,
        capture_output=True,
        text=True,
    )
    worktree_path = base_repo / ".bugpatrol-worktrees" / f"triage-{uuid4().hex}"
    subprocess.run(
        [
            "git",
            "-C",
            str(base_repo),
            "worktree",
            "add",
            "--detach",
            str(worktree_path),
            ref,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    try:
        yield worktree_path
    finally:
        subprocess.run(
            ["git", "-C", str(base_repo), "worktree", "remove", "--force", str(worktree_path)],
            check=False,
            capture_output=True,
            text=True,
        )
