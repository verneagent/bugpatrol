from __future__ import annotations

import unittest
from dataclasses import replace
from pathlib import Path

from bugpatrol.config import load_project_config
from bugpatrol.clients import GitHubIssue, GitHubIssueComment
from bugpatrol.intake import IntakeRecord, parse_intake_metadata, render_issue_body
from bugpatrol.testing.fakes import FakeLarkMessengerClient
from bugpatrol.triage_result import (
    TriageResult,
    append_triage_metadata,
    apply_triage_result,
    build_triage_dry_run_report,
    parse_triage_metadata,
    parse_triage_result,
    render_triage_summary_lark_message,
    render_needs_info_lark_message,
    render_triage_comment,
    triage_field_values_for_write,
    triage_result_fingerprint,
)


VALID = {
    "issue_type": "Bug",
    "priority": "High",
    "triage_status": "Done",
    "triage_verdict": "代码 Bug",
    "platform": "Web",
    "reproducibility": "必现",
    "other_platforms": "未验证",
    "capability": "Quest",
    "evidence": "文字描述",
    "prd_status": "已对齐",
    "triage_confidence": "高",
    "assignee": "@garlanddiego",
    "owner_reason": "CODEOWNERS",
    "blame_suggestion": "",
    "comment_markdown": "## Triage Analysis\n\n是代码 Bug。",
}


class FakeGithub:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.comments: list[str] = []
        self.issue_body = managed_issue_body()

    def set_issue_type(self, **kwargs: object) -> None:
        self.calls.append(("set_issue_type", kwargs))

    def add_issue_comment(self, **kwargs: object) -> None:
        self.calls.append(("add_issue_comment", kwargs))
        self.comments.append(str(kwargs["body"]))

    def add_assignee(self, **kwargs: object) -> None:
        self.calls.append(("add_assignee", kwargs))

    def close_issue_as_duplicate(self, **kwargs: object) -> None:
        self.calls.append(("close_issue_as_duplicate", kwargs))

    def list_issue_comments(self, **kwargs: object) -> tuple[GitHubIssueComment, ...]:
        self.calls.append(("list_issue_comments", kwargs))
        return tuple(
            GitHubIssueComment(id=str(index + 1), body=body)
            for index, body in enumerate(self.comments)
        )

    def get_issue(self, **kwargs: object) -> GitHubIssue:
        self.calls.append(("get_issue", kwargs))
        return GitHubIssue(
            number=int(kwargs["issue_number"]),
            url=f"https://github.test/o/r/issues/{kwargs['issue_number']}",
            title="issue",
            body=self.issue_body,
        )


class FakeIssueFields:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.current_values: dict[str, str] = {}

    def add_issue_field_values(self, **kwargs: object) -> None:
        self.calls.append(("add_issue_field_values", kwargs))

    def get_issue_field_values(self, **kwargs: object) -> dict[str, str]:
        self.calls.append(("get_issue_field_values", kwargs))
        return self.current_values


