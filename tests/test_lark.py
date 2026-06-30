from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock, patch

from bugpatrol.lark import LarkOpenApiMessengerClient


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


if __name__ == "__main__":
    unittest.main()

