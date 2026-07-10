from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from bugpatrol.clients import GitHubIssue, GitHubIssueComment
from bugpatrol.triage_context import (
    AssigneeIdentity,
    build_triage_context,
    extract_media_evidence,
    render_triage_context_markdown,
)


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

    def test_context_renders_assignee_roster(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            issue = GitHubIssue(
                number=1,
                url="https://github.test/o/r/issues/1",
                title="Assign to Naohn",
                body="这个给@Naohn 你来看下",
            )
            roster = (
                AssigneeIdentity(login="naohn42", aliases=("naohn42", "Naohn", "阿闹")),
            )

            context = build_triage_context(issue=issue, prd_root=root, roster=roster)
            markdown = render_triage_context_markdown(context)

        self.assertEqual(context.roster, roster)
        self.assertIn("## Assignee Roster", markdown)
        self.assertIn("`naohn42` — naohn42 / Naohn / 阿闹", markdown)

    def test_context_includes_comments_and_media_evidence_descriptions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "video.md").write_text("# Video\n\nThe export dialog can freeze.")
            issue = GitHubIssue(
                number=1,
                url="https://github.test/o/r/issues/1",
                title="Export freezes",
                body="\n".join(
                    [
                        "## Attachments",
                        "",
                        "- image: [打开附件](https://assets/screenshot.png)",
                        "",
                        "  ![图片 1](https://assets/screenshot.png)",
                        "",
                        "  - 生成描述: Screenshot shows spinner stuck at 80%.",
                    ]
                ),
            )
            comments = (
                GitHubIssueComment(
                    id="c1",
                    body="\n".join(
                        [
                            "## Lark 话题更新",
                            "",
                            "## 附件",
                            "",
                            "- video: [打开附件](https://assets/repro.mp4)",
                            "  - 生成描述: Video shows export progress freezing after tapping Done.",
                        ]
                    ),
                ),
            )

            context = build_triage_context(issue=issue, comments=comments, prd_root=root)
            markdown = render_triage_context_markdown(context)

        self.assertEqual([item.kind for item in context.media], ["image", "video"])
        self.assertIn("#### Comment c1", markdown)
        self.assertIn("## Media Evidence", markdown)
        self.assertIn("https://assets/repro.mp4", markdown)
        self.assertIn("Video shows export progress freezing", markdown)

    def test_extract_media_evidence(self) -> None:
        media = extract_media_evidence(
            "\n".join(
                [
                    "- image: https://assets/one.png",
                    "  - generated description: red error toast",
                    "- file: https://assets/log.txt",
                    "- video: [打开附件](https://assets/repro.mp4)",
                ]
            ),
            source="comment 1",
        )

        self.assertEqual(len(media), 2)
        self.assertEqual(media[0].url, "https://assets/one.png")
        self.assertEqual(media[1].url, "https://assets/repro.mp4")
        self.assertEqual(media[0].description, "red error toast")
        self.assertEqual(media[0].source, "comment 1")


if __name__ == "__main__":
    unittest.main()
