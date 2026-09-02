from __future__ import annotations

import dataclasses
import json
import tempfile
import unittest
from pathlib import Path

from bugpatrol import backfill as backfill_module
from bugpatrol.config import load_project_config
from bugpatrol.event_watcher import (
    ReconnectPolicy,
    is_heartbeat_payload,
    iter_json_event_lines,
    iter_reconnecting_event_payloads,
    run_lark_event_watcher,
)
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


class FakeLarkForward(FakeLarkMessengerClient):
    def __init__(self, forward_items: list[dict[str, object]], member_names: dict[str, str] | None = None) -> None:
        super().__init__()
        self._forward_items = list(forward_items)
        self._member_names = dict(member_names or {})
        self.forward_fetches: list[str] = []

    def fetch_forwarded_messages(self, *, message_id: str) -> list[dict[str, object]]:
        self.forward_fetches.append(message_id)
        return list(self._forward_items)

    def list_chat_members(self, *, chat_id: str) -> dict[str, str]:
        return dict(self._member_names)


class EventWatcherTest(unittest.TestCase):
    def test_iter_json_event_lines_skips_empty_lines(self) -> None:
        events = list(iter_json_event_lines(["\n", json.dumps({"event": {}}), "  "]))

        self.assertEqual(events, [{"event": {}}])

    def test_iter_json_event_lines_skips_heartbeats(self) -> None:
        events = list(
            iter_json_event_lines(
                [
                    json.dumps({"type": "heartbeat"}),
                    json.dumps({"header": {"event_type": "ping"}}),
                    json.dumps({"event": {"message": {}}}),
                ]
            )
        )

        self.assertEqual(events, [{"event": {"message": {}}}])
        self.assertTrue(is_heartbeat_payload({"event_type": "pong"}))

    def test_iter_reconnecting_event_payloads_retries_with_backoff(self) -> None:
        calls = 0
        sleeps: list[float] = []

        def connect():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise ConnectionError("socket closed")
            return [json.dumps({"event": {"ok": True}})]

        events = list(
            iter_reconnecting_event_payloads(
                connect=connect,
                policy=ReconnectPolicy(initial_delay_seconds=1, max_delay_seconds=5, multiplier=2),
                sleep=sleeps.append,
            )
        )

        self.assertEqual(events, [{"event": {"ok": True}}])
        self.assertEqual(sleeps, [1])

    def test_iter_reconnecting_event_payloads_respects_max_attempts(self) -> None:
        def connect():
            raise ConnectionError("socket closed")

        with self.assertRaisesRegex(ConnectionError, "socket closed"):
            list(
                iter_reconnecting_event_payloads(
                    connect=connect,
                    policy=ReconnectPolicy(initial_delay_seconds=1, max_attempts=2),
                    sleep=lambda delay: None,
                )
            )

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

    def test_run_lark_event_watcher_applies_since_cutoff(self) -> None:
        config = load_project_config(Path("projects/todo-sandbox.toml"))
        config = dataclasses.replace(
            config,
            intake=dataclasses.replace(config.intake, since="2026-07-06T00:00:00+08:00"),
        )
        github = FakeGitHubIssuesClient()
        lark = FakeLarkMessengerClient()
        workflow = IntakeWorkflow(config=config, github=github, lark=lark)

        result = run_lark_event_watcher(
            config=config,
            event_payloads=[
                event_payload(message_id="om_old", create_time="1783224000000"),
                event_payload(message_id="om_new", create_time="1783310400000"),
            ],
            lark=lark,  # type: ignore[arg-type]
            workflow=workflow,
        )

        self.assertEqual(result.processed, 1)
        self.assertEqual(result.skipped, 1)
        self.assertEqual(
            [(event.message_id, event.action, event.reason) for event in result.events],
            [
                ("om_old", "skipped", "before_intake_since"),
                ("om_new", "processed", "created"),
            ],
        )

    def test_run_lark_event_watcher_skips_orphan_replies(self) -> None:
        config = load_project_config(Path("projects/todo-sandbox.toml"))
        config = dataclasses.replace(
            config,
            intake=dataclasses.replace(config.intake, skip_orphan_replies=True),
        )
        github = FakeGitHubIssuesClient()
        lark = FakeLarkMessengerClient()
        workflow = IntakeWorkflow(config=config, github=github, lark=lark)

        result = run_lark_event_watcher(
            config=config,
            event_payloads=[
                event_payload(message_id="om_orphan", root_id="om_pre_cutover"),
            ],
            lark=lark,  # type: ignore[arg-type]
            workflow=workflow,
        )

        self.assertEqual(result.processed, 0)
        self.assertEqual(result.skipped, 1)
        self.assertEqual(result.events[0].reason, "orphan_reply")
        self.assertEqual(github.created, [])

    def test_run_lark_event_watcher_expands_merged_forward(self) -> None:
        backfill_module._chat_members_cache.clear()
        config = load_project_config(Path("projects/todo-sandbox.toml"))
        github = FakeGitHubIssuesClient()
        inner = [
            {
                "message_id": "om_a",
                "chat_id": "oc_source",
                "msg_type": "text",
                "create_time": "1788322389929",
                "body": {
                    "content": json.dumps({"text": "post有标题只展示两行正文，这个算bug么"}, ensure_ascii=False)
                },
                "sender": {"id": "ou_azer", "id_type": "open_id", "sender_type": "user"},
            }
        ]
        lark = FakeLarkForward(inner, member_names={"ou_azer": "Azer"})
        workflow = IntakeWorkflow(config=config, github=github, lark=lark)

        result = run_lark_event_watcher(
            config=config,
            event_payloads=[
                event_payload(
                    message_id="om_env",
                    root_id="",
                    msg_type="merge_forward",
                    body={"content": "Merged and Forwarded Message"},
                )
            ],
            lark=lark,  # type: ignore[arg-type]
            workflow=workflow,
        )

        self.assertEqual(result.processed, 1)
        self.assertEqual(len(github.created), 1)
        self.assertIn("Azer：post有标题只展示两行正文，这个算bug么", github.created[0].issue.body)
        self.assertEqual(lark.forward_fetches, ["om_env"])

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
