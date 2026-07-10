"""Ownership resolution from CODEOWNERS."""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from pathlib import Path

from bugpatrol.config import OwnersConfig


@dataclass(frozen=True)
class CodeownersRule:
    pattern: str
    owners: tuple[str, ...]


# Matches a "Name (@login)" pair inside a CODEOWNERS comment header, e.g.
# "#   Naohn      (@naohn42)     — match".
_CODEOWNERS_IDENTITY_RE = re.compile(r"(?P<name>\S+)\s*\(@(?P<login>[\w-]+)\)")


def parse_codeowners_identities(text: str) -> dict[str, tuple[str, ...]]:
    """Derive login -> display name from CODEOWNERS comment headers.

    The team roster is documented once, in the CODEOWNERS header, as
    "Name (@login) — area" lines. Reuse that as the primary alias source so
    the roster does not duplicate names the project already maintains.
    """
    identities: dict[str, tuple[str, ...]] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line.startswith("#"):
            continue
        for match in _CODEOWNERS_IDENTITY_RE.finditer(line):
            login = match.group("login")
            name = match.group("name")
            if login not in identities and name != login:
                identities[login] = (name,)
    return identities


def load_codeowners_identities(repo_path: Path) -> dict[str, tuple[str, ...]]:
    for relative in (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS"):
        path = repo_path / relative
        if path.exists():
            return parse_codeowners_identities(path.read_text())
    return {}


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


def resolve_configured_owners(
    *,
    path: str,
    capability: str = "",
    owners: OwnersConfig,
) -> tuple[str, ...]:
    normalized = _normalize_path(path)
    matched: tuple[str, ...] = ()
    for pattern, pattern_owners in (owners.paths or {}).items():
        if _matches(pattern, normalized):
            matched = pattern_owners
    if matched:
        return normalize_owner_handles(matched)
    if capability and capability in (owners.capabilities or {}):
        return normalize_owner_handles((owners.capabilities or {})[capability])
    return normalize_owner_handles(owners.default)


def normalize_owner_handles(owners: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(owner if owner.startswith("@") else f"@{owner}" for owner in owners if owner)


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
