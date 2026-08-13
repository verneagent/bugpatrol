from __future__ import annotations

import dataclasses
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

from bugpatrol.clients import GitHubIssue
from bugpatrol.config import load_project_config
from bugpatrol.lark import LarkMessage, parse_lark_message
from bugpatrol.slash_commands import (
    SlashCommand,
    SlashCommandHandler,
    make_dispatch,
    parse_slash_command,
    resolve_assignee_login,
)


def _config(user_open_ids: dict[str, str] | None = None):
    config = load_project_config(Path("projects/todo-sandbox.toml"))
    lark = dataclasses.replace(config.lark, user_open_ids=user_open_ids)
    return dataclasses.replace(config, github_repo="acme/widgets", lark=lark)


def _message(**overrides: object) -> LarkMessage:
    values = {
        "message_id": "om_1",
        "chat_id": "oc_chat",
        "root_id": "om_root",
        "sender_open_id": "ou_user",
        "sender_type": "user",
        "create_time": "2026-07-13T14:00:00Z",
        "msg_type": "text",
        "text": "/fix",
    }
    values.update(overrides)
    return LarkMessage(**values)  # type: ignore[arg-type]


class _StubIssueClient:
    def __init__(self, issue: GitHubIssue | None) -> None:
        self._issue = issue
        self.assigned: list[tuple[int, str]] = []
        self.reopened: list[int] = []
        self.comments: list[tuple[int, str]] = []

    def find_issue_by_intake_root(
        self, *, repo: str, chat_id: str, root_id: str
    ) -> GitHubIssue | None:
        return self._issue

    def reopen_issue(self, *, repo: str, issue_number: int) -> None:
        self.reopened.append(issue_number)

    def set_assignee(self, *, repo: str, issue_number: int, assignee: str) -> None:
        self.assigned.append((issue_number, assignee))

    def add_issue_comment(self, *, repo: str, issue_number: int, body: str) -> None:
        self.comments.append((issue_number, body))


class _StubReplyClient:
    def __init__(self) -> None:
        self.replies: list[str] = []

    def reply_to_message(self, *, chat_id: str, message_id: str, text: str) -> None:
        self.replies.append(text)


def _issue(number: int = 42, state: str = "open") -> GitHubIssue:
    return GitHubIssue(
        number=number,
        url=f"https://github.test/acme/widgets/issues/{number}",
        title="bug",
        body="body",
        state=state,
    )


class ParseSlashCommandTest(unittest.TestCase):
    def test_fix_exact(self) -> None:
        self.assertEqual(parse_slash_command("/fix"), SlashCommand(kind="fix"))

    def test_fix_case_insensitive(self) -> None:
        self.assertEqual(parse_slash_command("/FIX"), SlashCommand(kind="fix"))

    def test_fix_with_trailing_whitespace(self) -> None:
        self.assertEqual(parse_slash_command("  /fix  "), SlashCommand(kind="fix"))

    def test_fix_with_trailing_text_is_captured_as_body(self) -> None:
        self.assertEqual(
            parse_slash_command("/fix keep the delete button single"),
            SlashCommand(kind="fix", body="keep the delete button single"),
        )

    def test_fix_mentioned_in_sentence_is_not_a_command(self) -> None:
        self.assertIsNone(parse_slash_command("please run /fix on this"))

    def test_retriage_exact(self) -> None:
        self.assertEqual(parse_slash_command("/retriage"), SlashCommand(kind="retriage"))

    def test_retriage_case_insensitive(self) -> None:
        self.assertEqual(parse_slash_command("  /RETRIAGE  "), SlashCommand(kind="retriage"))

    def test_retriage_with_trailing_text_is_captured_as_body(self) -> None:
        self.assertEqual(
            parse_slash_command("/retriage repro is intermittent"),
            SlashCommand(kind="retriage", body="repro is intermittent"),
        )

    def test_reopen_exact(self) -> None:
        self.assertEqual(parse_slash_command("/reopen"), SlashCommand(kind="reopen"))

    def test_reopen_case_insensitive(self) -> None:
        self.assertEqual(parse_slash_command("  /REOPEN  "), SlashCommand(kind="reopen"))

    def test_reopen_with_trailing_text_is_captured_as_body(self) -> None:
        self.assertEqual(
            parse_slash_command("/reopen the delete button still shows twice"),
            SlashCommand(kind="reopen", body="the delete button still shows twice"),
        )

    def test_assign_with_target(self) -> None:
        self.assertEqual(
            parse_slash_command("/assign @Naohn"),
            SlashCommand(kind="assign", target="@Naohn"),
        )

    def test_bare_assign_is_still_a_command_with_empty_target(self) -> None:
        # A recognized command prefix must not silently fall through to intake;
        # the handler replies with usage instead.
        self.assertEqual(
            parse_slash_command("/assign"),
            SlashCommand(kind="assign", target=""),
        )
        self.assertEqual(
            parse_slash_command("/assign   "),
            SlashCommand(kind="assign", target=""),
        )

    def test_non_command(self) -> None:
        self.assertIsNone(parse_slash_command("hello world"))
        self.assertIsNone(parse_slash_command(""))
        self.assertIsNone(parse_slash_command(None))


