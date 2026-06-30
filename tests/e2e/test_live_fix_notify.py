from __future__ import annotations

import os
import time
import unittest
from datetime import UTC, datetime
from pathlib import Path

from bugpatrol.config import load_project_config
from bugpatrol.fix_notify import apply_fix_notification
from bugpatrol.github import GitHubCliIssuesClient
from bugpatrol.github_fields import GitHubIssueFieldsClient
from bugpatrol.intake import IntakeRecord
from bugpatrol.intake_workflow import IntakeWorkflow
from bugpatrol.lark import LarkOpenApiMessengerClient


@unittest.skipUnless(os.environ.get("BUGPATROL_LIVE_E2E") == "1", "live e2e is opt-in")
class LiveFixNotifyE2ETest(unittest.TestCase):
    def test_live_explicit_pr_notification_is_sent_once(self) -> None:
        config = load_project_config(Path("projects/todo-sandbox.toml"))
        app_secret = os.environ[config.lark.app_secret_env]
        issue_fields = GitHubIssueFieldsClient()
        github = GitHubCliIssuesClient(issue_fields=issue_fields, project_config=config)
        lark = LarkOpenApiMessengerClient(app_id=config.lark.app_id, app_secret=app_secret)
        workflow = IntakeWorkflow(config=config, github=github, lark=lark)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        pr = f"999{stamp[-4:]}"
        needle = f"{config.github_repo}#{pr}"
        created_issue_number: int | None = None

        try:
            seed = lark.send_chat_message(
                chat_id=config.lark.chat_id,
                text=f"BugPatrol live fix notify seed {stamp}.",
            )
            intake = workflow.process(
                IntakeRecord(
                    reporter_name="BugPatrol Live E2E",
                    reporter_open_id=config.lark.bot_open_id,
                    created_at=datetime.now(UTC).isoformat(),
                    chat_id=config.lark.chat_id,
                    root_id=f"live_fix_notify_{stamp}",
                    message_id=seed.message_id,
                    original_text=f"[test] Todo fix notify {stamp}",
                )
            )
            created_issue_number = intake.issue.number

            first = apply_fix_notification(
                repo=config.github_repo,
                issue_number=intake.issue.number,
                event="pr_opened",
                pr=pr,
                dry_run=False,
                github=github,
                lark=lark,
            )
            second = apply_fix_notification(
                repo=config.github_repo,
                issue_number=intake.issue.number,
                event="pr_opened",
                pr=pr,
                dry_run=False,
                github=github,
                lark=lark,
            )

            self.assertTrue(first.lark_sent)
            self.assertTrue(second.duplicate_skipped)
            self.assertEqual(_recent_message_count(lark=lark, chat_id=config.lark.chat_id, needle=needle), 1)
            comments = github.list_issue_comments(repo=config.github_repo, issue_number=intake.issue.number)
            self.assertEqual(sum(1 for comment in comments if "BUGPATROL_FIX_META" in comment.body), 1)
        finally:
            if created_issue_number is not None:
                github.close_issue(repo=config.github_repo, issue_number=created_issue_number)


def _recent_message_count(*, lark: LarkOpenApiMessengerClient, chat_id: str, needle: str) -> int:
    for _ in range(5):
        messages = lark.list_chat_messages(chat_id=chat_id, limit=20)
        count = sum(1 for message in messages if needle in message.text)
        if count:
            return count
        time.sleep(1)
    return 0


if __name__ == "__main__":
    unittest.main()
