"""Deterministic Lark slash commands (`/fix`, `/assign`).

These bypass the LLM triage path entirely: a reply in an existing bug topic
that starts with a known slash command is executed literally. `/fix` dispatches
the fix workflow for the topic's issue; `/assign <who>` sets the GitHub issue
assignee. Anything that is not an exact slash command is left untouched so it
flows into normal intake/triage.
"""

from __future__ import annotations

import shlex
import subprocess
import sys
from dataclasses import dataclass
from typing import Callable, Protocol, Sequence

from bugpatrol.clients import GitHubIssue
from bugpatrol.config import ProjectConfig
from bugpatrol.lark import LarkMessage, is_message_withdrawn_error

FIX_COMMAND = "/fix"
ASSIGN_COMMAND = "/assign"


@dataclass(frozen=True)
class SlashCommand:
    kind: str  # "fix" | "assign"
    target: str = ""  # raw assignee text for /assign

    def render(self) -> str:
        if self.kind == "assign":
            return f"{ASSIGN_COMMAND} {self.target}".strip()
        return FIX_COMMAND


@dataclass(frozen=True)
class SlashResult:
    command: str
    issue_number: int | None
    reason: str


def parse_slash_command(text: str | None) -> SlashCommand | None:
    """Parse an exact slash command; return None for anything else.

    Deterministic on purpose — no natural-language matching. The command must be
    the FIRST whitespace-delimited token. `/fix` takes no arguments; `/assign`
    requires an assignee argument. A message that merely mentions `/fix` inside a
    sentence does not trigger.
    """
    if not text:
        return None
    stripped = text.strip()
    if not stripped:
        return None
    tokens = stripped.split()
    head = tokens[0].lower()
    if head == FIX_COMMAND:
        if len(tokens) != 1:
            return None
        return SlashCommand(kind="fix")
    if head == ASSIGN_COMMAND:
        target = stripped[len(tokens[0]):].strip()
        if not target:
            return None
        return SlashCommand(kind="assign", target=target)
    return None


def resolve_assignee_login(
    *,
    target: str,
    mention_open_ids: Sequence[str],
    config: ProjectConfig,
) -> str | None:
    """Resolve a `/assign` argument to a known GitHub login, or None.

    A real Lark @mention carries the person's open_id, so prefer reverse-mapping
    that against `[lark.user_open_ids]` (login -> open_id). Otherwise fall back to
    a typed token, matching the known logins case-insensitively (leading `@`
    stripped). No LLM, no fuzzy matching: an unknown target returns None so the
    caller can surface the valid choices.
    """
    logins = config.lark.user_open_ids or {}
    by_open_id = {open_id: login for login, open_id in logins.items()}
    for open_id in mention_open_ids:
        login = by_open_id.get(open_id)
        if login:
            return login
    token = target.strip().lstrip("@").strip()
    if not token:
        return None
    by_lower = {login.lower(): login for login in logins}
    return by_lower.get(token.lower())


class _IssueLookupClient(Protocol):
    def find_issue_by_intake_root(
        self, *, repo: str, chat_id: str, root_id: str
    ) -> GitHubIssue | None: ...

    def add_assignee(self, *, repo: str, issue_number: int, assignee: str) -> None: ...


class _ReplyClient(Protocol):
    def reply_to_message(self, *, chat_id: str, message_id: str, text: str) -> None: ...


def make_fix_dispatch(command_template: str | Sequence[str]) -> Callable[[int], None]:
    """Build a callable that runs the fix workflow for an issue number.

    Mirrors CommandTriageDispatcher: the template supports `{issue_number}` and
    is run via subprocess (e.g. `gh workflow run bugpatrol-fix.yml ... -f
    issue_number={issue_number}`).
    """
    if isinstance(command_template, str):
        template = tuple(shlex.split(command_template))
    elif len(command_template) == 1 and isinstance(command_template[0], str):
        template = tuple(shlex.split(command_template[0]))
    else:
        template = tuple(command_template)
    if not template:
        raise ValueError("fix dispatch command must not be empty")

    def dispatch(issue_number: int) -> None:
        command = [part.format(issue_number=issue_number) for part in template]
        completed = subprocess.run(command, check=False)
        if completed.returncode != 0:
            raise RuntimeError(f"fix dispatch command failed with exit {completed.returncode}")

    return dispatch


