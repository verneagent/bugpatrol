from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from bugpatrol.worktree import (
    resolve_reference_branch,
    resolve_triage_branch,
    worktree_merge_abort,
    worktree_merge_base,
    worktree_unresolved_conflict_markers,
)


class FakeGitDriver:
    def __init__(
        self,
        *,
        existing_branches: set[str] | None = None,
        ancestors: set[str] | None = None,
        commits: set[str] | None = None,
    ) -> None:
        self.existing_branches = existing_branches or set()
        self.ancestors = ancestors or set()
        self.commits = commits or set()
        self.fetched: list[str] = []

    def fetch_branch(self, branch: str) -> None:
        self.fetched.append(branch)

    def remote_branch_exists(self, branch: str) -> bool:
        return branch in self.existing_branches

    def is_ancestor(self, sha: str, ref: str) -> bool:
        return sha in self.ancestors

    def commit_exists(self, sha: str) -> bool:
        return sha in self.commits


class ResolveTriageBranchTest(unittest.TestCase):
    def test_main_target_analyzes_main_without_touching_git(self) -> None:
        driver = FakeGitDriver()
        resolution = resolve_triage_branch(driver, target_branch="main")

        self.assertEqual(resolution.analyzed_branch, "main")
        self.assertEqual(resolution.ref, "origin/main")
        self.assertEqual(resolution.status, "main")
        self.assertEqual(resolution.note, "")
        self.assertFalse(resolution.needs_info)
        self.assertEqual(driver.fetched, [])

    def test_absent_target_resolves_to_main(self) -> None:
        resolution = resolve_triage_branch(FakeGitDriver(), target_branch="")
        self.assertEqual(resolution.status, "main")

    def test_existing_remote_branch_is_analyzed(self) -> None:
        driver = FakeGitDriver(existing_branches={"feature/x"})
        resolution = resolve_triage_branch(driver, target_branch="feature/x")

        self.assertEqual(resolution.analyzed_branch, "feature/x")
        self.assertEqual(resolution.ref, "origin/feature/x")
        self.assertEqual(resolution.status, "branch")
        self.assertIn("feature/x", resolution.note)
        self.assertFalse(resolution.needs_info)
        self.assertEqual(driver.fetched, ["feature/x"])

    def test_merged_branch_falls_back_to_main_with_note(self) -> None:
        driver = FakeGitDriver(ancestors={"abc123"})
        resolution = resolve_triage_branch(
            driver, target_branch="feature/x", branch_tip_sha="abc123"
        )

        self.assertEqual(resolution.analyzed_branch, "main")
        self.assertEqual(resolution.ref, "origin/main")
        self.assertEqual(resolution.status, "merged")
        self.assertIn("已合并", resolution.note)
        self.assertFalse(resolution.needs_info)

    def test_deleted_branch_with_fetchable_tip_analyzes_commit(self) -> None:
        driver = FakeGitDriver(commits={"abc123"})
        resolution = resolve_triage_branch(
            driver, target_branch="feature/x", branch_tip_sha="abc123"
        )

        self.assertEqual(resolution.analyzed_branch, "feature/x")
        self.assertEqual(resolution.ref, "abc123")
        self.assertEqual(resolution.status, "tip")
        self.assertFalse(resolution.needs_info)

    def test_deleted_branch_unknown_tip_falls_back_and_flags_needs_info(self) -> None:
        driver = FakeGitDriver()
        resolution = resolve_triage_branch(
            driver, target_branch="feature/x", branch_tip_sha="abc123"
        )

        self.assertEqual(resolution.analyzed_branch, "main")
        self.assertEqual(resolution.ref, "origin/main")
        self.assertEqual(resolution.status, "deleted_fallback")
        self.assertTrue(resolution.needs_info)

    def test_deleted_branch_without_tip_falls_back_and_flags_needs_info(self) -> None:
        driver = FakeGitDriver()
        resolution = resolve_triage_branch(driver, target_branch="feature/x")

        self.assertEqual(resolution.status, "deleted_fallback")
        self.assertTrue(resolution.needs_info)


