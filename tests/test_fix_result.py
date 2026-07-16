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
    render_reporter_feedback_markdown,
    render_review_feedback_markdown,
    render_revise_lark_message,
    render_revise_pr_comment,
    render_verify_failed_comment,
    render_verify_failed_lark_message,
    notify_fix_revise,
    append_ci_fix_metadata,
    extract_build_links,
    latest_ci_fix_meta,
    notify_build_ready,
    notify_ci_escalation,
    notify_ci_fix,
    parse_ci_fix_metadata,
    render_build_ready_issue_comment,
    render_build_ready_lark_message,
    render_ci_escalation_lark_message,
    render_ci_fix_feedback_markdown,
    render_verify_fix_feedback_markdown,
    render_ci_fix_lark_message,
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
        self.assertIn("已处理 3 条评审意见", text)
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

    def test_reporter_feedback_markdown_carries_correction_and_scope(self) -> None:
        text = render_reporter_feedback_markdown("其实是标签上下位置不统一")
        self.assertIn("上报人", text)
        self.assertIn("其实是标签上下位置不统一", text)
        # Explicitly allows overturning the prior fix but holds the scope.
        self.assertIn("推翻错误方向", text)
        self.assertIn("最小必要改动", text)

    def test_reporter_feedback_revise_comment_is_honest(self) -> None:
        text = render_revise_pr_comment(
            result=_result(), addressed=0, reporter_feedback=True
        )
        # No review threads addressed -> must not claim it addressed feedback count.
        self.assertIn("上报人", text)
        self.assertNotIn("已处理 0 条", text)

    def test_reporter_feedback_revise_lark_at_mentions_reviewer(self) -> None:
        text = render_revise_lark_message(
            issue_number=7,
            issue_url="u",
            pr_url="https://github.test/o/r/pull/9",
            result=_result(),
            addressed=0,
            reviewer_open_id="ou_dev",
            reporter_feedback=True,
        )
        self.assertIn('<at user_id="ou_dev">', text)
        self.assertIn("上报人", text)

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


class CiFixMetadataTest(unittest.TestCase):
    def test_append_parse_roundtrip(self) -> None:
        text = append_ci_fix_metadata(
            "body", {"attempts": 2, "last_fixed_sha": "abc"}
        )
        self.assertEqual(
            parse_ci_fix_metadata(text), {"attempts": 2, "last_fixed_sha": "abc"}
        )

    def test_parse_returns_none_without_marker(self) -> None:
        self.assertIsNone(parse_ci_fix_metadata("no marker here"))

    def test_latest_meta_picks_most_recent(self) -> None:
        from bugpatrol.clients import GitHubIssueComment

        comments = [
            GitHubIssueComment(id="1", body="chatter"),
            GitHubIssueComment(
                id="2", body=append_ci_fix_metadata("x", {"attempts": 1, "last_fixed_sha": "s1"})
            ),
            GitHubIssueComment(
                id="3", body=append_ci_fix_metadata("y", {"attempts": 2, "last_fixed_sha": "s2"})
            ),
        ]
        self.assertEqual(latest_ci_fix_meta(comments), {"attempts": 2, "last_fixed_sha": "s2"})

    def test_latest_meta_empty_when_no_marker(self) -> None:
        from bugpatrol.clients import GitHubIssueComment

        self.assertEqual(latest_ci_fix_meta([GitHubIssueComment(id="1", body="hi")]), {})


class CiFixRenderTest(unittest.TestCase):
    def test_feedback_markdown_includes_run_name_and_log(self) -> None:
        text = render_ci_fix_feedback_markdown((("iOS Build", "error: boom on line 5"),))
        self.assertIn("iOS Build", text)
        self.assertIn("error: boom on line 5", text)

    def test_verify_fix_feedback_names_preflight_and_carries_log(self) -> None:
        text = render_verify_fix_feedback_markdown((("preflight", "TS2322: type error at foo.ts:5"),))
        self.assertIn("preflight", text)
        self.assertIn("TS2322: type error at foo.ts:5", text)

    def test_ci_fix_lark_uses_masked_link_and_real_mention(self) -> None:
        text = render_ci_fix_lark_message(
            issue_number=7,
            issue_url="https://github.test/o/r/issues/7",
            pr_url="https://github.test/o/r/pull/9",
            result=_result(),
            attempt=2,
            cap=3,
            reviewer_open_id="ou_dev",
        )
        self.assertIn("[#9](https://github.test/o/r/pull/9)", text)
        self.assertNotIn("PR：https://", text)
        self.assertIn('<at user_id="ou_dev">', text)
        self.assertIn("2/3", text)

    def test_ci_escalation_lark_uses_masked_link_and_real_mention(self) -> None:
        text = render_ci_escalation_lark_message(
            issue_number=7,
            issue_url="https://github.test/o/r/issues/7",
            pr_url="https://github.test/o/r/pull/9",
            failed_names=("iOS Build", "Web Build"),
            cap=3,
            reviewer_open_id="ou_dev",
        )
        self.assertIn("[#9](https://github.test/o/r/pull/9)", text)
        self.assertNotIn("PR：https://", text)
        self.assertIn('<at user_id="ou_dev">', text)
        self.assertIn("人工", text)

    def test_build_ready_lark_uses_masked_link_and_real_mention(self) -> None:
        text = render_build_ready_lark_message(
            issue_number=7,
            issue_url="https://github.test/o/r/issues/7",
            pr_url="https://github.test/o/r/pull/9",
            assignee_open_id="ou_dev",
        )
        self.assertIn("[#9](https://github.test/o/r/pull/9)", text)
        self.assertNotIn("PR：https://", text)
        self.assertIn('<at user_id="ou_dev">', text)
        self.assertIn("可测试", text)

    def test_build_ready_issue_comment_links_pr(self) -> None:
        text = render_build_ready_issue_comment(pr_url="https://github.test/o/r/pull/9")
        self.assertIn("https://github.test/o/r/pull/9", text)

    def test_build_ready_lark_no_links_makes_no_install_claim(self) -> None:
        # A test-only fix deploys no artifact, so no link comment exists; the
        # notification must not point the user at PR comments that aren't there.
        text = render_build_ready_lark_message(
            issue_number=7,
            issue_url="https://github.test/o/r/issues/7",
            pr_url="https://github.test/o/r/pull/9",
        )
        self.assertNotIn("PR 评论", text)
        self.assertNotIn("安装 / 预览", text)

    def test_build_ready_lark_surfaces_real_links(self) -> None:
        text = render_build_ready_lark_message(
            issue_number=7,
            issue_url="https://github.test/o/r/issues/7",
            pr_url="https://github.test/o/r/pull/9",
            links=[("预览", "https://preview.test/x"), ("iOS 安装", "https://ota.test/i")],
        )
        self.assertIn("[预览](https://preview.test/x)", text)
        self.assertIn("[iOS 安装](https://ota.test/i)", text)
        self.assertNotIn("PR 评论", text)

    def test_build_ready_issue_comment_no_links_makes_no_claim(self) -> None:
        text = render_build_ready_issue_comment(pr_url="https://github.test/o/r/pull/9")
        self.assertNotIn("PR 评论", text)

    def test_build_ready_issue_comment_lists_real_links(self) -> None:
        text = render_build_ready_issue_comment(
            pr_url="https://github.test/o/r/pull/9",
            links=[("预览", "https://preview.test/x")],
        )
        self.assertIn("- 预览：https://preview.test/x", text)