class ResolveAssigneeLoginTest(unittest.TestCase):
    def test_prefers_mention_open_id(self) -> None:
        config = _config({"naohn42": "ou_naohn"})
        login = resolve_assignee_login(
            target="@_user_1",
            mention_open_ids=("ou_naohn",),
            config=config,
        )
        self.assertEqual(login, "naohn42")

    def test_typed_login_case_insensitive(self) -> None:
        config = _config({"Naohn42": "ou_naohn"})
        login = resolve_assignee_login(
            target="@naohn42", mention_open_ids=(), config=config
        )
        self.assertEqual(login, "Naohn42")

    def test_unknown_target_returns_none(self) -> None:
        config = _config({"naohn42": "ou_naohn"})
        self.assertIsNone(
            resolve_assignee_login(target="@ghost", mention_open_ids=(), config=config)
        )

    def test_empty_roster_returns_none(self) -> None:
        config = _config(None)
        self.assertIsNone(
            resolve_assignee_login(target="anyone", mention_open_ids=(), config=config)
        )


class MakeDispatchTest(unittest.TestCase):
    def test_formats_issue_number(self) -> None:
        dispatch = make_dispatch(["python3", "-c", "import sys; sys.exit(0)", "{issue_number}"])
        dispatch(7)  # should not raise

    def test_raises_on_nonzero_exit(self) -> None:
        dispatch = make_dispatch(["python3", "-c", "import sys; sys.exit(3)"])
        with self.assertRaises(RuntimeError):
            dispatch(1)

    def test_retries_transient_dispatch_failure(self) -> None:
        calls: list[list[str]] = []

        def fake_run(command: list[str], **_: object) -> CompletedProcess[str]:
            calls.append(command)
            if len(calls) == 1:
                return CompletedProcess(command, 1, "", "Post https://api.github.com/graphql: EOF")
            return CompletedProcess(command, 0, "", "")

        with patch("bugpatrol.slash_commands.subprocess.run", side_effect=fake_run), \
            patch("bugpatrol.slash_commands.time.sleep") as sleep:
            dispatch = make_dispatch(
                ["gh", "workflow", "run", "bugpatrol-triage.yml", "-f", "issue_number={issue_number}"],
                retry_delay_seconds=0,
            )
            dispatch(42)

        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][-1], "issue_number=42")
        sleep.assert_called_once()

    def test_raises_with_last_stderr_after_retries(self) -> None:
        command = ["gh", "workflow", "run", "bugpatrol-triage.yml"]

        def fake_run(args: list[str], **_: object) -> CompletedProcess[str]:
            return CompletedProcess(args, 1, "", "unable to determine default branch: EOF")

        with patch("bugpatrol.slash_commands.subprocess.run", side_effect=fake_run), \
            patch("bugpatrol.slash_commands.time.sleep"):
            dispatch = make_dispatch(command, attempts=2, retry_delay_seconds=0)
            with self.assertRaisesRegex(RuntimeError, "unable to determine default branch: EOF"):
                dispatch(42)

    def test_empty_template_rejected(self) -> None:
        with self.assertRaises(ValueError):
            make_dispatch([])

    def test_attempt_count_must_be_positive(self) -> None:
        with self.assertRaises(ValueError):
            make_dispatch(["true"], attempts=0)


