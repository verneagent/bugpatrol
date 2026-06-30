from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from bugpatrol.clients import GitHubIssue
from bugpatrol.triage_context import build_triage_context, render_triage_context_markdown


class TriageContextTest(unittest.TestCase):
    def test_builds_context_with_prd_hits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "todo-list.md").write_text("# Todo List\n\nEmpty state appears after deleting all todos.")
            issue = GitHubIssue(
                number=1,
                url="https://github.test/o/r/issues/1",
                title="Todo empty state missing",
                body="Deleting all todos does not show empty state.",
            )

            context = build_triage_context(issue=issue, prd_root=root)
            markdown = render_triage_context_markdown(context)

        self.assertEqual(context.prd_hits[0].path, "todo-list.md")
        self.assertIn("# BugPatrol Triage Context", markdown)
        self.assertIn("Todo empty state missing", markdown)
        self.assertIn("todo-list.md", markdown)


if __name__ == "__main__":
    unittest.main()
