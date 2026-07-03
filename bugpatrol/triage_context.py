"""Build deterministic triage context for agent providers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

from bugpatrol.clients import GitHubIssue, GitHubIssueComment
from bugpatrol.prd import PrdSearchHit, load_prd_documents, search_prd_documents


MARKDOWN_LINK_RE = re.compile(r"\[[^\]]+\]\((?P<url>https?://[^)\s]+)\)")


@dataclass(frozen=True)
class MediaEvidence:
    kind: str
    url: str
    description: str = ""
    source: str = ""


@dataclass(frozen=True)
class TriageContext:
    issue: GitHubIssue
    comments: tuple[GitHubIssueComment, ...]
    prd_hits: tuple[PrdSearchHit, ...]
    media: tuple[MediaEvidence, ...]


def build_triage_context(
    *,
    issue: GitHubIssue,
    comments: tuple[GitHubIssueComment, ...] = (),
    prd_root: Path,
    prd_include_globs: tuple[str, ...] = ("**/*.md",),
    prd_limit: int = 5,
) -> TriageContext:
    docs = load_prd_documents(prd_root, include_globs=prd_include_globs)
    comments_text = "\n".join(comment.body for comment in comments)
    query = f"{issue.title}\n{issue.body}\n{comments_text}"
    hits = search_prd_documents(query, docs, limit=prd_limit)
    media = (
        *extract_media_evidence(issue.body, source="issue body"),
        *(
            item
            for comment in comments
            for item in extract_media_evidence(comment.body, source=f"comment {comment.id}")
        ),
    )
    return TriageContext(issue=issue, comments=comments, prd_hits=hits, media=media)


def render_triage_context_markdown(context: TriageContext) -> str:
    lines = [
        "# BugPatrol Triage Context",
        "",
        "## GitHub Issue",
        "",
        f"- Number: #{context.issue.number}",
        f"- URL: {context.issue.url}",
        f"- Title: {context.issue.title}",
        "",
        "### Body",
        "",
        context.issue.body or "(empty)",
        "",
        "### Comments",
        "",
    ]
    if not context.comments:
        lines.append("- No issue comments.")
    for comment in context.comments:
        lines.extend(
            [
                f"#### Comment {comment.id}",
                "",
                comment.body or "(empty)",
                "",
            ]
        )
    lines.extend(
        [
            "## Media Evidence",
            "",
        ]
    )
    if not context.media:
        lines.append("- No image or video attachments found.")
    for item in context.media:
        lines.append(f"- {item.kind}: {item.url}")
        if item.description:
            lines.append(f"  - Description: {item.description}")
        if item.source:
            lines.append(f"  - Source: {item.source}")
    lines.extend(
        [
            "",
            "## PRD Search Hits",
            "",
        ]
    )
    if not context.prd_hits:
        lines.append("- No local PRD hits.")
    for hit in context.prd_hits:
        lines.extend(
            [
                f"### {hit.title}",
                "",
                f"- Path: `{hit.path}`",
                f"- Score: {hit.score}",
                "",
                hit.excerpt,
                "",
            ]
        )
    return "\n".join(lines)


def extract_media_evidence(markdown: str, *, source: str = "") -> tuple[MediaEvidence, ...]:
    items: list[MediaEvidence] = []
    current_index: int | None = None
    for raw_line in markdown.splitlines():
        line = raw_line.strip()
        if line.startswith("- image:") or line.startswith("- video:"):
            kind, value = line.removeprefix("- ").split(":", 1)
            url = extract_url(value.strip())
            if not url:
                current_index = None
                continue
            items.append(MediaEvidence(kind=kind.strip(), url=url, source=source))
            current_index = len(items) - 1
            continue
        if current_index is None:
            continue
        if line.startswith("- 生成描述:") or line.startswith("- generated description:") or line.startswith("- Description:"):
            description = line.split(":", 1)[1].strip()
            current = items[current_index]
            items[current_index] = MediaEvidence(
                kind=current.kind,
                url=current.url,
                description=description,
                source=current.source,
            )
    return tuple(items)


def extract_url(value: str) -> str:
    markdown_link = MARKDOWN_LINK_RE.search(value)
    if markdown_link:
        return markdown_link.group("url")
    if value.startswith("http://") or value.startswith("https://"):
        return value.split()[0]
    return ""