class ExtractBuildLinksTest(unittest.TestCase):
    def _comment(self, body: str):
        from bugpatrol.clients import GitHubIssueComment

        return GitHubIssueComment(id="c", body=body)

    def _patterns(self):
        from bugpatrol.config import BuildLinkPattern

        return (
            BuildLinkPattern(label="预览", pattern=r"\*\*Preview:\*\* (https://\S+)"),
            BuildLinkPattern(label="iOS 安装", pattern=r"\*\*iOS install:\*\* (https://\S+)"),
            BuildLinkPattern(label="Android 安装", pattern=r"Install APK: (https://\S+)"),
        )

    def test_harvests_all_three_formats(self) -> None:
        comments = [
            self._comment("🔗 **Preview:** https://preview.test/x\n📊 Unit: ✅"),
            self._comment("📱 **iOS install:** https://ota.test/i"),
            self._comment("Android dev build\n- Install APK: https://apk.test/a.apk"),
        ]
        links = extract_build_links(comments, self._patterns())
        self.assertEqual(
            links,
            (
                ("预览", "https://preview.test/x"),
                ("iOS 安装", "https://ota.test/i"),
                ("Android 安装", "https://apk.test/a.apk"),
            ),
        )

    def test_no_matching_comments_returns_empty(self) -> None:
        comments = [self._comment("just a chat comment, no links")]
        self.assertEqual(extract_build_links(comments, self._patterns()), ())

    def test_dedupes_repeated_url(self) -> None:
        comments = [
            self._comment("🔗 **Preview:** https://preview.test/x"),
            self._comment("🔗 **Preview:** https://preview.test/x"),
        ]
        links = extract_build_links(comments, self._patterns())
        self.assertEqual(links, (("预览", "https://preview.test/x"),))


def _managed_github(order: list[str]):
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
            order.append("issue_comment")

        def add_pull_request_comment(self, *, repo, pr, body):
            order.append("pr_comment")

    class Lark:
        def reply_to_message(self, *, chat_id, message_id, text):
            order.append("lark")

    return Github(), Lark()


class NotifyCiFixOrderingTest(unittest.TestCase):
    def test_lark_first_then_pr_comment_marker(self) -> None:
        order: list[str] = []
        github, lark = _managed_github(order)
        notify_ci_fix(
            repo="o/r",
            issue_number=7,
            issue_url="u",
            pr_url="https://github.test/o/r/pull/9",
            result=_result(),
            attempt=1,
            cap=3,
            meta={"attempts": 1, "last_fixed_sha": "sha"},
            github=github,  # type: ignore[arg-type]
            lark=lark,  # type: ignore[arg-type]
        )
        self.assertEqual(order, ["lark", "pr_comment"])

    def test_ci_escalation_lark_first_then_pr_comment(self) -> None:
        order: list[str] = []
        github, lark = _managed_github(order)
        notify_ci_escalation(
            repo="o/r",
            issue_number=7,
            issue_url="u",
            pr_url="https://github.test/o/r/pull/9",
            failed_names=("iOS Build",),
            cap=3,
            meta={"attempts": 3, "last_fixed_sha": "sha"},
            github=github,  # type: ignore[arg-type]
            lark=lark,  # type: ignore[arg-type]
        )
        self.assertEqual(order, ["lark", "pr_comment"])

    def test_build_ready_lark_first_then_issue_then_pr_marker(self) -> None:
        order: list[str] = []
        github, lark = _managed_github(order)
        notify_build_ready(
            repo="o/r",
            issue_number=7,
            issue_url="u",
            pr_url="https://github.test/o/r/pull/9",
            head_sha="sha",
            meta={"last_notified_sha": "sha"},
            github=github,  # type: ignore[arg-type]
            lark=lark,  # type: ignore[arg-type]
        )
        self.assertEqual(order, ["lark", "issue_comment", "pr_comment"])


if __name__ == "__main__":
    unittest.main()
