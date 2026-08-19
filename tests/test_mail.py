from __future__ import annotations

import base64
import json
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock, patch

from bugpatrol.config import MailConfig, load_project_config
from bugpatrol.intake import (
    Attachment,
    IntakeRecord,
    parse_intake_metadata,
    render_issue_body,
    resolve_reply_target,
    update_intake_metadata,
)
from bugpatrol.mail import (
    LarkMailClient,
    MailAddress,
    MailAttachment,
    MailMessage,
    decode_base64url_text,
)
from bugpatrol.watch_mail import (
    _mail_attachment_kind,
    _mail_attachment_url,
    build_mail_record,
)


def _b64(text: str) -> str:
    """URL-safe base64 of UTF-8 text, as the raw mail API encodes bodies."""
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode()


def make_mail_config(user_emails: dict[str, str] | None = None) -> object:
    base = load_project_config(Path("projects/example.toml"))
    return replace(
        base,
        mail=MailConfig(
            mailbox="bug@fivedegrees.ai",
            chat_id="oc_mail",
            app_id="cli_mail_app",
            app_secret_env="MAIL_APP_SECRET",
            user_emails=user_emails,
        ),
    )


def make_mail(**overrides: object) -> MailMessage:
    values = {
        "message_id": "mail_1",
        "thread_id": "thread_1",
        "subject": "登录后白屏",
        "head_from": MailAddress(name="客户张三", address="zhangsan@example.com"),
        "internal_date_ms": 1783099728900,
        "body_plain_text": "登录后白屏，无报错",
    }
    values.update(overrides)
    return MailMessage(**values)  # type: ignore[arg-type]


class Base64UrlDecodeTest(unittest.TestCase):
    def test_decodes_padded_value(self) -> None:
        self.assertEqual(decode_base64url_text(_b64("hello")), "hello")

    def test_decodes_unpadded_value(self) -> None:
        self.assertEqual(decode_base64url_text("aGVsbG8"), "hello")

    def test_decodes_unicode(self) -> None:
        self.assertEqual(decode_base64url_text(_b64("登录后白屏")), "登录后白屏")

    def test_empty_string_stays_empty(self) -> None:
        self.assertEqual(decode_base64url_text(""), "")

    def test_invalid_base64url_is_returned_verbatim(self) -> None:
        self.assertEqual(decode_base64url_text("!!!not-base64!!!"), "!!!not-base64!!!")


