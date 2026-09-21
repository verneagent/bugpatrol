"""Attachments must reach the agent as verified local files, never as a URL.

Regression cover for issue #6124: the agent fetched a private assets-repo URL,
got GitHub's 404 HTML page, saved it as `s1.jpg`, and the model gateway
answered `400 {"model":"deepseek-v4-flash"}`, aborting the run.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from bugpatrol.media_localize import localize_media, parse_asset_url, sniff_media
from bugpatrol.triage_context import MediaEvidence, render_triage_context_markdown

ASSETS_REPO = "TheCloverLab/fived-assets"
ASSET_URL = (
    "https://github.com/TheCloverLab/fived-assets/raw/main/"
    ".github/issue-assets/om_x1/img_v3_abc.jpg"
)
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
# The shape of the actual failure: a full GitHub 404 page, saved as `.jpg`.
HTML_PAGE = b"\n\n\n<!DOCTYPE html>\n<html lang=\"en\">\n<title>Page not found</title>"


class _FakeGh:
    """Stand-in for the `gh` CLI: records calls, replays scripted responses."""

    def __init__(self, responses: dict[str, subprocess.CompletedProcess[bytes]]) -> None:
        self.responses = responses
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        # args = [<gh>, "api", <endpoint>, "-H", "Accept: ..."].
        self.calls.append(list(args))
        return self.responses[args[2]]


def _install_gh(monkeypatch: pytest.MonkeyPatch, fake: _FakeGh) -> None:
    monkeypatch.setattr("bugpatrol.media_localize.subprocess.run", fake)


def _ok(body: bytes) -> subprocess.CompletedProcess[bytes]:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=body, stderr=b"")


def test_parse_asset_url_maps_to_contents_api() -> None:
    assert parse_asset_url(ASSET_URL, assets_repo=ASSETS_REPO, branch="main") == (
        f"/repos/{ASSETS_REPO}/contents/.github/issue-assets/om_x1/img_v3_abc.jpg?ref=main"
    )


def test_parse_asset_url_ignores_foreign_urls() -> None:
    # A Lark resource URL, another repo, and a non-raw link all stay untouched.
    assert parse_asset_url("https://open.larksuite.com/open-apis/x", assets_repo=ASSETS_REPO, branch="main") is None
    assert parse_asset_url(ASSET_URL.replace("fived-assets", "fived"), assets_repo=ASSETS_REPO, branch="main") is None
    assert parse_asset_url(ASSET_URL.replace("/raw/", "/blob/"), assets_repo=ASSETS_REPO, branch="main") is None
    assert parse_asset_url(ASSET_URL, assets_repo=ASSETS_REPO, branch="release") is None


@pytest.mark.parametrize(
    "head,expected",
    [
        (b"\xff\xd8\xff\xe0", ("image", ".jpg")),
        (b"\x89PNG\r\n\x1a\n", ("image", ".png")),
        (b"GIF89a", ("image", ".gif")),
        (b"RIFF\x00\x00\x00\x00WEBP", ("image", ".webp")),
        (b"\x00\x00\x00\x18ftypmp42", ("video", ".mp4")),
        (b"\x00\x00\x00\x14ftypqt  ", ("video", ".mov")),
        (b"\x1aE\xdf\xa3", ("video", ".webm")),
        (HTML_PAGE[:12], None),
        (b"", None),
    ],
)
def test_sniff_media(head: bytes, expected: tuple[str, str] | None) -> None:
    assert sniff_media(head) == expected


def test_localize_writes_a_verified_local_copy(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake = _FakeGh({"/repos/TheCloverLab/fived-assets/contents/.github/issue-assets/om_x1/img_v3_abc.jpg?ref=main": _ok(JPEG)})
    _install_gh(monkeypatch, fake)

    (item,) = localize_media(
        (MediaEvidence(kind="image", url=ASSET_URL),),
        dest_dir=tmp_path / "attachments",
        assets_repo=ASSETS_REPO,
        gh="gh",
    )

    assert item.local_path
    assert Path(item.local_path).read_bytes() == JPEG
    assert Path(item.local_path).name == "media-1.jpg"
    assert item.local_status == ""
    # Fetched with the runner's credentials, not the agent's anonymous curl.
    assert fake.calls[0][:3] == [
        "gh",
        "api",
        "/repos/TheCloverLab/fived-assets/contents/.github/issue-assets/om_x1/img_v3_abc.jpg?ref=main",
    ]


def test_html_error_page_is_rejected_and_leaves_no_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The exact #6124 failure: a 404 page must never become a readable `.jpg`."""
    fake = _FakeGh({"/repos/TheCloverLab/fived-assets/contents/.github/issue-assets/om_x1/img_v3_abc.jpg?ref=main": _ok(HTML_PAGE)})
    _install_gh(monkeypatch, fake)

    (item,) = localize_media(
        (MediaEvidence(kind="image", url=ASSET_URL),),
        dest_dir=tmp_path / "attachments",
        assets_repo=ASSETS_REPO,
        gh="gh",
    )

    assert item.local_path == ""
    assert "not a media file" in item.local_status
    # No stray file for the agent to trip over.
    assert list((tmp_path / "attachments").iterdir()) == []


