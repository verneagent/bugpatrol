from __future__ import annotations

import os
import unittest
from pathlib import Path

from bugpatrol.backfill import intake_record_from_lark_message, should_skip_message
from bugpatrol.config import load_project_config
from bugpatrol.github import GitHubCliIssuesClient
from bugpatrol.github_fields import GitHubIssueFieldsClient
from bugpatrol.intake_workflow import IntakeWorkflow
from bugpatrol.lark import LarkOpenApiMessengerClient


@unittest.skipUnless(os.environ.get("BUGPATROL_LIVE_E2E") == "1", "live e2e is opt-in")
@unittest.skipUnless(os.environ.get("BUGPATROL_LIVE_LARK_MESSAGE_ID"), "requires a real human Lark message id")
class LiveBackfillWriteE2ETest(unittest.TestCase):
    def test_live_human_lark_message_to_github_issue(self) -> None:
        config = load_project_config(Path("projects/todo-sandbox.toml"))
        app_secret = os.environ[config.lark.app_secret_env]
        lark = LarkOpenApiMessengerClient(app_id=config.lark.app_id, app_secret=app_secret)
        issue_fields = GitHubIssueFieldsClient()
        github = GitHubCliIssuesClient(issue_fields=issue_fields, project_config=config)
        workflow = IntakeWorkflow(config=config, github=github, lark=lark)
        created_issue_number: int | None = None

        message = lark.get_message(
            message_id=os.environ["BUGPATROL_LIVE_LARK_MESSAGE_ID"],
            default_chat_id=config.lark.chat_id,
        )
        self.assertFalse(should_skip_message(message, bot_open_id=config.lark.bot_open_id))
        try:
            outcome = workflow.process(intake_record_from_lark_message(message))
            created_issue_number = outcome.issue.number
            self.assertEqual(outcome.action, "created")
            self.assertEqual(
                github.get_issue_type(repo=config.github_repo, issue_number=outcome.issue.number),
                "Bug",
            )
            field_values = issue_fields.get_issue_field_values(
                repo=config.github_repo,
                issue_number=outcome.issue.number,
            )
            self.assertEqual(field_values["Source"], "Lark")
            self.assertEqual(field_values["Triage status"], "Pending")
        finally:
            if created_issue_number is not None:
                github.close_issue(repo=config.github_repo, issue_number=created_issue_number)


if __name__ == "__main__":
    unittest.main()
