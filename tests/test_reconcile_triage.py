from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from bugpatrol.config import load_project_config
from bugpatrol.reconcile_triage import reconcile_triage
from bugpatrol.testing.fakes import FakeGitHubIssuesClient
from bugpatrol.triage_runner import append_triage_run_metadata

INTAKE_META = '<!-- BUGPATROL_INTAKE_META:{"source":"lark","chat_id":"oc_x","root_id":"om_r"} -->'
TRIAGE_META_COMMENT = '<!-- BUGPATROL_TRIAGE_META\n{"verdict":"bug"}\nBUGPATROL_TRIAGE_META -->'


def run_meta_comment(started_at: str) -> str:
    return append_triage_run_metadata(
        {"version": 1, "issue": 1, "run_id": "run-1", "started_at": started_at, "context_comment_ids": []}
    )


def seeded_github(repo: str) -> FakeGitHubIssuesClient:
    github = FakeGitHubIssuesClient()
    github.create_issue(repo=repo, title="untriaged", body=f"报告\n{INTAKE_META}", issue_type="Bug", fields={})
    triaged = github.create_issue(
        repo=repo, title="triaged", body=f"报告\n{INTAKE_META}", issue_type="Bug", fields={}
    )
    github.add_issue_comment(repo=repo, issue_number=triaged.number, body=TRIAGE_META_COMMENT)
    github.create_issue(repo=repo, title="manual", body="手工创建的 issue", issue_type="Bug", fields={})
    return github


class FakeIssueFields:
    def __init__(self, values: dict[str, str] | None = None, *, fail_writes: bool = False) -> None:
        self.values = values or {}
        self.fail_writes = fail_writes
        self.writes: list[dict[str, object]] = []

    def get_issue_field_values(self, **kwargs: object) -> dict[str, str]:
        return dict(self.values)

    def add_issue_field_values(self, **kwargs: object) -> None:
        if self.fail_writes:
            raise RuntimeError("field write failed")
        self.writes.append(kwargs)


class ReconcileTriageTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_project_config(Path("projects/todo-sandbox.toml"))
        self.github = seeded_github(self.config.github_repo)

    def test_dry_run_lists_untriaged_managed_issues(self) -> None:
        result = reconcile_triage(config=self.config, github=self.github, execute=False)

        self.assertEqual(result.scanned, 3)
        self.assertEqual([candidate.issue_number for candidate in result.candidates], [1])
        self.assertEqual(
            [(event.issue_number, event.action, event.reason) for event in result.events],
            [
                (2, "skipped", "already_triaged"),
                (3, "skipped", "not_bugpatrol_managed"),
                (1, "candidate", "dry_run"),
            ],
        )
        self.assertEqual(result.failed, ())

    def test_execute_runs_injected_triage_per_candidate(self) -> None:
        ran: list[int] = []

        def run_triage(issue_number: int) -> str:
            ran.append(issue_number)
            return "applied"

        result = reconcile_triage(
            config=self.config,
            github=self.github,
            execute=True,
            run_triage=run_triage,
        )

        self.assertEqual(ran, [1])
        self.assertIn((1, "triaged", "applied"), [(e.issue_number, e.action, e.reason) for e in result.events])

    def test_event_reports_the_run_status_instead_of_a_blanket_executed(self) -> None:
        result = reconcile_triage(
            config=self.config,
            github=self.github,
            execute=True,
            run_triage=lambda issue_number: "superseded",
        )

        self.assertIn(
            (1, "triaged", "superseded"), [(e.issue_number, e.action, e.reason) for e in result.events]
        )

    def test_skips_issues_with_a_triage_run_still_in_flight(self) -> None:
        started = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
        self.github.add_issue_comment(
            repo=self.config.github_repo, issue_number=1, body=run_meta_comment(started)
        )

        result = reconcile_triage(config=self.config, github=self.github, execute=False)

        self.assertEqual(result.candidates, ())
        self.assertIn(
            (1, "skipped", "triage_in_flight"), [(e.issue_number, e.action, e.reason) for e in result.events]
        )

    def test_retriages_when_the_run_marker_is_older_than_the_workflow_timeout(self) -> None:
        started = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        self.github.add_issue_comment(
            repo=self.config.github_repo, issue_number=1, body=run_meta_comment(started)
        )

        result = reconcile_triage(config=self.config, github=self.github, execute=False)

        self.assertEqual([candidate.issue_number for candidate in result.candidates], [1])

    def test_retriages_when_the_in_flight_run_already_reported_failure(self) -> None:
        started = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
        self.github.add_issue_comment(
            repo=self.config.github_repo, issue_number=1, body=run_meta_comment(started)
        )
        self.github.add_issue_comment(
            repo=self.config.github_repo, issue_number=1, body="## BugPatrol triage failed\n\n炸了"
        )

        result = reconcile_triage(config=self.config, github=self.github, execute=False)

        self.assertEqual([candidate.issue_number for candidate in result.candidates], [1])

    def test_execute_records_failure_and_continues(self) -> None:
        self.github.create_issue(
            repo=self.config.github_repo,
            title="untriaged 2",
            body=f"另一个报告\n{INTAKE_META}",
            issue_type="Bug",
            fields={},
        )

        def run_triage(issue_number: int) -> str:
            if issue_number == 1:
                raise RuntimeError("agent exploded")
            return "applied"

        result = reconcile_triage(
            config=self.config,
            github=self.github,
            execute=True,
            run_triage=run_triage,
        )

        actions = {event.issue_number: (event.action, event.reason) for event in result.events if event.issue_number in {1, 4}}
        self.assertEqual(actions[1], ("failed", "agent exploded"))
        self.assertEqual(actions[4], ("triaged", "applied"))
        self.assertEqual(len(result.failed), 1)

    def test_execute_marks_failed_issue_when_reconcile_retry_raises(self) -> None:
        issue_fields = FakeIssueFields()

        result = reconcile_triage(
            config=self.config,
            github=self.github,
            issue_fields=issue_fields,  # type: ignore[arg-type]
            execute=True,
            run_triage=lambda issue_number: (_ for _ in ()).throw(RuntimeError("agent exploded")),
        )

        comments = self.github.list_issue_comments(repo=self.config.github_repo, issue_number=1)
        self.assertEqual(result.failed[0].reason, "agent exploded")
        self.assertEqual(issue_fields.writes[-1]["values"], {"Triage status": "Failed"})
        self.assertIn("## BugPatrol triage failed", comments[-1].body)
        self.assertIn("reconcile retry failed: agent exploded", comments[-1].body)

    def test_execute_leaves_last_resort_comment_when_failure_reporter_breaks(self) -> None:
        issue_fields = FakeIssueFields(fail_writes=True)

        result = reconcile_triage(
            config=self.config,
            github=self.github,
            issue_fields=issue_fields,  # type: ignore[arg-type]
            execute=True,
            run_triage=lambda issue_number: (_ for _ in ()).throw(RuntimeError("agent exploded")),
        )

        comments = self.github.list_issue_comments(repo=self.config.github_repo, issue_number=1)
        self.assertEqual(result.failed[0].reason, "agent exploded")
        self.assertIn("## BugPatrol triage failed", comments[-1].body)
        self.assertIn("Failure reporting also failed: field write failed", comments[-1].body)

    def test_default_runner_retries_stale_context_with_fresh_context(self) -> None:
        statuses = iter(["stale_context", "applied"])
        with (
            patch("bugpatrol.reconcile_triage.prepare_triage_run") as prepare,
            patch("bugpatrol.reconcile_triage.execute_triage_run") as execute,
        ):
            execute.side_effect = lambda **_: next(statuses)
            result = reconcile_triage(
                config=self.config,
                github=self.github,
                issue_fields=object(),
                repo_path=Path("."),
                execute=True,
            )

        self.assertEqual(prepare.call_count, 2)
        self.assertEqual(execute.call_count, 2)
        self.assertIn((1, "triaged", "applied"), [(e.issue_number, e.action, e.reason) for e in result.events])

    def test_execute_requires_repo_path_and_issue_fields(self) -> None:
        with self.assertRaisesRegex(ValueError, "repo_path and issue_fields"):
            reconcile_triage(config=self.config, github=self.github, execute=True)


if __name__ == "__main__":
    unittest.main()
