from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from bugpatrol.backfill import BackfillResult
from bugpatrol.config import load_project_config
from bugpatrol.intake_workflow import IntakeWorkflow
from bugpatrol.lark import LarkMessage
from bugpatrol.testing.fakes import FakeGitHubIssuesClient, FakeLarkMessengerClient
from bugpatrol.watcher import run_polling_watcher


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


if __name__ == "__main__":
    unittest.main()
