"""Ownership resolution from CODEOWNERS."""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CodeownersRule:
    pattern: str
    owners: tuple[str, ...]


def parse_codeowners(text: str) -> tuple[CodeownersRule, ...]:
    rules: list[CodeownersRule] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        pattern = parts[0]
        if pattern.startswith("!"):
            continue
        owners = tuple(part for part in parts[1:] if part.startswith("@"))
        if owners:
            rules.append(CodeownersRule(pattern=pattern, owners=owners))
    return tuple(rules)


def load_codeowners(repo_path: Path) -> tuple[CodeownersRule, ...]:
    for relative in (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS"):
        path = repo_path / relative
        if path.exists():
            return parse_codeowners(path.read_text())
    return ()


def resolve_owners(path: str, rules: tuple[CodeownersRule, ...]) -> tuple[str, ...]:
    normalized = _normalize_path(path)
    owners: tuple[str, ...] = ()
    for rule in rules:
        if _matches(rule.pattern, normalized):
            owners = rule.owners
    return owners


def resolve_first_owner(path: str, rules: tuple[CodeownersRule, ...]) -> str:
    owners = resolve_owners(path, rules)
    return owners[0].lstrip("@") if owners else ""


def _normalize_path(path: str) -> str:
    return path.strip().lstrip("/")


def _matches(pattern: str, path: str) -> bool:
    raw = pattern.strip()
    anchored = raw.startswith("/")
    pattern = raw.lstrip("/")
    if pattern == "*":
        return True
    if pattern.endswith("/"):
        prefix = pattern.rstrip("/") + "/"
        return path.startswith(prefix)
    if "/" not in pattern and not anchored:
        return fnmatch.fnmatch(path.rsplit("/", 1)[-1], pattern)
    return fnmatch.fnmatch(path, pattern)

