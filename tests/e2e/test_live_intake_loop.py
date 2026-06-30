from __future__ import annotations

import os
import unittest
from datetime import UTC, datetime
from pathlib import Path

from bugpatrol.config import load_project_config
from bugpatrol.github import GitHubCliIssuesClient
from bugpatrol.github_fields import GitHubIssueFieldsClient
from bugpatrol.intake import IntakeRecord
from bugpatrol.intake_workflow import IntakeWorkflow
from bugpatrol.lark import LarkOpenApiMessengerClient


@unittest.skipUnless(os.environ.get("BUGPATROL_LIVE_E2E") == "1", "live e2e is opt-in")
class LiveIntakeLoopE2ETest(unittest.TestCase):
    def test_live_sandbox_lark_to_github_issue_loop(self) -> None:
        config = load_project_config(Path("projects/todo-sandbox.toml"))
        app_secret = os.environ[config.lark.app_secret_env]
        issue_fields = GitHubIssueFieldsClient()
        github = GitHubCliIssuesClient(
            issue_fields=issue_fields,
            project_config=config,
        )
        lark = LarkOpenApiMessengerClient(app_id=config.lark.app_id, app_secret=app_secret)
        workflow = IntakeWorkflow(config=config, github=github, lark=lark)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        root_id = f"live_e2e_{stamp}"
        created_issue_number: int | None = None

        try:
            first_seed = lark.send_chat_message(
                chat_id=config.lark.chat_id,
                text=f"BugPatrol live e2e seed {stamp}: create issue.",
            )
            first = workflow.process(
                IntakeRecord(
                    reporter_name="BugPatrol Live E2E",
                    reporter_open_id=config.lark.bot_open_id,
                    created_at=datetime.now(UTC).isoformat(),
                    chat_id=config.lark.chat_id,
                    root_id=root_id,
                    message_id=first_seed.message_id,
                    original_text=f"[test] Todo live intake create issue {stamp}",
                )
            )
            created_issue_number = first.issue.number

            second_seed = lark.send_chat_message(
                chat_id=config.lark.chat_id,
                text=f"BugPatrol live e2e seed {stamp}: append follow-up.",
            )
            second = workflow.process(
                IntakeRecord(
                    reporter_name="BugPatrol Live E2E",
                    reporter_open_id=config.lark.bot_open_id,
                    created_at=datetime.now(UTC).isoformat(),
                    chat_id=config.lark.chat_id,
                    root_id=root_id,
                    message_id=second_seed.message_id,
                    original_text=f"[test] Todo live intake follow-up {stamp}",
                )
            )

            found = github.find_issue_by_intake_root(
                repo=config.github_repo,
                chat_id=config.lark.chat_id,
                root_id=root_id,
            )
            self.assertEqual(first.action, "created")
            self.assertEqual(second.action, "updated")
            self.assertIsNotNone(found)
            self.assertEqual(found.number, first.issue.number)
            self.assertEqual(
                github.get_issue_type(repo=config.github_repo, issue_number=first.issue.number),
                "Bug",
            )
            field_values = issue_fields.get_issue_field_values(
                repo=config.github_repo,
                issue_number=first.issue.number,
            )
            self.assertEqual(field_values["Source"], "Lark")
            self.assertEqual(field_values["Intake version"], "v2")
            self.assertEqual(field_values["Triage status"], "Pending")
            self.assertEqual(field_values["Evidence"], "文字描述")
        finally:
            if created_issue_number is not None:
                github.close_issue(repo=config.github_repo, issue_number=created_issue_number)


if __name__ == "__main__":
    unittest.main()
