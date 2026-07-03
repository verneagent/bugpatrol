"""Environment checks for a bugpatrol project."""

from __future__ import annotations

import shutil
from dataclasses import dataclass

from bugpatrol.config import ProjectConfig
from bugpatrol.fields import NATIVE_ISSUE_TYPES, default_field_specs
from bugpatrol.github import GitHubCliIssuesClient
from bugpatrol.github_fields import GitHubIssueFieldsClient
from bugpatrol.lark import LarkOpenApiMessengerClient


@dataclass(frozen=True)
class DoctorCheck:
    name: str
    ok: bool
    detail: str


def run_doctor(
    *,
    config: ProjectConfig,
    github: GitHubCliIssuesClient,
    issue_fields: GitHubIssueFieldsClient,
    lark: LarkOpenApiMessengerClient | None = None,
) -> tuple[DoctorCheck, ...]:
    checks: list[DoctorCheck] = []
    checks.append(_check("config", lambda: _check_config(config)))
    checks.append(_check("github_repo", lambda: _check_repo(config, github)))
    checks.append(_check("issue_types", lambda: _check_issue_types(config, github)))
    checks.append(_check("issue_fields", lambda: _check_issue_fields(config, issue_fields)))
    checks.append(_check("asset_repo", lambda: _check_asset_repo(config, github)))
    checks.append(_check("media_vision_command", lambda: _check_media_command(config)))
    checks.append(_check("ffmpeg", lambda: _check_ffmpeg(config)))
    checks.append(_check("triage_agent", lambda: _check_triage_agent(config)))
    if lark is not None:
        checks.append(_check("lark_history", lambda: _check_lark(config, lark)))
    return tuple(checks)


def _check(name: str, fn: object) -> DoctorCheck:
    try:
        detail = fn()  # type: ignore[operator]
    except Exception as exc:  # noqa: BLE001
        return DoctorCheck(name=name, ok=False, detail=str(exc))
    return DoctorCheck(name=name, ok=True, detail=detail)


def _check_config(config: ProjectConfig) -> str:
    config.validate_against(default_field_specs())
    return config.github_repo


def _check_repo(config: ProjectConfig, github: GitHubCliIssuesClient) -> str:
    repo = github.get_repository(repo=config.github_repo)
    return f"{repo.get('full_name')} private={repo.get('private')}"


def _check_issue_types(config: ProjectConfig, github: GitHubCliIssuesClient) -> str:
    names = github.list_issue_types(repo=config.github_repo)
    missing = sorted(set(NATIVE_ISSUE_TYPES) - set(names))
    if missing:
        raise ValueError(f"missing issue types: {missing}")
    return ", ".join(names)


def _check_issue_fields(config: ProjectConfig, issue_fields: GitHubIssueFieldsClient) -> str:
    owner = config.github_repo.split("/", 1)[0]
    live = issue_fields.list_org_fields(org=owner)
    live_options = {name: field.options for name, field in live.items()}
    config.validate_github_field_options(live_options, default_field_specs())
    return f"{len(live)} org fields"


def _check_asset_repo(config: ProjectConfig, github: GitHubCliIssuesClient) -> str:
    if not config.assets.github_repo:
        return "not configured"
    repo = github.get_repository(repo=config.assets.github_repo)
    return f"{repo.get('full_name')} private={repo.get('private')}"


def _check_media_command(config: ProjectConfig) -> str:
    if not config.media.description_command:
        return "not configured"
    executable = config.media.description_command[0]
    if shutil.which(executable) is None:
        raise ValueError(f"media command not found: {executable}")
    return executable


def _check_ffmpeg(config: ProjectConfig) -> str:
    if config.media.max_video_bytes <= 0:
        return "not required"
    if shutil.which("ffmpeg") is None:
        raise ValueError("ffmpeg not found")
    return "ffmpeg"


def _check_triage_agent(config: ProjectConfig) -> str:
    if config.triage_agent.provider not in {"codex", "claude"}:
        raise ValueError(f"unsupported provider: {config.triage_agent.provider}")
    if not config.triage_agent.runner_labels:
        raise ValueError("missing runner labels")
    return config.triage_agent.provider


def _check_lark(config: ProjectConfig, lark: LarkOpenApiMessengerClient) -> str:
    messages = lark.list_chat_messages(chat_id=config.lark.chat_id, limit=1)
    return f"read {len(messages)} recent messages"
