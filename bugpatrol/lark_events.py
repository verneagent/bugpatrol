"""Normalize Lark event payloads into LarkMessage objects."""

from __future__ import annotations

from typing import Any

from bugpatrol.lark import LarkMessage, parse_lark_message


def lark_message_from_event(payload: dict[str, Any], *, default_chat_id: str = "") -> LarkMessage:
    event = _dict(payload.get("event"), "event")
    message = dict(_dict(event.get("message"), "event.message"))
    if "sender" not in message and isinstance(event.get("sender"), dict):
        message["sender"] = event["sender"]
    return parse_lark_message(message, default_chat_id=default_chat_id)


def _dict(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return value
