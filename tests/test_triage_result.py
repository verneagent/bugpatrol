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
    reject_affected_branch,
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

    def test_blame_suggestion_is_visible_and_written_when_field_is_mapped(self) -> None:
        base_config = load_project_config(Path("projects/todo-sandbox.toml"))
        config = replace(
            base_config,
            issue_field_names={**base_config.issue_field_names, "Blame": "Blame"},
        )
        data = dict(VALID)
        data["blame_suggestion"] = "可能由 PR #123 的 push token 绑定改动引入"
        result = parse_triage_result(data)

        values = triage_field_values_for_write(result, config=config)
        comment = render_triage_comment(result)

        self.assertEqual(values["Blame"], "可能由 PR #123 的 push token 绑定改动引入")
        self.assertIn("Blame 建议：可能由 PR #123", comment)

    def test_unmatched_affected_branch_degrades_instead_of_failing(self) -> None:
        data = dict(VALID)
        data["affected_branch"] = "release-9"

        result = parse_triage_result(data, branch_patterns=("main", "post", "feature-*"))

        self.assertEqual(result.affected_branch, "")
        self.assertEqual(result.affected_branch_rejected, "release-9")
        self.assertIn("release-9", render_triage_comment(result))
        self.assertIn("未采信", render_triage_comment(result))

    def test_rejected_affected_branch_is_never_written_to_fields(self) -> None:
        base_config = load_project_config(Path("projects/todo-sandbox.toml"))
        config = replace(
            base_config,
            issue_field_names={**base_config.issue_field_names, "Affected branch": "Affected branch"},
        )
        data = dict(VALID)
        data["affected_branch"] = "release-9"
        result = parse_triage_result(data, branch_patterns=("main",))

        self.assertNotIn("Affected branch", triage_field_values_for_write(result, config=config))

    def test_reject_affected_branch_demotes_value(self) -> None:
        data = dict(VALID)
        data["affected_branch"] = "feature-ghost"
        result = parse_triage_result(data, branch_patterns=("feature-*",))

        rejected = reject_affected_branch(result)

        self.assertEqual(rejected.affected_branch, "")
        self.assertEqual(rejected.affected_branch_rejected, "feature-ghost")

    def test_affected_branch_matching_pattern_is_accepted(self) -> None:
        data = dict(VALID)
        data["affected_branch"] = "feature-login"

        result = parse_triage_result(data, branch_patterns=("main", "post", "feature-*"))

        self.assertEqual(result.affected_branch, "feature-login")

    def test_affected_branch_may_be_empty_when_unknown(self) -> None:
        result = parse_triage_result(dict(VALID), branch_patterns=("main",))

        self.assertEqual(result.affected_branch, "")

    def test_affected_branch_is_visible_and_written_when_field_is_mapped(self) -> None:
        base_config = load_project_config(Path("projects/todo-sandbox.toml"))
        config = replace(
            base_config,
            issue_field_names={**base_config.issue_field_names, "Affected branch": "Affected branch"},
        )
        data = dict(VALID)
        data["affected_branch"] = "chat-live"
        result = parse_triage_result(data, branch_patterns=("main", "chat-live"))

        values = triage_field_values_for_write(result, config=config)
        comment = render_triage_comment(result)

        self.assertEqual(values["Affected branch"], "chat-live")
        self.assertIn("影响分支：chat-live", comment)

    def test_affected_branch_not_written_when_field_is_not_mapped(self) -> None:
        config = load_project_config(Path("projects/todo-sandbox.toml"))
        config = replace(
            config,
            issue_field_names={
                name: value
                for name, value in config.issue_field_names.items()
                if name != "Affected branch"
            },
        )
        data = dict(VALID)
        data["affected_branch"] = "main"
        result = parse_triage_result(data)

        values = triage_field_values_for_write(result, config=config)

        self.assertNotIn("Affected branch", values)

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

    def test_render_needs_info_lark_message_lists_questions(self) -> None:
        message = render_needs_info_lark_message(
            issue_number=7,
            issue_url="https://github.test/o/r/issues/7",
            questions=("问题一", "问题二"),
        )

        self.assertIn("#7", message)
        self.assertIn("1. 问题一", message)
        self.assertIn("2. 问题二", message)


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


if __name__ == "__main__":
    unittest.main()
