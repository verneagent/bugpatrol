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


@dataclass(frozen=True)
class ReferenceResolution:
    # The reference repo (owner/name) this resolution is for.
    repo: str
    # What we actually analyze in the reference repo ("main" or the branch).
    analyzed_branch: str
    # Git ref to check the reference worktree out at.
    ref: str
    # Human-facing note for the triage context. Empty when analyzing main.
    note: str
    # One of: branch, main.
    status: str


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
            # The branch isn't inferred by triage; it's declared by the
            # reporting topic group (config branch_chats). Say "目标分支" so the
            # note reads as scope, not as an analysis conclusion.
            note=f"目标分支：`{target_branch}`",
            status="branch",
        )

    # Refresh origin/main before the ancestor check: on a stale runner checkout
    # a merged-and-deleted branch could otherwise be misclassified as unmerged.
    driver.fetch_branch("main")
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


def resolve_reference_branch(
    driver: GitDriver,
    *,
    repo: str,
    branch: str,
) -> ReferenceResolution:
    """Resolve the ref a triage run should analyze in a reference repo.

    A *thinner* variant of `resolve_triage_branch`: only two outcomes, and a
    missing branch just means this reference repo doesn't participate at that
    branch — it quietly falls back to main and NEVER flags needs-info (that
    would pollute the main issue for a repo the reporter didn't even declare).

      1. `branch` exists on the ref remote -> analyze it.
      2. otherwise -> analyze main.
    """
    if branch and branch != "main":
        driver.fetch_branch(branch)
        if driver.remote_branch_exists(branch):
            return ReferenceResolution(
                repo=repo,
                analyzed_branch=branch,
                ref=f"origin/{branch}",
                note=f"参考库 `{repo}`：分析分支 `{branch}`",
                status="branch",
            )
        return ReferenceResolution(
            repo=repo,
            analyzed_branch="main",
            ref="origin/main",
            note=f"参考库 `{repo}`：无 `{branch}` 分支，按 main 分析",
            status="main",
        )
    return ReferenceResolution(
        repo=repo,
        analyzed_branch="main",
        ref="origin/main",
        note="",
        status="main",
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
        # Use an explicit refspec so refs/remotes/origin/<branch> is actually
        # updated. A bare `git fetch origin <branch>` only writes FETCH_HEAD and
        # leaves origin/<branch> stale, so the worktree add below (checked out at
        # `origin/<branch>`) would analyze an outdated tip.
        self._git(
            "fetch",
            "origin",
            f"+refs/heads/{branch}:refs/remotes/origin/{branch}",
            check=False,
        )

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


@contextmanager
def fix_worktree(*, base_repo: Path, ref: str, branch: str) -> Iterator[Path]:
    """Yield a fresh worktree of `base_repo` on a new `branch` off `ref`.

    Unlike `triage_worktree` (detached, read-only), a fix run needs a real
    branch to commit and push. The worktree and the local branch are always
    removed afterwards; a stale local branch from a previous crashed run is
    force-deleted first so the run is idempotent-safe on one runner.
    """
    subprocess.run(
        ["git", "-C", str(base_repo), "worktree", "prune"],
        check=False,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(base_repo), "branch", "-D", branch],
        check=False,
        capture_output=True,
        text=True,
    )
    worktree_path = base_repo / ".bugpatrol-worktrees" / f"fix-{uuid4().hex}"
    subprocess.run(
        [
            "git",
            "-C",
            str(base_repo),
            "worktree",
            "add",
            "-b",
            branch,
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
        subprocess.run(
            ["git", "-C", str(base_repo), "branch", "-D", branch],
            check=False,
            capture_output=True,
            text=True,
        )


def _git_in(worktree: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(worktree), *args],
        check=check,
        capture_output=True,
        text=True,
    )


def worktree_changed_files(worktree: Path) -> tuple[str, ...]:
    """Repo-relative paths the agent changed in the worktree (staged or not)."""
    _git_in(worktree, "add", "-A")
    result = _git_in(worktree, "diff", "--cached", "--name-only")
    return tuple(line.strip() for line in result.stdout.splitlines() if line.strip())


def worktree_diff_line_count(worktree: Path) -> int:
    """Total added+removed lines of the staged diff (the gate's size metric)."""
    _git_in(worktree, "add", "-A")
    result = _git_in(worktree, "diff", "--cached", "--numstat")
    total = 0
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        added, removed = parts[0], parts[1]
        # Binary files report "-"; count them as 0 lines (their path is still
        # gated separately, and a binary blob has no meaningful line count).
        total += (int(added) if added.isdigit() else 0)
        total += (int(removed) if removed.isdigit() else 0)
    return total


def worktree_commit_all(worktree: Path, *, message: str) -> str:
    """Stage everything and commit; return the new commit SHA."""
    _git_in(worktree, "add", "-A")
    _git_in(worktree, "commit", "-m", message)
    return _git_in(worktree, "rev-parse", "HEAD").stdout.strip()


def worktree_push_branch(worktree: Path, *, branch: str) -> None:
    """Push the fix branch to origin, setting upstream."""
    _git_in(worktree, "push", "--set-upstream", "origin", branch)
