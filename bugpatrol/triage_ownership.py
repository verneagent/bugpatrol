"""Issue write permissions derived from a plan snapshot and field history."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from bugpatrol.clients import GitHubIssue, GitHubIssueComment

TRIAGE_RUN_META_START = "<!-- BUGPATROL_TRIAGE_RUN_META"
TRIAGE_RUN_META_END = "BUGPATROL_TRIAGE_RUN_META -->"


@dataclass(frozen=True)
class WriteEvent:
    id: str
    field: str
    actor_type: str
    actor: str = ""


@dataclass(frozen=True)
class WriteHistory:
    events: tuple[WriteEvent, ...] = ()
    complete: bool = False


@dataclass(frozen=True)
class TriageWriteSnapshot:
    issue: GitHubIssue
    fields: dict[str, str]
    history: WriteHistory


def _new_events(previous_history: WriteHistory, current: WriteHistory, field: str) -> tuple[WriteEvent, ...] | None:
    if not previous_history.complete or not current.complete:
        return None
    previous = tuple(event for event in previous_history.events if event.field == field)
    latest = tuple(event for event in current.events if event.field == field)
    previous_ids = tuple(event.id for event in previous)
    latest_ids = tuple(event.id for event in latest)
    if (
        any(not event_id for event_id in (*previous_ids, *latest_ids))
        or len(set(latest_ids)) != len(latest_ids)
        or latest_ids[:len(previous_ids)] != previous_ids
    ):
        return None
    return latest[len(previous_ids):]


def assignment_block_reason(
    snapshot: TriageWriteSnapshot | None, current: GitHubIssue, history: WriteHistory,
) -> str | None:
    if snapshot is None:
        return "No plan snapshot; assignment is a suggestion."
    if snapshot.issue.assignees or current.assignees:
        return "An assignee already exists; preserve the current assignment."
    changes = _new_events(snapshot.history, history, "assignee")
    if changes is None:
        return "Assignment history is incomplete."
    if any(event.actor_type != "Bot" for event in changes):
        return "A human or unknown actor changed assignment after the plan watermark."
    previous = [event for event in snapshot.history.events if event.field == "assignee"]
    if previous and previous[-1].actor_type != "Bot":
        return "The last assignment decision belongs to a human or unknown actor."
    return None


def field_block_reason(
    snapshot: TriageWriteSnapshot | None, current: GitHubIssue, values: dict[str, str],
    history: WriteHistory, field: str,
) -> str | None:
    if snapshot is None:
        return "No plan snapshot; field changes are suggestions."
    before = _value(snapshot.issue, snapshot.fields, field)
    if _value(current, values, field) != before:
        return "The current value differs from the plan snapshot."
    changes = _new_events(snapshot.history, history, field)
    if changes is None:
        return "Field history is incomplete."
    if any(event.actor_type != "Bot" for event in changes):
        return "A human or unknown actor changed this field after its plan watermark."
    events = [event for event in history.events if event.field == field]
    if events:
        return None if events[-1].actor_type == "Bot" else "The latest field edit belongs to a human or unknown actor."
    if field in ("title", "issue_type") and current.created_by_type == "Bot":
        return None
    return None if not before else "The author of the existing value is unknown."


def assignment_changed_by_human(
    previous: WriteHistory, current: WriteHistory,
) -> bool:
    changes = _new_events(previous, current, "assignee")
    return changes is None or any(event.actor_type != "Bot" for event in changes)


def _value(issue: GitHubIssue, fields: dict[str, str], field: str) -> str:
    if field == "title":
        return issue.title
    if field == "issue_type":
        return issue.issue_type
    return fields.get(field, "")


def append_triage_run_metadata(metadata: dict[str, object]) -> str:
    return f"{TRIAGE_RUN_META_START}\n{json.dumps(metadata, ensure_ascii=False, sort_keys=True, indent=2)}\n{TRIAGE_RUN_META_END}"


def parse_triage_run_metadata(body: str) -> dict[str, Any] | None:
    start = body.find(TRIAGE_RUN_META_START)
    if start == -1:
        return None
    start += len(TRIAGE_RUN_META_START)
    end = body.find(TRIAGE_RUN_META_END, start)
    if end == -1:
        return None
    try:
        data = json.loads(body[start:end].strip())
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def latest_triage_run_id(comments: tuple[GitHubIssueComment, ...]) -> str:
    latest = ""
    for comment in comments:
        if comment.author_type != "Bot":
            continue
        metadata = parse_triage_run_metadata(comment.body)
        if metadata is not None and isinstance(metadata.get("run_id"), str):
            latest = metadata["run_id"]
    return latest


class TriageWriteSuperseded(RuntimeError):
    pass