class TriageResultTest(unittest.TestCase):
    def test_parse_triage_result_validates_fields(self) -> None:
        result = parse_triage_result(dict(VALID))

        self.assertEqual(result.issue_type, "Bug")
        self.assertEqual(result.assignee, "garlanddiego")
        self.assertEqual(result.fields["Triage verdict"], "代码 Bug")
        self.assertEqual(result.blame_suggestion, "")

    def test_parse_triage_result_rejects_invalid_enum(self) -> None:
        data = dict(VALID)
        data["triage_verdict"] = "Not a verdict"

        with self.assertRaisesRegex(ValueError, "invalid value"):
            parse_triage_result(data)

    def test_parse_triage_result_rejects_lifecycle_triage_status(self) -> None:
        for status in ("Pending", "Running", "Failed"):
            data = dict(VALID)
            data["triage_status"] = status

            with self.assertRaisesRegex(ValueError, "terminal state"):
                parse_triage_result(data)

    def test_parse_triage_result_requires_questions_for_needs_info(self) -> None:
        data = dict(VALID)
        data["triage_status"] = "Needs info"

        with self.assertRaisesRegex(ValueError, "follow_up_questions"):
            parse_triage_result(data)

    def test_parse_triage_result_ignores_questions_unless_needs_info(self) -> None:
        data = dict(VALID)
        data["follow_up_questions"] = ["无效追问"]

        result = parse_triage_result(data)

        self.assertEqual(result.follow_up_questions, ())

    def test_suspected_owner_is_visible_and_written_when_field_is_mapped(self) -> None:
        base_config = load_project_config(Path("projects/todo-sandbox.toml"))
        config = replace(
            base_config,
            issue_field_names={**base_config.issue_field_names, "Owner": "Owner"},
        )
        data = dict(VALID)
        data["blame_suggestion"] = "可能由 PR #123 的 push token 绑定改动引入"
        data["suspected_owner"] = "@AndyCokeZero"
        result = parse_triage_result(data)

        values = triage_field_values_for_write(result, config=config)
        comment = render_triage_comment(result)

        self.assertEqual(result.suspected_owner, "AndyCokeZero")
        self.assertEqual(values["Owner"], "AndyCokeZero")
        self.assertIn("疑似引入人（Owner）：AndyCokeZero", comment)
        self.assertIn("归因线索：可能由 PR #123", comment)

    def test_suspected_owner_not_written_when_empty_or_unmapped(self) -> None:
        base_config = load_project_config(Path("projects/todo-sandbox.toml"))
        config_without_owner = replace(
            base_config,
            issue_field_names={k: v for k, v in base_config.issue_field_names.items() if k != "Owner"},
        )
        data = dict(VALID)
        data["suspected_owner"] = "AndyCokeZero"
        result = parse_triage_result(data)

        self.assertNotIn("Owner", triage_field_values_for_write(result, config=config_without_owner))
        empty = parse_triage_result(dict(VALID))
        self.assertNotIn("Owner", triage_field_values_for_write(empty, config=base_config))

    def test_apply_triage_result_writes_type_fields_comment_and_assignee(self) -> None:
        config = load_project_config(Path("projects/todo-sandbox.toml"))
        github = FakeGithub()
        issue_fields = FakeIssueFields()
        result = TriageResult(
            issue_type="Bug",
            fields={"Source": "Lark"},
            assignee="garlanddiego",
            comment_markdown="done",
        )

        summary = apply_triage_result(
            repo=config.github_repo,
            issue_number=1,
            config=config,
            result=result,
            github=github,  # type: ignore[arg-type]
            issue_fields=issue_fields,  # type: ignore[arg-type]
        )

        self.assertEqual(
            [name for name, _ in github.calls],
            ["get_issue", "list_issue_comments", "set_issue_type", "add_issue_comment", "add_assignee"],
        )
        self.assertEqual(
            [name for name, _ in issue_fields.calls],
            ["get_issue_field_values", "add_issue_field_values"],
        )
        self.assertTrue(summary.comment_added)
        self.assertFalse(summary.duplicate_comment_skipped)
        self.assertIn("BUGPATROL_TRIAGE_META", github.comments[0])
        metadata = parse_triage_metadata(github.comments[0])
        self.assertIsNotNone(metadata)
        assert metadata is not None
        self.assertIn("decision_key", metadata)

    def test_apply_triage_result_rejects_unmanaged_issue_before_writes(self) -> None:
        config = load_project_config(Path("projects/todo-sandbox.toml"))
        github = FakeGithub()
        github.issue_body = "legacy issue"
        issue_fields = FakeIssueFields()
        result = TriageResult(
            issue_type="Bug",
            fields={"Source": "Lark"},
            assignee="garlanddiego",
            comment_markdown="done",
        )

        with self.assertRaisesRegex(ValueError, "missing BUGPATROL_INTAKE_META"):
            apply_triage_result(
                repo=config.github_repo,
                issue_number=1,
                config=config,
                result=result,
                github=github,  # type: ignore[arg-type]
                issue_fields=issue_fields,  # type: ignore[arg-type]
            )

        self.assertEqual([name for name, _ in github.calls], ["get_issue"])
        self.assertEqual(issue_fields.calls, [])

    def test_build_triage_dry_run_report_shows_field_changes_without_writes(self) -> None:
        config = load_project_config(Path("projects/todo-sandbox.toml"))
        issue_fields = FakeIssueFields()
        issue_fields.current_values = {
            "Priority": "Low",
            "Triage status": "Pending",
            "Source": "Lark",
        }
        result = parse_triage_result(dict(VALID))

        report = build_triage_dry_run_report(
            repo=config.github_repo,
            issue_number=7,
            config=config,
            result=result,
            issue_fields=issue_fields,  # type: ignore[arg-type]
        )

        changes = {change.field: (change.current, change.proposed) for change in report.field_changes}
        self.assertEqual(changes["Priority"], ("Low", "High"))
        self.assertEqual(changes["Triage status"], ("Pending", "Done"))
        self.assertEqual(report.issue_type, "Bug")
        self.assertEqual(report.assignee, "garlanddiego")
        self.assertNotIn("add_issue_field_values", [name for name, _ in issue_fields.calls])

    def test_triage_metadata_round_trips(self) -> None:
        body = append_triage_metadata(
            "## Triage Analysis\n\nDone.",
            {"version": 1, "issue": 1, "result_fingerprint": "abc"},
        )

        self.assertEqual(
            parse_triage_metadata(body),
            {"version": 1, "issue": 1, "result_fingerprint": "abc"},
        )

    def test_apply_triage_result_skips_duplicate_comment_for_same_fingerprint(self) -> None:
        config = load_project_config(Path("projects/todo-sandbox.toml"))
        github = FakeGithub()
        issue_fields = FakeIssueFields()
        result = parse_triage_result(dict(VALID))

        first = apply_triage_result(
            repo=config.github_repo,
            issue_number=1,
            config=config,
            result=result,
            github=github,  # type: ignore[arg-type]
            issue_fields=issue_fields,  # type: ignore[arg-type]
        )
        second = apply_triage_result(
            repo=config.github_repo,
            issue_number=1,
            config=config,
            result=result,
            github=github,  # type: ignore[arg-type]
            issue_fields=issue_fields,  # type: ignore[arg-type]
        )

        self.assertTrue(first.comment_added)
        self.assertFalse(first.duplicate_comment_skipped)
        self.assertFalse(second.comment_added)
        self.assertTrue(second.duplicate_comment_skipped)
        self.assertEqual(len(github.comments), 1)
        self.assertEqual(second.result_fingerprint, triage_result_fingerprint(result))

    def test_triage_result_fingerprint_ignores_comment_wording(self) -> None:
        first = parse_triage_result(dict(VALID))
        data = dict(VALID)
        data["comment_markdown"] = "## Triage\n\n同一个结构化结论，但换一种说明。"
        second = parse_triage_result(data)

        self.assertEqual(triage_result_fingerprint(first), triage_result_fingerprint(second))

    def test_apply_triage_result_skips_legacy_metadata_when_core_fields_unchanged(self) -> None:
        config = load_project_config(Path("projects/todo-sandbox.toml"))
        github = FakeGithub()
        github.comments.append(
            append_triage_metadata(
                "## Triage\n\n旧算法写过的 triage。",
                {"version": 1, "issue": 1, "result_fingerprint": "legacy"},
            )
        )
        issue_fields = FakeIssueFields()
        result = parse_triage_result(dict(VALID))
        issue_fields.current_values = {
            field: result.fields[field]
            for field in ("Priority", "Triage status", "Triage verdict", "Capability", "PRD status")
        }

        summary = apply_triage_result(
            repo=config.github_repo,
            issue_number=1,
            config=config,
            result=result,
            github=github,  # type: ignore[arg-type]
            issue_fields=issue_fields,  # type: ignore[arg-type]
        )

        self.assertFalse(summary.comment_added)
        self.assertTrue(summary.duplicate_comment_skipped)
        self.assertEqual(len(github.comments), 1)

    def test_apply_triage_result_skips_legacy_comment_when_decision_line_matches(self) -> None:
        config = load_project_config(Path("projects/todo-sandbox.toml"))
        github = FakeGithub()
        github.comments.append(
            append_triage_metadata(
                "## Triage\n\n结论：代码 Bug，优先级 High，归属 Notifications owner @garlanddiego。",
                {"version": 1, "issue": 1, "result_fingerprint": "legacy"},
            )
        )
        issue_fields = FakeIssueFields()
        result = parse_triage_result(dict(VALID))

        summary = apply_triage_result(
            repo=config.github_repo,
            issue_number=1,
            config=config,
            result=result,
            github=github,  # type: ignore[arg-type]
            issue_fields=issue_fields,  # type: ignore[arg-type]
        )

        self.assertFalse(summary.comment_added)
        self.assertTrue(summary.duplicate_comment_skipped)
        self.assertEqual(len(github.comments), 1)

    def test_intake_metadata_round_trips_for_lark_follow_up(self) -> None:
        body = render_issue_body(
            IntakeRecord(
                reporter_name="Reporter",
                reporter_open_id="ou_1",
                created_at="2026-07-01T00:00:00Z",
                chat_id="oc_1",
                root_id="om_root",
                message_id="om_1",
                original_text="bug",
            ),
            language="zh-CN",
        )

        metadata = parse_intake_metadata(body)

        self.assertIsNotNone(metadata)
        assert metadata is not None
        self.assertEqual(metadata["chat_id"], "oc_1")
        self.assertEqual(metadata["message_id"], "om_1")

    def test_apply_needs_info_sends_lark_follow_up_once(self) -> None:
        config = load_project_config(Path("projects/todo-sandbox.toml"))
        github = FakeGithub()
        github.issue_body = render_issue_body(
            IntakeRecord(
                reporter_name="Reporter",
                reporter_open_id="ou_1",
                created_at="2026-07-01T00:00:00Z",
                chat_id=config.lark.chat_id,
                root_id="om_root",
                message_id="om_1",
                original_text="bug",
            ),
            language=config.intake.language,
        )
        issue_fields = FakeIssueFields()
        lark = FakeLarkMessengerClient()
        data = dict(VALID)
        data["triage_status"] = "Needs info"
        data["follow_up_questions"] = ["请补充复现账号", "请补充发生时间"]
        result = parse_triage_result(data)

        first = apply_triage_result(
            repo=config.github_repo,
            issue_number=1,
            config=config,
            result=result,
            github=github,  # type: ignore[arg-type]
            issue_fields=issue_fields,  # type: ignore[arg-type]
            lark=lark,
        )
        second = apply_triage_result(
            repo=config.github_repo,
            issue_number=1,
            config=config,
            result=result,
            github=github,  # type: ignore[arg-type]
            issue_fields=issue_fields,  # type: ignore[arg-type]
            lark=lark,
        )

        self.assertTrue(first.comment_added)
        self.assertFalse(second.comment_added)
        self.assertEqual(len(lark.replies), 1)
        self.assertEqual(lark.replies[0].chat_id, config.lark.chat_id)
        self.assertEqual(lark.replies[0].message_id, "om_1")
        self.assertIn("请补充复现账号", lark.replies[0].text)

    def test_render_triage_summary_lark_message_includes_fields(self) -> None:
        result = parse_triage_result(dict(VALID))
        message = render_triage_summary_lark_message(
            issue_number=9,
            issue_url="https://github.test/o/r/issues/9",
            result=result,
        )

        self.assertIn("#9", message)
        self.assertIn("分诊完成", message)
        self.assertIn("结论：代码 Bug", message)
        self.assertIn("状态：Done", message)
        self.assertIn("优先级：High", message)
        self.assertIn("负责人：garlanddiego", message)

    def test_parse_triage_result_requires_duplicate_of_for_duplicate_verdict(self) -> None:
        data = dict(VALID)
        data["triage_verdict"] = "重复"

        with self.assertRaisesRegex(ValueError, "duplicate_of"):
            parse_triage_result(data)

    def test_parse_triage_result_rejects_duplicate_of_without_duplicate_verdict(self) -> None:
        data = dict(VALID)
        data["duplicate_of"] = 5

        with self.assertRaisesRegex(ValueError, "duplicate_of"):
            parse_triage_result(data)

    def test_parse_triage_result_accepts_duplicate(self) -> None:
        data = dict(VALID)
        data["triage_verdict"] = "重复"
        data["duplicate_of"] = 5

        result = parse_triage_result(data)

        self.assertEqual(result.duplicate_of, 5)
        self.assertEqual(result.fields["Triage verdict"], "重复")

    def test_apply_duplicate_closes_issue_and_skips_assignee(self) -> None:
        config = load_project_config(Path("projects/todo-sandbox.toml"))
        github = FakeGithub()
        issue_fields = FakeIssueFields()
        data = dict(VALID)
        data["triage_verdict"] = "重复"
        data["duplicate_of"] = 5
        result = parse_triage_result(data)

        summary = apply_triage_result(
            repo=config.github_repo,
            issue_number=1,
            config=config,
            result=result,
            github=github,  # type: ignore[arg-type]
            issue_fields=issue_fields,  # type: ignore[arg-type]
        )

        call_names = [name for name, _ in github.calls]
        self.assertIn("close_issue_as_duplicate", call_names)
        self.assertNotIn("add_assignee", call_names)
        close_kwargs = dict(github.calls[call_names.index("close_issue_as_duplicate")][1])
        self.assertEqual(close_kwargs["duplicate_of"], 5)
        self.assertTrue(summary.closed_as_duplicate)
        self.assertFalse(summary.assignee_written)

    def test_apply_duplicate_rejects_self_reference(self) -> None:
        config = load_project_config(Path("projects/todo-sandbox.toml"))
        github = FakeGithub()
        issue_fields = FakeIssueFields()
        data = dict(VALID)
        data["triage_verdict"] = "重复"
        data["duplicate_of"] = 1
        result = parse_triage_result(data)

        with self.assertRaisesRegex(ValueError, "different issue"):
            apply_triage_result(
                repo=config.github_repo,
                issue_number=1,
                config=config,
                result=result,
                github=github,  # type: ignore[arg-type]
                issue_fields=issue_fields,  # type: ignore[arg-type]
            )

    def test_render_triage_summary_lark_message_for_duplicate(self) -> None:
        data = dict(VALID)
        data["triage_verdict"] = "重复"
        data["duplicate_of"] = 5
        result = parse_triage_result(data)

        message = render_triage_summary_lark_message(
            issue_number=9,
            issue_url="https://github.test/o/r/issues/9",
            result=result,
        )

        self.assertIn("重复于 [#5](", message)
        self.assertIn("https://github.test/o/r/issues/5", message)
        self.assertNotIn("负责人", message)

    def test_render_triage_summary_lark_message_includes_runner_name(self) -> None:
        result = parse_triage_result(dict(VALID))

        message = render_triage_summary_lark_message(
            issue_number=9,
            issue_url="https://github.test/o/r/issues/9",
            result=result,
            runner_name="minici32g-bugpatrol",
        )

        self.assertIn("分诊执行机：minici32g-bugpatrol", message)
        self.assertIn("[#9](https://github.test/o/r/issues/9)", message)

    def test_render_triage_summary_lark_message_includes_run_stats(self) -> None:
        from bugpatrol.triage_result import TriageRunStats

        message = render_triage_summary_lark_message(
            issue_number=9,
            issue_url="https://github.test/o/r/issues/9",
            result=parse_triage_result(dict(VALID)),
            run_stats=TriageRunStats(
                duration_seconds=83.4,
                input_tokens=12345,
                output_tokens=678,
                model="deepseek-v4-pro[1m]",
            ),
        )

        self.assertIn("用时 1m23s", message)
        self.assertIn("模型 deepseek-v4-pro[1m]", message)
        self.assertIn("token 输入12,345/输出678", message)

    def test_format_run_stats_omits_missing_pieces(self) -> None:
        from bugpatrol.triage_result import TriageRunStats, format_run_stats

        self.assertEqual(format_run_stats(None), "")
        self.assertEqual(format_run_stats(TriageRunStats()), "")
        self.assertEqual(
            format_run_stats(TriageRunStats(duration_seconds=5)),
            "用时 5s",
        )

    def test_triage_runner_name_prefers_bugpatrol_env(self) -> None:
        import os
        from unittest.mock import patch as mock_patch

        from bugpatrol.triage_result import triage_runner_name

        with mock_patch.dict(os.environ, {"RUNNER_NAME": "gh-runner", "BUGPATROL_RUNNER_NAME": "custom"}):
            self.assertEqual(triage_runner_name(), "custom")
        env = {k: v for k, v in os.environ.items() if k != "BUGPATROL_RUNNER_NAME"}
        env["RUNNER_NAME"] = "gh-runner"
        with mock_patch.dict(os.environ, env, clear=True):
            self.assertEqual(triage_runner_name(), "gh-runner")

    def test_render_needs_info_lark_message_lists_questions(self) -> None:
        message = render_needs_info_lark_message(
            issue_number=7,
            issue_url="https://github.test/o/r/issues/7",
            questions=("问题一", "问题二"),
        )

        self.assertIn("#7", message)
        self.assertIn("1. 问题一", message)
        self.assertIn("2. 问题二", message)
        self.assertNotIn("<at", message)

    def test_render_needs_info_lark_message_mentions_reporter(self) -> None:
        message = render_needs_info_lark_message(
            issue_number=7,
            issue_url="https://github.test/o/r/issues/7",
            questions=("问题一",),
            reporter_open_id="ou_reporter",
        )

        self.assertIn('<at user_id="ou_reporter"></at>', message)

    def test_render_triage_summary_lark_message_mentions_assignee(self) -> None:
        result = parse_triage_result(dict(VALID))
        message = render_triage_summary_lark_message(
            issue_number=9,
            issue_url="https://github.test/o/r/issues/9",
            result=result,
            assignee_open_id="ou_dev",
        )

        self.assertIn('负责人：<at user_id="ou_dev">garlanddiego</at>', message)

    def test_apply_needs_info_lark_follow_up_mentions_reporter(self) -> None:
        config = load_project_config(Path("projects/todo-sandbox.toml"))
        github = FakeGithub()
        issue_fields = FakeIssueFields()
        lark = FakeLarkMessengerClient()
        data = dict(VALID)
        data["triage_status"] = "Needs info"
        data["follow_up_questions"] = ["请补充复现账号"]
        result = parse_triage_result(data)

        apply_triage_result(
            repo=config.github_repo,
            issue_number=1,
            config=config,
            result=result,
            github=github,  # type: ignore[arg-type]
            issue_fields=issue_fields,  # type: ignore[arg-type]
            lark=lark,
        )

        self.assertEqual(len(lark.replies), 1)
        self.assertIn('<at user_id="ou_1"></at>', lark.replies[0].text)

    def test_apply_triage_summary_mentions_mapped_assignee(self) -> None:
        from dataclasses import replace as dc_replace

        base_config = load_project_config(Path("projects/todo-sandbox.toml"))
        config = dc_replace(
            base_config,
            lark=dc_replace(base_config.lark, user_open_ids={"garlanddiego": "ou_dev"}),
        )
        github = FakeGithub()
        issue_fields = FakeIssueFields()
        lark = FakeLarkMessengerClient()
        result = parse_triage_result(dict(VALID))

        apply_triage_result(
            repo=config.github_repo,
            issue_number=1,
            config=config,
            result=result,
            github=github,  # type: ignore[arg-type]
            issue_fields=issue_fields,  # type: ignore[arg-type]
            lark=lark,
        )

        self.assertEqual(len(lark.replies), 1)
        self.assertIn('<at user_id="ou_dev">garlanddiego</at>', lark.replies[0].text)


