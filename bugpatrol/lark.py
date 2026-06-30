"""Lark OpenAPI messenger client."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import urlencode


class LarkOpenApiError(RuntimeError):
    pass


@dataclass(frozen=True)
class SentLarkMessage:
    message_id: str


@dataclass(frozen=True)
class LarkMessage:
    message_id: str
    chat_id: str
    root_id: str
    sender_open_id: str
    sender_type: str
    create_time: str
    msg_type: str
    text: str


class LarkOpenApiMessengerClient:
    def __init__(
        self,
        *,
        app_id: str,
        app_secret: str,
        base_url: str = "https://open.larksuite.com/open-apis",
    ) -> None:
        self._app_id = app_id
        self._app_secret = app_secret
        self._base_url = base_url.rstrip("/")
        self._tenant_access_token: str | None = None

    def reply_to_message(self, *, chat_id: str, message_id: str, text: str) -> None:
        del chat_id
        self._request(
            "POST",
            f"/im/v1/messages/{message_id}/reply",
            {
                "msg_type": "text",
                "content": json.dumps({"text": text}, ensure_ascii=False),
            },
        )

    def send_chat_message(self, *, chat_id: str, text: str) -> SentLarkMessage:
        data = self._request(
            "POST",
            "/im/v1/messages?receive_id_type=chat_id",
            {
                "receive_id": chat_id,
                "msg_type": "text",
                "content": json.dumps({"text": text}, ensure_ascii=False),
            },
        )
        message_id = data.get("data", {}).get("message_id")
        if not isinstance(message_id, str) or not message_id:
            raise LarkOpenApiError(f"missing message_id in send response: {data}")
        return SentLarkMessage(message_id=message_id)

    def list_chat_messages(self, *, chat_id: str, limit: int = 20) -> list[LarkMessage]:
        query = urlencode(
            {
                "container_id_type": "chat",
                "container_id": chat_id,
                "page_size": str(limit),
                "sort_type": "ByCreateTimeDesc",
            }
        )
        data = self._request("GET", f"/im/v1/messages?{query}")
        return [parse_lark_message(item, default_chat_id=chat_id) for item in data.get("data", {}).get("items", ())]

    def get_message(self, *, message_id: str, default_chat_id: str) -> LarkMessage:
        data = self._request("GET", f"/im/v1/messages/{message_id}")
        item = data.get("data", {}).get("items")
        if isinstance(item, list) and item:
            return parse_lark_message(item[0], default_chat_id=default_chat_id)
        single = data.get("data", {}).get("message")
        if isinstance(single, dict):
            return parse_lark_message(single, default_chat_id=default_chat_id)
        raise LarkOpenApiError(f"message not found in response: {data}")

    def _tenant_token(self) -> str:
        if self._tenant_access_token:
            return self._tenant_access_token
        data = self._request_without_auth(
            "POST",
            "/auth/v3/tenant_access_token/internal",
            {"app_id": self._app_id, "app_secret": self._app_secret},
        )
        token = data.get("tenant_access_token")
        if not isinstance(token, str) or not token:
            raise LarkOpenApiError(f"missing tenant_access_token: {data}")
        self._tenant_access_token = token
        return token

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, object] | None = None,
    ) -> dict[str, object]:
        return self._request_without_auth(
            method,
            path,
            payload or {},
            headers={"Authorization": f"Bearer {self._tenant_token()}"},
        )

    def _request_without_auth(
        self,
        method: str,
        path: str,
        payload: dict[str, object],
        *,
        headers: dict[str, str] | None = None,
    ) -> dict[str, object]:
        body = None if method == "GET" else json.dumps(payload, ensure_ascii=False).encode()
        request = urllib.request.Request(
            f"{self._base_url}{path}",
            data=body,
            method=method,
            headers={"Content-Type": "application/json", **(headers or {})},
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                data = json.loads(response.read().decode())
        except urllib.error.HTTPError as error:
            detail = error.read().decode(errors="replace")
            raise LarkOpenApiError(f"Lark HTTP {error.code}: {detail}") from error
        except urllib.error.URLError as error:
            raise LarkOpenApiError(f"Lark request failed: {error}") from error
        if data.get("code") != 0:
            raise LarkOpenApiError(f"Lark API error: {data}")
        return data


def parse_lark_message(item: dict[str, object], *, default_chat_id: str) -> LarkMessage:
    body = item.get("body")
    content = ""
    if isinstance(body, dict):
        raw_content = body.get("content")
        if isinstance(raw_content, str):
            content = raw_content
    sender = item.get("sender")
    sender_id = ""
    sender_type = ""
    if isinstance(sender, dict):
        sender_type = str(sender.get("sender_type") or "")
        sender_id_data = sender.get("id")
        if isinstance(sender_id_data, dict):
            sender_id = str(sender_id_data.get("open_id") or "")
    chat_id = str(item.get("chat_id") or default_chat_id)
    message_id = str(item.get("message_id") or "")
    return LarkMessage(
        message_id=message_id,
        chat_id=chat_id,
        root_id=str(item.get("root_id") or item.get("parent_id") or message_id),
        sender_open_id=sender_id,
        sender_type=sender_type,
        create_time=str(item.get("create_time") or ""),
        msg_type=str(item.get("msg_type") or ""),
        text=_extract_text(content),
    )


def _extract_text(content: str) -> str:
    if not content:
        return ""
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        return content
    if isinstance(data, dict):
        text = data.get("text")
        if isinstance(text, str):
            return text
    return content