class MailClientParsingTest(unittest.TestCase):
    def _client(self) -> LarkMailClient:
        return LarkMailClient(app_id="app", app_secret="secret")

    def test_list_messages_parses_item_and_quotes_mailbox(self) -> None:
        client = self._client()

        with patch("urllib.request.urlopen") as urlopen:
            urlopen.side_effect = [
                self._json({"code": 0, "tenant_access_token": "token"}),
                self._json(
                    {
                        "code": 0,
                        "data": {
                            "items": [
                                {
                                    "message_id": "mail/1",
                                    "thread_id": "thread_1",
                                    "subject": "登录后白屏",
                                    "head_from": {"name": "张三", "mail_address": "zhangsan@example.com"},
                                    "folder_id": "INBOX",
                                    "internal_date": "1783099728900",
                                    "message_state": "received",
                                    "body_preview": _b64("登录后白屏"),
                                }
                            ],
                            "has_more": False,
                            "page_token": "",
                        },
                    }
                ),
            ]

            items, has_more, next_token = client.list_messages(
                mailbox="bug@fivedegrees.ai",
                page_size=10,
            )

        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item.message_id, "mail/1")
        self.assertEqual(item.thread_id, "thread_1")
        self.assertEqual(item.subject, "登录后白屏")
        self.assertEqual(item.head_from.name, "张三")
        self.assertEqual(item.head_from.address, "zhangsan@example.com")
        self.assertEqual(item.internal_date_ms, 1783099728900)
        self.assertEqual(item.body_preview, "登录后白屏")
        self.assertFalse(has_more)
        self.assertEqual(next_token, "")
        url = urlopen.call_args_list[1].args[0].full_url
        self.assertIn("/mail/v1/user_mailboxes/bug%40fivedegrees.ai/messages", url)
        self.assertIn("page_size=10", url)
        self.assertIn("folder_id=INBOX", url)

    def test_list_messages_falls_back_to_from_list_and_create_time(self) -> None:
        client = self._client()

        with patch("urllib.request.urlopen") as urlopen:
            urlopen.side_effect = [
                self._json({"code": 0, "tenant_access_token": "token"}),
                self._json(
                    {
                        "code": 0,
                        "data": {
                            "items": [
                                {
                                    "message_id": "mail_2",
                                    "thread_id": "thread_2",
                                    "subject": "crash",
                                    "from": [{"name": "李四", "mail_address": "lisi@example.com"}],
                                    "create_time": "1783099700000",
                                }
                            ],
                            "has_more": False,
                            "page_token": "",
                        },
                    }
                ),
            ]

            items, _, _ = client.list_messages(mailbox="bug@fivedegrees.ai", page_size=10)

        item = items[0]
        self.assertEqual(item.head_from.name, "李四")
        self.assertEqual(item.head_from.address, "lisi@example.com")
        self.assertEqual(item.internal_date_ms, 1783099700000)

    def test_get_message_decodes_body_and_parses_attachments(self) -> None:
        client = self._client()

        with patch("urllib.request.urlopen") as urlopen:
            urlopen.side_effect = [
                self._json({"code": 0, "tenant_access_token": "token"}),
                self._json(
                    {
                        "code": 0,
                        "data": {
                            "message": {
                                "message_id": "mail/1",
                                "thread_id": "thread_1",
                                "subject": "崩溃日志",
                                "head_from": {"name": "王五", "mail_address": "wangwu@example.com"},
                                "internal_date": "1783099728900",
                                "body_plain_text": _b64("崩溃日志如下："),
                                "body_html": _b64("<p>hi</p>"),
                                "attachments": [
                                    {
                                        "id": "attach_1",
                                        "filename": "crash.log",
                                        "content_type": "text/plain",
                                        "is_inline": False,
                                        "attachment_type": 1,
                                    },
                                    {
                                        "id": "attach_2",
                                        "filename": "signature.png",
                                        "content_type": "image/png",
                                        "is_inline": True,
                                        "attachment_type": 1,
                                    },
                                ],
                            }
                        },
                    }
                ),
            ]

            message = client.get_message(mailbox="bug@fivedegrees.ai", message_id="mail/1")

        self.assertEqual(message.body_plain_text, "崩溃日志如下：")
        self.assertEqual(message.body_html, "<p>hi</p>")
        self.assertEqual(
            message.attachments,
            (
                MailAttachment(
                    attachment_id="attach_1",
                    filename="crash.log",
                    content_type="text/plain",
                    is_inline=False,
                    attachment_type=1,
                ),
                MailAttachment(
                    attachment_id="attach_2",
                    filename="signature.png",
                    content_type="image/png",
                    is_inline=True,
                    attachment_type=1,
                ),
            ),
        )
        url = urlopen.call_args_list[1].args[0].full_url
        # message ids contain '/' and '='; both must be percent-encoded.
        self.assertIn("/mail/v1/user_mailboxes/bug%40fivedegrees.ai/messages/mail%2F1", url)
        self.assertIn("format=full", url)

    def test_download_attachment_fetches_signed_url(self) -> None:
        client = self._client()
        signed_response = MagicMock()
        signed_response.__enter__.return_value.read.return_value = b"PNG-BYTES"
        signed_response.__enter__.return_value.headers = {
            "Content-Type": "image/png",
            "Content-Disposition": 'attachment; filename="shot.png"',
        }

        with patch("urllib.request.urlopen") as urlopen:
            urlopen.side_effect = [
                self._json({"code": 0, "tenant_access_token": "token"}),
                self._json(
                    {
                        "code": 0,
                        "data": {
                            "download_urls": [
                                {"attachment_id": "attach_1", "download_url": "https://cdn.example/signed?x=1"}
                            ],
                            "failed_ids": [],
                        },
                    }
                ),
                signed_response,
            ]

            resource = client.download_attachment(
                mailbox="bug@fivedegrees.ai",
                message_id="mail/1",
                attachment_id="attach_1",
            )

        self.assertEqual(resource.content, b"PNG-BYTES")
        self.assertEqual(resource.content_type, "image/png")
        self.assertEqual(resource.filename, "shot.png")
        url = urlopen.call_args_list[1].args[0].full_url
        self.assertIn("attachment_ids=attach_1", url)
        self.assertIn("mail%2F1", url)

    def _json(self, payload: dict[str, object]) -> MagicMock:
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(payload).encode()
        return response


