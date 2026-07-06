"""Project configuration for bugpatrol."""

from __future__ import annotations

import fnmatch
from datetime import datetime
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
    sender_names: dict[str, str] | None = None
    message_url_template: str = ""


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
    include_globs: tuple[str, ...] = ("**/*.md",)


@dataclass(frozen=True)
class IntakeConfig:
    language: str
    since: str = ""
    skip_orphan_replies: bool = False

    def since_ms(self) -> int:
        if not self.since:
            return 0
        return int(datetime.fromisoformat(self.since).timestamp() * 1000)


@dataclass(frozen=True)
class AssetsConfig:
    github_repo: str = ""
    checkout_path: str = ""
    base_path: str = ".github/issue-assets"
    branch: str = "main"
    remote_url: str = ""


@dataclass(frozen=True)
class BranchesConfig:
    """Branch attribution rules for the target project repo.

    `allowed` is a list of fnmatch patterns (e.g. "main", "feature-*") that
    constrain which branches a bug can be attributed to. `default` is the
    branch assumed when nothing else is known.
    """

    default: str = "main"
    allowed: tuple[str, ...] = ("main",)


@dataclass(frozen=True)
class MediaConfig:
    description_command: tuple[str, ...] = ()
    description_timeout_seconds: int = 300
    description_temp_dir: str = ""
    redaction_command: tuple[str, ...] = ()
    redaction_timeout_seconds: int = 300
    resize_max_image_width: int = 0
    resize_max_image_height: int = 0
    resize_image_quality: int = 85
    max_image_bytes: int = 0
    max_video_bytes: int = 0
    max_file_bytes: int = 0
    max_video_duration_seconds: float = 0.0
    video_probe_command: tuple[str, ...] = ()
    video_probe_timeout_seconds: int = 30
    video_frame_command: tuple[str, ...] = ()
    video_frame_timeout_seconds: int = 300
    video_frame_min_duration_seconds: float = 0.0
    description_retries: int = 0
    description_retry_backoff_seconds: float = 1.0


@dataclass(frozen=True)
class OwnersConfig:
    default: tuple[str, ...] = ()
    paths: dict[str, tuple[str, ...]] | None = None
    capabilities: dict[str, tuple[str, ...]] | None = None


