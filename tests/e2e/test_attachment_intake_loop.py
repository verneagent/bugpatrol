from __future__ import annotations

import json
import unittest
from pathlib import Path

from bugpatrol.backfill import run_lark_backfill
from bugpatrol.config import load_project_config
from bugpatrol.triage_context import build_triage_context
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

    def test_video_follow_up_appends_comment_and_appears_in_triage_context(self) -> None:
        config = load_project_config(Path("projects/todo-sandbox.toml"))
        github = FakeGitHubIssuesClient()
        lark = FakeLarkHistory(
            [
                LarkMessage(
                    message_id="om_video",
                    chat_id=config.lark.chat_id,
                    root_id="om_root",
                    sender_open_id="ou_reporter",
                    sender_type="user",
                    create_time="2026-07-01T00:01:00Z",
                    msg_type="media",
                    text="",
                    raw_content=json.dumps({"file_key": "file_v2_repro", "file_name": "repro.mp4"}),
                ),
                LarkMessage(
                    message_id="om_text",
                    chat_id=config.lark.chat_id,
                    root_id="om_root",
                    sender_open_id="ou_reporter",
                    sender_type="user",
                    create_time="2026-07-01T00:00:00Z",
                    msg_type="text",
                    text="导出卡住",
                    raw_content=json.dumps({"text": "导出卡住"}),
                ),
            ]
        )
        workflow = IntakeWorkflow(config=config, github=github, lark=lark)

        result = run_lark_backfill(config=config, lark=lark, workflow=workflow)

        self.assertEqual(result.processed, 2)
        self.assertEqual(github.created[0].fields["Evidence"], "文字描述")
        self.assertEqual(len(github.created[0].comments), 1)
        self.assertIn("video: lark://message/om_video/media/file_v2_repro", github.created[0].comments[0])
        with self.subTest("triage context reads media from comments"):
            context = build_triage_context(
                issue=github.created[0].issue,
                comments=github.list_issue_comments(repo=config.github_repo, issue_number=1),
                prd_root=Path("tests/fixtures/empty-prd"),
            )
            self.assertEqual(context.media[0].kind, "video")
            self.assertEqual(context.media[0].description, "repro.mp4")


if __name__ == "__main__":
    unittest.main()
