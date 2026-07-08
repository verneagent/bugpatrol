from __future__ import annotations

import json
import unittest
from pathlib import Path

from bugpatrol.backfill import run_lark_backfill
from bugpatrol.config import load_project_config
from bugpatrol.triage_context import build_triage_context
from bugpatrol.intake_workflow import IntakeWorkflow
from bugpatrol.lark import DownloadedLarkResource, LarkMessage
from bugpatrol.resources import LarkResourceRef, ResourcePolicy
from bugpatrol.testing.fakes import FakeGitHubIssuesClient, FakeLarkMessengerClient


class FakeLarkHistory(FakeLarkMessengerClient):
    def __init__(self, messages: list[LarkMessage]) -> None:
        super().__init__()
        self._messages = messages
        self.downloads: list[tuple[str, str, str]] = []

    def list_chat_messages(self, *, chat_id: str, limit: int = 20) -> list[LarkMessage]:
        return self._messages[:limit]

    def download_message_resource(
        self,
        *,
        message_id: str,
        resource_key: str,
        resource_type: str = "",
    ) -> DownloadedLarkResource:
        self.downloads.append((message_id, resource_key, resource_type))
        if resource_type == "image":
            return DownloadedLarkResource(
                content=b"image-bytes",
                content_type="image/png",
                filename="bug.png",
            )
        return DownloadedLarkResource(
            content=b"video-bytes",
            content_type="video/mp4",
            filename="repro.mp4",
        )


class FakeAssetStore:
    def write(self, *, ref: LarkResourceRef, resource: DownloadedLarkResource) -> str:
        return f"https://github.com/example-org/example-assets/raw/main/.github/issue-assets/{ref.message_id}/{resource.filename}"


class FakeDescriber:
    def describe(self, *, ref: LarkResourceRef, resource: DownloadedLarkResource) -> str:
        return "Video shows tapping Export, then the progress spinner freezes at 80%."


