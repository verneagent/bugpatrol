"""Classify recurrence using report and effective delivery evidence."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Literal

DuplicateClassification = Literal[
    "delivery_incomplete", "pre_fix_duplicate", "confirmed_regression",
    "old_build_duplicate", "needs_verification",
]
FIX_STATES = ("effective", "missing", "reverted", "unknown")
CONTENT_CHECKS = ("content", "behavior", "ancestry", "patch_id", "unknown")


@dataclass(frozen=True)
class ComponentDeliveryEvidence:
    repository: str
    reported_build: str
    build_evidence: str
    reported_fix: str
    build_predates_fix: bool
    content_check: str
    content_evidence: str
    current_delivery: str
    delivered_at: str
    delivery_evidence: str


@dataclass(frozen=True)
class DuplicateEvidence:
    environment: str
    environment_evidence: str
    occurred_at: str
    occurrence_evidence: str
    required_repositories: tuple[str, ...]
    components: tuple[ComponentDeliveryEvidence, ...]
    conflicts: tuple[str, ...]


def parse_duplicate_evidence(value: Any) -> DuplicateEvidence | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("duplicate_evidence must be an object or null")
    components = value.get("components")
    if not isinstance(components, list):
        raise ValueError("duplicate_evidence.components must be an array")
    parsed = []
    for component in components:
        if not isinstance(component, dict):
            raise ValueError("duplicate_evidence component must be an object")
        reported_fix = _text(component, "reported_fix")
        current_delivery = _text(component, "current_delivery")
        content_check = _text(component, "content_check")
        if reported_fix not in FIX_STATES or current_delivery not in FIX_STATES:
            raise ValueError("duplicate_evidence contains an invalid fix state")
        if content_check not in CONTENT_CHECKS:
            raise ValueError("duplicate_evidence contains an invalid content check")
        predates = component.get("build_predates_fix")
        if not isinstance(predates, bool):
            raise ValueError("duplicate_evidence.build_predates_fix must be a boolean")
        parsed.append(ComponentDeliveryEvidence(
            repository=_text(component, "repository"),
            reported_build=_text(component, "reported_build"),
            build_evidence=_text(component, "build_evidence"),
            reported_fix=reported_fix,
            build_predates_fix=predates,
            content_check=content_check,
            content_evidence=_text(component, "content_evidence"),
            current_delivery=current_delivery,
            delivered_at=_text(component, "delivered_at"),
            delivery_evidence=_text(component, "delivery_evidence"),
        ))
    return DuplicateEvidence(
        environment=_text(value, "environment"),
        environment_evidence=_text(value, "environment_evidence"),
        occurred_at=_text(value, "occurred_at"),
        occurrence_evidence=_text(value, "occurrence_evidence"),
        required_repositories=_texts(value, "required_repositories"),
        components=tuple(parsed),
        conflicts=_texts(value, "conflicts"),
    )


def duplicate_evidence_payload(evidence: DuplicateEvidence | None) -> dict[str, Any] | None:
    if evidence is None:
        return None
    return {
        "environment": evidence.environment,
        "environment_evidence": evidence.environment_evidence,
        "occurred_at": evidence.occurred_at,
        "occurrence_evidence": evidence.occurrence_evidence,
        "required_repositories": list(evidence.required_repositories),
        "components": [asdict(component) for component in evidence.components],
        "conflicts": list(evidence.conflicts),
    }


def classify_duplicate_evidence(evidence: DuplicateEvidence | None) -> DuplicateClassification:
    unknown: DuplicateClassification = "needs_verification"
    if evidence is None or evidence.conflicts or not evidence.environment or not evidence.environment_evidence:
        return unknown
    required = set(evidence.required_repositories)
    by_repo = {component.repository: component for component in evidence.components}
    if (
        not required or "" in required or len(required) != len(evidence.required_repositories)
        or len(by_repo) != len(evidence.components) or not set(by_repo).issubset(required)
    ):
        return unknown
    # A proven gap in any required component takes precedence over timestamps.
    if any(
        component.current_delivery in ("missing", "reverted") and component.delivery_evidence
        for component in evidence.components
    ):
        return "delivery_incomplete"
    if set(by_repo) != required:
        return unknown
    delivery_times = []
    for component in evidence.components:
        delivered_at = _timestamp(component.delivered_at)
        if (
            component.current_delivery != "effective" or not component.delivery_evidence
            or delivered_at is None or not component.reported_build or not component.build_evidence
            or component.reported_fix not in ("effective", "missing")
            or component.content_check not in ("content", "behavior") or not component.content_evidence
            or (component.reported_fix == "effective" and component.build_predates_fix)
        ):
            return unknown
        delivery_times.append(delivered_at)
    occurred_at = _timestamp(evidence.occurred_at)
    if occurred_at is None or not evidence.occurrence_evidence:
        return unknown
    if any(
        component.reported_fix == "effective" and occurred_at <= delivered_at
        for component, delivered_at in zip(evidence.components, delivery_times, strict=True)
    ):
        return unknown
    effective_since = max(delivery_times)
    if occurred_at < effective_since:
        return "pre_fix_duplicate"
    if occurred_at == effective_since:
        return unknown
    if all(component.reported_fix == "effective" for component in evidence.components):
        return "confirmed_regression"
    if all(
        component.reported_fix == "effective" or component.build_predates_fix
        for component in evidence.components
    ):
        return "old_build_duplicate"
    return unknown


def _timestamp(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None and parsed.utcoffset() is not None else None


def _text(data: dict[str, Any], name: str) -> str:
    value = data.get(name)
    if not isinstance(value, str):
        raise ValueError(f"duplicate_evidence.{name} must be a string")
    return value.strip()


def _texts(data: dict[str, Any], name: str) -> tuple[str, ...]:
    value = data.get(name)
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"duplicate_evidence.{name} must be an array of strings")
    return tuple(item.strip() for item in value)


def duplicate_evidence_schema() -> dict[str, Any]:
    component_properties = {
        "repository": {"type": "string"},
        "reported_build": {"type": "string"},
        "build_evidence": {"type": "string", "description": "Source identifying the actual build used by this report."},
        "reported_fix": {"type": "string", "enum": list(FIX_STATES)},
        "build_predates_fix": {"type": "boolean", "description": "True only when build evidence establishes this is an older build preceding the fix."},
        "content_check": {"type": "string", "enum": list(CONTENT_CHECKS)},
        "content_evidence": {"type": "string", "description": "Source or behavior check of the fix in the actual reported build, including any revert or counteracting change. Ancestry or patch-id alone is insufficient."},
        "current_delivery": {"type": "string", "enum": list(FIX_STATES)},
        "delivered_at": {"type": "string", "description": "Timezone-aware time the effective fix reached this report's environment, not merge or issue-close time. Empty when unknown."},
        "delivery_evidence": {"type": "string", "description": "Evidence of the actual current deployment and still-effective fix in the report's environment, or of a missing/reverted delivery."},
    }
    properties = {
        "environment": {"type": "string"},
        "environment_evidence": {"type": "string"},
        "occurred_at": {"type": "string", "description": "Original occurrence timestamp with timezone, never intake or triage time. Empty when unknown."},
        "occurrence_evidence": {"type": "string"},
        "required_repositories": {"type": "array", "items": {"type": "string"}, "description": "All repositories whose fixes must be delivered together, including reference backends."},
        "components": {
            "type": "array",
            "items": {
                "type": "object", "additionalProperties": False,
                "required": list(component_properties), "properties": component_properties,
            },
        },
        "conflicts": {"type": "array", "items": {"type": "string"}, "description": "References to inconsistent evidence; any conflict requires manual verification."},
    }
    return {
        "type": ["object", "null"], "additionalProperties": False,
        "required": list(properties), "properties": properties,
        "description": "Report and delivery evidence for duplicate_of; use null when unavailable. Never infer a regression from a closed issue, merge, ancestry or reporting time alone.",
    }
