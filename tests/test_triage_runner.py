from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from bugpatrol.clients import GitHubIssue
from bugpatrol.config import load_project_config
from bugpatrol.triage_runner import prepare_triage_run


class FakeGithub:
    def get_issue(self, *, repo: str, issue_number: int) -> GitHubIssue:
        return GitHubIssue(
            number=issue_number,
            url=f"https://github.test/{repo}/issues/{issue_number}",
            title="Todo empty state missing",
            body="Deleting all todos does not show empty state.",
        )


class TriageRunnerTest(unittest.TestCase):
    def test_prepare_triage_run_writes_context_schema_and_command(self) -> None:
        config = load_project_config(Path("projects/todo-sandbox.toml"))
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "run"
            plan = prepare_triage_run(
                config=config,
                issue_number=7,
                repo_path=Path("../bugpatrol-todo-sandbox"),
                output_dir=output_dir,
                github=FakeGithub(),  # type: ignore[arg-type]
            )

            self.assertTrue(plan.context_path.exists())
            self.assertTrue(plan.schema_path.exists())
            self.assertEqual(plan.output_path, output_dir / "triage-output.json")
            self.assertIn("Todo empty state missing", plan.context_path.read_text())
            self.assertIn("triage-context.md", plan.invocation.command[-1])


if __name__ == "__main__":
    unittest.main()