class MailIntakeHelpersTest(unittest.TestCase):
    def test_resolve_reply_target_prefers_anchor(self) -> None:
        self.assertEqual(
            resolve_reply_target(
                {"chat_id": "oc_mail", "message_id": "mail_1", "notify_anchor_message_id": "om_anchor"}
            ),
            ("oc_mail", "om_anchor"),
        )

    def test_resolve_reply_target_falls_back_to_message_id(self) -> None:
        self.assertEqual(
            resolve_reply_target({"chat_id": "oc_mail", "message_id": "mail_1"}),
            ("oc_mail", "mail_1"),
        )

    def test_resolve_reply_target_returns_empty_without_chat(self) -> None:
        self.assertEqual(resolve_reply_target({"message_id": "mail_1"}), ("", ""))

    def test_update_intake_metadata_merges_and_preserves(self) -> None:
        body = render_issue_body(
            IntakeRecord(
                reporter_name="张三",
                reporter_open_id="mail:zhangsan@example.com",
                created_at="1783099728900",
                chat_id="oc_mail",
                root_id="thread_1",
                message_id="mail_1",
                original_text="登录后白屏",
            ),
            language="zh-CN",
            source="mail",
        )

        updated = update_intake_metadata(body, {"notify_anchor_message_id": "om_anchor"})

        metadata = parse_intake_metadata(updated)
        assert metadata is not None
        self.assertEqual(metadata["notify_anchor_message_id"], "om_anchor")
        self.assertEqual(metadata["message_id"], "mail_1")
        self.assertEqual(metadata["source"], "mail")
        self.assertIn("## Lark 上报", updated)

    def test_update_intake_metadata_rejects_missing_marker(self) -> None:
        with self.assertRaisesRegex(ValueError, "BUGPATROL_INTAKE_META"):
            update_intake_metadata("no meta here", {"notify_anchor_message_id": "om_anchor"})

    def test_render_issue_body_source_mail(self) -> None:
        body = render_issue_body(
            IntakeRecord(
                reporter_name="张三",
                reporter_open_id="mail:zhangsan@example.com",
                created_at="1783099728900",
                chat_id="oc_mail",
                root_id="thread_1",
                message_id="mail_1",
                original_text="登录后白屏",
            ),
            language="zh-CN",
            source="mail",
        )

        self.assertIn('"source":"mail"', body)

    def test_build_mail_record_maps_message_to_intake_record(self) -> None:
        config = make_mail_config()
        mail = make_mail()
        attachments = (Attachment(kind="screenshot", url="https://assets/s.png"),)

        record = build_mail_record(config=config, mail=mail, body_text=mail.body_plain_text, attachments=attachments)

        self.assertEqual(record.chat_id, "oc_mail")
        self.assertEqual(record.root_id, "thread_1")
        self.assertEqual(record.message_id, "mail_1")
        self.assertEqual(record.reporter_open_id, "mail:zhangsan@example.com")
        self.assertEqual(record.reporter_name, "客户张三")
        self.assertEqual(record.created_at, "1783099728900")
        self.assertEqual(record.attachments, attachments)
        # The subject leads the text so the issue title derives from it.
        self.assertEqual(record.original_text, "登录后白屏\n\n登录后白屏，无报错")

    def test_build_mail_record_resolves_configured_user_email_case_insensitively(self) -> None:
        config = make_mail_config(user_emails={"ZhanGSan@Example.COM": "ou_zhangsan"})
        mail = make_mail(head_from=MailAddress(name="张三", address="zhangsan@example.com"))

        record = build_mail_record(config=config, mail=mail, body_text="", attachments=())

        self.assertEqual(record.reporter_open_id, "ou_zhangsan")

    def test_build_mail_record_falls_back_to_name_for_reporter(self) -> None:
        config = make_mail_config()
        mail = make_mail(head_from=MailAddress(name="", address="noreply@example.com"))

        record = build_mail_record(config=config, mail=mail, body_text="", attachments=())

        self.assertEqual(record.reporter_name, "noreply@example.com")
        self.assertEqual(record.reporter_open_id, "mail:noreply@example.com")

    def test_mail_attachment_kind_classifies_by_content_and_extension(self) -> None:
        self.assertEqual(
            _mail_attachment_kind(MailAttachment("a", "shot.png", content_type="image/png")),
            "screenshot",
        )
        self.assertEqual(
            _mail_attachment_kind(MailAttachment("b", "demo.mp4", content_type="video/mp4")),
            "video",
        )
        self.assertEqual(
            _mail_attachment_kind(MailAttachment("c", "crash.log", content_type="text/plain")),
            "log",
        )
        self.assertEqual(
            _mail_attachment_kind(MailAttachment("d", "notes.txt", content_type="")),
            "log",
        )
        self.assertEqual(
            _mail_attachment_kind(MailAttachment("e", "report.pdf", content_type="application/pdf")),
            "file",
        )

    def test_mail_attachment_url_percent_encodes_message_id(self) -> None:
        url = _mail_attachment_url("Ab3/xyz=+pq", "attach_1")
        self.assertEqual(url, "lark://message/Ab3%2Fxyz%3D%2Bpq/mail/attach_1")


if __name__ == "__main__":
    unittest.main()
