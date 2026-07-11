"""Project configuration for bugpatrol."""

from __future__ import annotations

from datetime import datetime
from dataclasses import dataclass
import os
from pathlib import Path
from typing import Any

import tomllib

from bugpatrol.fields import FieldSpec


_LARK_PLATFORM_API_BASE_URLS = {
    "lark": "https://open.larksuite.com/open-apis",
    "feishu": "https://open.feishu.cn/open-apis",
}
_LARK_PLATFORM_APPLINK_DOMAINS = {
    "lark": "applink.larksuite.com",
    "feishu": "applink.feishu.cn",
}


@dataclass(frozen=True)
class LarkConfig:
    chat_id: str
    app_id: str
    app_secret_env: str
    bot_open_id: str
    sender_names: dict[str, str] | None = None
    # GitHub login -> Lark open_id, used to render real @mentions.
    user_open_ids: dict[str, str] | None = None
    message_url_template: str = ""
    # "lark" (international) or "feishu" (China); drives API base URL and
    # the default message link domain.
    platform: str = "lark"
    # Extra topic groups scoped to a feature branch: Lark chat_id -> branch.
    # The main `chat_id` above always maps to "main". A topic posted in one of
    # these groups declares (does not infer) that its build was the branch.
    branch_chats: dict[str, str] | None = None

    @property
    def api_base_url(self) -> str:
        return _LARK_PLATFORM_API_BASE_URLS[self.platform]

    def branch_for_chat(self, chat_id: str) -> str:
        """Resolve the declared feature branch for a chat; main chat -> "main"."""
        if self.branch_chats and chat_id in self.branch_chats:
            return self.branch_chats[chat_id]
        return "main"

    def all_chat_ids(self) -> tuple[str, ...]:
        """Every chat the watcher scans: the main chat plus branch chats."""
        ids = [self.chat_id, *(self.branch_chats or {})]
        return tuple(dict.fromkeys(ids))


def _default_message_url_template(platform: str) -> str:
    domain = _LARK_PLATFORM_APPLINK_DOMAINS[platform]
    return f"https://{domain}/client/chat/open?openChatId={{chat_id}}&messageId={{message_id}}"


@dataclass(frozen=True)
class TriageAgentConfig:
    runner_labels: tuple[str, ...]
    provider: str = "codex"
    model: str = ""
    effort: str = ""


@dataclass(frozen=True)
class ReferenceRepo:
    """A sibling repo triage may read read-only to reason about cross-repo bugs.

    `path` is where the runner checks the repo out (relative to the runner
    workspace, or absolute); the workflow supplies the concrete checkout at
    run time. `branch_map` maps a main-repo branch to the reference-repo branch;
    unmapped branches default to the same name (and fall back to the reference
    repo's main when that branch does not exist).
    """

    repo: str
    path: str
    purpose: str = ""
    branch_map: dict[str, str] | None = None

    def branch_for(self, main_branch: str) -> str:
        if self.branch_map and main_branch in self.branch_map:
            return self.branch_map[main_branch]
        return main_branch


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
class MediaConfig:
    description_command: tuple[str, ...] = ()
    description_timeout_seconds: int = 300
    description_temp_dir: str = ""
    redaction_command: tuple[str, ...] = ()
    redaction_timeout_seconds: int = 300
    resize_max_image_width: int = 0
    resize_max_image_height: int = 0
    resize_image_quality: int = 85
    convert_images_to_jpeg: bool = False
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


# Default paths a fix run must never touch. Editing CI/CD, lockfiles, secrets,
# or DB migrations from an auto-fix is high-blast-radius and must go through a
# human, so any diff touching these blocks the PR.
DEFAULT_FIX_PROTECTED_GLOBS: tuple[str, ...] = (
    ".github/**",
    "**/*.lock",
    "**/package-lock.json",
    "**/pnpm-lock.yaml",
    "**/yarn.lock",
    "**/Cargo.lock",
    "**/poetry.lock",
    "**/go.sum",
    "**/*.pem",
    "**/*.key",
    "**/.env",
    "**/.env.*",
    "**/migrations/**",
)