class ResolveReferenceBranchTest(unittest.TestCase):
    def test_existing_branch_is_analyzed(self) -> None:
        driver = FakeGitDriver(existing_branches={"feature/x"})
        resolution = resolve_reference_branch(
            driver, repo="org/weaver", branch="feature/x"
        )

        self.assertEqual(resolution.repo, "org/weaver")
        self.assertEqual(resolution.analyzed_branch, "feature/x")
        self.assertEqual(resolution.ref, "origin/feature/x")
        self.assertEqual(resolution.status, "branch")
        self.assertIn("feature/x", resolution.note)
        self.assertEqual(driver.fetched, ["feature/x"])

    def test_missing_branch_falls_back_to_main_no_needs_info(self) -> None:
        driver = FakeGitDriver()
        resolution = resolve_reference_branch(
            driver, repo="org/weaver", branch="feature/x"
        )

        self.assertEqual(resolution.analyzed_branch, "main")
        self.assertEqual(resolution.ref, "origin/main")
        self.assertEqual(resolution.status, "main")
        self.assertIn("按 main", resolution.note)
        self.assertFalse(hasattr(resolution, "needs_info"))

    def test_main_branch_skips_git(self) -> None:
        driver = FakeGitDriver()
        resolution = resolve_reference_branch(driver, repo="org/weaver", branch="main")

        self.assertEqual(resolution.analyzed_branch, "main")
        self.assertEqual(resolution.ref, "origin/main")
        self.assertEqual(resolution.status, "main")
        self.assertEqual(resolution.note, "")
        self.assertEqual(driver.fetched, [])


def _git(path: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)


def _make_fix_clone(root: Path):
    """origin repo + a clone whose fix branch diverges from origin/main.

    Returns (origin, clone). The clone has `origin` remote set and a checked-out
    fix branch; callers move origin/main to create clean or conflicting merges.
    """
    origin = root / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "main")
    _git(origin, "config", "user.email", "t@t.test")
    _git(origin, "config", "user.name", "t")
    (origin / "file.txt").write_text("base\n")
    (origin / "other.txt").write_text("other\n")
    _git(origin, "add", "-A")
    _git(origin, "commit", "-q", "-m", "init")

    clone = root / "clone"
    subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True, capture_output=True)
    _git(clone, "config", "user.email", "t@t.test")
    _git(clone, "config", "user.name", "t")
    _git(clone, "checkout", "-q", "-b", "fix")
    (clone / "file.txt").write_text("fix\n")
    _git(clone, "add", "-A")
    _git(clone, "commit", "-q", "-m", "fix change")
    return origin, clone


class WorktreeMergeBaseTest(unittest.TestCase):
    def test_conflicting_merge_reports_conflicted_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            origin, clone = _make_fix_clone(root)
            # origin/main moves on the SAME line the fix touched -> conflict.
            (origin / "file.txt").write_text("moved\n")
            _git(origin, "add", "-A")
            _git(origin, "commit", "-q", "-m", "origin move")

            outcome = worktree_merge_base(clone, base_branch="main")
            self.assertEqual(outcome.status, "conflict")
            self.assertEqual(outcome.conflicted_files, ("file.txt",))
            # Markers are present until resolved.
            self.assertEqual(
                worktree_unresolved_conflict_markers(clone, ("file.txt",)), ("file.txt",)
            )

    def test_clean_merge_when_target_moves_elsewhere(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            origin, clone = _make_fix_clone(root)
            # origin/main moves a DIFFERENT file -> merges cleanly, auto-commits.
            (origin / "other.txt").write_text("other moved\n")
            _git(origin, "add", "-A")
            _git(origin, "commit", "-q", "-m", "origin move other")

            outcome = worktree_merge_base(clone, base_branch="main")
            self.assertEqual(outcome.status, "clean")
            self.assertEqual(outcome.conflicted_files, ())

    def test_abort_restores_pre_merge_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            origin, clone = _make_fix_clone(root)
            (origin / "file.txt").write_text("moved\n")
            _git(origin, "add", "-A")
            _git(origin, "commit", "-q", "-m", "origin move")

            worktree_merge_base(clone, base_branch="main")
            worktree_merge_abort(clone)
            self.assertEqual((clone / "file.txt").read_text(), "fix\n")
            self.assertEqual(
                worktree_unresolved_conflict_markers(clone, ("file.txt",)), ()
            )

    def test_resolved_markers_are_detected_as_clean(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            origin, clone = _make_fix_clone(root)
            (origin / "file.txt").write_text("moved\n")
            _git(origin, "add", "-A")
            _git(origin, "commit", "-q", "-m", "origin move")

            worktree_merge_base(clone, base_branch="main")
            # Agent resolves the conflict (removes all markers).
            (clone / "file.txt").write_text("resolved\n")
            self.assertEqual(
                worktree_unresolved_conflict_markers(clone, ("file.txt",)), ()
            )


if __name__ == "__main__":
    unittest.main()
