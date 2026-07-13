from __future__ import annotations

import unittest

from bugpatrol.fix_gate import VerifyOutcome
from bugpatrol.fix_result import (
    FixResult,
    append_fix_metadata,
    build_pr_body,
    fix_result_fingerprint,
    notify_fix_pr,
    parse_fix_metadata,
    parse_fix_result,
    render_baseline_broken_comment,
    render_baseline_broken_lark_message,
    render_fix_blocked_lark_message,
    render_fix_comment,
    render_fix_lark_message,
    notify_conflict_escalation,
    render_conflict_escalation_pr_comment,
    render_conflict_instructions_markdown,
    render_review_feedback_markdown,
    render_revise_lark_message,
    render_revise_pr_comment,
    render_verify_failed_comment,
    render_verify_failed_lark_message,
    notify_fix_revise,
)


def _result() -> FixResult:
    return FixResult(
        summary="给空列表加上空状态提示",
        root_cause="删除最后一条 todo 时没有渲染 empty state",
        tests_added=True,
        pr_title="fix: todo 空状态缺失",
        pr_body="修复删除全部 todo 后不显示空状态的问题。",
    )


def _outcomes() -> tuple[VerifyOutcome, ...]:
    return (
        VerifyOutcome(label="typecheck", command="npm run typecheck", returncode=0, stdout_tail="", stderr_tail=""),
        VerifyOutcome(label="test", command="npm test", returncode=0, stdout_tail="ok", stderr_tail=""),
    )


class ParseFixResultTest(unittest.TestCase):
    def test_parses_valid_payload(self) -> None:
        result = parse_fix_result(
            {
                "summary": "s",
                "root_cause": "rc",
                "tests_added": False,
                "pr_title": "t",
                "pr_body": "b",
            }
        )
        self.assertEqual(result.summary, "s")
        self.assertFalse(result.tests_added)

    def test_rejects_missing_string(self) -> None:
        with self.assertRaisesRegex(ValueError, "summary"):
            parse_fix_result(
                {"summary": "", "root_cause": "rc", "tests_added": True, "pr_title": "t", "pr_body": "b"}
            )

    def test_rejects_non_bool_tests_added(self) -> None:
        with self.assertRaisesRegex(ValueError, "tests_added"):
            parse_fix_result(
                {"summary": "s", "root_cause": "rc", "tests_added": "yes", "pr_title": "t", "pr_body": "b"}
            )


class FingerprintTest(unittest.TestCase):
    def test_stable_regardless_of_file_order(self) -> None:
        a = fix_result_fingerprint(issue_number=7, changed_files=("src/b.ts", "src/a.ts"))
        b = fix_result_fingerprint(issue_number=7, changed_files=("src/a.ts", "src/b.ts"))
        self.assertEqual(a, b)

    def test_changes_with_issue(self) -> None:
        a = fix_result_fingerprint(issue_number=7, changed_files=("src/a.ts",))
        b = fix_result_fingerprint(issue_number=8, changed_files=("src/a.ts",))
        self.assertNotEqual(a, b)


class PrBodyTest(unittest.TestCase):
    def test_includes_fixes_link_root_cause_files_and_verify(self) -> None:
        body = build_pr_body(
            result=_result(),
            issue_number=7,
            issue_url="https://github.test/o/r/issues/7",
            changed_files=("src/todo.ts", "tests/todo.test.ts"),
            verify_outcomes=_outcomes(),
        )
        self.assertIn("Fixes #7", body)
        self.assertIn("## 根因", body)
        self.assertIn("src/todo.ts", body)
        self.assertIn("`npm test`", body)
        self.assertIn("不会自动合并", body)


