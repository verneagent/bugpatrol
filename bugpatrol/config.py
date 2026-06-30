"""Project configuration for bugpatrol."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tomllib

from bugpatrol.fields import FieldSpec


@dataclass(frozen=True)
class LarkConfig:
    chat_id: str
    app_id: str
    app_secret_env: str
    bot_open_id: str


@dataclass(frozen=True)
class TriageAgentConfig:
    runner_labels: tuple[str, ...]
    provider: str = "codex"
    model: str = ""
    effort: str = ""


@dataclass(frozen=True)
class PrdConfig:
    root_wiki_node: str
    cache_path: str = ""


@dataclass(frozen=True)
class IntakeConfig:
    language: str


@dataclass(frozen=True)
class ProjectConfig:
    github_repo: str
    lark: LarkConfig
    triage_agent: TriageAgentConfig
    prd: PrdConfig
    intake: IntakeConfig
    issue_field_names: dict[str, str]

    @property
    def project(self) -> str:
        return self.github_repo.rsplit("/", 1)[-1]

    def validate_against(self, field_specs: dict[str, FieldSpec]) -> None:
        missing = [name for name in field_specs if name not in self.issue_field_names]
        if missing:
            raise ValueError(f"missing issue field mapping: {', '.join(sorted(missing))}")

    def validate_github_field_options(
        self,
        github_options: dict[str, tuple[str, ...]],
        field_specs: dict[str, FieldSpec],
    ) -> None:
        """Validate live GitHub Issue Field options against bugpatrol semantics.

        `github_options` is keyed by actual GitHub field display name. The
        project config maps bugpatrol's canonical logical field names to those
        display names, so projects can rename fields without changing code.
        """
        self.validate_against(field_specs)
        for logical_name, spec in field_specs.items():
            github_name = self.issue_field_names[logical_name]
            configured = set(github_options.get(github_name, ()))
            expected = set(spec.values)
            if configured != expected:
                extra = sorted(configured - expected)
                absent = sorted(expected - configured)
                parts = []
                if extra:
                    parts.append(f"extra={extra}")
                if absent:
                    parts.append(f"missing={absent}")
                raise ValueError(f"GitHub issue field {github_name!r} values mismatch: {' '.join(parts)}")


def load_project_config(path: Path) -> ProjectConfig:
    data = tomllib.loads(path.read_text())
    return parse_project_config(data)


def parse_project_config(data: dict[str, Any]) -> ProjectConfig:
    lark = _required_table(data, "lark")
    triage_agent = _required_table(data, "triage_agent")
    prd = _required_table(data, "prd")
    intake = _required_table(data, "intake")
    field_names = _required_table(data, "issue_field_names")
    language = _required_str(intake, "language")
    if language not in {"zh-CN", "en-US"}:
        raise ValueError("intake.language must be one of: zh-CN, en-US")

    return ProjectConfig(
        github_repo=_required_str(data, "github_repo"),
        lark=LarkConfig(
            chat_id=_required_str(lark, "chat_id"),
            app_id=_required_str(lark, "app_id"),
            app_secret_env=_required_str(lark, "app_secret_env"),
            bot_open_id=_required_str(lark, "bot_open_id"),
        ),
        triage_agent=TriageAgentConfig(
            runner_labels=tuple(_required_list(triage_agent, "runner_labels")),
            provider=str(triage_agent.get("provider") or "codex"),
            model=str(triage_agent.get("model") or ""),
            effort=str(triage_agent.get("effort") or ""),
        ),
        prd=PrdConfig(
            root_wiki_node=_required_str(prd, "root_wiki_node"),
            cache_path=str(prd.get("cache_path") or ""),
        ),
        intake=IntakeConfig(language=language),
        issue_field_names={
            str(name): str(value)
            for name, value in field_names.items()
            if isinstance(value, str)
        },
    )


def _required_table(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"missing table [{key}]")
    return value


def _required_str(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"missing string field {key!r}")
    return value


def _required_list(data: dict[str, Any], key: str) -> list[str]:
    value = data.get(key)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"missing string list field {key!r}")
    return value