def managed_issue_body() -> str:
    return render_issue_body(
        IntakeRecord(
            reporter_name="Reporter",
            reporter_open_id="ou_1",
            created_at="2026-07-01T00:00:00Z",
            chat_id="oc_1",
            root_id="om_root",
            message_id="om_1",
            original_text="bug",
        )
    )


class IntakeTopicReplyTest(unittest.TestCase):
    def _issue_body(self) -> str:
        return render_issue_body(
            IntakeRecord(
                reporter_name="Diego",
                reporter_open_id="ou_reporter",
                created_at="2026-06-30T10:00:00Z",
                chat_id="oc_chat",
                root_id="om_root",
                message_id="om_msg",
                original_text="发完图片后卡在 thinking",
                lark_topic_url="https://lark.example/topic/om_root",
                attachments=(),
            )
        )

    def test_tolerates_withdrawn_source_message(self) -> None:
        from bugpatrol.lark import LarkOpenApiError
        from bugpatrol.triage_result import _reply_to_intake_topic

        lark = FakeLarkMessengerClient()

        def failing_reply(**kwargs: object) -> None:
            raise LarkOpenApiError('Lark HTTP 400: {"code":230011,"msg":"The message was withdrawn."}')

        lark.reply_to_message = failing_reply  # type: ignore[method-assign]

        _reply_to_intake_topic(issue_body=self._issue_body(), lark=lark, text="分诊完成")

    def test_reraises_other_lark_errors(self) -> None:
        from bugpatrol.lark import LarkOpenApiError
        from bugpatrol.triage_result import _reply_to_intake_topic

        lark = FakeLarkMessengerClient()

        def failing_reply(**kwargs: object) -> None:
            raise LarkOpenApiError('Lark HTTP 500: {"code":9999,"msg":"boom"}')

        lark.reply_to_message = failing_reply  # type: ignore[method-assign]

        with self.assertRaises(LarkOpenApiError):
            _reply_to_intake_topic(issue_body=self._issue_body(), lark=lark, text="分诊完成")


if __name__ == "__main__":
    unittest.main()