class RenderTest(unittest.TestCase):
    def test_fix_comment_roundtrips_metadata(self) -> None:
        comment = render_fix_comment(
            pr_url="https://github.test/o/r/pull/9",
            result=_result(),
            fingerprint="abc123",
            issue_number=7,
        )
        meta = parse_fix_metadata(comment)
        self.assertIsNotNone(meta)
        assert meta is not None
        self.assertEqual(meta["result_fingerprint"], "abc123")
        self.assertEqual(meta["pr_url"], "https://github.test/o/r/pull/9")

    def test_lark_message_includes_pr_and_reviewer(self) -> None:
        text = render_fix_lark_message(
            issue_number=7,
            issue_url="https://github.test/o/r/issues/7",
            pr_url="https://github.test/o/r/pull/9",
            result=_result(),
            reviewer_open_id="ou_reviewer",
        )
        # PR is a masked link (`[#9](url)`), never a bare full URL in Lark.
        self.assertIn("[#9](https://github.test/o/r/pull/9)", text)
        self.assertNotIn("PR：https://", text)
        self.assertIn('<at user_id="ou_reviewer">', text)

    def test_blocked_and_verify_failed_messages(self) -> None:
        blocked = render_fix_blocked_lark_message(
            issue_number=7, issue_url="u", reason="diff too large"
        )
        self.assertIn("diff too large", blocked)
        failed_outcomes = (
            VerifyOutcome(label="test", command="npm test", returncode=1, stdout_tail="", stderr_tail="boom"),
        )
        lark = render_verify_failed_lark_message(issue_number=7, issue_url="u", verify_outcomes=failed_outcomes)
        self.assertIn("test", lark)
        comment = render_verify_failed_comment(verify_outcomes=failed_outcomes)
        self.assertIn("boom", comment)

    def test_baseline_broken_messages_name_branch_and_do_not_blame_fix(self) -> None:
        failed_outcomes = (
            VerifyOutcome(label="preflight", command="./scripts/preflight.sh", returncode=1, stdout_tail="", stderr_tail="tsc boom"),
        )
        lark = render_baseline_broken_lark_message(
            issue_number=7, issue_url="u", base_branch="2026/chat-live", verify_outcomes=failed_outcomes
        )
        self.assertIn("2026/chat-live", lark)
        self.assertIn("baseline 本就红", lark)
        self.assertIn("暂停", lark)
        comment = render_baseline_broken_comment(
            base_branch="2026/chat-live", verify_outcomes=failed_outcomes
        )
        self.assertIn("2026/chat-live", comment)
        self.assertIn("tsc boom", comment)

    def test_metadata_append_parse(self) -> None:
        text = append_fix_metadata("body", {"version": 1, "issue": 7})
        self.assertEqual(parse_fix_metadata(text), {"version": 1, "issue": 7})


class NotifyOrderingTest(unittest.TestCase):
    def test_lark_first_then_github_comment(self) -> None:
        order: list[str] = []

        class Github:
            def get_issue(self, *, repo, issue_number):
                from bugpatrol.clients import GitHubIssue

                return GitHubIssue(
                    number=issue_number,
                    url="u",
                    title="t",
                    body='<!-- BUGPATROL_INTAKE_META:{"chat_id":"oc_1","message_id":"om_1"} -->',
                )

            def add_issue_comment(self, *, repo, issue_number, body):
                order.append("github")

        class Lark:
            def reply_to_message(self, *, chat_id, message_id, text):
                order.append("lark")

        notify_fix_pr(
            repo="o/r",
            issue_number=7,
            issue_url="u",
            pr_url="https://github.test/o/r/pull/9",
            result=_result(),
            fingerprint="fp",
            github=Github(),  # type: ignore[arg-type]
            lark=Lark(),  # type: ignore[arg-type]
        )
        self.assertEqual(order, ["lark", "github"])


