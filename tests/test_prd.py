from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from bugpatrol.prd import load_prd_documents, search_prd_documents


class PrdTest(unittest.TestCase):
    def test_loads_markdown_documents_with_titles(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "todo.md").write_text("# Todo List\n\nDeleting a todo updates empty state.")

            docs = load_prd_documents(root)

        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0].path, "todo.md")
        self.assertEqual(docs[0].title, "Todo List")

    def test_loads_only_included_globs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "specs" / "todo").mkdir(parents=True)
            (root / "changes" / "todo").mkdir(parents=True)
            (root / "specs" / "todo" / "spec.md").write_text("# Todo Spec\n\nAccepted behavior.")
            (root / "changes" / "todo" / "prd-snapshot.md").write_text("# Todo Snapshot\n\nSnapshot behavior.")
            (root / "changes" / "todo" / "proposal.md").write_text("# Proposal\n\nProcess notes.")

            docs = load_prd_documents(
                root,
                include_globs=("specs/**/spec.md", "changes/**/prd-snapshot.md"),
            )

        self.assertEqual(
            [doc.path for doc in docs],
            ["changes/todo/prd-snapshot.md", "specs/todo/spec.md"],
        )

    def test_search_scores_and_excerpts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "todo-list.md").write_text(
                "# Todo List PRD\n\nEmpty state text appears when there are no todo items."
            )
            (root / "notifications.md").write_text(
                "# Reminder Notifications PRD\n\nNotifications can be scheduled from a todo due date."
            )
            docs = load_prd_documents(root)

        hits = search_prd_documents("todo empty state", docs)

        self.assertTrue(hits)
        self.assertEqual(hits[0].path, "todo-list.md")
        self.assertGreater(hits[0].score, 0)
        self.assertIn("Todo", hits[0].title)


if __name__ == "__main__":
    unittest.main()
