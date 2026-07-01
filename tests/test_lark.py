from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock, patch

from bugpatrol.lark import LarkOpenApiMessengerClient, parse_lark_message


class LarkOpenApiMessengerClientTest(unittest.TestCase):
    def test_send_chat_message_gets_token_and_posts_text(self) -> None:
        client = LarkOpenApiMessengerClient(app_id="app", app_secret="secret")

        with patch("urllib.request.urlopen") as urlopen:
            token_response = MagicMock()
            token_response.__enter__.return_value.read.return_value = json.dumps(
                {"code": 0, "tenant_access_token": "token"}
            ).encode()
            send_response = MagicMock()
            send_response.__enter__.return_value.read.return_value = json.dumps(
                {"code": 0, "data": {"message_id": "om_1"}}
            ).encode()
            urlopen.side_effect = [token_response, send_response]

            sent = client.send_chat_message(chat_id="oc_1", text="hello")

        self.assertEqual(sent.message_id, "om_1")
        send_request = urlopen.call_args_list[1].args[0]
        self.assertIn("/im/v1/messages?receive_id_type=chat_id", send_request.full_url)
        self.assertEqual(send_request.get_header("Authorization"), "Bearer token")
        self.assertIn("hello", send_request.data.decode())

    def test_reply_to_message_uses_reply_endpoint(self) -> None:
        client = LarkOpenApiMessengerClient(app_id="app", app_secret="secret")

        with patch("urllib.request.urlopen") as urlopen:
            token_response = MagicMock()
            token_response.__enter__.return_value.read.return_value = json.dumps(
                {"code": 0, "tenant_access_token": "token"}
            ).encode()
            reply_response = MagicMock()
            reply_response.__enter__.return_value.read.return_value = json.dumps({"code": 0}).encode()
            urlopen.side_effect = [token_response, reply_response]

            client.reply_to_message(chat_id="oc_1", message_id="om_1", text="done")

        reply_request = urlopen.call_args_list[1].args[0]
        self.assertIn("/im/v1/messages/om_1/reply", reply_request.full_url)
        self.assertIn("done", reply_request.data.decode())

    def test_list_chat_messages_parses_text_history(self) -> None:
        client = LarkOpenApiMessengerClient(app_id="app", app_secret="secret")

        with patch("urllib.request.urlopen") as urlopen:
            token_response = MagicMock()
            token_response.__enter__.return_value.read.return_value = json.dumps(
                {"code": 0, "tenant_access_token": "token"}
            ).encode()
            history_response = MagicMock()
            history_response.__enter__.return_value.read.return_value = json.dumps(
                {
                    "code": 0,
                    "data": {
                        "items": [
                            {
                                "message_id": "om_1",
                                "root_id": "",
                                "chat_id": "oc_1",
                                "msg_type": "text",
                                "create_time": "1000",
                                "sender": {
                                    "sender_type": "user",
                                    "id": {"open_id": "ou_1"},
                                },
                                "body": {"content": json.dumps({"text": "hello"})},
                            }
                        ]
                    },
                }
            ).encode()
            urlopen.side_effect = [token_response, history_response]

            messages = client.list_chat_messages(chat_id="oc_1", limit=5)

        self.assertEqual(messages[0].message_id, "om_1")
        self.assertEqual(messages[0].root_id, "om_1")
        self.assertEqual(messages[0].sender_open_id, "ou_1")
        self.assertEqual(messages[0].text, "hello")
        self.assertEqual(messages[0].raw_content, json.dumps({"text": "hello"}))
        self.assertIn("page_size=5", urlopen.call_args_list[1].args[0].full_url)

    def test_parse_lark_message_falls_back_to_raw_content(self) -> None:
        parsed = parse_lark_message(
            {
                "message_id": "om_1",
                "msg_type": "text",
                "body": {"content": "not json"},
            },
            default_chat_id="oc_1",
        )

        self.assertEqual(parsed.text, "not json")

    def test_get_message_parses_single_message_items_shape(self) -> None:
        client = LarkOpenApiMessengerClient(app_id="app", app_secret="secret")

        with patch("urllib.request.urlopen") as urlopen:
            token_response = MagicMock()
            token_response.__enter__.return_value.read.return_value = json.dumps(
                {"code": 0, "tenant_access_token": "token"}
            ).encode()
            message_response = MagicMock()
            message_response.__enter__.return_value.read.return_value = json.dumps(
                {
                    "code": 0,
                    "data": {
                        "items": [
                            {
                                "message_id": "om_1",
                                "chat_id": "oc_1",
                                "msg_type": "text",
                                "sender": {"id": {"open_id": "ou_1"}},
                                "body": {"content": json.dumps({"text": "hello"})},
                            }
                        ]
                    },
                }
            ).encode()
            urlopen.side_effect = [token_response, message_response]

            parsed = client.get_message(message_id="om_1", default_chat_id="oc_1")

        self.assertEqual(parsed.message_id, "om_1")
        self.assertEqual(parsed.text, "hello")
        self.assertIn("/im/v1/messages/om_1", urlopen.call_args_list[1].args[0].full_url)

    def test_download_message_resource_reads_binary_response(self) -> None:
        client = LarkOpenApiMessengerClient(app_id="app", app_secret="secret")

        with patch("urllib.request.urlopen") as urlopen:
            token_response = MagicMock()
            token_response.__enter__.return_value.read.return_value = json.dumps(
                {"code": 0, "tenant_access_token": "token"}
            ).encode()
            resource_response = MagicMock()
            resource_context = resource_response.__enter__.return_value
            resource_context.read.return_value = b"image-bytes"
            resource_context.headers = {
                "Content-Type": "image/png",
                "Content-Disposition": 'attachment; filename="bug.png"',
            }
            urlopen.side_effect = [token_response, resource_response]

            downloaded = client.download_message_resource(
                message_id="om_1",
                resource_key="img_v2_abc",
            )

        self.assertEqual(downloaded.content, b"image-bytes")
        self.assertEqual(downloaded.content_type, "image/png")
        self.assertEqual(downloaded.filename, "bug.png")
        resource_request = urlopen.call_args_list[1].args[0]
        self.assertIn("/im/v1/messages/om_1/resources/img_v2_abc", resource_request.full_url)
        self.assertEqual(resource_request.get_header("Authorization"), "Bearer token")


if __name__ == "__main__":
    unittest.main()
