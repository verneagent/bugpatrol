from __future__ import annotations

import tempfile
import unittest
import subprocess
from pathlib import Path
from unittest.mock import patch

from bugpatrol.agents import AgentInvocation
from bugpatrol.clients import GitHubIssue
from bugpatrol.config import load_project_config
from bugpatrol.triage_runner import TriageRunPlan, execute_triage_run, prepare_triage_run, render_triage_failed_comment


class FakeGithub:
    def __init__(self) -> None:
        self.comments: list[str] = []

    def get_issue(self, *, repo: str, issue_number: int) -> GitHubIssue:
        return GitHubIssue(
            number=issue_number,
            url=f"https://github.test/{repo}/issues/{issue_number}",
            title="Todo empty state missing",
            body="Deleting all todos does not show empty state.",
        )

    def add_issue_comment(self, *, repo: str, issue_number: int, body: str) -> None:
        self.comments.append(body)


class FakeIssueFields:
    def __init__(self) -> None:
        self.writes: list[dict[str, object]] = []

    def add_issue_field_values(self, **kwargs: object) -> None:
        self.writes.append(kwargs)


class TriageRunnerTest(unittest.TestCase):
    def test_prepare_triage_run_writes_context_schema_and_command(self) -> None:
        config = load_project_config(Path("projects/todo-sandbox.toml"))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prd_root = root / config.prd.cache_path
            prd_root.mkdir(parents=True)
            (prd_root / "todo-list.md").write_text(
                "# Todo List PRD\n\nEmpty state appears after deleting all todos."
            )
            output_dir = root / "run"
            plan = prepare_triage_run(
                config=config,
                issue_number=7,
                repo_path=root,
                output_dir=output_dir,
                github=FakeGithub(),  # type: ignore[arg-type]
            )

            self.assertTrue(plan.context_path.exists())
            self.assertTrue(plan.schema_path.exists())
            self.assertEqual(plan.output_path, output_dir / "triage-output.json")
            self.assertIn("Todo empty state missing", plan.context_path.read_text())
            self.assertIn("triage-context.md", plan.invocation.command[-1])

    def test_execute_triage_run_marks_failed_when_agent_exits_nonzero(self) -> None:
        config = load_project_config(Path("projects/todo-sandbox.toml"))
        github = FakeGithub()
        issue_fields = FakeIssueFields()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan = TriageRunPlan(
                context_path=root / "context.md",
                schema_path=root / "schema.json",
                output_path=root / "output.json",
                invocation=AgentInvocation(provider="codex", command=["false"]),
            )

            with patch("subprocess.run") as run:
                run.return_value = subprocess.CompletedProcess(["false"], 42)
                with self.assertRaisesRegex(RuntimeError, "exit 42"):
                    execute_triage_run(
                        config=config,
                        issue_number=7,
                        plan=plan,
                        github=github,  # type: ignore[arg-type]
                        issue_fields=issue_fields,  # type: ignore[arg-type]
                    )

        self.assertEqual(issue_fields.writes[0]["values"], {"Triage status": "Failed"})
        self.assertIn("exited with code `42`", github.comments[0])

    def test_render_triage_failed_comment_is_actionable(self) -> None:
        comment = render_triage_failed_comment(exit_code=2)

        self.assertIn("BugPatrol triage failed", comment)
        self.assertIn("credentials", comment)


if __name__ == "__main__":
    unittest.main()
