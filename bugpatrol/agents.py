"""Triage agent invocation helpers for trusted self-hosted runners."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

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


def parse_claude_token_usage(stdout: str) -> tuple[int, int]:
    """Best-effort (input, output) token counts from `claude -p --output-format json`.

    Returns (0, 0) when the stdout can't be parsed (e.g. codex, or an older
    CLI). Cache tokens are folded into the input count.
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
        return (0, 0)
    usage = data.get("usage")
    if not isinstance(usage, dict):
        return (0, 0)
    input_tokens = 0
    for key in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
        value = usage.get(key)
        if isinstance(value, int):
            input_tokens += value
    output = usage.get("output_tokens")
    return (input_tokens, output if isinstance(output, int) else 0)


def build_triage_agent_invocation(
    config: ProjectConfig,
    *,
    issue_number: int,
    prompt_path: Path,
    schema_path: Path,
    output_path: Path,
    context_path: Path | None = None,
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
    # Non-interactive `claude -p` auto-denies file writes by default, which
    # would block writing the output JSON. Runs happen on trusted runners.
    # stream-json (+ --verbose, which it requires under -p) emits the full
    # turn-by-turn conversation so the runner can persist a turn log for
    # debugging and parse token usage from the final result event.
    command = [
        "claude",
        "-p",
        prompt,
        "--permission-mode",
        "acceptEdits",
        "--output-format",
        "stream-json",
        "--verbose",
    ]
    model = config.triage_agent.model or default_model
    if model:
        command.extend(["--model", model])
    return command
