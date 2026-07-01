from __future__ import annotations

import json
import unittest
from pathlib import Path

from bugpatrol.backfill import intake_record_from_lark_message, should_skip_message
from bugpatrol.config import load_project_config
from bugpatrol.intake_workflow import IntakeWorkflow
from bugpatrol.lark_events import lark_message_from_event
from bugpatrol.testing.fakes import FakeGitHubIssuesClient, FakeLarkMessengerClient


class LarkEventIntakeLoopE2ETest(unittest.TestCase):
    def test_lark_event_payload_creates_github_issue(self) -> None:
        config = load_project_config(Path("projects/todo-sandbox.toml"))
        github = FakeGitHubIssuesClient()
        lark = FakeLarkMessengerClient()
        workflow = IntakeWorkflow(config=config, github=github, lark=lark)
        message = lark_message_from_event(
            {
                "event": {
                    "sender": {"sender_type": "user", "id": {"open_id": "ou_reporter"}},
                    "message": {
                        "message_id": "om_event",
                        "chat_id": config.lark.chat_id,
                        "msg_type": "text",
                        "create_time": "2026-07-01T00:00:00Z",
                        "body": {"content": json.dumps({"text": "Todo 保存后列表没刷新"})},
                    },
                },
            }
        )

        self.assertFalse(should_skip_message(message, bot_open_id=config.lark.bot_open_id))
        outcome = workflow.process(intake_record_from_lark_message(message))

        self.assertEqual(outcome.action, "created")
        self.assertEqual(github.created[0].fields["Triage status"], "Pending")
        self.assertIn("Todo 保存后列表没刷新", github.created[0].issue.body)
        self.assertIn("已创建 GitHub issue", lark.replies[0].text)


if __name__ == "__main__":
    unittest.main()