class FakeIssueFields:
    def __init__(self, current_values: dict[str, str]) -> None:
        self.current_values = current_values
        self.writes: list[dict[str, object]] = []

    def get_issue_field_values(self, *, repo: str, issue_number: int) -> dict[str, str]:
        return dict(self.current_values)

    def add_issue_field_values(self, **kwargs: object) -> None:
        self.writes.append(kwargs)
        values = kwargs.get("values")
        if isinstance(values, dict):
            self.current_values.update({str(key): str(value) for key, value in values.items()})


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

        result = run_lark_backfill(
            config=config,
            lark=lark,
            workflow=workflow,
            resource_store=FakeAssetStore(),
            resource_describer=FakeDescriber(),
        )

        self.assertEqual(result.processed, 2)
        self.assertEqual(github.created[0].fields["Evidence"], "文字描述")
        self.assertEqual(len(github.created[0].comments), 1)
        self.assertEqual(lark.downloads, [("om_video", "file_v2_repro", "file")])
        self.assertIn(
            "video: [打开附件](https://github.com/example-org/example-assets/raw/main/.github/issue-assets/om_video/repro.mp4)",
            github.created[0].comments[0],
        )
        self.assertIn("Video shows tapping Export", github.created[0].comments[0])
        with self.subTest("triage context reads media from comments"):
            context = build_triage_context(
                issue=github.created[0].issue,
                comments=github.list_issue_comments(repo=config.github_repo, issue_number=1),
                prd_root=Path("tests/fixtures/empty-prd"),
            )
            self.assertEqual(context.media[0].kind, "video")
            self.assertEqual(
                context.media[0].description,
                "Video shows tapping Export, then the progress spinner freezes at 80%.",
            )

    def test_external_app_topic_text_and_image_followups_mark_done_issue_for_review(self) -> None:
        config = load_project_config(Path("projects/todo-sandbox.toml"))
        github = FakeGitHubIssuesClient()
        issue_fields = FakeIssueFields({"Triage status": "Pending"})
        app_sender = {
            "sender_open_id": "",
            "sender_type": "app",
            "sender_id": "cli_external_reporter",
            "sender_id_type": "app_id",
        }
        lark = FakeLarkHistory(
            [
                LarkMessage(
                    message_id="om_text_followup",
                    chat_id=config.lark.chat_id,
                    root_id="om_root",
                    create_time="2026-07-01T00:01:00Z",
                    msg_type="text",
                    text="补充：iOS 和 Android 都能复现。",
                    raw_content=json.dumps({"text": "补充：iOS 和 Android 都能复现。"}),
                    **app_sender,
                ),
                LarkMessage(
                    message_id="om_root",
                    chat_id=config.lark.chat_id,
                    root_id="om_root",
                    create_time="2026-07-01T00:00:00Z",
                    msg_type="text",
                    text="登录 A 后切换到 B，拨打 A 时 B 仍收到来电。",
                    raw_content=json.dumps({"text": "登录 A 后切换到 B，拨打 A 时 B 仍收到来电。"}),
                    **app_sender,
                ),
            ]
        )
        workflow = IntakeWorkflow(
            config=config,
            github=github,
            lark=lark,
            issue_fields=issue_fields,
        )

        first = run_lark_backfill(config=config, lark=lark, workflow=workflow)
        issue_fields.current_values["Triage status"] = "Done"
        lark._messages = [
            LarkMessage(
                message_id="om_image_followup",
                chat_id=config.lark.chat_id,
                root_id="om_root",
                create_time="2026-07-01T00:02:00Z",
                msg_type="image",
                text="",
                raw_content=json.dumps({"image_key": "img_v2_followup"}),
                **app_sender,
            )
        ]
        second = run_lark_backfill(
            config=config,
            lark=lark,
            workflow=workflow,
            resource_store=FakeAssetStore(),
            resource_describer=ImageDescriber(),
        )

        self.assertEqual(first.processed, 2)
        self.assertEqual([outcome.action for outcome in first.outcomes], ["created", "updated"])
        self.assertEqual(second.processed, 1)
        self.assertEqual(second.outcomes[0].action, "updated")
        self.assertEqual(second.outcomes[0].triage_signal.reason, "material_followup")
        self.assertEqual(issue_fields.writes[-1]["values"], {"Triage status": "Pending"})
        self.assertEqual(len(github.created), 1)
        self.assertIn("Lark app (cli_external_reporter)", github.created[0].issue.body)
        self.assertEqual(len(github.created[0].comments), 2)
        self.assertIn("补充：iOS 和 Android 都能复现。", github.created[0].comments[0])
        self.assertIn(
            "image: [打开附件](https://github.com/example-org/example-assets/raw/main/.github/issue-assets/om_image_followup/bug.png)",
            github.created[0].comments[1],
        )
        self.assertIn(
            "![图片 1](https://github.com/example-org/example-assets/raw/main/.github/issue-assets/om_image_followup/bug.png)",
            github.created[0].comments[1],
        )
        self.assertIn("Screenshot shows account switch call routing", github.created[0].comments[1])
        self.assertEqual(lark.downloads, [("om_image_followup", "img_v2_followup", "image")])
        self.assertEqual([reply.message_id for reply in lark.replies], ["om_root", "om_text_followup", "om_image_followup"])


    def test_video_follow_up_over_duration_limit_records_skipped_evidence(self) -> None:
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

        result = run_lark_backfill(
            config=config,
            lark=lark,
            workflow=workflow,
            resource_store=FakeAssetStore(),
            resource_describer=FakeDescriber(),
            resource_policy=ResourcePolicy(
                max_video_duration_seconds=10,
                video_duration_probe=FakeDurationProbe(11),
            ),
        )

        self.assertEqual(result.processed, 2)
        self.assertEqual(len(github.created[0].comments), 1)
        self.assertIn("resource skipped: video duration is 11.0s", github.created[0].comments[0])
        self.assertNotIn("https://github.com/example-org/example-assets", github.created[0].comments[0])


class FakeDurationProbe:
    def __init__(self, duration: float) -> None:
        self.duration = duration

    def duration_seconds(self, *, ref, resource) -> float:  # type: ignore[no-untyped-def]
        return self.duration


class ImageDescriber:
    def describe(self, *, ref: LarkResourceRef, resource: DownloadedLarkResource) -> str:
        return "Screenshot shows account switch call routing to the wrong device."


if __name__ == "__main__":
    unittest.main()
