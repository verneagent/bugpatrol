"""Materialize issue attachments locally before the triage agent starts.

Issue bodies link attachments as
``https://github.com/<owner>/<repo>/raw/<branch>/<path>``. The assets repo is
private, so an unauthenticated fetch — which is what an agent's own ``curl``
is — gets GitHub's 404 HTML page. The agent saved that page as ``s1.jpg``,
handed it to the model as an image, and the gateway answered with a bare
``400 {"model":"deepseek-v4-flash"}`` that aborted the whole run after minutes
of work (issue #6124).

Downloading here, through the same authenticated ``gh`` the runner already
uses, keeps the bytes off the agent's network path entirely — and lets us
verify the payload really is the media it claims to be before the agent reads
it. A failed download never leaves a file behind: the evidence carries an
explicit status the agent can see instead.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

from bugpatrol.triage_context import MediaEvidence

ASSET_URL_RE = re.compile(
    r"^https://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/raw/(?P<rest>.+)$"
)

# Bytes the download must start with for each container we accept. A 404 HTML
# page matches none of them, which is the whole point: `curl`/`gh` exiting 0
# says nothing about *what* came back.
_IMAGE_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", ".jpg"),
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
    (b"BM", ".bmp"),
)
_HEIC_BRANDS = (b"heic", b"heix", b"hevc", b"heim", b"heis", b"mif1", b"msf1")

# Longest magic we need to inspect is the 12-byte ISO-BMFF header (ftyp brand).
_SNIFF_BYTES = 12


def sniff_media(head: bytes) -> tuple[str, str] | None:
    """Return ``(kind, extension)`` for the container ``head`` starts with.

    ``kind`` is ``"image"`` or ``"video"``. Returns ``None`` when the bytes are
    not a media container we recognize (an HTML error page, a truncated file,
    a raw git LFS pointer).
    """
    for magic, extension in _IMAGE_MAGIC:
        if head.startswith(magic):
            return ("image", extension)
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return ("image", ".webp")
    if len(head) >= 12 and head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in _HEIC_BRANDS:
            return ("image", ".heic")
        if brand == b"qt  ":
            return ("video", ".mov")
        return ("video", ".mp4")
    if head.startswith(b"\x1aE\xdf\xa3"):
        return ("video", ".webm")
    return None


def parse_asset_url(url: str, *, assets_repo: str, branch: str) -> str | None:
    """Map an assets-repo raw URL to its GitHub contents API path.

    Returns ``None`` for anything that is not a raw URL inside the configured
    assets repo — Lark resource URLs, unrelated hosts, and other repositories
    are left for the caller to pass through untouched.
    """
    match = ASSET_URL_RE.match(url)
    if match is None:
        return None
    if f"{match['owner']}/{match['repo']}" != assets_repo:
        return None
    rest = match["rest"]
    prefix = f"{branch}/"
    if not rest.startswith(prefix):
        return None
    path = rest[len(prefix) :]
    if not path:
        return None
    return f"/repos/{assets_repo}/contents/{path}?ref={branch}"


def localize_media(
    media: Sequence[MediaEvidence],
    *,
    dest_dir: Path,
    assets_repo: str,
    branch: str = "main",
    gh: str = "gh",
    timeout_seconds: float = 120.0,
) -> tuple[MediaEvidence, ...]:
    """Download this run's assets-repo attachments next to the triage context.

    Every returned item reports where its bytes are (``local_path``) or why
    they are not (``local_status``). Nothing is fetched when ``assets_repo`` is
    unset, so a project without an assets repo keeps the previous behaviour.
    """
    if not assets_repo:
        return tuple(media)
    localized: list[MediaEvidence] = []
    for index, item in enumerate(media, start=1):
        api_path = parse_asset_url(item.url, assets_repo=assets_repo, branch=branch)
        if api_path is None:
            localized.append(item)
            continue
        dest_dir.mkdir(parents=True, exist_ok=True)
        localized.append(
            _localize_one(
                item,
                api_path=api_path,
                dest_dir=dest_dir,
                index=index,
                gh=gh,
                timeout_seconds=timeout_seconds,
            )
        )
    return tuple(localized)


def _localize_one(
    item: MediaEvidence,
    *,
    api_path: str,
    dest_dir: Path,
    index: int,
    gh: str,
    timeout_seconds: float,
) -> MediaEvidence:
    try:
        completed = subprocess.run(
            [gh, "api", api_path, "-H", "Accept: application/vnd.github.raw"],
            capture_output=True,
            text=False,
            check=False,
            timeout=timeout_seconds,
        )
    except FileNotFoundError as err:
        return replace(item, local_status=f"not fetched: gh CLI {gh!r} not found ({err})")
    except subprocess.TimeoutExpired:
        return replace(
            item,
            local_status=f"not fetched: download timed out after {timeout_seconds:g}s",
        )
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        return replace(
            item,
            local_status=(
                f"not fetched: gh exited {completed.returncode} — {detail[-300:]}"
            ),
        )
    body = completed.stdout
    if not body:
        return replace(item, local_status="not fetched: empty response body")
    sniffed = sniff_media(body[:_SNIFF_BYTES])
    if sniffed is None:
        # The failure that started all this: a private-repo 404 renders as an
        # HTML page, which the Read tool happily base64s as `image/jpeg` and
        # the model gateway rejects with a 400 that kills the run.
        return replace(
            item,
            local_status=(
                "not fetched: response is not a media file "
                f"(starts with {body[:16]!r}); it is most likely an error page. "
                "Do not fetch the URL yourself — use the generated description."
            ),
        )
    kind, extension = sniffed
    if kind != item.kind:
        return replace(
            item,
            local_status=(
                f"not fetched: expected {item.kind}, got {kind} data — "
                "use the generated description."
            ),
        )
    path = (dest_dir / f"media-{index}{extension}").resolve()
    path.write_bytes(body)
    return replace(item, local_path=str(path))
