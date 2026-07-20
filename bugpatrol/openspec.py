"""Parse OpenSpec change owners for triage assignment.

OpenSpec organizes work into "changes" (`openspec/changes/<id>/`), each with a
`tasks.md`. Fived records the owner of each task as a trailing ``· @login`` and,
optionally, a change-level ``> **Owner**：… @login`` header. Triage prefers this
owner over CODEOWNERS path inference when an issue maps to a change.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


# GitHub login: alphanumeric or single hyphens, must start/end alphanumeric.
_LOGIN = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?"
# Owner marker: a middot separator then ``@login`` (the fived tasks.md convention).
# Anchored on ``·`` so an unrelated ``@mention`` inside a description never matches.
_OWNER_RE = re.compile(rf"·\s*@({_LOGIN})")
# A markdown task line: ``- [ ] body`` or ``- [x] body``.
_TASK_RE = re.compile(r"^\s*-\s*\[(?P<mark>[ xX])\]\s*(?P<body>.+?)\s*$")
# Leading ``task-id:`` slug at the start of a task body.
_TASK_ID_RE = re.compile(r"^(?P<id>[a-z0-9][a-z0-9-]*):")
# Role/placeholder tokens some changes use in place of a real person (e.g. a
# ``> **Owner**：全部 · @owner`` template line). They are not people, so drop
# them — surfacing them would only add noise the agent cannot map to a login.
_PLACEHOLDER_OWNERS = frozenset({"owner", "owners", "team", "all", "tbd", "todo"})


@dataclass(frozen=True)
class OpenSpecTask:
    task_id: str
    description: str
    owner: str  # GitHub login without '@', "" when unannotated
    done: bool


@dataclass(frozen=True)
class OpenSpecChange:
    change_id: str
    title: str
    path: str  # tasks.md path relative to the openspec root
    default_owner: str  # from the ``> **Owner**`` header, "" when absent
    tasks: tuple[OpenSpecTask, ...]
    text: str  # full tasks.md text, for relevance scoring

    def owners(self) -> tuple[str, ...]:
        """Distinct owner nicknames referenced by this change (header first).

        These are the short handles authors write as ``· @name`` — display-name
        nicknames, not necessarily GitHub logins. Deduped case-insensitively
        (first-seen casing wins); the triage agent maps each to a real login via
        the Assignee Roster.
        """
        ordered: dict[str, str] = {}
        for name in (self.default_owner, *(task.owner for task in self.tasks)):
            key = name.lower()
            if name and key not in ordered:
                ordered[key] = name
        return tuple(ordered.values())


@dataclass(frozen=True)
class OpenSpecOwnerHit:
    change_id: str
    title: str
    path: str
    score: int
    owners: tuple[str, ...]
    matched_tasks: tuple[OpenSpecTask, ...]


def load_openspec_changes(openspec_root: Path) -> tuple[OpenSpecChange, ...]:
    """Load every ``changes/**/tasks.md`` under an openspec root.

    Returns () when the root has no ``changes`` directory, so projects that do
    not use OpenSpec (or a PRD cache that isn't openspec-shaped) are a no-op.
    """
    changes_root = openspec_root / "changes"
    if not changes_root.is_dir():
        return ()
    changes: list[OpenSpecChange] = []
    for tasks_path in sorted(changes_root.rglob("tasks.md")):
        text = tasks_path.read_text()
        if not text.strip():
            continue
        change_dir = tasks_path.parent
        default_owner, tasks = _parse_tasks(text)
        changes.append(
            OpenSpecChange(
                change_id=change_dir.name,
                title=_change_title(change_dir, fallback=change_dir.name),
                path=str(tasks_path.relative_to(openspec_root)),
                default_owner=default_owner,
                tasks=tasks,
                text=text,
            )
        )
    return tuple(changes)


def score_openspec_changes(
    query: str, changes: tuple[OpenSpecChange, ...], *, limit: int = 3
) -> tuple[OpenSpecOwnerHit, ...]:
    """Rank changes by term overlap with the issue, keeping owner-bearing hits.

    Only changes that both match the query and record at least one owner are
    returned — a change with no owner cannot inform assignment.
    """
    terms = _terms(query)
    if not terms:
        return ()
    hits: list[OpenSpecOwnerHit] = []
    for change in changes:
        owners = change.owners()
        if not owners:
            continue
        haystack = f"{change.title}\n{change.text}".lower()
        score = sum(haystack.count(term) for term in terms)
        if score <= 0:
            continue
        matched = tuple(
            task for task in change.tasks if task.owner and _task_matches(task, terms)
        )
        hits.append(
            OpenSpecOwnerHit(
                change_id=change.change_id,
                title=change.title,
                path=change.path,
                score=score,
                owners=owners,
                matched_tasks=matched,
            )
        )
    hits.sort(key=lambda hit: (-hit.score, hit.path))
    return tuple(hits[:limit])


def _parse_tasks(text: str) -> tuple[str, tuple[OpenSpecTask, ...]]:
    default_owner = ""
    tasks: list[OpenSpecTask] = []
    for raw_line in text.splitlines():
        task_match = _TASK_RE.match(raw_line)
        if task_match:
            body = task_match.group("body")
            owner_match = _OWNER_RE.findall(body)
            id_match = _TASK_ID_RE.match(body)
            tasks.append(
                OpenSpecTask(
                    task_id=id_match.group("id") if id_match else "",
                    description=body,
                    owner=_clean_owner(owner_match[-1]) if owner_match else "",
                    done=task_match.group("mark") in ("x", "X"),
                )
            )
            continue
        if not default_owner and "owner" in raw_line.lower():
            header_owner = _OWNER_RE.search(raw_line)
            if header_owner:
                default_owner = _clean_owner(header_owner.group(1))
    return default_owner, tuple(tasks)


def _clean_owner(name: str) -> str:
    return "" if name.lower() in _PLACEHOLDER_OWNERS else name


def _change_title(change_dir: Path, *, fallback: str) -> str:
    proposal = change_dir / "proposal.md"
    if proposal.is_file():
        for line in proposal.read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith("# "):
                return stripped[2:].strip()
    return fallback


def _task_matches(task: OpenSpecTask, terms: tuple[str, ...]) -> bool:
    haystack = f"{task.task_id} {task.description}".lower()
    return any(term in haystack for term in terms)


def _terms(query: str) -> tuple[str, ...]:
    return tuple(
        term.lower()
        for term in re.findall(r"[\w\u4e00-\u9fff]+", query)
        if len(term) >= 2
    )
