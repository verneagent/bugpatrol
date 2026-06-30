from __future__ import annotations

import unittest
from pathlib import Path

from bugpatrol.config import load_project_config
from bugpatrol.fix_notify import apply_fix_notification
from bugpatrol.intake import IntakeRecord
from bugpatrol.intake_workflow import IntakeWorkflow
from bugpatrol.testing.fakes import FakeGitHubIssuesClient, FakeLarkMessengerClient


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


if __name__ == "__main__":
    unittest.main()
