from __future__ import annotations

import unittest

from bugpatrol.worktree import resolve_triage_branch


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


if __name__ == "__main__":
    unittest.main()
