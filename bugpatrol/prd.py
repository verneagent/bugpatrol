"""Local PRD document indexing and search."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PrdDocument:
    path: str
    title: str
    text: str


@dataclass(frozen=True)
class PrdSearchHit:
    path: str
    title: str
    score: int
    excerpt: str


def load_prd_documents(root: Path) -> tuple[PrdDocument, ...]:
    docs: list[PrdDocument] = []
    if not root.exists():
        return ()
    for path in sorted(root.rglob("*.md")):
        text = path.read_text()
        docs.append(
            PrdDocument(
                path=str(path.relative_to(root)),
                title=_title_from_markdown(text, fallback=path.stem),
                text=text,
            )
        )
    return tuple(docs)


def search_prd_documents(query: str, documents: tuple[PrdDocument, ...], *, limit: int = 5) -> tuple[PrdSearchHit, ...]:
    terms = _terms(query)
    if not terms:
        return ()
    hits: list[PrdSearchHit] = []
    for doc in documents:
        lower = doc.text.lower()
        score = sum(lower.count(term) for term in terms)
        if score <= 0:
            continue
        hits.append(
            PrdSearchHit(
                path=doc.path,
                title=doc.title,
                score=score,
                excerpt=_excerpt(doc.text, terms),
            )
        )
    hits.sort(key=lambda hit: (-hit.score, hit.path))
    return tuple(hits[:limit])


def _title_from_markdown(text: str, *, fallback: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
    return fallback


def _terms(query: str) -> tuple[str, ...]:
    return tuple(term.lower() for term in re.findall(r"[\w\u4e00-\u9fff]+", query) if len(term) >= 2)


def _excerpt(text: str, terms: tuple[str, ...]) -> str:
    lower = text.lower()
    index = min((lower.find(term) for term in terms if term in lower), default=0)
    start = max(0, index - 80)
    end = min(len(text), index + 180)
    excerpt = re.sub(r"\s+", " ", text[start:end]).strip()
    if start > 0:
        excerpt = "..." + excerpt
    if end < len(text):
        excerpt += "..."
    return excerpt