def test_failed_download_reports_status_without_raising(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    failure = subprocess.CompletedProcess(args=[], returncode=1, stdout=b"", stderr=b"gh: Not Found (HTTP 404)")
    fake = _FakeGh({"/repos/TheCloverLab/fived-assets/contents/.github/issue-assets/om_x1/img_v3_abc.jpg?ref=main": failure})
    _install_gh(monkeypatch, fake)

    (item,) = localize_media(
        (MediaEvidence(kind="image", url=ASSET_URL),),
        dest_dir=tmp_path / "attachments",
        assets_repo=ASSETS_REPO,
        gh="gh",
    )

    assert item.local_path == ""
    assert "Not Found" in item.local_status


def test_kind_mismatch_is_reported(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake = _FakeGh({"/repos/TheCloverLab/fived-assets/contents/.github/issue-assets/om_x1/img_v3_abc.jpg?ref=main": _ok(JPEG)})
    _install_gh(monkeypatch, fake)

    (item,) = localize_media(
        (MediaEvidence(kind="video", url=ASSET_URL),),
        dest_dir=tmp_path / "attachments",
        assets_repo=ASSETS_REPO,
        gh="gh",
    )

    assert item.local_path == ""
    assert "expected video, got image" in item.local_status


def test_localize_is_a_noop_without_an_assets_repo(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake = _FakeGh({})
    _install_gh(monkeypatch, fake)

    (item,) = localize_media(
        (MediaEvidence(kind="image", url=ASSET_URL),),
        dest_dir=tmp_path / "attachments",
        assets_repo="",
        gh="gh",
    )

    assert item == MediaEvidence(kind="image", url=ASSET_URL)
    assert fake.calls == []
    assert not (tmp_path / "attachments").exists()


def test_context_renders_local_path_instead_of_a_fetch_invitation() -> None:
    context = _context_with(
        MediaEvidence(kind="image", url=ASSET_URL, description="闪退截图", local_path="/w/attachments/media-1.jpg")
    )
    markdown = render_triage_context_markdown(context)

    assert "- Local file: /w/attachments/media-1.jpg" in markdown
    assert "do **not** fetch the `url`" in markdown


def test_context_renders_a_failed_copy_explicitly() -> None:
    context = _context_with(
        MediaEvidence(kind="image", url=ASSET_URL, description="闪退截图", local_status="not fetched: gh exited 1")
    )
    markdown = render_triage_context_markdown(context)

    assert "- Local copy: not fetched: gh exited 1" in markdown


def _context_with(*media: MediaEvidence):
    from bugpatrol.clients import GitHubIssue
    from bugpatrol.triage_context import TriageContext

    return TriageContext(
        issue=GitHubIssue(number=1, url="https://example.test/1", title="t", body="b"),
        comments=(),
        prd_hits=(),
        media=media,
    )
