"""Lark public-mailbox client and mail attachment downloader.

The mail watcher reads `bug@fivedegrees.ai` with the bot's tenant access token
(zero user OAuth). The raw OpenAPI returns mail bodies base64url-encoded, so
the client decodes them defensively here. See docs/MAIL-INTAKE.md.
"""

from __future__ import annotations

import base64
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import quote, urlencode

from bugpatrol.lark import (
    DownloadedLarkResource,
    LarkOpenApiError,
    _filename_from_content_disposition,
    _TenantApiClient,
)

# body fields the raw API returns base64url-encoded (lark-cli decodes them
# transparently; the raw client must). Values that are not valid base64url are
# returned verbatim so a misbehaving field never blows up intake.
MAIL_BODY_FIELDS = ("body_plain_text", "body_html", "body_preview")


def decode_base64url_text(value: str) -> str:
    if not value:
        return ""
    padded = value + "=" * (-len(value) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded)
        return raw.decode("utf-8", errors="replace")
    except (ValueError, base64.binascii.Error):
        return value


@dataclass(frozen=True)
class MailAddress:
    name: str
    address: str


@dataclass(frozen=True)
class MailAttachment:
    attachment_id: str
    filename: str
    content_type: str = ""
    is_inline: bool = False
    attachment_type: int = 1


@dataclass(frozen=True)
class MailMessage:
    message_id: str
    thread_id: str
    subject: str
    head_from: MailAddress
    internal_date_ms: int
    body_preview: str = ""
    folder_id: str = ""
    message_state: str = ""
    body_plain_text: str = ""
    body_html: str = ""
    attachments: tuple[MailAttachment, ...] = ()

    @property
    def sender_address(self) -> str:
        return self.head_from.address.lower()


class MailResourceDownloader(Protocol):
    def download_attachment(
        self, *, mailbox: str, message_id: str, attachment_id: str
    ) -> DownloadedLarkResource:
        """Download one mail attachment as raw bytes."""


class LarkMailClient(_TenantApiClient):
    """Lark mail OpenAPI client (tenant identity, zero user auth)."""

    def list_messages(
        self,
        *,
        mailbox: str,
        folder_id: str = "INBOX",
        page_size: int = 20,
        page_token: str = "",
    ) -> tuple[list[MailMessage], bool, str]:
        """List mail in `folder_id`. Returns (items, has_more, next_page_token).

        `page_size` is required by the API (max 20); callers paginate on
        `has_more` / `next_page_token`.
        """
        params = {"page_size": str(page_size), "folder_id": folder_id}
        if page_token:
            params["page_token"] = page_token
        data = self._request(
            "GET",
            f"/mail/v1/user_mailboxes/{quote(mailbox, safe='')}/messages?{urlencode(params)}",
        )
        payload = data.get("data")
        if not isinstance(payload, dict):
            raise LarkOpenApiError(f"mail list response missing data: {data}")
        # The list endpoint returns only base64-encoded message IDs (no headers
        # or bodies); each full message is fetched separately via get_message.
        items = [
            MailMessage(
                message_id=message_id,
                thread_id="",
                subject="",
                head_from=MailAddress(name="", address=""),
                internal_date_ms=0,
            )
            for message_id in payload.get("items", ())
            if isinstance(message_id, str) and message_id
        ]
        has_more = bool(payload.get("has_more"))
        next_token = str(payload.get("page_token") or "")
        return items, has_more, next_token

    def get_message(self, *, mailbox: str, message_id: str, format: str = "full") -> MailMessage:
        """Fetch a full message (decoded body + attachment list)."""
        data = self._request(
            "GET",
            f"/mail/v1/user_mailboxes/{quote(mailbox, safe='')}/messages/"
            f"{quote(message_id, safe='')}?format={format}",
        )
        payload = data.get("data")
        message = payload.get("message") if isinstance(payload, dict) else None
        if not isinstance(message, dict):
            raise LarkOpenApiError(f"mail message not found in response: {data}")
        return _parse_mail_message(message, full=True)

    def download_attachment(
        self, *, mailbox: str, message_id: str, attachment_id: str
    ) -> DownloadedLarkResource:
        """Resolve the signed download URL for an attachment and fetch it."""
        query = urlencode({"attachment_ids": attachment_id})
        data = self._request(
            "GET",
            f"/mail/v1/user_mailboxes/{quote(mailbox, safe='')}/messages/"
            f"{quote(message_id, safe='')}/attachments/download_url?{query}",
        )
        payload = data.get("data")
        urls = payload.get("download_urls") if isinstance(payload, dict) else None
        found = next(
            (
                item
                for item in urls
                if isinstance(item, dict)
                and item.get("attachment_id") == attachment_id
                and isinstance(item.get("download_url"), str)
                and item["download_url"]
            ),
            None,
        )
        if found is None:
            failed = (payload or {}).get("failed_ids") or []
            raise LarkOpenApiError(
                f"no download url for attachment {attachment_id} (failed_ids={failed})"
            )
        return self._download_signed_url(str(found["download_url"]))

    def _download_signed_url(self, url: str) -> DownloadedLarkResource:
        request = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                content = response.read()
                headers = response.headers
        except urllib.error.HTTPError as error:
            detail = error.read().decode(errors="replace")
            raise LarkOpenApiError(f"mail attachment download HTTP {error.code}: {detail}") from error
        except urllib.error.URLError as error:
            raise LarkOpenApiError(f"mail attachment download failed: {error}") from error
        return DownloadedLarkResource(
            content=content,
            content_type=headers.get("Content-Type", ""),
            filename=_filename_from_content_disposition(headers.get("Content-Disposition", "")),
        )


