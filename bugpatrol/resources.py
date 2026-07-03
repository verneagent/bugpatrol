"""Local resource materialization for intake attachments."""

from __future__ import annotations

import re
import subprocess
import tempfile
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

from bugpatrol.intake import Attachment, IntakeRecord
from bugpatrol.lark import DownloadedLarkResource

LARK_RESOURCE_RE = re.compile(r"^lark://message/([^/]+)/([^/]+)/([^/]+)$")


class LarkResourceDownloader(Protocol):
    def download_message_resource(
        self,
        *,
        message_id: str,
        resource_key: str,
        resource_type: str = "",
    ) -> DownloadedLarkResource:
        """Download a Lark message resource."""


class ResourceStore(Protocol):
    def write(self, *, ref: "LarkResourceRef", resource: DownloadedLarkResource) -> Path | str:
        """Persist a downloaded resource and return the URL/path to put in intake."""


class ResourceDescriber(Protocol):
    def describe(self, *, ref: "LarkResourceRef", resource: DownloadedLarkResource) -> str:
        """Return a textual media description for triage."""


@dataclass(frozen=True)
class ResourcePolicy:
    max_image_bytes: int = 0
    max_video_bytes: int = 0
    max_file_bytes: int = 0

    def rejection_reason(self, *, ref: "LarkResourceRef", resource: DownloadedLarkResource) -> str:
        limit = self._limit_for(ref=ref, resource=resource)
        if limit > 0 and len(resource.content) > limit:
            return f"resource skipped: {ref.kind} is {len(resource.content)} bytes, limit is {limit} bytes"
        return ""

    def _limit_for(self, *, ref: "LarkResourceRef", resource: DownloadedLarkResource) -> int:
        content_type = resource.content_type.split(";", 1)[0].strip().lower()
        if ref.kind == "image" or content_type.startswith("image/"):
            return self.max_image_bytes
        if ref.kind in {"video", "media"} or content_type.startswith("video/"):
            return self.max_video_bytes
        return self.max_file_bytes


@dataclass(frozen=True)
class LarkResourceRef:
    message_id: str
    kind: str
    resource_key: str


class LocalResourceStore:
    def __init__(self, root: Path) -> None:
        self._root = root

    def write(self, *, ref: LarkResourceRef, resource: DownloadedLarkResource) -> Path:
        directory = self._root / _safe_segment(ref.message_id)
        directory.mkdir(parents=True, exist_ok=True)
        filename = _resource_filename(resource=resource, fallback=ref.resource_key)
        path = directory / filename
        path.write_bytes(resource.content)
        return path


