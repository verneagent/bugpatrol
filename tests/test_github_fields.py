from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from bugpatrol.config import load_project_config
from bugpatrol.fields import FieldSpec, default_field_specs
from bugpatrol.github_fields import (
    GitHubIssueFieldsClient,
    GitHubIssueFieldsError,
    IssueField,
    build_issue_field_values_payload,
    find_field_option_drift,
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


class GitHubIssueFieldsRetryTest(unittest.TestCase):
    def test_retries_transient_tls_timeout_then_succeeds(self) -> None:
        # The TLS handshake timeout that crashed the watcher mid-poll (a
        # `get_issue_field_values` in `triage_status`) must retry, not raise.
        client = GitHubIssueFieldsClient(sleep=lambda _s: None)
        tls = (
            'Post "https://api.github.com/graphql": tls: failed to verify certificate: '
            "x509: certificate signed by unknown authority"
        )

        with patch("subprocess.run") as run:
            run.side_effect = [
                subprocess.CompletedProcess(["gh"], 1, "", tls),
                subprocess.CompletedProcess(["gh"], 0, "[]", ""),
            ]
            values = client.get_issue_field_values(repo="o/r", issue_number=4057)

        self.assertEqual(values, {})
        self.assertEqual(run.call_count, 2)

    def test_does_not_retry_non_transient_error(self) -> None:
        client = GitHubIssueFieldsClient(sleep=lambda _s: None)

        with patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(["gh"], 1, "", "bad auth")
            with self.assertRaisesRegex(GitHubIssueFieldsError, "bad auth"):
                client.get_issue_field_values(repo="o/r", issue_number=1)
        run.assert_called_once()

    def test_raises_after_exhausting_transient_retries(self) -> None:
        client = GitHubIssueFieldsClient(transient_retries=3, sleep=lambda _s: None)
        tls = "net/http: TLS handshake timeout"

        with patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(["gh"], 1, "", tls)
            with self.assertRaisesRegex(GitHubIssueFieldsError, "TLS handshake timeout"):
                client.get_issue_field_values(repo="o/r", issue_number=1)
        self.assertEqual(run.call_count, 3)

    def test_missing_issue_fields_error_is_not_retried(self) -> None:
        # A repo without Issue Fields enabled fails deterministically; retrying
        # would only delay the actionable error.
        client = GitHubIssueFieldsClient(transient_retries=3, sleep=lambda _s: None)

        with patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(["gh"], 1, "", "Not Found")
            with self.assertRaisesRegex(GitHubIssueFieldsError, "organization-owned"):
                client.list_org_fields(org="o")
        run.assert_called_once()


class FieldOptionDriftTest(unittest.TestCase):
    def _live(self, name: str, options: tuple[str, ...], data_type: str = "single_select") -> dict[str, IssueField]:
        return {name: IssueField(id=1, name=name, data_type=data_type, options=options)}

    def test_no_drift_when_specs_subset_of_live(self) -> None:
        specs = {"Owner reason": FieldSpec("Owner reason", ("CODEOWNERS", "OpenSpec"), "")}
        live = self._live("Owner reason", ("CODEOWNERS", "OpenSpec", "Manual"))
        self.assertEqual(find_field_option_drift(specs, live), [])

    def test_reports_missing_option(self) -> None:
        specs = {"Owner reason": FieldSpec("Owner reason", ("CODEOWNERS", "OpenSpec"), "")}
        live = self._live("Owner reason", ("CODEOWNERS", "Manual"))
        drift = find_field_option_drift(specs, live)
        self.assertEqual(len(drift), 1)
        self.assertIn("Owner reason", drift[0])
        self.assertIn("OpenSpec", drift[0])

    def test_ignores_fields_absent_from_live(self) -> None:
        specs = {"Owner reason": FieldSpec("Owner reason", ("OpenSpec",), "")}
        self.assertEqual(find_field_option_drift(specs, {}), [])

    def test_ignores_non_select_fields(self) -> None:
        specs = {"Affected branch": FieldSpec("Affected branch", ("anything",), "")}
        live = self._live("Affected branch", (), data_type="text")
        self.assertEqual(find_field_option_drift(specs, live), [])

    def test_canonical_specs_pass_against_matching_live_fields(self) -> None:
        specs = default_field_specs()
        live = {
            name: IssueField(id=i, name=name, data_type="single_select", options=spec.values)
            for i, (name, spec) in enumerate(specs.items())
        }
        self.assertEqual(find_field_option_drift(specs, live), [])


if __name__ == "__main__":
    unittest.main()
