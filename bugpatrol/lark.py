"""Lark OpenAPI messenger client."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import urlencode
from uuid import uuid4


class LarkOpenApiError(RuntimeError):
    pass


# Markdown link, e.g. [#3890](https://github.com/...). Messages containing one
# are sent as rich-text ("post") so the link renders as clickable custom text.
_MD_LINK_RE = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")
_AT_TAG_RE = re.compile(r'<at user_id="([^"]+)">([^<]*)</at>')


def build_post_content(text: str) -> dict[str, object]:
    """Convert message text with markdown links / <at> tags to Lark post content."""
    paragraphs: list[list[dict[str, object]]] = []
    for line in text.split("\n"):
        runs: list[dict[str, object]] = []
        pos = 0
        for match in re.finditer(f"{_MD_LINK_RE.pattern}|{_AT_TAG_RE.pattern}", line):
            if match.start() > pos:
                runs.append({"tag": "text", "text": line[pos : match.start()]})
            if match.group(1) is not None:
                runs.append({"tag": "a", "text": match.group(1), "href": match.group(2)})
            else:
                runs.append({"tag": "at", "user_id": match.group(3)})
            pos = match.end()
        if pos < len(line):
            runs.append({"tag": "text", "text": line[pos:]})
        paragraphs.append(runs)
    return {"zh_cn": {"content": paragraphs}}


# Lark error code returned when acting on a withdrawn/recalled message.
MESSAGE_WITHDRAWN_CODE = 230011


def is_message_withdrawn_error(error: Exception) -> bool:
    return isinstance(error, LarkOpenApiError) and f'"code":{MESSAGE_WITHDRAWN_CODE}' in str(error)


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
    raw_content: str = ""
    sender_id: str = ""
    sender_id_type: str = ""
    # open_ids of people @mentioned in this message, used to resolve
    # `/assign @person` deterministically.
    mention_open_ids: tuple[str, ...] = ()
    # True when the message was withdrawn/recalled in Lark.
    deleted: bool = False


@dataclass(frozen=True)
class DownloadedLarkResource:
    content: bytes
    content_type: str
    filename: str


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
        if _MD_LINK_RE.search(text):
            self._reply_to_message(
                message_id=message_id,
                msg_type="post",
                content=build_post_content(text),
            )
            return
        self._reply_to_message(
            message_id=message_id,
            msg_type="text",
            content={"text": text},
        )

    def reply_image_to_message(self, *, chat_id: str, message_id: str, image_key: str) -> SentLarkMessage:
        del chat_id
        data = self._reply_to_message(
            message_id=message_id,
            msg_type="image",
            content={"image_key": image_key},
        )
        reply_message_id = data.get("data", {}).get("message_id")
        if not isinstance(reply_message_id, str) or not reply_message_id:
            raise LarkOpenApiError(f"missing message_id in reply response: {data}")
        return SentLarkMessage(message_id=reply_message_id)

    def _reply_to_message(self, *, message_id: str, msg_type: str, content: dict[str, object]) -> dict[str, object]:
        return self._request(
            "POST",
            f"/im/v1/messages/{message_id}/reply",
            {
                "msg_type": msg_type,
                "content": json.dumps(content, ensure_ascii=False),
            },
        )

    def send_chat_message(self, *, chat_id: str, text: str) -> SentLarkMessage:
        return self._send_chat_message(
            chat_id=chat_id,
            msg_type="text",
            content={"text": text},
        )

    def upload_image(self, *, filename: str, content: bytes) -> str:
        boundary = f"----bugpatrol-{uuid4().hex}"
        body = _multipart_form_data(
            boundary=boundary,
            fields={"image_type": "message"},
            files={
                "image": (
                    filename,
                    content,
                    "image/png",
                )
            },
        )
        data = self._request_raw(
            "POST",
            "/im/v1/images",
            body,
            headers={
                "Authorization": f"Bearer {self._tenant_token()}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
        )
        image_key = data.get("data", {}).get("image_key")
        if not isinstance(image_key, str) or not image_key:
            raise LarkOpenApiError(f"missing image_key in upload response: {data}")
        return image_key

    def send_chat_image(self, *, chat_id: str, image_key: str) -> SentLarkMessage:
        return self._send_chat_message(
            chat_id=chat_id,
            msg_type="image",
            content={"image_key": image_key},
        )

    def _send_chat_message(self, *, chat_id: str, msg_type: str, content: dict[str, object]) -> SentLarkMessage:
        data = self._request(
            "POST",
            "/im/v1/messages?receive_id_type=chat_id",
            {
                "receive_id": chat_id,
                "msg_type": msg_type,
                "content": json.dumps(content, ensure_ascii=False),
            },
        )
        message_id = data.get("data", {}).get("message_id")
        if not isinstance(message_id, str) or not message_id:
            raise LarkOpenApiError(f"missing message_id in send response: {data}")
        return SentLarkMessage(message_id=message_id)

    def list_chat_messages(self, *, chat_id: str, limit: int = 20) -> list[LarkMessage]:
        messages: list[LarkMessage] = []
        page_token = ""
        while len(messages) < limit:
            params = {
                "container_id_type": "chat",
                "container_id": chat_id,
                "page_size": str(min(limit - len(messages), 50)),
                "sort_type": "ByCreateTimeDesc",
            }
            if page_token:
                params["page_token"] = page_token
            data = self._request("GET", f"/im/v1/messages?{urlencode(params)}")
            payload = data.get("data", {})
            messages.extend(
                parse_lark_message(item, default_chat_id=chat_id) for item in payload.get("items", ())
            )
            page_token = payload.get("page_token") or ""
            if not payload.get("has_more") or not page_token:
                break
        return messages

    def get_message(self, *, message_id: str, default_chat_id: str) -> LarkMessage:
        data = self._request("GET", f"/im/v1/messages/{message_id}")
        item = data.get("data", {}).get("items")
        if isinstance(item, list) and item:
            return parse_lark_message(item[0], default_chat_id=default_chat_id)
        single = data.get("data", {}).get("message")
        if isinstance(single, dict):
            return parse_lark_message(single, default_chat_id=default_chat_id)
        raise LarkOpenApiError(f"message not found in response: {data}")

    def download_message_resource(
        self,
        *,
        message_id: str,
        resource_key: str,
        resource_type: str = "",
    ) -> DownloadedLarkResource:
        query = f"?{urlencode({'type': resource_type})}" if resource_type else ""
        request = urllib.request.Request(
            f"{self._base_url}/im/v1/messages/{message_id}/resources/{resource_key}{query}",
            method="GET",
            headers={"Authorization": f"Bearer {self._tenant_token()}"},
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                content = response.read()
                headers = response.headers
        except urllib.error.HTTPError as error:
            detail = error.read().decode(errors="replace")
            raise LarkOpenApiError(f"Lark HTTP {error.code}: {detail}") from error
        except urllib.error.URLError as error:
            raise LarkOpenApiError(f"Lark request failed: {error}") from error
        return DownloadedLarkResource(
            content=content,
            content_type=headers.get("Content-Type", ""),
            filename=_filename_from_content_disposition(headers.get("Content-Disposition", "")),
        )

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
        return self._request_raw(
            method,
            path,
            body,
            headers={"Content-Type": "application/json", **(headers or {})},
        )

    def _request_raw(
        self,
        method: str,
        path: str,
        body: bytes | None,
        *,
        headers: dict[str, str],
    ) -> dict[str, object]:
        request = urllib.request.Request(
            f"{self._base_url}{path}",
            data=body,
            method=method,
            headers=headers,
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
    sender_open_id = ""
    sender_raw_id = ""
    sender_id_type = ""
    sender_type = ""
    if isinstance(sender, dict):
        sender_type = str(sender.get("sender_type") or "")
        sender_id_type = str(sender.get("id_type") or "")
        sender_id_data = sender.get("id")
        if isinstance(sender_id_data, dict):
            sender_open_id = str(sender_id_data.get("open_id") or "")
            if sender_open_id:
                sender_raw_id = sender_open_id
                sender_id_type = sender_id_type or "open_id"
            else:
                for key in ("app_id", "user_id", "union_id"):
                    value = sender_id_data.get(key)
                    if isinstance(value, str) and value:
                        sender_raw_id = value
                        sender_id_type = sender_id_type or key
                        break
        elif isinstance(sender_id_data, str):
            sender_raw_id = sender_id_data
            if sender_id_type in {"open_id", "user_open_id"}:
                sender_open_id = sender_id_data
    chat_id = str(item.get("chat_id") or default_chat_id)
    message_id = str(item.get("message_id") or "")
    return LarkMessage(
        message_id=message_id,
        chat_id=chat_id,
        root_id=str(item.get("root_id") or item.get("parent_id") or message_id),
        sender_open_id=sender_open_id,
        sender_type=sender_type,
        create_time=str(item.get("create_time") or ""),
        msg_type=str(item.get("msg_type") or ""),
        text=_resolve_mentions(_extract_text(content), item.get("mentions")),
        raw_content=content,
        sender_id=sender_raw_id,
        sender_id_type=sender_id_type,
        mention_open_ids=_mention_open_ids(item.get("mentions")),
        deleted=bool(item.get("deleted")),
    )


def _mention_open_ids(mentions: object) -> tuple[str, ...]:
    """Collect the open_ids of @mentioned users (order preserved, de-duped)."""
    if not isinstance(mentions, list):
        return ()
    open_ids: list[str] = []
    for mention in mentions:
        if not isinstance(mention, dict):
            continue
        identity = mention.get("id")
        open_id = ""
        if isinstance(identity, dict):
            open_id = str(identity.get("open_id") or "")
        elif isinstance(identity, str):
            open_id = identity
        if open_id and open_id not in open_ids:
            open_ids.append(open_id)
    return tuple(open_ids)


def _resolve_mentions(text: str, mentions: object) -> str:
    """Replace Lark @mention placeholders (@_user_1, ...) with the real name.

    Lark stores @mentions in message text as opaque placeholders and puts the
    resolved identities in a sibling `mentions` array. Without this, an
    instruction like "这个给 @Naohn" reaches triage as "这个给@_user_1", so the
    "Lark @mention" owner path can never fire.
    """
    if not text or not isinstance(mentions, list):
        return text
    for mention in mentions:
        if not isinstance(mention, dict):
            continue
        key = mention.get("key")
        name = mention.get("name")
        if isinstance(key, str) and key and isinstance(name, str) and name:
            text = text.replace(key, f"@{name}")
    return text


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
        post_text = _extract_post_text(data)
        if post_text:
            return post_text
    return content


def _extract_post_text(data: object) -> str:
    parts: list[str] = []

    def visit(value: object) -> None:
        if isinstance(value, dict):
            if value.get("tag") == "text" and isinstance(value.get("text"), str):
                text = str(value["text"]).strip()
                if text:
                    parts.append(text)
                return
            for child in value.values():
                visit(child)
            return
        if isinstance(value, list):
            for child in value:
                visit(child)

    for key in ("content", "content_v2"):
        visit(data.get(key))
    return "\n".join(dict.fromkeys(parts))


def _filename_from_content_disposition(value: str) -> str:
    for part in value.split(";"):
        part = part.strip()
        if part.startswith("filename="):
            return part.removeprefix("filename=").strip('"')
    return ""


def _multipart_form_data(
    *,
    boundary: str,
    fields: dict[str, str],
    files: dict[str, tuple[str, bytes, str]],
) -> bytes:
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.extend(
            [
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                value.encode(),
                b"\r\n",
            ]
        )
    for name, (filename, content, content_type) in files.items():
        chunks.extend(
            [
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'.encode(),
                f"Content-Type: {content_type}\r\n\r\n".encode(),
                content,
                b"\r\n",
            ]
        )
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks)
