from __future__ import annotations

import json
import subprocess
import unittest
from unittest.mock import patch

from bugpatrol.github import GitHubCliError, GitHubCliIssuesClient


class GitHubCliIssuesClientTest(unittest.TestCase):
    def test_find_issue_by_intake_root_reads_issue_bodies(self) -> None:
        client = GitHubCliIssuesClient()
        stdout = json.dumps(
            [
                {
                    "number": 7,
                    "url": "https://github.com/o/r/issues/7",
                    "title": "bug",
                    "body": '<!-- BUGPATROL_INTAKE_META:{"chat_id":"oc_1","root_id":"om_1"} -->',
                }
            ]
        )

        with patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(["gh"], 0, stdout, "")
            issue = client.find_issue_by_intake_root(repo="o/r", chat_id="oc_1", root_id="om_1")

        self.assertIsNotNone(issue)
        self.assertEqual(issue.number, 7)
        run.assert_called_once()
        self.assertIn("--state", run.call_args.args[0])
        self.assertIn("all", run.call_args.args[0])

    def test_create_issue_posts_body_via_stdin_and_parses_url(self) -> None:
        client = GitHubCliIssuesClient()

        with patch("subprocess.run") as run:
            run.side_effect = [
                subprocess.CompletedProcess(["gh"], 0, "https://github.com/o/r/issues/42\n", ""),
                subprocess.CompletedProcess(["gh"], 0, "{}", ""),
            ]
            issue = client.create_issue(
                repo="o/r",
                title="title",
                body="body",
                issue_type="Bug",
                fields={"Source": "Lark"},
            )

        self.assertEqual(issue.number, 42)
        self.assertEqual(issue.url, "https://github.com/o/r/issues/42")
        self.assertEqual(run.call_args_list[0].kwargs["input"], "body")
        self.assertIn("--body-file", run.call_args_list[0].args[0])
        self.assertIn("type=Bug", run.call_args_list[1].args[0])

    def test_create_issue_writes_fields_when_configured(self) -> None:
        from pathlib import Path

        from bugpatrol.config import load_project_config

        config = load_project_config(Path("projects/todo-sandbox.toml"))
        field_writer = unittest.mock.Mock()
        client = GitHubCliIssuesClient(issue_fields=field_writer, project_config=config)

        with patch("subprocess.run") as run:
            run.side_effect = [
                subprocess.CompletedProcess(["gh"], 0, "https://github.com/o/r/issues/42\n", ""),
                subprocess.CompletedProcess(["gh"], 0, "{}", ""),
            ]
            client.create_issue(
                repo="o/r",
                title="title",
                body="body",
                issue_type="Bug",
                fields={"Source": "Lark"},
            )

        field_writer.add_issue_field_values.assert_called_once_with(
            repo="o/r",
            issue_number=42,
            values={"Source": "Lark"},
            config=config,
        )

    def test_add_issue_comment_uses_body_file_stdin(self) -> None:
        client = GitHubCliIssuesClient()

        with patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(["gh"], 0, "", "")
            client.add_issue_comment(repo="o/r", issue_number=42, body="comment")

        self.assertEqual(run.call_args.kwargs["input"], "comment")
        self.assertIn("comment", run.call_args.args[0])

    def test_list_issue_comments_reads_rest_comments(self) -> None:
        client = GitHubCliIssuesClient()

        with patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(
                ["gh"],
                0,
                json.dumps([{"id": 123, "body": "hello"}]),
                "",
            )
            comments = client.list_issue_comments(repo="o/r", issue_number=42)

        self.assertEqual(len(comments), 1)
        self.assertEqual(comments[0].id, "123")
        self.assertEqual(comments[0].body, "hello")
        self.assertIn("/repos/o/r/issues/42/comments", run.call_args.args[0])

    def test_get_issue_type_reads_rest_issue_type(self) -> None:
        client = GitHubCliIssuesClient()

        with patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(
                ["gh"], 0, json.dumps({"type": {"name": "Bug"}}), ""
            )
            issue_type = client.get_issue_type(repo="o/r", issue_number=42)

        self.assertEqual(issue_type, "Bug")
        self.assertIn("/repos/o/r/issues/42", run.call_args.args[0])

    def test_get_pull_request_reads_body_and_closing_issue_references(self) -> None:
        client = GitHubCliIssuesClient()

        with patch("subprocess.run") as run:
            run.side_effect = (
                subprocess.CompletedProcess(
                    ["gh"],
                    0,
                    json.dumps(
                        {
                            "number": 7,
                            "url": "https://github.com/o/r/pull/7",
                            "title": "Fix thing",
                            "body": "Closes #2",
                            "closingIssuesReferences": [{"number": 2}],
                        }
                    ),
                    "",
                ),
                subprocess.CompletedProcess(
                    ["gh"],
                    0,
                    json.dumps([{"event": "connected", "subject": {"type": "issue", "number": 3}}]),
                    "",
                ),
            )
            pr = client.get_pull_request(repo="o/r", pr="7")

        self.assertEqual(pr.number, 7)
        self.assertEqual(pr.closing_issue_numbers, (2,))
        self.assertEqual(pr.timeline_issue_numbers, (3,))
        self.assertIn("pr", run.call_args_list[0].args[0])
        self.assertTrue(
            any("closingIssuesReferences" in arg for arg in run.call_args_list[0].args[0])
        )
        self.assertIn("/repos/o/r/issues/7/timeline", run.call_args_list[1].args[0])

    def test_list_merged_pull_requests_reads_closing_refs(self) -> None:
        client = GitHubCliIssuesClient()

        with patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(
                ["gh"],
                0,
                json.dumps(
                    [
                        {
                            "number": 7,
                            "url": "https://github.com/o/r/pull/7",
                            "title": "Fix thing",
                            "body": "Closes #2",
                            "closingIssuesReferences": [{"number": 2}],
                        }
                    ]
                ),
                "",
            )
            pulls = client.list_merged_pull_requests(repo="o/r", limit=5)

        self.assertEqual(len(pulls), 1)
        self.assertEqual(pulls[0].number, 7)
        self.assertEqual(pulls[0].closing_issue_numbers, (2,))
        self.assertEqual(pulls[0].timeline_issue_numbers, ())
        args = run.call_args.args[0]
        self.assertIn("merged", args)
        self.assertIn("5", args)

    def test_raises_on_gh_failure(self) -> None:
        client = GitHubCliIssuesClient()

        with patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(["gh"], 1, "", "bad auth")
            with self.assertRaisesRegex(GitHubCliError, "bad auth"):
                client.find_issue_by_intake_root(repo="o/r", chat_id="oc", root_id="om")


if __name__ == "__main__":
    unittest.main()