@dataclass(frozen=True)
class FollowupClassifierConfig:
    acknowledgement_texts: tuple[str, ...] = ()
    fix_status_keywords: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProjectConfig:
    github_repo: str
    github_cli: str
    lark: LarkConfig
    triage_agent: TriageAgentConfig
    prd: PrdConfig
    intake: IntakeConfig
    assets: AssetsConfig
    branches: BranchesConfig
    media: MediaConfig
    owners: OwnersConfig
    followup_classifier: FollowupClassifierConfig
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
    assets = data.get("assets") or {}
    if not isinstance(assets, dict):
        raise ValueError("[assets] must be a table")
    branches = data.get("branches") or {}
    if not isinstance(branches, dict):
        raise ValueError("[branches] must be a table")
    media = data.get("media") or {}
    if not isinstance(media, dict):
        raise ValueError("[media] must be a table")
    owners = data.get("owners") or {}
    if not isinstance(owners, dict):
        raise ValueError("[owners] must be a table")
    followup_classifier = data.get("followup_classifier") or {}
    if not isinstance(followup_classifier, dict):
        raise ValueError("[followup_classifier] must be a table")
    field_names = _required_table(data, "issue_field_names")
    language = _required_str(intake, "language")
    if language not in {"zh-CN", "en-US"}:
        raise ValueError("intake.language must be one of: zh-CN, en-US")

    include_globs = prd.get("include_globs") or ("**/*.md",)
    if not isinstance(include_globs, (list, tuple)) or not all(isinstance(item, str) for item in include_globs):
        raise ValueError("prd.include_globs must be a string list")
    sender_names = lark.get("sender_names") or {}
    if not isinstance(sender_names, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in sender_names.items()
    ):
        raise ValueError("lark.sender_names must be a string map")

    return ProjectConfig(
        github_repo=_required_str(data, "github_repo"),
        github_cli=str(data.get("github_cli") or "gh"),
        lark=LarkConfig(
            chat_id=_required_str(lark, "chat_id"),
            app_id=_required_str(lark, "app_id"),
            app_secret_env=_required_str(lark, "app_secret_env"),
            bot_open_id=_required_str(lark, "bot_open_id"),
            sender_names=dict(sender_names),
            message_url_template=str(lark.get("message_url_template") or ""),
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
            include_globs=tuple(include_globs),
        ),
        intake=_parse_intake(language=language, intake=intake),
        assets=AssetsConfig(
            github_repo=str(assets.get("github_repo") or ""),
            checkout_path=str(assets.get("checkout_path") or ""),
            base_path=str(assets.get("base_path") or ".github/issue-assets"),
            branch=str(assets.get("branch") or "main"),
            remote_url=str(assets.get("remote_url") or ""),
        ),
        branches=_parse_branches(branches),
        media=MediaConfig(
            description_command=tuple(_optional_str_list(media, "description_command")),
            description_timeout_seconds=int(media.get("description_timeout_seconds") or 300),
            description_temp_dir=str(media.get("description_temp_dir") or ""),
            redaction_command=tuple(_optional_str_list(media, "redaction_command")),
            redaction_timeout_seconds=int(media.get("redaction_timeout_seconds") or 300),
            resize_max_image_width=int(media.get("resize_max_image_width") or 0),
            resize_max_image_height=int(media.get("resize_max_image_height") or 0),
            resize_image_quality=int(media.get("resize_image_quality") or 85),
            max_image_bytes=int(media.get("max_image_bytes") or 0),
            max_video_bytes=int(media.get("max_video_bytes") or 0),
            max_file_bytes=int(media.get("max_file_bytes") or 0),
            max_video_duration_seconds=float(media.get("max_video_duration_seconds") or 0.0),
            video_probe_command=tuple(_optional_str_list(media, "video_probe_command")),
            video_probe_timeout_seconds=int(media.get("video_probe_timeout_seconds") or 30),
            video_frame_command=tuple(_optional_str_list(media, "video_frame_command")),
            video_frame_timeout_seconds=int(media.get("video_frame_timeout_seconds") or 300),
            video_frame_min_duration_seconds=float(media.get("video_frame_min_duration_seconds") or 0.0),
            description_retries=int(media.get("description_retries") or 0),
            description_retry_backoff_seconds=float(media.get("description_retry_backoff_seconds") or 1.0),
        ),
        owners=OwnersConfig(
            default=tuple(_optional_str_list(owners, "default")),
            paths=_optional_owner_map(owners, "paths"),
            capabilities=_optional_owner_map(owners, "capabilities"),
        ),
        followup_classifier=FollowupClassifierConfig(
            acknowledgement_texts=tuple(_optional_str_list(followup_classifier, "acknowledgement_texts")),
            fix_status_keywords=tuple(_optional_str_list(followup_classifier, "fix_status_keywords")),
        ),
        issue_field_names={
            str(name): str(value)
            for name, value in field_names.items()
            if isinstance(value, str)
        },
    )


def _parse_intake(*, language: str, intake: dict[str, Any]) -> IntakeConfig:
    since = str(intake.get("since") or "")
    if since:
        datetime.fromisoformat(since)  # validate eagerly; raises ValueError on bad input
    skip_orphan = bool(intake.get("skip_orphan_replies", False))
    return IntakeConfig(language=language, since=since, skip_orphan_replies=skip_orphan)


def _parse_branches(branches: dict[str, Any]) -> BranchesConfig:
    default = str(branches.get("default") or "main")
    allowed_raw = branches.get("allowed")
    if allowed_raw is None:
        allowed: tuple[str, ...] = (default,)
    else:
        if not isinstance(allowed_raw, list) or not all(
            isinstance(item, str) and item for item in allowed_raw
        ):
            raise ValueError("branches.allowed must be a non-empty string list")
        allowed = tuple(allowed_raw)
    if not branch_matches_patterns(default, allowed):
        raise ValueError(f"branches.default {default!r} does not match branches.allowed patterns")
    return BranchesConfig(default=default, allowed=allowed)


def branch_matches_patterns(branch: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatchcase(branch, pattern) for pattern in patterns)


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


def _optional_str_list(data: dict[str, Any], key: str) -> list[str]:
    value = data.get(key)
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{key!r} must be a string list")
    return value


def _optional_owner_map(data: dict[str, Any], key: str) -> dict[str, tuple[str, ...]]:
    value = data.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"owners.{key} must be a table")
    result: dict[str, tuple[str, ...]] = {}
    for name, owners in value.items():
        if not isinstance(name, str):
            raise ValueError(f"owners.{key} keys must be strings")
        if not isinstance(owners, list) or not all(isinstance(item, str) for item in owners):
            raise ValueError(f"owners.{key}.{name} must be a string list")
        result[name] = tuple(owners)
    return result
