from __future__ import annotations

import unittest
from pathlib import Path

from bugpatrol.config import load_project_config
from bugpatrol.triage_result import TriageResult, apply_triage_result, parse_triage_result


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

    def set_issue_type(self, **kwargs: object) -> None:
        self.calls.append(("set_issue_type", kwargs))

    def add_issue_comment(self, **kwargs: object) -> None:
        self.calls.append(("add_issue_comment", kwargs))

    def add_assignee(self, **kwargs: object) -> None:
        self.calls.append(("add_assignee", kwargs))


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

        apply_triage_result(
            repo=config.github_repo,
            issue_number=1,
            config=config,
            result=result,
            github=github,  # type: ignore[arg-type]
            issue_fields=issue_fields,  # type: ignore[arg-type]
        )

        self.assertEqual([name for name, _ in github.calls], ["set_issue_type", "add_issue_comment", "add_assignee"])
        self.assertEqual(issue_fields.calls[0][0], "add_issue_field_values")


if __name__ == "__main__":
    unittest.main()
