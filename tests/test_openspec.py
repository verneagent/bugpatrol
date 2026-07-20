from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from bugpatrol.openspec import load_openspec_changes, score_openspec_changes


def _write_change(root: Path, change_id: str, tasks_md: str, *, proposal: str = "") -> None:
    change_dir = root / "changes" / change_id
    change_dir.mkdir(parents=True)
    (change_dir / "tasks.md").write_text(tasks_md)
    if proposal:
        (change_dir / "proposal.md").write_text(proposal)


class OpenSpecParseTest(unittest.TestCase):
    def test_parses_header_and_task_owners(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_change(
                root,
                "post-comments",
                "\n".join(
                    [
                        "> **Owner**：全部 `· @naohn`（按用户指派）。",
                        "",
                        "## 1. 后端",
                        "",
                        "- [x] comment-api: 新增评论路由 · @naohn",
                        "- [ ] comment-ui: 评论列表项组件 · @diego",
                        "- [x] no-owner-task: 一条没标 owner 的任务",
                    ]
                ),
                proposal="# 帖子评论与表情\n\nWhy...",
            )

            (change,) = load_openspec_changes(root)

        self.assertEqual(change.change_id, "post-comments")
        self.assertEqual(change.title, "帖子评论与表情")
        self.assertEqual(change.path, "changes/post-comments/tasks.md")
        self.assertEqual(change.default_owner, "naohn")
        self.assertEqual(len(change.tasks), 3)
        self.assertEqual(change.tasks[0].task_id, "comment-api")
        self.assertEqual(change.tasks[0].owner, "naohn")
        self.assertTrue(change.tasks[0].done)
        self.assertEqual(change.tasks[1].owner, "diego")
        self.assertFalse(change.tasks[1].done)
        self.assertEqual(change.tasks[2].owner, "")
        self.assertEqual(change.owners(), ("naohn", "diego"))

    def test_no_changes_dir_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(load_openspec_changes(Path(tmp)), ())

    def test_at_mention_without_middot_is_not_an_owner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_change(
                root,
                "c",
                "- [ ] task: 请 @someone 帮忙 review，无 owner 标注",
            )
            (change,) = load_openspec_changes(root)
        self.assertEqual(change.tasks[0].owner, "")
        self.assertEqual(change.owners(), ())

    def test_title_falls_back_to_change_id_without_proposal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_change(root, "screen-audit", "- [x] t: x · @naohn")
            (change,) = load_openspec_changes(root)
        self.assertEqual(change.title, "screen-audit")

    def test_placeholder_owner_tokens_are_dropped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_change(
                root,
                "group-chat",
                "\n".join(
                    [
                        "> **Owner**：全部 `· @owner`（模板占位）。",
                        "- [x] real-task: 真实任务 · @naohn",
                        "- [x] team-task: 待定 · @team",
                    ]
                ),
            )
            (change,) = load_openspec_changes(root)
        self.assertEqual(change.default_owner, "")
        self.assertEqual(change.tasks[1].owner, "")  # @team dropped
        self.assertEqual(change.owners(), ("naohn",))

    def test_owners_dedupe_case_insensitively(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_change(
                root,
                "c",
                "\n".join(
                    [
                        "- [x] a: x · @Naohn",
                        "- [x] b: y · @naohn",
                    ]
                ),
            )
            (change,) = load_openspec_changes(root)
        self.assertEqual(change.owners(), ("Naohn",))


class OpenSpecScoreTest(unittest.TestCase):
    def test_ranks_relevant_change_and_skips_ownerless(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_change(
                root,
                "comments",
                "- [x] comment-api: 帖子评论 comment 路由 · @naohn",
            )
            _write_change(
                root,
                "reactions",
                "- [x] reaction-api: 帖子 reaction 表情 · @diego",
            )
            _write_change(
                root,
                "ownerless",
                "- [x] comment-orphan: 帖子评论 comment 没有 owner",
            )
            changes = load_openspec_changes(root)

            hits = score_openspec_changes("帖子评论 comment 崩溃", changes)

        self.assertEqual([h.change_id for h in hits], ["comments"])
        self.assertEqual(hits[0].owners, ("naohn",))
        self.assertEqual(hits[0].matched_tasks[0].task_id, "comment-api")

    def test_no_terms_returns_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_change(root, "c", "- [x] t: x · @naohn")
            changes = load_openspec_changes(root)
        self.assertEqual(score_openspec_changes("", changes), ())


if __name__ == "__main__":
    unittest.main()
