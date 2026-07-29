"""Discover branch-scoped topic chats by convention instead of config.

A project's CI creates one Lark topic group per feature branch, named exactly
after the branch (see fived `.github/workflows/bugpatrol-feature-topic.yml`).
Listing those groups in `[[lark.branch_chats]]` meant every new branch needed a
config edit plus a watcher restart before its group was scanned at all -- until
then bugs reported there were silently dropped.

The group name IS the branch, so the mapping can be derived: take the chats the
bot belongs to, keep those whose name matches a live remote branch, and treat
that branch as the chat's target branch. Config entries stay supported as
overrides for groups that predate (or don't follow) the convention.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from typing import Callable, Protocol

from bugpatrol.config import ProjectConfig


class ChatLister(Protocol):
    def list_bot_chats(self) -> tuple[tuple[str, str], ...]:
        """Return (chat_id, name) for every chat the bot belongs to."""


class BranchLister(Protocol):
    def list_remote_branch_names(self, *, repo: str) -> tuple[str, ...]:
        """Return every branch name on the remote repo."""


@dataclass(frozen=True)
class BranchChatDiscovery:
    """Chat id -> branch, plus the names that were considered but unmatched."""

    branch_chats: dict[str, str]
    unmatched_chats: tuple[str, ...]


def discover_branch_chats(
    *,
    lark: ChatLister,
    github: BranchLister,
    config: ProjectConfig,
) -> BranchChatDiscovery:
    """Map bot chats named after a live remote branch to that branch.

    The main watcher chat is never remapped, and "main" is not a matchable
    branch: it is the value `branch_for_chat` already returns for every
    unmapped chat, so a group named after it would be the main chat under
    another name rather than a branch topic.
    """
    branches = set(github.list_remote_branch_names(repo=config.github_repo))
    branches.discard("main")
    matched: dict[str, str] = {}
    unmatched: list[str] = []
    for chat_id, name in lark.list_bot_chats():
        if chat_id == config.lark.chat_id:
            continue
        branch = name.strip()
        if branch and branch in branches:
            matched[chat_id] = branch
        else:
            unmatched.append(name)
    return BranchChatDiscovery(branch_chats=matched, unmatched_chats=tuple(unmatched))


def apply_branch_chats(config: ProjectConfig, branch_chats: dict[str, str]) -> ProjectConfig:
    """Return `config` with discovered chats merged in; config entries win.

    An explicit `[[lark.branch_chats]]` entry is a deliberate override (e.g.
    the `2026/chat-live` group, whose name does not match its branch), so it
    takes precedence over whatever discovery inferred for the same chat.
    """
    merged = {**branch_chats, **(config.lark.branch_chats or {})}
    return replace(config, lark=replace(config.lark, branch_chats=merged or None))


class CachedBranchChatDiscoverer:
    """Re-runs discovery at most once per `ttl_seconds`.

    The watcher polls every ~30s but branches and groups change on a human
    timescale, so refreshing on a slower clock keeps the poll loop cheap while
    still picking up a new feature branch without a restart.
    """

    def __init__(
        self,
        *,
        lark: ChatLister,
        github: BranchLister,
        config: ProjectConfig,
        ttl_seconds: float = 300,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._lark = lark
        self._github = github
        self._config = config
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._cached: BranchChatDiscovery | None = None
        self._fetched_at = 0.0

    def resolve(self) -> BranchChatDiscovery:
        now = self._clock()
        if self._cached is not None and now - self._fetched_at < self._ttl_seconds:
            return self._cached
        discovery = discover_branch_chats(
            lark=self._lark,
            github=self._github,
            config=self._config,
        )
        self._cached = discovery
        self._fetched_at = now
        return discovery
