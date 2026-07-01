from __future__ import annotations

import json
import unittest
from pathlib import Path

from bugpatrol.backfill import run_lark_backfill
from bugpatrol.config import load_project_config
from bugpatrol.intake_workflow import IntakeWorkflow
from bugpatrol.lark import LarkMessage
from bugpatrol.testing.fakes import FakeGitHubIssuesClient, FakeLarkMessengerClient


class FakeLarkHistory(FakeLarkMessengerClient):
    def __init__(self, messages: list[LarkMessage]) -> None:
        super().__init__()
        self._messages = messages

    def list_chat_messages(self, *, chat_id: str, limit: int = 20) -> list[LarkMessage]:
        return self._messages[:limit]


class AttachmentIntakeLoopE2ETest(unittest.TestCase):
    def test_image_message_creates_issue_with_attachment_evidence(self) -> None:
        config = load_project_config(Path("projects/todo-sandbox.toml"))
        github = FakeGitHubIssuesClient()
        lark = FakeLarkHistory(
            [
                LarkMessage(
                    message_id="om_image",
                    chat_id=config.lark.chat_id,
                    root_id="om_image",
                    sender_open_id="ou_reporter",
                    sender_type="user",
                    create_time="2026-07-01T00:00:00Z",
                    msg_type="image",
                    text="",
                    raw_content=json.dumps({"image_key": "img_v2_bug"}),
                )
            ]
        )
        workflow = IntakeWorkflow(config=config, github=github, lark=lark)

        result = run_lark_backfill(config=config, lark=lark, workflow=workflow)

        self.assertEqual(result.processed, 1)
        self.assertEqual(github.created[0].fields["Evidence"], "截图")
        self.assertIn("lark://message/om_image/image/img_v2_bug", github.created[0].issue.body)
        self.assertIn("已创建 GitHub issue", lark.replies[0].text)


if __name__ == "__main__":
    unittest.main()
