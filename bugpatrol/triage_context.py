"""Build deterministic triage context for agent providers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import re

from bugpatrol.clients import GitHubIssue, GitHubIssueComment
from bugpatrol.openspec import OpenSpecChange, OpenSpecOwnerHit, score_openspec_changes
from bugpatrol.prd import PrdSearchHit, load_prd_documents, search_prd_documents
from bugpatrol.watermark.reporter import render_payload_summary


MARKDOWN_LINK_RE = re.compile(r"\[[^\]]+\]\((?P<url>https?://[^)\s]+)\)")


@dataclass(frozen=True)
class MediaEvidence:
    kind: str
    url: str
    description: str = ""
    source: str = ""
    # Compact JSON of the decoded diagnostic watermark payload ("" when absent).
    watermark: str = ""


@dataclass(frozen=True)
class AssigneeIdentity:
    login: str
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReferenceRepoContext:
    repo: str
    path: str
    analyzed_branch: str
    purpose: str = ""


@dataclass(frozen=True)
class TriageContext:
    issue: GitHubIssue
    comments: tuple[GitHubIssueComment, ...]
    prd_hits: tuple[PrdSearchHit, ...]
    media: tuple[MediaEvidence, ...]
    roster: tuple[AssigneeIdentity, ...] = ()
    reference_repos: tuple[ReferenceRepoContext, ...] = ()
    openspec_hits: tuple[OpenSpecOwnerHit, ...] = ()


def build_triage_context(
    *,
    issue: GitHubIssue,
    comments: tuple[GitHubIssueComment, ...] = (),
    prd_root: Path,
    prd_include_globs: tuple[str, ...] = ("**/*.md",),
    prd_limit: int = 5,
    roster: tuple[AssigneeIdentity, ...] = (),
    reference_repos: tuple[ReferenceRepoContext, ...] = (),
    openspec_changes: tuple[OpenSpecChange, ...] = (),
    openspec_limit: int = 3,
) -> TriageContext:
    docs = load_prd_documents(prd_root, include_globs=prd_include_globs)
    comments_text = "\n".join(comment.body for comment in comments)
    query = f"{issue.title}\n{issue.body}\n{comments_text}"
    hits = search_prd_documents(query, docs, limit=prd_limit)
    openspec_hits = score_openspec_changes(query, openspec_changes, limit=openspec_limit)
    media = (
        *extract_media_evidence(issue.body, source="issue body"),
        *(
            item
            for comment in comments
            for item in extract_media_evidence(comment.body, source=f"comment {comment.id}")
        ),
    )
    return TriageContext(
        issue=issue,
        comments=comments,
        prd_hits=hits,
        media=media,
        roster=roster,
        reference_repos=reference_repos,
        openspec_hits=openspec_hits,
    )


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
            "## Assignee Roster",
            "",
            (
                "Use this to map a free-form assignment instruction in the issue/"
                "comments (an @mention, a typed Lark or GitHub name, or a short "
                "form) to the correct GitHub login. Only assign when a name "
                "clearly matches one entry."
            ),
            "",
        ]
    )
    if not context.roster:
        lines.append("- No roster configured.")
    for identity in context.roster:
        aliases = " / ".join(identity.aliases) if identity.aliases else "(no aliases)"
        lines.append(f"- `{identity.login}` — {aliases}")
    lines.extend(
        [
            "",
            "## OpenSpec Owners",
            "",
            (
                "These OpenSpec changes match this issue and record an owner. The "
                "owner is a display-name nickname (e.g. `naohn`, `andy`), NOT a "
                "GitHub login — map it to a login via the Assignee Roster above, "
                "the same way you resolve a human's `assign to X`. When the issue "
                "clearly belongs to one of these changes, prefer that change's "
                "mapped owner as the assignee and set `owner_reason` to `OpenSpec` "
                "(priority: a human's `Manual` instruction > OpenSpec > "
                "CODEOWNERS). If no change matches, or the nickname maps to no "
                "roster entry, fall back to CODEOWNERS inference."
            ),
            "",
        ]
    )
    if not context.openspec_hits:
        lines.append("- No matching OpenSpec change with a recorded owner.")
    for hit in context.openspec_hits:
        owners = ", ".join(f"@{login}" for login in hit.owners)
        lines.append(f"- `{hit.change_id}` — {hit.title} (owners: {owners})")
        lines.append(f"  - Path: `{hit.path}` · Score: {hit.score}")
        for task in hit.matched_tasks:
            label = task.task_id or task.description[:40]
            lines.append(f"  - Task `{label}` → @{task.owner}")
    if context.reference_repos:
        lines.extend(
            [
                "",
                "## Reference Repos",
                "",
                (
                    "These sibling repos are checked out read-only for "
                    "cross-repo reasoning (e.g. a frontend bug whose backend "
                    "lives here). Cross-check them but only edit the main repo."
                ),
                "",
            ]
        )
        for ref in context.reference_repos:
            lines.append(f"- `{ref.repo}` at `{ref.path}` (branch `{ref.analyzed_branch}`)")
            if ref.purpose:
                lines.append(f"  - Purpose: {ref.purpose}")
    lines.extend(
        [
            "",
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
        if item.watermark:
            lines.append(f"  - Watermark: {_watermark_summary(item.watermark)}")
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
            continue
        if line.startswith("- watermark:"):
            watermark = line.split(":", 1)[1].strip()
            current = items[current_index]
            items[current_index] = MediaEvidence(
                kind=current.kind,
                url=current.url,
                description=current.description,
                source=current.source,
                watermark=watermark,
            )
    return tuple(items)


def _watermark_summary(watermark: str) -> str:
    """Render a stored compact watermark JSON as a readable triage line."""
    try:
        payload = json.loads(watermark)
    except ValueError:
        return watermark
    if isinstance(payload, dict):
        return render_payload_summary(payload)
    return watermark


def extract_url(value: str) -> str:
    markdown_link = MARKDOWN_LINK_RE.search(value)
    if markdown_link:
        return markdown_link.group("url")
    if value.startswith("http://") or value.startswith("https://"):
        return value.split()[0]
    return ""
