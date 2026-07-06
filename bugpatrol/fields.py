"""Canonical GitHub field definitions and triage output schema."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class FieldSpec:
    name: str
    values: tuple[str, ...]
    description: str


NATIVE_ISSUE_TYPES = ("Bug", "Feature", "Task")


def default_field_specs() -> dict[str, FieldSpec]:
    specs = [
        FieldSpec("Priority", ("Urgent", "High", "Medium", "Low"), "Impact and urgency."),
        FieldSpec(
            "Triage status",
            ("Pending", "Running", "Needs info", "Needs review", "Done", "Failed", "Skipped"),
            "Automation state for triage.",
        ),
        FieldSpec("Source", ("Lark", "GitHub", "Manual", "Import"), "Where the issue came from."),
        FieldSpec("Intake version", ("v2", "manual", "unknown"), "Which intake path created it."),
        FieldSpec(
            "Triage verdict",
            ("代码 Bug", "PRD 错误", "PRD 缺失", "Case 错误", "信息不足", "预期行为"),
            "The product/engineering verdict after triage.",
        ),
        FieldSpec("Platform", ("iOS", "Android", "Web", "Desktop", "多平台", "未知"), "Affected platform."),
        FieldSpec("Reproducibility", ("必现", "偶发", "仅一次", "未知"), "How often it reproduces."),
        FieldSpec(
            "Other platforms",
            ("其他平台正常", "其他平台也异常", "未验证", "不适用"),
            "Whether other platforms are affected.",
        ),
        FieldSpec(
            "Capability",
            ("Auth", "Quest", "Buddy", "Match", "Message", "Me", "Contacts", "Notifications", "Unknown"),
            "Product capability area.",
        ),
        FieldSpec("Evidence", ("截图", "视频", "日志", "文字描述", "多种", "无"), "Evidence provided."),
        FieldSpec("PRD status", ("已对齐", "PRD 错误", "PRD 缺失", "未校验"), "PRD comparison result."),
        FieldSpec("Triage confidence", ("高", "中", "低"), "Confidence in the triage result."),
        FieldSpec(
            "Owner reason",
            ("CODEOWNERS", "Lark @mention", "Git history", "Capability fallback", "Manual"),
            "Why the dev owner was selected.",
        ),
    ]
    return {spec.name: spec for spec in specs}


TRIAGE_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "issue_type",
        "triage_verdict",
        "priority",
        "triage_status",
        "platform",
        "reproducibility",
        "other_platforms",
        "capability",
        "evidence",
        "prd_status",
        "triage_confidence",
        "assignee",
        "owner_reason",
        "prd_refs",
        "likely_locations",
        "summary_cn",
        "affected_branch",
        "blame_suggestion",
        "follow_up_questions",
        "comment_markdown",
    ],
    "properties": {
        "issue_type": {"type": "string", "enum": list(NATIVE_ISSUE_TYPES)},
        "triage_verdict": {"type": "string", "enum": list(default_field_specs()["Triage verdict"].values)},
        "priority": {"type": "string", "enum": list(default_field_specs()["Priority"].values)},
        "triage_status": {"type": "string", "enum": list(default_field_specs()["Triage status"].values)},
        "platform": {"type": "string", "enum": list(default_field_specs()["Platform"].values)},
        "reproducibility": {"type": "string", "enum": list(default_field_specs()["Reproducibility"].values)},
        "other_platforms": {"type": "string", "enum": list(default_field_specs()["Other platforms"].values)},
        "capability": {"type": "string", "enum": list(default_field_specs()["Capability"].values)},
        "evidence": {"type": "string", "enum": list(default_field_specs()["Evidence"].values)},
        "prd_status": {"type": "string", "enum": list(default_field_specs()["PRD status"].values)},
        "triage_confidence": {"type": "string", "enum": list(default_field_specs()["Triage confidence"].values)},
        "assignee": {"type": "string", "minLength": 1},
        "owner_reason": {"type": "string", "enum": list(default_field_specs()["Owner reason"].values)},
        "prd_refs": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["title", "url", "section"],
                "properties": {
                    "title": {"type": "string"},
                    "url": {"type": "string"},
                    "section": {"type": "string"},
                },
            },
        },
        "likely_locations": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["repo", "path", "reason"],
                "properties": {
                    "repo": {"type": "string"},
                    "path": {"type": "string"},
                    "reason": {"type": "string"},
                },
            },
        },
        "summary_cn": {"type": "string", "minLength": 1},
        "affected_branch": {
            "type": "string",
            "description": (
                "Branch the bug was observed on. Must be a concrete branch name matching one of "
                "the project's allowed branch patterns; use an empty string when the branch "
                "cannot be determined from the report."
            ),
        },
        "blame_suggestion": {
            "type": "string",
            "description": "Best-effort person, team, PR, commit, or code area that may have introduced the issue. Use an empty string when unknown.",
        },
        "follow_up_questions": {"type": "array", "items": {"type": "string"}},
        "comment_markdown": {"type": "string", "minLength": 1},
    },
}


def triage_output_schema(*, branch_patterns: tuple[str, ...] = ()) -> dict[str, Any]:
    """Return the triage output schema, specialized with project branch rules."""
    schema = copy.deepcopy(TRIAGE_OUTPUT_SCHEMA)
    if branch_patterns:
        schema["properties"]["affected_branch"]["description"] = (
            "Branch the bug was observed on. Must be a concrete branch name matching one of "
            f"these patterns: {', '.join(branch_patterns)}. Use an empty string when the "
            "branch cannot be determined from the report."
        )
    return schema


def validate_field_value(field_name: str, value: str, specs: dict[str, FieldSpec] | None = None) -> None:
    field_specs = specs or default_field_specs()
    spec = field_specs.get(field_name)
    if spec is None:
        raise ValueError(f"unknown field: {field_name}")
    if value not in spec.values:
        raise ValueError(f"invalid value for {field_name}: {value}")