@dataclass(frozen=True)
class FixConfig:
    """Configuration for the auto-fix runner.

    ``verify`` maps a human label (build/typecheck/test/lint or any name) to a
    shell command the project owns; BugPatrol only runs each and reads the exit
    code, so the toolchain stays fully decoupled from BugPatrol. The gate keeps
    fixes small and out of high-blast-radius paths; ``allowed_verdicts`` limits
    auto-fix to triage conclusions that are actually code fixes.
    """

    verify: dict[str, str]
    max_diff_lines: int = 800
    protected_globs: tuple[str, ...] = DEFAULT_FIX_PROTECTED_GLOBS
    allowed_verdicts: tuple[str, ...] = ("代码 Bug",)
    branch_prefix: str = "bugpatrol/fix-issue-"
    # 0 = unlimited; otherwise a device-level counting semaphore caps how many
    # heavy fix builds run at once across all runners sharing one machine.
    max_concurrent_per_device: int = 0

    def branch_for_issue(self, issue_number: int) -> str:
        return f"{self.branch_prefix}{issue_number}"


@dataclass(frozen=True)
class ProjectConfig:
    github_repo: str
    github_cli: str
    lark: LarkConfig
    triage_agent: TriageAgentConfig
    prd: PrdConfig
    intake: IntakeConfig
    assets: AssetsConfig
    media: MediaConfig
    owners: OwnersConfig
    followup_classifier: FollowupClassifierConfig
    issue_field_names: dict[str, str]
    reference_repos: tuple[ReferenceRepo, ...] = ()
    fix: FixConfig | None = None

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
    user_open_ids = lark.get("user_open_ids") or {}
    if not isinstance(user_open_ids, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in user_open_ids.items()
    ):
        raise ValueError("lark.user_open_ids must be a string map")
    platform = str(lark.get("platform") or "lark")
    if platform not in _LARK_PLATFORM_API_BASE_URLS:
        raise ValueError('lark.platform must be "lark" or "feishu"')
    branch_chats = _parse_branch_chats(lark, main_chat_id=_required_str(lark, "chat_id"))

    return ProjectConfig(
        github_repo=_required_str(data, "github_repo"),
        # expanduser so one config works across machines with different homes.
        # BUGPATROL_GITHUB_CLI overrides the config value at runtime so one
        # shared config serves two runtimes: the watcher keeps the config's
        # self-refreshing bot wrapper (mints from an on-disk key), while a
        # triage runner sets BUGPATROL_GITHUB_CLI=gh and passes a workflow-minted
        # GH_TOKEN, keeping no credential on the box.
        github_cli=str(
            Path(
                os.environ.get("BUGPATROL_GITHUB_CLI") or str(data.get("github_cli") or "gh")
            ).expanduser()
        ),
        lark=LarkConfig(
            chat_id=_required_str(lark, "chat_id"),
            app_id=_required_str(lark, "app_id"),
            app_secret_env=_required_str(lark, "app_secret_env"),
            bot_open_id=_required_str(lark, "bot_open_id"),
            sender_names=dict(sender_names),
            user_open_ids=dict(user_open_ids),
            message_url_template=str(lark.get("message_url_template") or "")
            or _default_message_url_template(platform),
            platform=platform,
            branch_chats=branch_chats or None,
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
        media=MediaConfig(
            description_command=tuple(_optional_str_list(media, "description_command")),
            description_timeout_seconds=int(media.get("description_timeout_seconds") or 300),
            description_temp_dir=str(media.get("description_temp_dir") or ""),
            redaction_command=tuple(_optional_str_list(media, "redaction_command")),
            redaction_timeout_seconds=int(media.get("redaction_timeout_seconds") or 300),
            resize_max_image_width=int(media.get("resize_max_image_width") or 0),
            resize_max_image_height=int(media.get("resize_max_image_height") or 0),
            resize_image_quality=int(media.get("resize_image_quality") or 85),
            convert_images_to_jpeg=bool(media.get("convert_images_to_jpeg") or False),
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
        reference_repos=_parse_reference_repos(data),
        fix=_parse_fix(data),
    )


def _parse_fix(data: dict[str, Any]) -> FixConfig | None:
    """Parse the optional `[fix]` table (auto-fix runner).

    Absent `[fix]` -> None (the project has no fix runner). When present, at
    least one `[fix.verify]` command must be non-empty: an auto-fix with no
    verification is not shippable, so a config that would skip every gate is
    rejected loudly rather than opening unverified PRs.
    """
    fix = data.get("fix")
    if fix is None:
        return None
    if not isinstance(fix, dict):
        raise ValueError("[fix] must be a table")
    verify_raw = fix.get("verify") or {}
    if not isinstance(verify_raw, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in verify_raw.items()
    ):
        raise ValueError("[fix.verify] must be a table of string commands")
    verify = {key: value for key, value in verify_raw.items() if value.strip()}
    if not verify:
        raise ValueError(
            "[fix.verify] must define at least one non-empty command; "
            "an auto-fix with no verification is not shippable"
        )
    gate = fix.get("gate") or {}
    if not isinstance(gate, dict):
        raise ValueError("[fix.gate] must be a table")
    max_diff_lines = int(gate.get("max_diff_lines") or 800)
    if max_diff_lines <= 0:
        raise ValueError("fix.gate.max_diff_lines must be positive")
    protected_globs = gate.get("protected_globs")
    if protected_globs is None:
        protected = DEFAULT_FIX_PROTECTED_GLOBS
    else:
        if not isinstance(protected_globs, list) or not all(
            isinstance(item, str) for item in protected_globs
        ):
            raise ValueError("fix.gate.protected_globs must be a string list")
        protected = tuple(protected_globs)
    allowed_verdicts = gate.get("allowed_verdicts")
    if allowed_verdicts is None:
        verdicts = ("代码 Bug",)
    else:
        if not isinstance(allowed_verdicts, list) or not all(
            isinstance(item, str) and item for item in allowed_verdicts
        ):
            raise ValueError("fix.gate.allowed_verdicts must be a non-empty string list")
        verdicts = tuple(allowed_verdicts)
    runner = fix.get("runner") or {}
    if not isinstance(runner, dict):
        raise ValueError("[fix.runner] must be a table")
    max_concurrent = int(runner.get("max_concurrent_per_device") or 0)
    if max_concurrent < 0:
        raise ValueError("fix.runner.max_concurrent_per_device must be >= 0")
    branch_prefix = str(fix.get("branch_prefix") or "bugpatrol/fix-issue-")
    return FixConfig(
        verify=verify,
        max_diff_lines=max_diff_lines,
        protected_globs=protected,
        allowed_verdicts=verdicts,
        branch_prefix=branch_prefix,
        max_concurrent_per_device=max_concurrent,
    )


def _parse_intake(*, language: str, intake: dict[str, Any]) -> IntakeConfig:
    since = str(intake.get("since") or "")
    if since:
        datetime.fromisoformat(since)  # validate eagerly; raises ValueError on bad input
    skip_orphan = bool(intake.get("skip_orphan_replies", False))
    return IntakeConfig(language=language, since=since, skip_orphan_replies=skip_orphan)


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


def _parse_branch_chats(lark: dict[str, Any], *, main_chat_id: str) -> dict[str, str]:
    value = lark.get("branch_chats")
    if value is None:
        return {}
    if not isinstance(value, list):
        raise ValueError("lark.branch_chats must be an array of tables")
    result: dict[str, str] = {}
    for entry in value:
        if not isinstance(entry, dict):
            raise ValueError("each [[lark.branch_chats]] entry must be a table")
        chat_id = _required_str(entry, "chat_id")
        branch = _required_str(entry, "branch")
        if chat_id == main_chat_id:
            raise ValueError("lark.branch_chats cannot re-map the main lark.chat_id")
        if chat_id in result:
            raise ValueError(f"duplicate lark.branch_chats chat_id: {chat_id}")
        result[chat_id] = branch
    return result


def _parse_reference_repos(data: dict[str, Any]) -> tuple[ReferenceRepo, ...]:
    """Parse optional `[[triage.reference_repos]]` sibling-repo declarations.

    Bounded by the config's array length; each entry is a table with `repo`,
    `path`, optional `purpose`, and an optional `branch_map` (main-repo branch
    -> reference-repo branch). Duplicate `repo` values are rejected.
    """
    triage = data.get("triage")
    if triage is None:
        return ()
    if not isinstance(triage, dict):
        raise ValueError("[triage] must be a table")
    entries = triage.get("reference_repos")
    if entries is None:
        return ()
    if not isinstance(entries, list):
        raise ValueError("triage.reference_repos must be an array of tables")
    result: list[ReferenceRepo] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("each [[triage.reference_repos]] entry must be a table")
        repo = _required_str(entry, "repo")
        path = _required_str(entry, "path")
        if repo in seen:
            raise ValueError(f"duplicate triage.reference_repos repo: {repo}")
        seen.add(repo)
        branch_map_raw = entry.get("branch_map")
        branch_map: dict[str, str] | None = None
        if branch_map_raw is not None:
            if not isinstance(branch_map_raw, dict) or not all(
                isinstance(key, str) and isinstance(value, str)
                for key, value in branch_map_raw.items()
            ):
                raise ValueError(
                    "triage.reference_repos branch_map must be a string map"
                )
            branch_map = dict(branch_map_raw)
        result.append(
            ReferenceRepo(
                repo=repo,
                path=path,
                purpose=str(entry.get("purpose") or ""),
                branch_map=branch_map,
            )
        )
    return tuple(result)


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
