from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from bugpatrol.config import load_project_config
from bugpatrol.github_fields import (
    GitHubIssueFieldsClient,
    GitHubIssueFieldsError,
    IssueField,
    build_issue_field_values_payload,
)


class GitHubIssueFieldsTest(unittest.TestCase):
    def test_builds_payload_from_logical_field_names(self) -> None:
        config = load_project_config(Path("projects/todo-sandbox.toml"))
        payload = build_issue_field_values_payload(
            config=config,
            live_fields={
                "Source": IssueField(id=1, name="Source", data_type="single_select", options=("Lark",)),
                "Triage status": IssueField(
                    id=2,
                    name="Triage status",
                    data_type="single_select",
                    options=("Pending", "Done"),
                ),
                "Evidence": IssueField(
                    id=3,
                    name="Evidence",
                    data_type="single_select",
                    options=("文字描述", "截图"),
                ),
            },
            logical_values={"Source": "Lark", "Triage status": "Pending", "Evidence": "截图"},
        )

        self.assertEqual(
            payload,
            [
                {"field_id": 1, "value": "Lark"},
                {"field_id": 2, "value": "Pending"},
                {"field_id": 3, "value": "截图"},
            ],
        )

    def test_rejects_unknown_single_select_option(self) -> None:
        config = load_project_config(Path("projects/todo-sandbox.toml"))

        with self.assertRaisesRegex(GitHubIssueFieldsError, "not one of"):
            build_issue_field_values_payload(
                config=config,
                live_fields={
                    "Source": IssueField(
                        id=1,
                        name="Source",
                        data_type="single_select",
                        options=("GitHub",),
                    )
                },
                logical_values={"Source": "Lark"},
            )

    def test_client_lists_org_fields(self) -> None:
        client = GitHubIssueFieldsClient()
        response = json.dumps(
            [
                {
                    "id": 10,
                    "name": "Priority",
                    "data_type": "single_select",
                    "options": [{"id": 1, "name": "High"}],
                }
            ]
        )

        with patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(["gh"], 0, response, "")
            fields = client.list_org_fields(org="TheCloverLab")

        self.assertEqual(fields["Priority"].id, 10)
        self.assertEqual(fields["Priority"].options, ("High",))
        self.assertIn("/orgs/TheCloverLab/issue-fields", run.call_args.args[0])

    def test_client_writes_issue_values(self) -> None:
        config = load_project_config(Path("projects/todo-sandbox.toml"))
        client = GitHubIssueFieldsClient()
        fields_response = json.dumps(
            [
                {
                    "id": 1,
                    "name": "Source",
                    "data_type": "single_select",
                    "options": [{"name": "Lark"}],
                }
            ]
        )

        with patch("subprocess.run") as run:
            run.side_effect = [
                subprocess.CompletedProcess(["gh"], 0, fields_response, ""),
                subprocess.CompletedProcess(["gh"], 0, "[]", ""),
            ]
            client.add_issue_field_values(
                repo="TheCloverLab/example",
                issue_number=9,
                values={"Source": "Lark"},
                config=config,
            )

        write_call = run.call_args_list[1]
        self.assertIn("/repos/TheCloverLab/example/issues/9/issue-field-values", write_call.args[0])
        self.assertEqual(
            json.loads(write_call.kwargs["input"]),
            {"issue_field_values": [{"field_id": 1, "value": "Lark"}]},
        )

    def test_client_reads_issue_values(self) -> None:
        client = GitHubIssueFieldsClient()
        response = json.dumps(
            [
                {
                    "issue_field_name": "Source",
                    "single_select_option": {"name": "Lark"},
                    "value": 1,
                },
                {"issue_field_name": "Estimate", "value": 3},
            ]
        )

        with patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(["gh"], 0, response, "")
            values = client.get_issue_field_values(repo="TheCloverLab/example", issue_number=9)

        self.assertEqual(values, {"Source": "Lark", "Estimate": "3"})
        self.assertIn(
            "/repos/TheCloverLab/example/issues/9/issue-field-values",
            run.call_args.args[0],
        )


if __name__ == "__main__":
    unittest.main()
