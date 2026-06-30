from __future__ import annotations

import unittest
from pathlib import Path

from bugpatrol.config import load_project_config
from bugpatrol.clients import GitHubIssueComment
from bugpatrol.triage_result import (
    TriageResult,
    append_triage_metadata,
    apply_triage_result,
    parse_triage_metadata,
    parse_triage_result,
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
    "comment_markdown": "## Triage Analysis\n\n是代码 Bug。",
}


class FakeGithub:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.comments: list[str] = []

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


class FakeIssueFields:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def add_issue_field_values(self, **kwargs: object) -> None:
        self.calls.append(("add_issue_field_values", kwargs))


class TriageResultTest(unittest.TestCase):
    def test_parse_triage_result_validates_fields(self) -> None:
        result = parse_triage_result(dict(VALID))

        self.assertEqual(result.issue_type, "Bug")
        self.assertEqual(result.assignee, "garlanddiego")
        self.assertEqual(result.fields["Triage verdict"], "代码 Bug")

    def test_parse_triage_result_rejects_invalid_enum(self) -> None:
        data = dict(VALID)
        data["triage_verdict"] = "Not a verdict"

        with self.assertRaisesRegex(ValueError, "invalid value"):
            parse_triage_result(data)

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
            ["set_issue_type", "list_issue_comments", "add_issue_comment", "add_assignee"],
        )
        self.assertEqual(issue_fields.calls[0][0], "add_issue_field_values")
        self.assertTrue(summary.comment_added)
        self.assertFalse(summary.duplicate_comment_skipped)
        self.assertIn("BUGPATROL_TRIAGE_META", github.comments[0])

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


if __name__ == "__main__":
    unittest.main()