def _parse_mail_message(item: dict[str, object], *, full: bool = False) -> MailMessage:
    address = item.get("head_from")
    if address is None:
        address = item.get("from")
    head_from = _parse_mail_address(address)
    message_id = _str(item, "message_id")
    if not message_id:
        raise LarkOpenApiError(f"mail message missing message_id: {item}")
    internal_date = _int_ms(item, "internal_date")
    message = MailMessage(
        message_id=message_id,
        thread_id=_str(item, "thread_id"),
        subject=_str(item, "subject"),
        head_from=head_from,
        internal_date_ms=internal_date,
        body_preview=decode_base64url_text(_str(item, "body_preview")),
        folder_id=_str(item, "folder_id"),
        message_state=str(item.get("message_state") or ""),
        body_plain_text=decode_base64url_text(_str(item, "body_plain_text")),
        body_html=decode_base64url_text(_str(item, "body_html")) if full else "",
        attachments=_parse_mail_attachments(item.get("attachments")),
    )
    return message


def _parse_mail_attachments(raw: object) -> tuple[MailAttachment, ...]:
    if not isinstance(raw, list):
        return ()
    result: list[MailAttachment] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        attachment_id = _str(item, "id")
        if not attachment_id:
            continue
        result.append(
            MailAttachment(
                attachment_id=attachment_id,
                filename=_str(item, "filename"),
                content_type=_str(item, "content_type"),
                is_inline=bool(item.get("is_inline") or False),
                attachment_type=int(item.get("attachment_type") or 1),
            )
        )
    return tuple(result)


def _parse_mail_address(value: object) -> MailAddress:
    if isinstance(value, dict):
        return MailAddress(
            name=_str(value, "name"),
            address=_str(value, "mail_address"),
        )
    if isinstance(value, list) and value and isinstance(value[0], dict):
        return _parse_mail_address(value[0])
    return MailAddress(name="", address="")


def _str(item: dict[str, object], key: str) -> str:
    value = item.get(key)
    return value if isinstance(value, str) else ""


def _int_ms(item: dict[str, object], key: str) -> int:
    value = item.get(key)
    if isinstance(value, str) and value.isdigit():
        return int(value)
    if isinstance(value, int):
        return value
    # The list API may name the timestamp create_time instead.
    alt = item.get("create_time")
    if isinstance(alt, str) and alt.isdigit():
        return int(alt)
    if isinstance(alt, int):
        return alt
    return 0
