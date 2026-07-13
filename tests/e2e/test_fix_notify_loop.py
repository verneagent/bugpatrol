from __future__ import annotations

import unittest
from dataclasses import replace
from pathlib import Path

from bugpatrol.clients import GitHubPullRequest
from bugpatrol.config import load_project_config
from bugpatrol.fix_notify import (
    FixEventCandidate,
    apply_fix_notification,
    collect_fix_candidates_from_github,
    reconcile_fix_notifications,
)
from bugpatrol.intake import IntakeRecord
from bugpatrol.intake_workflow import IntakeWorkflow
from bugpatrol.testing.fakes import FakeGitHubIssuesClient, FakeLarkMessengerClient


class _CollectingFakeGitHub(FakeGitHubIssuesClient):
    """Fake that also serves the merged-PR / timeline surfaces reconcile scans."""

    def list_issues(self, *, repo: str, state: str = "open"):
        return tuple(
            replace(item.issue, state="closed", state_reason="completed", closed_at="2026-07-10T00:00:00Z")
            for item in self.created
            if item.repo == repo
        )

    def list_merged_pull_requests(self, *, repo: str, limit: int = 30):
        return (
            GitHubPullRequest(
                number=456,
                url=f"https://github.test/{repo}/pull/456",
                title="Fix",
                body="Closes #1",
                closing_issue_numbers=(1,),
                merged_at="2026-07-10T00:00:00Z",
            ),
        )

    def list_issue_timeline(self, *, repo: str, issue_number: int):
        return ({"event": "referenced", "commit_id": "deadbeef"},)


class FixNotifyLoopE2ETest(unittest.TestCase):
    def test_intake_then_pr_notification_dedupes_on_second_run(self) -> None:
        config = load_project_config(Path("projects/todo-sandbox.toml"))
        github = FakeGitHubIssuesClient()
        lark = FakeLarkMessengerClient()
        workflow = IntakeWorkflow(config=config, github=github, lark=lark)
        issue = workflow.process(
            IntakeRecord(
                reporter_name="Reporter",
                reporter_open_id="ou_1",
                created_at="2026-07-01T00:00:00Z",
                chat_id=config.lark.chat_id,
                root_id="om_root",
                message_id="om_1",
                original_text="bug",
            )
        ).issue

        first = apply_fix_notification(
            repo=config.github_repo,
            issue_number=issue.number,
            event="pr_opened",
            pr="456",
            dry_run=False,
            github=github,  # type: ignore[arg-type]
            lark=lark,
        )
        second = apply_fix_notification(
            repo=config.github_repo,
            issue_number=issue.number,
            event="pr_opened",
            pr="456",
            dry_run=False,
            github=github,  # type: ignore[arg-type]
            lark=lark,
        )

        self.assertTrue(first.lark_sent)
        self.assertTrue(second.duplicate_skipped)
        self.assertEqual(len(lark.replies), 2)
        self.assertIn("修复 PR 已创建", lark.replies[1].text)
        self.assertEqual(len(github.created[0].comments), 1)
        self.assertIn("BUGPATROL_FIX_META", github.created[0].comments[0])

    def test_reconcile_loop_dedupes_multiple_reruns(self) -> None:
        config = load_project_config(Path("projects/todo-sandbox.toml"))
        github = FakeGitHubIssuesClient()
        lark = FakeLarkMessengerClient()
        workflow = IntakeWorkflow(config=config, github=github, lark=lark)
        issue = workflow.process(
            IntakeRecord(
                reporter_name="Reporter",
                reporter_open_id="ou_1",
                created_at="2026-07-01T00:00:00Z",
                chat_id=config.lark.chat_id,
                root_id="om_root",
                message_id="om_1",
                original_text="bug",
            )
        ).issue

        first = reconcile_fix_notifications(
            repo=config.github_repo,
            candidates=(FixEventCandidate(event="pr_merged", issue_number=issue.number, pr="456"),),
            dry_run=False,
            github=github,  # type: ignore[arg-type]
            lark=lark,
        )
        rerun = reconcile_fix_notifications(
            repo=config.github_repo,
            candidates=(FixEventCandidate(event="pr_merged", issue_number=issue.number, pr="456"),),
            dry_run=False,
            github=github,  # type: ignore[arg-type]
            lark=lark,
        )

        self.assertEqual(first.sent, 1)
        self.assertEqual(rerun.duplicate_skipped, 1)
        self.assertEqual(len(lark.replies), 2)
        self.assertEqual(len(github.created[0].comments), 1)


    def test_collect_from_github_then_reconcile_dedupes_on_rerun(self) -> None:
        config = load_project_config(Path("projects/todo-sandbox.toml"))
        github = _CollectingFakeGitHub()
        lark = FakeLarkMessengerClient()
        workflow = IntakeWorkflow(config=config, github=github, lark=lark)
        issue = workflow.process(
            IntakeRecord(
                reporter_name="Reporter",
                reporter_open_id="ou_1",
                created_at="2026-07-01T00:00:00Z",
                chat_id=config.lark.chat_id,
                root_id="om_root",
                message_id="om_1",
                original_text="bug",
            )
        ).issue

        candidates = collect_fix_candidates_from_github(
            repo=config.github_repo,
            github=github,  # type: ignore[arg-type]
        )
        # The merged PR is the canonical fix signal. Because #1 is PR-covered the
        # timeline commit is suppressed, and the evidence-less issue_fixed is gone.
        self.assertIn(
            FixEventCandidate(event="pr_merged", issue_number=issue.number, pr="456"),
            candidates,
        )
        self.assertFalse(any(c.event == "issue_fixed" for c in candidates))
        self.assertFalse(any(c.event == "commit_linked" for c in candidates))

        first = reconcile_fix_notifications(
            repo=config.github_repo,
            candidates=candidates,
            dry_run=False,
            github=github,  # type: ignore[arg-type]
            lark=lark,
        )
        rerun_candidates = collect_fix_candidates_from_github(
            repo=config.github_repo,
            github=github,  # type: ignore[arg-type]
        )
        rerun = reconcile_fix_notifications(
            repo=config.github_repo,
            candidates=rerun_candidates,
            dry_run=False,
            github=github,  # type: ignore[arg-type]
            lark=lark,
        )

        self.assertGreaterEqual(first.sent, 1)
        self.assertEqual(rerun.sent, 0)
        self.assertEqual(rerun.duplicate_skipped, first.sent)


if __name__ == "__main__":
    unittest.main()
