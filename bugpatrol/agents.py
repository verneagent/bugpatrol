"""Triage agent invocation helpers for trusted self-hosted runners."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from bugpatrol.config import ProjectConfig


@dataclass(frozen=True)
class AgentInvocation:
    provider: str
    command: list[str]


def build_triage_agent_invocation(
    config: ProjectConfig,
    *,
    issue_number: int,
    prompt_path: Path,
    schema_path: Path,
    output_path: Path,
) -> AgentInvocation:
    provider = config.triage_agent.provider
    if provider == "codex":
        command = _build_codex_command(
            config,
            issue_number=issue_number,
            prompt_path=prompt_path,
            schema_path=schema_path,
            output_path=output_path,
        )
    elif provider == "claude":
        command = _build_claude_command(
            config,
            issue_number=issue_number,
            prompt_path=prompt_path,
            schema_path=schema_path,
            output_path=output_path,
        )
    else:
        raise ValueError(f"unsupported triage agent provider: {provider}")
    return AgentInvocation(provider=provider, command=command)


def _common_prompt(config: ProjectConfig, *, issue_number: int, prompt_path: Path) -> str:
    return "\n".join(
        [
            f"Use prompt file: {prompt_path}",
            f"Project: {config.project}",
            f"Repository: {config.github_repo}",
            f"Issue: #{issue_number}",
            "Return only JSON matching the provided schema.",
        ]
    )


def _build_codex_command(
    config: ProjectConfig,
    *,
    issue_number: int,
    prompt_path: Path,
    schema_path: Path,
    output_path: Path,
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
    command.append(_common_prompt(config, issue_number=issue_number, prompt_path=prompt_path))
    return command


def _build_claude_command(
    config: ProjectConfig,
    *,
    issue_number: int,
    prompt_path: Path,
    schema_path: Path,
    output_path: Path,
) -> list[str]:
    prompt = "\n".join(
        [
            _common_prompt(config, issue_number=issue_number, prompt_path=prompt_path),
            f"Schema file: {schema_path}",
            f"Write final JSON to: {output_path}",
        ]
    )
    command = ["claude", "-p", prompt]
    if config.triage_agent.model:
        command.extend(["--model", config.triage_agent.model])
    return command
