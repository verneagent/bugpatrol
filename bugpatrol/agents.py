"""Triage agent invocation helpers for trusted self-hosted runners."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from bugpatrol.config import ProjectConfig

DEEPSEEK_ANTHROPIC_BASE_URL = "https://api.deepseek.com/anthropic"
DEEPSEEK_DEFAULT_MODEL = "deepseek-v4-pro[1m]"


@dataclass(frozen=True)
class AgentInvocation:
    provider: str
    command: list[str]
    # Extra environment for the agent process (e.g. third-party endpoint
    # overrides). Merged over os.environ at execution time; never printed.
    env: dict[str, str] = field(default_factory=dict)
    # Model name actually used, for run-completion reporting.
    model: str = ""


def parse_claude_token_usage(stdout: str) -> tuple[int, int, int]:
    """Best-effort (input, cached_input, output) token counts from `claude -p`.

    ``input`` is the freshly-processed prompt tokens; ``cached_input`` is the
    cache-creation + cache-read tokens (cheap, reported separately so the real
    cost is legible). Returns (0, 0, 0) when the stdout can't be parsed (e.g.
    codex, or an older CLI).
    """
    data: object = None
    text = stdout.strip()
    if text:
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            for line in reversed(text.splitlines()):
                candidate = line.strip()
                if candidate.startswith("{") and candidate.endswith("}"):
                    try:
                        data = json.loads(candidate)
                        break
                    except json.JSONDecodeError:
                        continue
    if not isinstance(data, dict):
        return (0, 0, 0)
    usage = data.get("usage")
    if not isinstance(usage, dict):
        return (0, 0, 0)

    def _int(key: str) -> int:
        value = usage.get(key)
        return value if isinstance(value, int) else 0

    input_tokens = _int("input_tokens")
    cached_input = _int("cache_creation_input_tokens") + _int("cache_read_input_tokens")
    return (input_tokens, cached_input, _int("output_tokens"))


# Stable substrings Claude Code emits in a tool_result when it refuses a Bash
# command or a file read that falls outside the sandbox. A denied run silently
# yields a degraded triage (no CODEOWNERS/repo/git/dup-search) while still
# exiting 0, so the runner treats any of these as a hard failure — this catches
# regressions the invocation-flag unit test can't (CLI renames a flag, a wrong
# --add-dir path, a missing cwd), independent of whether the model self-reports.
_SANDBOX_DENIAL_MARKERS = (
    "requires approval",
    "allowed working directories",
)


def _tool_result_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        return "\n".join(parts)
    return ""


def detect_sandbox_denial(stdout: str) -> str | None:
    """Return the first sandbox/permission-denial message in a stream-json turn
    log, or None. Only ``tool_result`` blocks flagged ``is_error`` are inspected,
    so ordinary error output (and issue text that merely quotes these phrases)
    never trips the guard.
    """
    for raw in stdout.splitlines():
        line = raw.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        message = event.get("message") if isinstance(event, dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            if not block.get("is_error"):
                continue
            text = _tool_result_text(block.get("content"))
            if any(marker in text for marker in _SANDBOX_DENIAL_MARKERS):
                return text.strip()
    return None


def build_triage_agent_invocation(
    config: ProjectConfig,
    *,
    issue_number: int,
    prompt_path: Path,
    schema_path: Path,
    output_path: Path,
    context_path: Path | None = None,
    workspace_dirs: Sequence[Path] = (),
) -> AgentInvocation:
    provider = config.triage_agent.provider
    env: dict[str, str] = {}
    if provider == "codex":
        command = _build_codex_command(
            config,
            issue_number=issue_number,
            prompt_path=prompt_path,
            schema_path=schema_path,
            output_path=output_path,
            context_path=context_path,
        )
    elif provider == "claude":
        command = _build_claude_command(
            config,
            issue_number=issue_number,
            prompt_path=prompt_path,
            schema_path=schema_path,
            output_path=output_path,
            context_path=context_path,
            workspace_dirs=workspace_dirs,
        )
    elif provider == "deepseek":
        # DeepSeek's Anthropic-compatible endpoint driven through the claude
        # CLI (codex+DeepSeek is broken since codex 0.128 dropped wire_api=chat).
        api_key = os.environ.get("DEEPSEEK_API_KEY", "")
        if not api_key:
            raise ValueError("deepseek provider requires DEEPSEEK_API_KEY in the environment")
        command = _build_claude_command(
            config,
            issue_number=issue_number,
            prompt_path=prompt_path,
            schema_path=schema_path,
            output_path=output_path,
            context_path=context_path,
            workspace_dirs=workspace_dirs,
            default_model=DEEPSEEK_DEFAULT_MODEL,
        )
        env = {
            "ANTHROPIC_BASE_URL": DEEPSEEK_ANTHROPIC_BASE_URL,
            "ANTHROPIC_API_KEY": api_key,
            "ANTHROPIC_AUTH_TOKEN": api_key,
        }
    else:
        raise ValueError(f"unsupported triage agent provider: {provider}")
    model = config.triage_agent.model
    if provider == "deepseek" and not model:
        model = DEEPSEEK_DEFAULT_MODEL
    return AgentInvocation(provider=provider, command=command, env=env, model=model)


def _common_prompt(
    config: ProjectConfig,
    *,
    issue_number: int,
    prompt_path: Path,
    context_path: Path | None,
) -> str:
    lines = [
        f"Use prompt file: {prompt_path}",
        f"Project: {config.project}",
        f"Repository: {config.github_repo}",
        f"Issue: #{issue_number}",
    ]
    if context_path is not None:
        lines.append(f"Use triage context file: {context_path}")
    lines.append("Return only JSON matching the provided schema.")
    return "\n".join(lines)


def _build_codex_command(
    config: ProjectConfig,
    *,
    issue_number: int,
    prompt_path: Path,
    schema_path: Path,
    output_path: Path,
    context_path: Path | None,
) -> list[str]:
    command = [
        "codex",
        "exec",
        "--output-schema",
        str(schema_path),
        "-o",
        str(output_path),
    ]
    if config.triage_agent.model:
        command.extend(["--model", config.triage_agent.model])
    if config.triage_agent.effort:
        command.extend(["--effort", config.triage_agent.effort])
    command.append(
        _common_prompt(
            config,
            issue_number=issue_number,
            prompt_path=prompt_path,
            context_path=context_path,
        )
    )
    return command


def _build_claude_command(
    config: ProjectConfig,
    *,
    issue_number: int,
    prompt_path: Path,
    schema_path: Path,
    output_path: Path,
    context_path: Path | None,
    workspace_dirs: Sequence[Path] = (),
    default_model: str = "",
) -> list[str]:
    prompt = "\n".join(
        [
            _common_prompt(
                config,
                issue_number=issue_number,
                prompt_path=prompt_path,
                context_path=context_path,
            ),
            f"Schema file: {schema_path}",
            f"Write final JSON to: {output_path}",
        ]
    )
    # The agent runs with cwd set to the repo checkout (main or a branch
    # worktree), so CODEOWNERS, source, and git history/blame of the right
    # branch are naturally in scope. `--dangerously-skip-permissions` is
    # required because non-interactive `claude -p` cannot answer approval
    # prompts: without it every Bash call (gh dedup search, git log) and the
    # output-JSON write is auto-denied. Runs happen on trusted runners.
    # `--add-dir` re-admits the runner-side workspace (prompt/context/schema/
    # output) that lives outside the checkout. stream-json (+ --verbose, which
    # it requires under -p) emits the full turn-by-turn conversation so the
    # runner can persist a turn log and parse token usage from the result event.
    command = [
        "claude",
        "-p",
        prompt,
        "--dangerously-skip-permissions",
        "--output-format",
        "stream-json",
        "--verbose",
    ]
    for workspace in workspace_dirs:
        command.extend(["--add-dir", str(workspace)])
    model = config.triage_agent.model or default_model
    if model:
        command.extend(["--model", model])
    return command