class SlashCommandHandler:
    """Execute deterministic slash commands found in Lark topic replies."""

    def __init__(
        self,
        *,
        config: ProjectConfig,
        github: _IssueLookupClient,
        lark: _ReplyClient,
        fix_dispatch: Callable[[int], None] | None = None,
    ) -> None:
        self._config = config
        self._github = github
        self._lark = lark
        self._fix_dispatch = fix_dispatch

    def handle(self, message: LarkMessage) -> SlashResult | None:
        """Handle one message. Return None if it is not a slash command.

        A returned result means the message was consumed (mark it processed);
        it never re-enters normal intake/triage. Execution errors are reported
        into the Lark thread rather than raised, so one bad command cannot
        crash-loop the whole topic scan.
        """
        command = parse_slash_command(message.text)
        if command is None:
            return None
        try:
            return self._execute(message, command)
        except Exception as error:  # noqa: BLE001 - surface, do not crash the scan
            print(
                f"slash-command {command.render()} failed for {message.message_id}: {error}",
                file=sys.stderr,
            )
            self._reply(message, f"⚠️ 执行 `{command.render()}` 失败：{error}")
            return SlashResult(command=command.kind, issue_number=None, reason="slash_error")

    def _execute(self, message: LarkMessage, command: SlashCommand) -> SlashResult:
        issue = self._github.find_issue_by_intake_root(
            repo=self._config.github_repo,
            chat_id=message.chat_id,
            root_id=message.root_id,
        )
        if issue is None:
            self._reply(message, f"⚠️ 本话题还没有对应的 issue，无法执行 `{command.render()}`")
            return SlashResult(command=command.kind, issue_number=None, reason="slash_no_issue")
        if command.kind == "fix":
            return self._handle_fix(message, issue)
        return self._handle_assign(message, issue, command)

    def _handle_fix(self, message: LarkMessage, issue: GitHubIssue) -> SlashResult:
        if self._fix_dispatch is None:
            self._reply(message, "⚠️ 未配置修复触发命令，无法从 Lark 启动修复")
            return SlashResult(command="fix", issue_number=issue.number, reason="slash_fix_unconfigured")
        self._fix_dispatch(issue.number)
        self._reply(
            message,
            f"🛠️ 已触发修复 [#{issue.number}]({issue.url})（若已有进行中的修复会自动跳过）",
        )
        return SlashResult(command="fix", issue_number=issue.number, reason="slash_fix")

    def _handle_assign(
        self, message: LarkMessage, issue: GitHubIssue, command: SlashCommand
    ) -> SlashResult:
        login = resolve_assignee_login(
            target=command.target,
            mention_open_ids=message.mention_open_ids,
            config=self._config,
        )
        if login is None:
            known = ", ".join(sorted((self._config.lark.user_open_ids or {}).keys())) or "（未配置）"
            self._reply(message, f"⚠️ 无法识别负责人「{command.target}」。可指派：{known}")
            return SlashResult(command="assign", issue_number=issue.number, reason="slash_assign_unknown")
        self._github.add_assignee(
            repo=self._config.github_repo,
            issue_number=issue.number,
            assignee=login,
        )
        # A real Lark @mention (`<at user_id=...>`) actually pings the assignee;
        # plain `@login` text does not. `login` always comes from
        # `user_open_ids`, so the open_id is present.
        open_id = (self._config.lark.user_open_ids or {})[login]
        mention = f'<at user_id="{open_id}">{login}</at>'
        self._reply(message, f"✅ 已指派 [#{issue.number}]({issue.url}) 给 {mention}")
        return SlashResult(command="assign", issue_number=issue.number, reason="slash_assign")

    def _reply(self, message: LarkMessage, text: str) -> None:
        try:
            self._lark.reply_to_message(
                chat_id=message.chat_id,
                message_id=message.message_id,
                text=text,
            )
        except Exception as error:  # noqa: BLE001 - a recalled source message must not abort
            if not is_message_withdrawn_error(error):
                raise
