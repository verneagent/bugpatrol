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

    def test_search_scores_and_excerpts(self) -> None:
        docs = load_prd_documents(Path("../bugpatrol-todo-sandbox/docs/prd"))

        hits = search_prd_documents("todo empty state", docs)

        self.assertTrue(hits)
        self.assertEqual(hits[0].path, "todo-list.md")
        self.assertGreater(hits[0].score, 0)
        self.assertIn("Todo", hits[0].title)


if __name__ == "__main__":
    unittest.main()
