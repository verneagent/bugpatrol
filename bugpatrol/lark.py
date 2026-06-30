"""Lark OpenAPI messenger client."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass


class LarkOpenApiError(RuntimeError):
    pass


@dataclass(frozen=True)
class SentLarkMessage:
    message_id: str


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

    def _request(self, method: str, path: str, payload: dict[str, object]) -> dict[str, object]:
        return self._request_without_auth(
            method,
            path,
            payload,
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
        body = json.dumps(payload, ensure_ascii=False).encode()
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

