from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from bugpatrol.config import MailConfig, load_project_config
from bugpatrol.intake import IntakeRecord, parse_intake_metadata, render_issue_body
from bugpatrol.lark import DownloadedLarkResource, LarkOpenApiError
from bugpatrol.ledger import JsonMessageLedger
from bugpatrol.mail import MailAddress, MailAttachment, MailMessage
from bugpatrol.resources import LocalResourceStore
from bugpatrol.testing.fakes import FakeGitHubIssuesClient, FakeLarkMessengerClient
from bugpatrol.triage_queue import TriageRequestQueue
from bugpatrol.watch_mail import (
    MailIntakeWorkflow,
    _MailResourceDownloaderAdapter,
    build_mail_record,
    materialize_mail_attachments,
    run_mail_watcher,
)


def make_mail_config() -> object:
    base = load_project_config(Path("projects/example.toml"))
    return replace(
        base,
        mail=MailConfig(
            mailbox="bug@fivedegrees.ai",
            chat_id="oc_mail",
            app_id="cli_mail_app",
            app_secret_env="MAIL_APP_SECRET",
        ),
    )


def make_mail(message_id: str = "mail_1", **overrides: object) -> MailMessage:
    values = {
        "thread_id": "thread_1",
        "subject": "登录后白屏",
        "head_from": MailAddress(name="客户张三", address="zhangsan@example.com"),
        "internal_date_ms": 1783099728900,
        "body_plain_text": "登录后白屏，无报错",
    }
    values.update(overrides)
    return MailMessage(message_id=message_id, **values)  # type: ignore[arg-type]


def make_workflow(
    *,
    github: FakeGitHubIssuesClient | None = None,
    lark: FakeLarkMessengerClient | None = None,
) -> tuple[object, FakeGitHubIssuesClient, FakeLarkMessengerClient, MailIntakeWorkflow]:
    config = make_mail_config()
    github = github or FakeGitHubIssuesClient()
    lark = lark or FakeLarkMessengerClient()
    workflow = MailIntakeWorkflow(config=config, github=github, lark=lark)
    return config, github, lark, workflow


class RecordingMailDownloader:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def download_attachment(self, *, mailbox: str, message_id: str, attachment_id: str) -> DownloadedLarkResource:
        self.calls.append((mailbox, message_id, attachment_id))
        return DownloadedLarkResource(content=b"data", content_type="text/plain", filename="crash.log")


class FakeMailClient:
    """In-memory mailbox: list/get/download with optional failure injection."""

    def __init__(
        self,
        messages: list[MailMessage],
        *,
        list_failures: int = 0,
        fail_fetch: tuple[str, ...] = (),
    ) -> None:
        self._messages = list(messages)
        self._list_failures = list_failures
        self._fail_fetch = set(fail_fetch)
        self._by_id = {message.message_id: message for message in self._messages}
        self.list_calls = 0

    def list_messages(
        self,
        *,
        mailbox: str,
        folder_id: str = "INBOX",
        page_size: int = 20,
        page_token: str = "",
    ) -> tuple[list[MailMessage], bool, str]:
        self.list_calls += 1
        if self._list_failures > 0:
            self._list_failures -= 1
            raise LarkOpenApiError("transient list failure")
        start = int(page_token) if page_token else 0
        batch = self._messages[start : start + page_size]
        has_more = start + page_size < len(self._messages)
        next_token = str(start + page_size) if has_more else ""
        return batch, has_more, next_token

    def get_message(self, *, mailbox: str, message_id: str, format: str = "full") -> MailMessage:
        if message_id in self._fail_fetch:
            raise LarkOpenApiError(f"fetch failed for {message_id}")
        return self._by_id[message_id]


class RecordingDispatcher:
    def __init__(self) -> None:
        self.requests: list[object] = []

    def dispatch(self, request: object) -> object:
        self.requests.append(request)
        return object()


class StaticStatusReader:
    def __init__(self, status: str, *, state: str = "open") -> None:
        self._status = status
        self._state = state

    def issue_state(self, issue_number: int) -> str:
        return self._state

    def triage_status(self, issue_number: int) -> str:
        return self._status


