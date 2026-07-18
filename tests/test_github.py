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

    def test_get_open_pull_request_by_head_reads_number_and_url(self) -> None:
        client = GitHubCliIssuesClient()
        stdout = json.dumps(
            [
                {
                    "number": 9,
                    "url": "https://github.com/o/r/pull/9",
                    "baseRefName": "feature-demo",
                    "mergeable": "CONFLICTING",
                }
            ]
        )

        with patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(["gh"], 0, stdout, "")
            pr = client.get_open_pull_request_by_head(repo="o/r", head="bugpatrol/fix-issue-7")

        self.assertIsNotNone(pr)
        self.assertEqual(pr.number, 9)
        self.assertEqual(pr.url, "https://github.com/o/r/pull/9")
        self.assertEqual(pr.base_ref, "feature-demo")
        self.assertEqual(pr.mergeable, "CONFLICTING")

    def test_get_open_pull_request_by_head_none_when_absent(self) -> None:
        client = GitHubCliIssuesClient()
        with patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(["gh"], 0, "[]", "")
            self.assertIsNone(client.get_open_pull_request_by_head(repo="o/r", head="h"))

    def test_list_unresolved_review_threads_filters_resolved(self) -> None:
        client = GitHubCliIssuesClient()
        stdout = json.dumps(
            {
                "data": {
                    "repository": {
                        "pullRequest": {
                            "reviewThreads": {
                                "nodes": [
                                    {
                                        "id": "RT_open",
                                        "isResolved": False,
                                        "comments": {
                                            "nodes": [
                                                {
                                                    "body": "改小一点",
                                                    "path": "src/todo.ts",
                                                    "line": 12,
                                                    "author": {"login": "rev"},
                                                }
                                            ]
                                        },
                                    },
                                    {
                                        "id": "RT_done",
                                        "isResolved": True,
                                        "comments": {"nodes": []},
                                    },
                                ]
                            }
                        }
                    }
                }
            }
        )

        with patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(["gh"], 0, stdout, "")
            threads = client.list_unresolved_review_threads(repo="o/r", pr_number=9)

        self.assertEqual(len(threads), 1)
        self.assertEqual(threads[0].id, "RT_open")
        self.assertEqual(threads[0].comments[0].author, "rev")
        self.assertEqual(threads[0].comments[0].path, "src/todo.ts")
        self.assertEqual(threads[0].comments[0].line, 12)

    def test_resolve_review_thread_calls_mutation(self) -> None:
        client = GitHubCliIssuesClient()
        with patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(["gh"], 0, "{}", "")
            client.resolve_review_thread(thread_id="RT_open")
        args = run.call_args.args[0]
        self.assertIn("graphql", args)
        self.assertTrue(any("resolveReviewThread" in a for a in args))

    def test_list_failed_runs_keeps_only_failures(self) -> None:
        client = GitHubCliIssuesClient()
        stdout = json.dumps(
            [
                {"databaseId": 1, "name": "iOS Build", "workflowName": "iOS Build", "conclusion": "failure"},
                {"databaseId": 2, "name": "Web Build", "workflowName": "Web Build", "conclusion": "success"},
                {"databaseId": 3, "name": "API Tests", "workflowName": "API Tests", "conclusion": "failure"},
            ]
        )
        with patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(["gh"], 0, stdout, "")
            runs = client.list_failed_runs_for_sha(repo="o/r", head_sha="abc")
        self.assertEqual([r.run_id for r in runs], [1, 3])
        args = run.call_args.args[0]
        self.assertIn("--commit", args)
        self.assertIn("abc", args)

    def test_list_failed_check_runs_for_sha_returns_names(self) -> None:
        client = GitHubCliIssuesClient()
        # The --jq already filters to failed check-run names, one per line.
        with patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(
                ["gh"], 0, "web / test\napi / api-tests\n", ""
            )
            names = client.list_failed_check_runs_for_sha(repo="o/r", head_sha="abc")
        self.assertEqual(names, ("web / test", "api / api-tests"))
        args = run.call_args.args[0]
        self.assertIn("repos/o/r/commits/abc/check-runs", args)
        self.assertIn("--jq", args)

    def test_get_run_failed_logs_truncates_tail(self) -> None:
        client = GitHubCliIssuesClient()
        big = "\n".join(f"line {i}" for i in range(500))
        with patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(["gh"], 0, big, "")
            tail = client.get_run_failed_logs(repo="o/r", run_id=1)
        # Only the tail (last 200 lines) is kept; the head is dropped.
        self.assertNotIn("line 0\n", tail)
        self.assertIn("line 499", tail)
        self.assertLessEqual(len(tail.splitlines()), 200)
        self.assertIn("--log-failed", run.call_args.args[0])

    def test_list_pull_request_comments_parses_bodies(self) -> None:
        client = GitHubCliIssuesClient()
        stdout = json.dumps([{"id": 11, "body": "hello"}, {"id": 12, "body": None}])
        with patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(["gh"], 0, stdout, "")
            comments = client.list_pull_request_comments(repo="o/r", pr_number=9)
        self.assertEqual([c.id for c in comments], ["11", "12"])
        self.assertEqual(comments[1].body, "")
        self.assertIn("/repos/o/r/issues/9/comments", run.call_args.args[0])

    def test_raises_on_gh_failure(self) -> None:
        client = GitHubCliIssuesClient()

        with patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(["gh"], 1, "", "bad auth")
            with self.assertRaisesRegex(GitHubCliError, "bad auth"):
                client.find_issue_by_intake_root(repo="o/r", chat_id="oc", root_id="om")

    def test_retries_transient_gateway_error_then_succeeds(self) -> None:
        # A flaky 502 on `add_assignee` (the last apply step) must not drop the
        # assignment nor fail the whole run: retry until it lands.
        client = GitHubCliIssuesClient(sleep=lambda _s: None)
        gateway = 'failed to update ...: non-200 OK status code: 502 Bad Gateway'

        with patch("subprocess.run") as run:
            run.side_effect = [
                subprocess.CompletedProcess(["gh"], 1, "", gateway),
                subprocess.CompletedProcess(["gh"], 0, "", ""),
            ]
            client.add_assignee(repo="o/r", issue_number=4004, assignee="SoxiaLiSA")

        self.assertEqual(run.call_count, 2)

    def test_does_not_retry_non_transient_error(self) -> None:
        client = GitHubCliIssuesClient(sleep=lambda _s: None)

        with patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(["gh"], 1, "", "bad auth")
            with self.assertRaisesRegex(GitHubCliError, "bad auth"):
                client.add_assignee(repo="o/r", issue_number=1, assignee="a")
        run.assert_called_once()

    def test_raises_after_exhausting_transient_retries(self) -> None:
        client = GitHubCliIssuesClient(transient_retries=3, sleep=lambda _s: None)
        gateway = 'non-200 OK status code: 503 Service Unavailable'

        with patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(["gh"], 1, "", gateway)
            with self.assertRaisesRegex(GitHubCliError, "503"):
                client.add_assignee(repo="o/r", issue_number=1, assignee="a")
        self.assertEqual(run.call_count, 3)


if __name__ == "__main__":
    unittest.main()