class GitHubAssetRepoStore:
    def __init__(
        self,
        *,
        repo: str,
        checkout_path: Path,
        base_path: str = ".github/issue-assets",
        branch: str = "main",
        remote_url: str = "",
        git: str = "git",
    ) -> None:
        self._repo = repo
        self._checkout_path = checkout_path.expanduser()
        self._base_path = base_path.strip("/")
        self._branch = branch
        self._remote_url = remote_url
        self._git = git

    def write(self, *, ref: LarkResourceRef, resource: DownloadedLarkResource) -> str:
        self._ensure_checkout()
        rel_path = Path(self._base_path) / _safe_segment(ref.message_id) / _resource_filename(
            resource=resource,
            fallback=ref.resource_key,
        )
        path = self._checkout_path / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(resource.content)
        self._run(["-C", str(self._checkout_path), "add", str(rel_path)])
        self._run(
            [
                "-C",
                str(self._checkout_path),
                "commit",
                "--no-verify",
                "-m",
                f"add: bug attachment {ref.message_id}",
            ],
            allow_no_changes=True,
        )
        self._run(["-C", str(self._checkout_path), "push", "--no-verify", self._existing_remote(), self._branch])
        return f"https://github.com/{self._repo}/raw/{self._branch}/{rel_path.as_posix()}"

    def _ensure_checkout(self) -> None:
        if (self._checkout_path / ".git").exists():
            self._run(["-C", str(self._checkout_path), "pull", "--quiet", self._existing_remote(), self._branch])
            return
        self._checkout_path.parent.mkdir(parents=True, exist_ok=True)
        self._run(
            [
                "clone",
                "--branch",
                self._branch,
                self._clone_remote(),
                str(self._checkout_path),
            ]
        )

    def _existing_remote(self) -> str:
        return self._remote_url or "origin"

    def _clone_remote(self) -> str:
        return self._remote_url or f"git@github.com:{self._repo}.git"

    def _run(self, args: list[str], *, allow_no_changes: bool = False) -> None:
        completed = subprocess.run(
            [self._git, *args],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode == 0:
            return
        combined = f"{completed.stdout}\n{completed.stderr}"
        if allow_no_changes and "nothing to commit" in combined:
            return
        raise RuntimeError(f"git {' '.join(args)} failed: {completed.stderr.strip()}")


class CommandResourceDescriber:
    def __init__(
        self,
        *,
        command: tuple[str, ...],
        timeout_seconds: int = 300,
        temp_dir: Path | None = None,
        retries: int = 0,
        retry_backoff_seconds: float = 1.0,
    ) -> None:
        self._command = command
        self._timeout_seconds = timeout_seconds
        self._temp_dir = temp_dir
        self._retries = max(0, retries)
        self._retry_backoff_seconds = max(0.0, retry_backoff_seconds)

    def describe(self, *, ref: LarkResourceRef, resource: DownloadedLarkResource) -> str:
        if not self._command:
            return ""
        temp_root = self._temp_dir
        if temp_root is not None:
            temp_root.mkdir(parents=True, exist_ok=True)
        filename = _resource_filename(resource=resource, fallback=ref.resource_key)
        with tempfile.TemporaryDirectory(dir=temp_root) as tmp:
            path = Path(tmp) / filename
            path.write_bytes(resource.content)
            command = tuple(
                _format_command_part(part, path=path, ref=ref, resource=resource)
                for part in self._command
            )
            completed = None
            for attempt in range(self._retries + 1):
                try:
                    completed = subprocess.run(
                        command,
                        capture_output=True,
                        text=True,
                        check=False,
                        timeout=self._timeout_seconds,
                    )
                except subprocess.TimeoutExpired:
                    if attempt >= self._retries:
                        return f"vision description unavailable: timed out after {self._timeout_seconds}s"
                    time.sleep(self._retry_backoff_seconds)
                    continue
                if completed.returncode == 0 and completed.stdout.strip():
                    break
                if attempt < self._retries:
                    time.sleep(self._retry_backoff_seconds)
        if completed.returncode == 0 and completed.stdout.strip():
            return completed.stdout.strip()
        detail = (completed.stderr or completed.stdout or f"exit {completed.returncode}").strip()
        return f"vision description unavailable: {detail[:300]}"


def materialize_lark_attachments(
    *,
    record: IntakeRecord,
    lark: LarkResourceDownloader,
    store: ResourceStore,
    describer: ResourceDescriber | None = None,
    policy: ResourcePolicy | None = None,
) -> IntakeRecord:
    attachments = tuple(
        materialize_attachment(
            attachment=attachment,
            lark=lark,
            store=store,
            describer=describer,
            policy=policy,
        )
        for attachment in record.attachments
    )
    return replace(record, attachments=attachments)


def materialize_attachment(
    *,
    attachment: Attachment,
    lark: LarkResourceDownloader,
    store: ResourceStore,
    describer: ResourceDescriber | None = None,
    policy: ResourcePolicy | None = None,
) -> Attachment:
    ref = parse_lark_resource_url(attachment.url)
    if ref is None:
        return attachment
    resource = lark.download_message_resource(
        message_id=ref.message_id,
        resource_key=ref.resource_key,
        resource_type=_download_resource_type(ref.kind),
    )
    if policy is not None:
        rejection = policy.rejection_reason(ref=ref, resource=resource)
        if rejection:
            return Attachment(kind=attachment.kind, url=attachment.url, description=rejection)
    path = store.write(ref=ref, resource=resource)
    description = attachment.description or resource.filename
    if describer is not None:
        generated = describer.describe(ref=ref, resource=resource).strip()
        if generated:
            description = generated
    return Attachment(
        kind=attachment.kind,
        url=str(path),
        description=description,
    )


def parse_lark_resource_url(url: str) -> LarkResourceRef | None:
    match = LARK_RESOURCE_RE.match(url)
    if not match:
        return None
    return LarkResourceRef(
        message_id=match.group(1),
        kind=match.group(2),
        resource_key=match.group(3),
    )


def _safe_segment(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._")
    return safe or "resource"


def _resource_filename(*, resource: DownloadedLarkResource, fallback: str) -> str:
    filename = _safe_segment(resource.filename or fallback)
    if Path(filename).suffix:
        return filename
    extension = _extension_for_content_type(resource.content_type)
    return f"{filename}{extension}" if extension else filename


def _extension_for_content_type(content_type: str) -> str:
    media_type = content_type.split(";", 1)[0].strip().lower()
    return {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/gif": ".gif",
        "image/webp": ".webp",
        "video/mp4": ".mp4",
        "video/quicktime": ".mov",
    }.get(media_type, "")


def _download_resource_type(kind: str) -> str:
    if kind == "image":
        return "image"
    if kind in {"file", "video", "media"}:
        return "file"
    return kind


def _format_command_part(
    part: str,
    *,
    path: Path,
    ref: LarkResourceRef,
    resource: DownloadedLarkResource,
) -> str:
    formatted = part.format(
        path=str(path),
        kind=ref.kind,
        message_id=ref.message_id,
        resource_key=ref.resource_key,
        filename=resource.filename,
        content_type=resource.content_type,
    )
    return Path(formatted).expanduser().as_posix() if formatted.startswith("~") else formatted
