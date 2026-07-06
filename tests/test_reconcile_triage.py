from __future__ import annotations

import unittest
from pathlib import Path

from bugpatrol.config import load_project_config
from bugpatrol.reconcile_triage import reconcile_triage
from bugpatrol.testing.fakes import FakeGitHubIssuesClient

INTAKE_META = '<!-- BUGPATROL_INTAKE_META:{"source":"lark","chat_id":"oc_x","root_id":"om_r"} -->'
TRIAGE_META_COMMENT = '<!-- BUGPATROL_TRIAGE_META\n{"verdict":"bug"}\nBUGPATROL_TRIAGE_META -->'


def seeded_github(repo: str) -> FakeGitHubIssuesClient:
    github = FakeGitHubIssuesClient()
    github.create_issue(repo=repo, title="untriaged", body=f"报告\n{INTAKE_META}", issue_type="Bug", fields={})
    triaged = github.create_issue(
        repo=repo, title="triaged", body=f"报告\n{INTAKE_META}", issue_type="Bug", fields={}
    )
    github.add_issue_comment(repo=repo, issue_number=triaged.number, body=TRIAGE_META_COMMENT)
    github.create_issue(repo=repo, title="manual", body="手工创建的 issue", issue_type="Bug", fields={})
    return github


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

        result = reconcile_triage(
            config=self.config,
            github=self.github,
            execute=True,
            run_triage=ran.append,
        )

        self.assertEqual(ran, [1])
        self.assertIn((1, "triaged", "executed"), [(e.issue_number, e.action, e.reason) for e in result.events])

    def test_execute_records_failure_and_continues(self) -> None:
        self.github.create_issue(
            repo=self.config.github_repo,
            title="untriaged 2",
            body=f"另一个报告\n{INTAKE_META}",
            issue_type="Bug",
            fields={},
        )

        def run_triage(issue_number: int) -> None:
            if issue_number == 1:
                raise RuntimeError("agent exploded")

        result = reconcile_triage(
            config=self.config,
            github=self.github,
            execute=True,
            run_triage=run_triage,
        )

        actions = {event.issue_number: (event.action, event.reason) for event in result.events if event.issue_number in {1, 4}}
        self.assertEqual(actions[1], ("failed", "agent exploded"))
        self.assertEqual(actions[4], ("triaged", "executed"))
        self.assertEqual(len(result.failed), 1)

    def test_execute_requires_repo_path_and_issue_fields(self) -> None:
        with self.assertRaisesRegex(ValueError, "repo_path and issue_fields"):
            reconcile_triage(config=self.config, github=self.github, execute=True)


if __name__ == "__main__":
    unittest.main()
