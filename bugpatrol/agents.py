"""Triage agent invocation helpers for trusted self-hosted runners."""

from __future__ import annotations

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
    return AgentInvocation(provider=provider, command=command, env=env)


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
    command = ["claude", "-p", prompt, "--permission-mode", "acceptEdits"]
    model = config.triage_agent.model or default_model
    if model:
        command.extend(["--model", model])
    return command