class MailIntakeWorkflowTest(unittest.TestCase):
    def test_process_creates_issue_and_anchors_receipt(self) -> None:
        config, github, lark, workflow = make_workflow()
        record = build_mail_record(config=config, mail=make_mail(), body_text="登录后白屏，无报错", attachments=())

        outcome = workflow.process(record)

        self.assertEqual(outcome.action, "created")
        self.assertEqual(outcome.issue.number, 1)
        self.assertTrue(outcome.triage_signal.should_enqueue)
        self.assertTrue(github.created[0].issue.title.startswith("[邮件] "))
        self.assertIn('"source":"mail"', github.created[0].issue.body)
        # The receipt goes to the dedicated group, then is anchored into the
        # body meta so later replies thread onto it (never the customer).
        self.assertEqual([message.chat_id for message in lark.chat_messages], ["oc_mail"])
        metadata = parse_intake_metadata(github.created[0].issue.body)
        assert metadata is not None
        self.assertEqual(metadata["notify_anchor_message_id"], "om_sent_1")
        self.assertEqual(metadata["chat_id"], "oc_mail")

    def test_process_updated_appends_comment_and_replies_to_anchor(self) -> None:
        config, github, lark, workflow = make_workflow()
        workflow.process(build_mail_record(config=config, mail=make_mail(), body_text="", attachments=()))

        followup = make_mail(message_id="mail_2", body_plain_text="补充：重启后仍白屏")
        outcome = workflow.process(build_mail_record(config=config, mail=followup, body_text="补充：重启后仍白屏", attachments=()))

        self.assertEqual(outcome.action, "updated")
        self.assertTrue(outcome.triage_signal.should_enqueue)
        # The follow-up lands as a comment and threads onto the anchor receipt.
        self.assertEqual(len(github.created[0].comments), 1)
        self.assertIn("## Lark 话题更新", github.created[0].comments[0])
        self.assertIn('"message_id":"mail_2"', github.created[0].comments[0])
        self.assertEqual(len(lark.chat_messages), 1)
        self.assertEqual(len(lark.replies), 1)
        self.assertEqual(lark.replies[0].message_id, "om_sent_1")
        self.assertEqual(lark.replies[0].chat_id, "oc_mail")

    def test_process_duplicate_skips_second_comment_and_receipt(self) -> None:
        config, github, lark, workflow = make_workflow()
        record = build_mail_record(config=config, mail=make_mail(), body_text="", attachments=())
        workflow.process(record)

        outcome = workflow.process(record)

        self.assertEqual(outcome.action, "duplicate")
        self.assertFalse(outcome.triage_signal.should_enqueue)
        self.assertEqual(outcome.lark_reply, "（该邮件之前已处理，跳过重复追加）")
        self.assertEqual(github.created[0].comments, [])
        self.assertEqual(len(lark.chat_messages), 1)
        self.assertEqual(lark.replies, [])

    def test_process_duplicate_with_missing_anchor_self_heals(self) -> None:
        # A crash between issue create and the anchor patch leaves an anchored
        # issue without the anchor: the duplicate replay must post the receipt.
        config, github, lark, workflow = make_workflow()
        record = build_mail_record(config=config, mail=make_mail(), body_text="", attachments=())
        github.create_issue(
            repo="example-org/example-app",
            title="[邮件] 登录后白屏",
            body=render_issue_body(record, language="zh-CN", source="mail"),
            issue_type="Bug",
            fields={"Source": "Lark"},
        )

        outcome = workflow.process(record)

        self.assertEqual(outcome.action, "duplicate")
        self.assertEqual(len(lark.chat_messages), 1)
        metadata = parse_intake_metadata(github.created[0].issue.body)
        assert metadata is not None
        self.assertEqual(metadata["notify_anchor_message_id"], "om_sent_1")

    def test_process_closed_issue_ignores_followup(self) -> None:
        config, github, lark, workflow = make_workflow()
        workflow.process(build_mail_record(config=config, mail=make_mail(), body_text="", attachments=()))
        github.created[0].issue = replace(github.created[0].issue, state="closed")

        followup = make_mail(message_id="mail_2")
        outcome = workflow.process(build_mail_record(config=config, mail=followup, body_text="", attachments=()))

        self.assertEqual(outcome.action, "ignored_closed")
        self.assertFalse(outcome.triage_signal.should_enqueue)
        self.assertIn("已关闭", lark.replies[0].text)
        self.assertEqual(len(lark.chat_messages), 1)

    def test_process_rejects_foreign_chat_id(self) -> None:
        _, _, _, workflow = make_workflow()
        record = IntakeRecord(
            reporter_name="x",
            reporter_open_id="ou_x",
            created_at="1783099728900",
            chat_id="oc_other",
            root_id="root",
            message_id="msg",
            original_text="not mine",
        )
        with self.assertRaises(ValueError):
            workflow.process(record)


