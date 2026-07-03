from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from bugpatrol.config import load_project_config
from bugpatrol.event_watcher import iter_json_event_lines, run_lark_event_watcher
from bugpatrol.intake_workflow import IntakeWorkflow
from bugpatrol.testing.fakes import FakeGitHubIssuesClient, FakeLarkMessengerClient
from bugpatrol.triage_queue import TriageRequest, TriageRequestQueue


def event_payload(**overrides: object) -> dict[str, object]:
    message = {
        "message_id": "om_1",
        "chat_id": "oc_d371f022f168b567a141ced142691894",
        "root_id": "",
        "msg_type": "text",
        "create_time": "1000",
        "body": {"content": json.dumps({"text": "Todo 空状态不显示"})},
    }
    message.update(overrides)
    return {
        "schema": "2.0",
        "event": {
            "sender": {"sender_type": "user", "id": {"open_id": "ou_user"}},
            "message": message,
        },
    }


class EventWatcherTest(unittest.TestCase):
    def test_iter_json_event_lines_skips_empty_lines(self) -> None:
        events = list(iter_json_event_lines(["\n", json.dumps({"event": {}}), "  "]))

        self.assertEqual(events, [{"event": {}}])

    def test_run_lark_event_watcher_processes_event_payloads(self) -> None:
        config = load_project_config(Path("projects/todo-sandbox.toml"))
        github = FakeGitHubIssuesClient()
        lark = FakeLarkMessengerClient()
        workflow = IntakeWorkflow(config=config, github=github, lark=lark)

        result = run_lark_event_watcher(
            config=config,
            event_payloads=[event_payload()],
            lark=lark,  # type: ignore[arg-type]
            workflow=workflow,
        )

        self.assertEqual(result.scanned, 1)
        self.assertEqual(result.processed, 1)
        self.assertEqual(github.created[0].issue.number, 1)
        self.assertEqual(result.events[0].reason, "created")

    def test_run_lark_event_watcher_logs_and_dispatches_triage(self) -> None:
        config = load_project_config(Path("projects/todo-sandbox.toml"))
        github = FakeGitHubIssuesClient()
        lark = FakeLarkMessengerClient()
        workflow = IntakeWorkflow(config=config, github=github, lark=lark)
        dispatcher = RecordingDispatcher()

        with tempfile.TemporaryDirectory() as temp:
            event_log = Path(temp) / "events.jsonl"
            queue = TriageRequestQueue(Path(temp) / "triage-queue.json")
            result = run_lark_event_watcher(
                config=config,
                event_payloads=[event_payload()],
                lark=lark,  # type: ignore[arg-type]
                workflow=workflow,
                event_log_path=event_log,
                triage_queue=queue,
                triage_quiet_seconds=0,
                triage_dispatcher=dispatcher,
            )
            events = [json.loads(line) for line in event_log.read_text().splitlines()]

        self.assertEqual(result.processed, 1)
        self.assertEqual(events[0]["event"], "event_watch_scan")
        self.assertEqual(events[1]["event"], "lark_message")
        self.assertEqual(dispatcher.requests[0].issue_number, 1)


class RecordingDispatcher:
    def __init__(self) -> None:
        self.requests: list[TriageRequest] = []

    def dispatch(self, request: TriageRequest) -> object:
        self.requests.append(request)
        return object()


if __name__ == "__main__":
    unittest.main()