class SlashCommandHandlerTest(unittest.TestCase):
    def test_non_command_returns_none(self) -> None:
        github = _StubIssueClient(_issue())
        lark = _StubReplyClient()
        handler = SlashCommandHandler(config=_config(), github=github, lark=lark)
        self.assertIsNone(handler.handle(_message(text="just a normal report")))
        self.assertEqual(lark.replies, [])

    def test_fix_dispatches_and_replies(self) -> None:
        calls: list[int] = []
        github = _StubIssueClient(_issue(42))
        lark = _StubReplyClient()
        handler = SlashCommandHandler(
            config=_config(), github=github, lark=lark, fix_dispatch=calls.append
        )
        result = handler.handle(_message(text="/fix"))
        assert result is not None
        self.assertEqual(result.reason, "slash_fix")
        self.assertEqual(result.issue_number, 42)
        self.assertEqual(calls, [42])
        self.assertEqual(len(lark.replies), 1)
        self.assertIn("已触发修复", lark.replies[0])

    def test_fix_unconfigured_reports(self) -> None:
        github = _StubIssueClient(_issue(42))
        lark = _StubReplyClient()
        handler = SlashCommandHandler(
            config=_config(), github=github, lark=lark, fix_dispatch=None
        )
        result = handler.handle(_message(text="/fix"))
        assert result is not None
        self.assertEqual(result.reason, "slash_fix_unconfigured")
        self.assertIn("未配置", lark.replies[0])

    def test_retriage_dispatches_and_replies(self) -> None:
        calls: list[int] = []
        github = _StubIssueClient(_issue(42))
        lark = _StubReplyClient()
        handler = SlashCommandHandler(
            config=_config(), github=github, lark=lark, retriage_dispatch=calls.append
        )
        result = handler.handle(_message(text="/retriage"))
        assert result is not None
        self.assertEqual(result.reason, "slash_retriage")
        self.assertEqual(result.issue_number, 42)
        self.assertEqual(calls, [42])
        self.assertEqual(len(lark.replies), 1)
        self.assertIn("重新触发分诊", lark.replies[0])

    def test_retriage_unconfigured_reports(self) -> None:
        github = _StubIssueClient(_issue(42))
        lark = _StubReplyClient()
        handler = SlashCommandHandler(
            config=_config(), github=github, lark=lark, retriage_dispatch=None
        )
        result = handler.handle(_message(text="/retriage"))
        assert result is not None
        self.assertEqual(result.reason, "slash_retriage_unconfigured")
        self.assertIn("未配置", lark.replies[0])

    def test_fix_on_closed_issue_does_not_dispatch(self) -> None:
        calls: list[int] = []
        github = _StubIssueClient(_issue(42, state="closed"))
        lark = _StubReplyClient()
        handler = SlashCommandHandler(
            config=_config(), github=github, lark=lark, fix_dispatch=calls.append
        )
        result = handler.handle(_message(text="/fix"))
        assert result is not None
        self.assertEqual(result.reason, "slash_fix_closed")
        self.assertEqual(calls, [])
        self.assertIn("已关闭", lark.replies[0])
        self.assertIn("reopen", lark.replies[0])

    def test_retriage_on_closed_issue_does_not_dispatch(self) -> None:
        calls: list[int] = []
        github = _StubIssueClient(_issue(42, state="closed"))
        lark = _StubReplyClient()
        handler = SlashCommandHandler(
            config=_config(), github=github, lark=lark, retriage_dispatch=calls.append
        )
        result = handler.handle(_message(text="/retriage"))
        assert result is not None
        self.assertEqual(result.reason, "slash_retriage_closed")
        self.assertEqual(calls, [])
        self.assertIn("已关闭", lark.replies[0])
        self.assertIn("reopen", lark.replies[0])

    def test_reopen_closed_issue_reopens_and_dispatches(self) -> None:
        calls: list[int] = []
        github = _StubIssueClient(_issue(42, state="closed"))
        lark = _StubReplyClient()
        handler = SlashCommandHandler(
            config=_config(), github=github, lark=lark, retriage_dispatch=calls.append
        )
        result = handler.handle(_message(text="/reopen"))
        assert result is not None
        self.assertEqual(result.reason, "slash_reopen")
        self.assertEqual(result.issue_number, 42)
        self.assertEqual(github.reopened, [42])
        self.assertEqual(calls, [42])
        self.assertIn("已重新打开并触发分诊", lark.replies[0])

    def test_reopen_open_issue_only_dispatches_triage(self) -> None:
        calls: list[int] = []
        github = _StubIssueClient(_issue(42, state="open"))
        lark = _StubReplyClient()
        handler = SlashCommandHandler(
            config=_config(), github=github, lark=lark, retriage_dispatch=calls.append
        )
        result = handler.handle(_message(text="/reopen"))
        assert result is not None
        self.assertEqual(result.reason, "slash_reopen_open")
        self.assertEqual(github.reopened, [])
        self.assertEqual(calls, [42])
        self.assertIn("已是打开状态", lark.replies[0])

    def test_reopen_unconfigured_reports(self) -> None:
        github = _StubIssueClient(_issue(42, state="closed"))
        lark = _StubReplyClient()
        handler = SlashCommandHandler(
            config=_config(), github=github, lark=lark, retriage_dispatch=None
        )
        result = handler.handle(_message(text="/reopen"))
        assert result is not None
        self.assertEqual(result.reason, "slash_reopen_unconfigured")
        self.assertEqual(github.reopened, [])
        self.assertIn("未配置", lark.replies[0])

    def test_reopen_with_body_records_reporter_input(self) -> None:
        calls: list[int] = []
        github = _StubIssueClient(_issue(42, state="closed"))
        lark = _StubReplyClient()
        handler = SlashCommandHandler(
            config=_config(), github=github, lark=lark, retriage_dispatch=calls.append
        )
        result = handler.handle(
            _message(text="/reopen 每次左滑会把上一次的 Cancel 掉")
        )
        assert result is not None
        self.assertEqual(result.reason, "slash_reopen")
        self.assertEqual(github.reopened, [42])
        self.assertEqual(calls, [42])
        self.assertEqual(len(github.comments), 1)
        issue_number, body = github.comments[0]
        self.assertEqual(issue_number, 42)
        self.assertIn("每次左滑会把上一次的 Cancel 掉", body)

    def test_fix_with_body_records_reporter_input(self) -> None:
        github = _StubIssueClient(_issue(42))
        lark = _StubReplyClient()
        handler = SlashCommandHandler(
            config=_config(), github=github, lark=lark, fix_dispatch=lambda n: None
        )
        result = handler.handle(_message(text="/fix 保持删除按钮唯一"))
        assert result is not None
        self.assertEqual(result.reason, "slash_fix")
        self.assertEqual(len(github.comments), 1)
        self.assertIn("保持删除按钮唯一", github.comments[0][1])

    def test_retriage_with_body_records_reporter_input(self) -> None:
        github = _StubIssueClient(_issue(42))
        lark = _StubReplyClient()
        handler = SlashCommandHandler(
            config=_config(), github=github, lark=lark, retriage_dispatch=lambda n: None
        )
        result = handler.handle(_message(text="/retriage 滚动也应该取消左滑"))
        assert result is not None
        self.assertEqual(result.reason, "slash_retriage")
        self.assertEqual(len(github.comments), 1)
        self.assertIn("滚动也应该取消左滑", github.comments[0][1])

    def test_bare_command_records_no_reporter_input(self) -> None:
        github = _StubIssueClient(_issue(42))
        lark = _StubReplyClient()
        handler = SlashCommandHandler(
            config=_config(), github=github, lark=lark, fix_dispatch=lambda n: None
        )
        handler.handle(_message(text="/fix"))
        self.assertEqual(github.comments, [])

    def test_assign_on_closed_issue_does_not_assign(self) -> None:
        github = _StubIssueClient(_issue(42, state="closed"))
        lark = _StubReplyClient()
        handler = SlashCommandHandler(
            config=_config({"naohn42": "ou_naohn"}), github=github, lark=lark
        )
        message = _message(text="/assign @_user_1", mention_open_ids=("ou_naohn",))
        result = handler.handle(message)
        assert result is not None
        self.assertEqual(result.reason, "slash_assign_closed")
        self.assertEqual(github.assigned, [])
        self.assertIn("已关闭", lark.replies[0])
        self.assertIn("reopen", lark.replies[0])

    def test_no_issue_reports(self) -> None:
        github = _StubIssueClient(None)
        lark = _StubReplyClient()
        handler = SlashCommandHandler(
            config=_config(), github=github, lark=lark, fix_dispatch=lambda n: None
        )
        result = handler.handle(_message(text="/fix"))
        assert result is not None
        self.assertEqual(result.reason, "slash_no_issue")
        self.assertIn("还没有对应的 issue", lark.replies[0])

    def test_assign_success(self) -> None:
        github = _StubIssueClient(_issue(42))
        lark = _StubReplyClient()
        handler = SlashCommandHandler(
            config=_config({"naohn42": "ou_naohn"}), github=github, lark=lark
        )
        message = _message(text="/assign @_user_1", mention_open_ids=("ou_naohn",))
        result = handler.handle(message)
        assert result is not None
        self.assertEqual(result.reason, "slash_assign")
        self.assertEqual(github.assigned, [(42, "naohn42")])
        self.assertIn("已指派", lark.replies[0])
        # Real Lark @mention (pings the person), never plain-text `@login`.
        self.assertIn('<at user_id="ou_naohn">naohn42</at>', lark.replies[0])
        self.assertNotIn("@naohn42", lark.replies[0])

    def test_assign_unknown_reports_choices(self) -> None:
        github = _StubIssueClient(_issue(42))
        lark = _StubReplyClient()
        handler = SlashCommandHandler(
            config=_config({"naohn42": "ou_naohn"}), github=github, lark=lark
        )
        result = handler.handle(_message(text="/assign @ghost"))
        assert result is not None
        self.assertEqual(result.reason, "slash_assign_unknown")
        self.assertEqual(github.assigned, [])
        self.assertIn("naohn42", lark.replies[0])

    def test_bare_assign_reports_usage(self) -> None:
        github = _StubIssueClient(_issue(42))
        lark = _StubReplyClient()
        handler = SlashCommandHandler(
            config=_config({"naohn42": "ou_naohn"}), github=github, lark=lark
        )
        result = handler.handle(_message(text="/assign"))
        assert result is not None
        self.assertEqual(result.reason, "slash_assign_usage")
        self.assertEqual(github.assigned, [])
        self.assertIn("用法", lark.replies[0])

    def test_execution_error_is_surfaced_not_raised(self) -> None:
        def boom(_: int) -> None:
            raise RuntimeError("dispatch exploded")

        github = _StubIssueClient(_issue(42))
        lark = _StubReplyClient()
        handler = SlashCommandHandler(
            config=_config(), github=github, lark=lark, fix_dispatch=boom
        )
        result = handler.handle(_message(text="/fix"))
        assert result is not None
        self.assertEqual(result.reason, "slash_error")
        self.assertIn("失败", lark.replies[0])


class MentionOpenIdsParseTest(unittest.TestCase):
    def test_parse_lark_message_extracts_mention_open_ids(self) -> None:
        item = {
            "message_id": "om_1",
            "chat_id": "oc_chat",
            "root_id": "om_root",
            "msg_type": "text",
            "create_time": "1720000000000",
            "sender": {"id": {"open_id": "ou_user"}, "sender_type": "user", "id_type": "open_id"},
            "body": {"content": '{"text": "/assign @_user_1"}'},
            "mentions": [
                {"key": "@_user_1", "name": "Naohn", "id": {"open_id": "ou_naohn"}},
            ],
        }
        message = parse_lark_message(item, default_chat_id="oc_chat")
        self.assertEqual(message.mention_open_ids, ("ou_naohn",))


if __name__ == "__main__":
    unittest.main()