class MailAttachmentPipelineTest(unittest.TestCase):
    def test_materialize_skips_inline_and_stores_the_rest(self) -> None:
        downloader = RecordingMailDownloader()
        with tempfile.TemporaryDirectory() as tmp:
            result = materialize_mail_attachments(
                mailbox="bug@fivedegrees.ai",
                message_id="mail/1",
                mail_attachments=(
                    MailAttachment("inline_1", "signature.png", content_type="image/png", is_inline=True),
                    MailAttachment("attach/1", "crash.log", content_type="text/plain"),
                ),
                mail=downloader,
                store=LocalResourceStore(Path(tmp)),
            )

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].kind, "log")
        self.assertEqual(result[0].description, "crash.log")
        self.assertTrue(str(result[0].url).startswith(tmp))
        # The non-inline attachment downloaded with the percent-decoded ids.
        self.assertEqual(downloader.calls, [("bug@fivedegrees.ai", "mail/1", "attach/1")])

    def test_downloader_adapter_unquotes_url_segments(self) -> None:
        downloader = RecordingMailDownloader()
        adapter = _MailResourceDownloaderAdapter(mailbox="bug@fivedegrees.ai", mail=downloader)

        adapter.download_message_resource(message_id="mail%2F1", resource_key="attach%2B1", resource_type="mail")

        self.assertEqual(downloader.calls, [("bug@fivedegrees.ai", "mail/1", "attach+1")])


class RunMailWatcherTest(unittest.TestCase):
    def test_once_processes_scan_and_marks_ledger(self) -> None:
        config, _, _, workflow = make_workflow()
        mail = FakeMailClient([make_mail()])
        with tempfile.TemporaryDirectory() as tmp:
            ledger = JsonMessageLedger(Path(tmp) / "ledger.json")
            result = run_mail_watcher(
                config=config,
                mail=mail,
                workflow=workflow,
                once=True,
                processed_ledger=ledger,
            )

        self.assertEqual(result.iterations, 1)
        self.assertEqual(result.scanned, 1)
        self.assertEqual(result.processed, 1)
        self.assertEqual(result.skipped, 0)
        self.assertTrue(ledger.is_processed("mail_1"))

    def test_dry_run_makes_no_github_writes_and_no_ledger_marks(self) -> None:
        config, github, _, workflow = make_workflow()
        mail = FakeMailClient([make_mail()])
        with tempfile.TemporaryDirectory() as tmp:
            ledger = JsonMessageLedger(Path(tmp) / "ledger.json")
            result = run_mail_watcher(
                config=config,
                mail=mail,
                workflow=workflow,
                once=True,
                dry_run=True,
                processed_ledger=ledger,
            )

        self.assertEqual(result.processed, 1)
        self.assertEqual(github.created, [])
        self.assertFalse(ledger.is_processed("mail_1"))

    def test_transient_scan_failure_retries_then_succeeds(self) -> None:
        config, _, _, workflow = make_workflow()
        mail = FakeMailClient([make_mail()], list_failures=1)

        with contextlib.redirect_stderr(io.StringIO()):
            result = run_mail_watcher(
                config=config,
                mail=mail,
                workflow=workflow,
                max_iterations=2,
                interval_seconds=0,
            )

        self.assertEqual(result.iterations, 2)
        self.assertEqual(result.processed, 1)
        self.assertEqual(mail.list_calls, 2)

    def test_persistent_scan_failure_raises_after_max(self) -> None:
        config, _, _, workflow = make_workflow()
        mail = FakeMailClient([], list_failures=100)

        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(LarkOpenApiError):
                run_mail_watcher(
                    config=config,
                    mail=mail,
                    workflow=workflow,
                    max_iterations=30,
                    interval_seconds=0,
                )

        # MAX_CONSECUTIVE_SCAN_FAILURES=10: it must retry, then crash.
        self.assertEqual(mail.list_calls, 10)

    def test_per_message_fetch_failure_is_skipped_and_not_ledgered(self) -> None:
        config, _, _, workflow = make_workflow()
        mail = FakeMailClient([make_mail()], fail_fetch=("mail_1",))
        with tempfile.TemporaryDirectory() as tmp:
            ledger = JsonMessageLedger(Path(tmp) / "ledger.json")
            with contextlib.redirect_stderr(io.StringIO()):
                result = run_mail_watcher(
                    config=config,
                    mail=mail,
                    workflow=workflow,
                    once=True,
                    processed_ledger=ledger,
                )

        self.assertEqual(result.skipped, 1)
        self.assertEqual(result.processed, 0)
        self.assertFalse(ledger.is_processed("mail_1"))

    def test_triage_enqueue_and_dispatch_wiring(self) -> None:
        config, _, _, workflow = make_workflow()
        mail = FakeMailClient([make_mail()])
        dispatcher = RecordingDispatcher()
        status_reader = StaticStatusReader("Pending")
        with tempfile.TemporaryDirectory() as tmp:
            queue = TriageRequestQueue(Path(tmp) / "queue.json")
            result = run_mail_watcher(
                config=config,
                mail=mail,
                workflow=workflow,
                once=True,
                triage_queue=queue,
                triage_quiet_seconds=0,
                triage_dispatcher=dispatcher,
                triage_status_reader=status_reader,
            )

        self.assertEqual(result.queued_triage, 1)
        self.assertEqual(result.dispatched_triage, 1)
        self.assertEqual([request.issue_number for request in dispatcher.requests], [1])


if __name__ == "__main__":
    unittest.main()
