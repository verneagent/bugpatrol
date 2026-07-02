"""Local resource materialization for intake attachments."""

from __future__ import annotations

import re
import subprocess
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
        filename = _safe_segment(resource.filename or ref.resource_key)
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
        rel_path = Path(self._base_path) / _safe_segment(ref.message_id) / _safe_segment(
            resource.filename or ref.resource_key
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


def materialize_lark_attachments(
    *,
    record: IntakeRecord,
    lark: LarkResourceDownloader,
    store: ResourceStore,
) -> IntakeRecord:
    attachments = tuple(
        materialize_attachment(attachment=attachment, lark=lark, store=store)
        for attachment in record.attachments
    )
    return replace(record, attachments=attachments)


def materialize_attachment(
    *,
    attachment: Attachment,
    lark: LarkResourceDownloader,
    store: ResourceStore,
) -> Attachment:
    ref = parse_lark_resource_url(attachment.url)
    if ref is None:
        return attachment
    resource = lark.download_message_resource(
        message_id=ref.message_id,
        resource_key=ref.resource_key,
        resource_type=_download_resource_type(ref.kind),
    )
    path = store.write(ref=ref, resource=resource)
    return Attachment(
        kind=attachment.kind,
        url=str(path),
        description=attachment.description or resource.filename,
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


def _download_resource_type(kind: str) -> str:
    if kind == "image":
        return "image"
    if kind in {"file", "video"}:
        return "file"
    return kind