class ReviseRenderTest(unittest.TestCase):
    def _threads(self):
        from bugpatrol.clients import ReviewComment, ReviewThread

        return (
            ReviewThread(
                id="RT_1",
                comments=(ReviewComment(author="rev", body="这里改小一点", path="src/todo.ts", line=12),),
            ),
        )

    def test_feedback_markdown_includes_location_author_and_body(self) -> None:
        text = render_review_feedback_markdown(self._threads())
        self.assertIn("src/todo.ts:12", text)
        self.assertIn("@rev", text)
        self.assertIn("这里改小一点", text)

    def test_revise_pr_comment_reports_count(self) -> None:
        text = render_revise_pr_comment(result=_result(), addressed=3)
        self.assertIn("3", text)
        self.assertIn("已按评审反馈", text)
        self.assertIn("resolve", text)

    def test_revise_lark_message_at_mentions_reviewer(self) -> None:
        text = render_revise_lark_message(
            issue_number=7,
            issue_url="u",
            pr_url="https://github.test/o/r/pull/9",
            result=_result(),
            addressed=2,
            reviewer_open_id="ou_dev",
        )
        self.assertIn('<at user_id="ou_dev">', text)
        self.assertIn("2", text)

    def test_conflict_instructions_list_files_and_base(self) -> None:
        text = render_conflict_instructions_markdown(
            base_branch="feature-demo", files=("src/a.ts", "src/b.ts")
        )
        self.assertIn("feature-demo", text)
        self.assertIn("src/a.ts", text)
        self.assertIn("冲突标记", text)

    def test_conflict_only_revise_comment_mentions_merge_not_feedback_count(self) -> None:
        text = render_revise_pr_comment(
            result=_result(), addressed=0, conflicted=True, base_branch="feature-demo"
        )
        self.assertIn("解决冲突", text)
        self.assertIn("feature-demo", text)

    def test_conflict_escalation_comment_lists_files(self) -> None:
        text = render_conflict_escalation_pr_comment(
            base_branch="main", files=("a.ts", "b.ts", "c.ts")
        )
        self.assertIn("人工", text)
        self.assertIn("a.ts", text)


class NotifyReviseOrderingTest(unittest.TestCase):
    def test_lark_first_then_pr_comment(self) -> None:
        order: list[str] = []

        class Github:
            def get_issue(self, *, repo, issue_number):
                from bugpatrol.clients import GitHubIssue

                return GitHubIssue(
                    number=issue_number,
                    url="u",
                    title="t",
                    body='<!-- BUGPATROL_INTAKE_META:{"chat_id":"oc_1","message_id":"om_1"} -->',
                )

            def add_pull_request_comment(self, *, repo, pr, body):
                order.append("pr_comment")

        class Lark:
            def reply_to_message(self, *, chat_id, message_id, text):
                order.append("lark")

        notify_fix_revise(
            repo="o/r",
            issue_number=7,
            issue_url="u",
            pr_url="https://github.test/o/r/pull/9",
            result=_result(),
            addressed=1,
            github=Github(),  # type: ignore[arg-type]
            lark=Lark(),  # type: ignore[arg-type]
        )
        self.assertEqual(order, ["lark", "pr_comment"])


class NotifyConflictEscalationOrderingTest(unittest.TestCase):
    def test_lark_first_then_pr_comment(self) -> None:
        order: list[str] = []

        class Github:
            def get_issue(self, *, repo, issue_number):
                from bugpatrol.clients import GitHubIssue

                return GitHubIssue(
                    number=issue_number,
                    url="u",
                    title="t",
                    body='<!-- BUGPATROL_INTAKE_META:{"chat_id":"oc_1","message_id":"om_1"} -->',
                )

            def add_pull_request_comment(self, *, repo, pr, body):
                order.append("pr_comment")

        class Lark:
            def reply_to_message(self, *, chat_id, message_id, text):
                order.append("lark")

        notify_conflict_escalation(
            repo="o/r",
            issue_number=7,
            issue_url="u",
            pr_url="https://github.test/o/r/pull/9",
            base_branch="main",
            files=("a.ts", "b.ts"),
            github=Github(),  # type: ignore[arg-type]
            lark=Lark(),  # type: ignore[arg-type]
        )
        self.assertEqual(order, ["lark", "pr_comment"])


if __name__ == "__main__":
    unittest.main()
