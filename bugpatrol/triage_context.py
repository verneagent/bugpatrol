"""Build deterministic triage context for agent providers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from bugpatrol.clients import GitHubIssue
from bugpatrol.prd import PrdSearchHit, load_prd_documents, search_prd_documents


@dataclass(frozen=True)
class TriageContext:
    issue: GitHubIssue
    prd_hits: tuple[PrdSearchHit, ...]


def build_triage_context(
    *,
    issue: GitHubIssue,
    prd_root: Path,
    prd_limit: int = 5,
) -> TriageContext:
    docs = load_prd_documents(prd_root)
    query = f"{issue.title}\n{issue.body}"
    hits = search_prd_documents(query, docs, limit=prd_limit)
    return TriageContext(issue=issue, prd_hits=hits)


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
        "## PRD Search Hits",
        "",
    ]
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
