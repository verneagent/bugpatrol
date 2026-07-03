from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bugpatrol.backfill import BackfillResult
from bugpatrol.config import load_project_config
from bugpatrol.intake_workflow import IntakeWorkflow
from bugpatrol.lease import FileLease, LeaseHeldError
from bugpatrol.lark import LarkMessage
from bugpatrol.testing.fakes import FakeGitHubIssuesClient, FakeLarkMessengerClient
from bugpatrol.triage_queue import TriageRequest, TriageRequestQueue
from bugpatrol.watcher import dispatch_due_triage, run_polling_watcher


class FakeHistoryLark(FakeLarkMessengerClient):
    def list_chat_messages(self, *, chat_id: str, limit: int = 20) -> list[LarkMessage]:
        return [
            LarkMessage(
                message_id="om_1",
                chat_id=chat_id,
                root_id="om_1",
                sender_open_id="ou_user",
                sender_type="user",
                create_time="1000",
                msg_type="text",
                text="Todo 空状态不显示",
            )
        ]


class WatcherTest(unittest.TestCase):
    def test_run_polling_watcher_once_processes_one_scan(self) -> None:
        config = load_project_config(Path("projects/todo-sandbox.toml"))
        github = FakeGitHubIssuesClient()
        lark = FakeHistoryLark()
        workflow = IntakeWorkflow(config=config, github=github, lark=lark)

        result = run_polling_watcher(
            config=config,
            lark=lark,  # type: ignore[arg-type]
            workflow=workflow,
            once=True,
            interval_seconds=0,
        )

        self.assertEqual(result.iterations, 1)
        self.assertEqual(result.scanned, 1)
        self.assertEqual(result.processed, 1)
        self.assertEqual(len(github.created), 1)

    def test_run_polling_watcher_passes_resource_options_to_backfill(self) -> None:
        config = load_project_config(Path("projects/todo-sandbox.toml"))
        github = FakeGitHubIssuesClient()
        lark = FakeHistoryLark()
        workflow = IntakeWorkflow(config=config, github=github, lark=lark)
        store = object()
        describer = object()

        with patch("bugpatrol.watcher.run_lark_backfill") as backfill:
            backfill.return_value = BackfillResult(scanned=1, processed=0, skipped=1, outcomes=())
            result = run_polling_watcher(
                config=config,
                lark=lark,  # type: ignore[arg-type]
                workflow=workflow,
                once=True,
                interval_seconds=0,
                dry_run=True,
                resource_store=store,  # type: ignore[arg-type]
                resource_describer=describer,  # type: ignore[arg-type]
            )

        self.assertEqual(result.skipped, 1)
        self.assertIs(backfill.call_args.kwargs["resource_store"], store)
        self.assertIs(backfill.call_args.kwargs["resource_describer"], describer)

    def test_run_polling_watcher_can_enqueue_and_dispatch_triage(self) -> None:
        config = load_project_config(Path("projects/todo-sandbox.toml"))
        github = FakeGitHubIssuesClient()
        lark = FakeHistoryLark()
        workflow = IntakeWorkflow(config=config, github=github, lark=lark)
        with tempfile.TemporaryDirectory() as temp:
            queue = TriageRequestQueue(Path(temp) / "triage-queue.json")
            dispatcher = RecordingDispatcher()

            result = run_polling_watcher(
                config=config,
                lark=lark,  # type: ignore[arg-type]
                workflow=workflow,
                once=True,
                interval_seconds=0,
                triage_queue=queue,
                triage_quiet_seconds=0,
                triage_dispatcher=dispatcher,
            )

        self.assertEqual(result.queued_triage, 1)
        self.assertEqual(result.dispatched_triage, 1)
        self.assertEqual(dispatcher.requests[0].issue_number, 1)
        self.assertEqual(queue.due_requests(now=999999), ())

    def test_run_polling_watcher_releases_lease_after_once(self) -> None:
        config = load_project_config(Path("projects/todo-sandbox.toml"))
        github = FakeGitHubIssuesClient()
        lark = FakeHistoryLark()
        workflow = IntakeWorkflow(config=config, github=github, lark=lark)

        with tempfile.TemporaryDirectory() as temp:
            lease_file = Path(temp) / "watch.lock"
            result = run_polling_watcher(
                config=config,
                lark=lark,  # type: ignore[arg-type]
                workflow=workflow,
                once=True,
                interval_seconds=0,
                lease_file=lease_file,
            )

            self.assertEqual(result.iterations, 1)
            self.assertFalse(lease_file.exists())

    def test_run_polling_watcher_rejects_active_lease(self) -> None:
        config = load_project_config(Path("projects/todo-sandbox.toml"))
        github = FakeGitHubIssuesClient()
        lark = FakeHistoryLark()
        workflow = IntakeWorkflow(config=config, github=github, lark=lark)

        with tempfile.TemporaryDirectory() as temp:
            lease_file = Path(temp) / "watch.lock"
            FileLease(lease_file, ttl_seconds=120, owner="other").acquire()

            with self.assertRaises(LeaseHeldError):
                run_polling_watcher(
                    config=config,
                    lark=lark,  # type: ignore[arg-type]
                    workflow=workflow,
                    once=True,
                    interval_seconds=0,
                    lease_file=lease_file,
                )

    def test_run_polling_watcher_writes_event_log(self) -> None:
        config = load_project_config(Path("projects/todo-sandbox.toml"))
        github = FakeGitHubIssuesClient()
        lark = FakeHistoryLark()
        workflow = IntakeWorkflow(config=config, github=github, lark=lark)

        with tempfile.TemporaryDirectory() as temp:
            event_log = Path(temp) / "watch-events.jsonl"
            run_polling_watcher(
                config=config,
                lark=lark,  # type: ignore[arg-type]
                workflow=workflow,
                once=True,
                interval_seconds=0,
                event_log_path=event_log,
            )
            events = [json.loads(line) for line in event_log.read_text().splitlines()]

        self.assertEqual(events[0]["event"], "watch_scan")
        self.assertEqual(events[0]["processed"], 1)
        self.assertEqual(events[1]["event"], "lark_message")
        self.assertEqual(events[1]["action"], "processed")
        self.assertEqual(events[1]["reason"], "created")

    def test_dispatch_due_triage_defers_when_issue_is_running(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            queue = TriageRequestQueue(Path(temp) / "triage-queue.json")
            request = queue.enqueue(
                issue_number=7,
                signal=FakeSignal(),
                quiet_seconds=0,
                now=100,
            )
            self.assertIsNotNone(request)
            dispatcher = RecordingDispatcher()

            dispatched = dispatch_due_triage(
                queue=queue,
                dispatcher=dispatcher,
                triage_quiet_seconds=60,
                status_reader=StaticStatusReader("Running"),
            )

            due = queue.due_requests(now=10**20)
            self.assertEqual(dispatched, 0)
            self.assertEqual(dispatcher.requests, [])
            self.assertTrue(due[0].pending_review)
            self.assertIn("pending_review_running", due[0].reasons)


class RecordingDispatcher:
    def __init__(self) -> None:
        self.requests: list[TriageRequest] = []

    def dispatch(self, request: TriageRequest) -> object:
        self.requests.append(request)
        return object()


class FakeSignal:
    should_enqueue = True
    reason = "material_followup"
    material_message_ids = ("om_1",)
    asset_urls = ()


class StaticStatusReader:
    def __init__(self, status: str) -> None:
        self._status = status

    def triage_status(self, issue_number: int) -> str:
        return self._status


if __name__ == "__main__":
    unittest.main()
