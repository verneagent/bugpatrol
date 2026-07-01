from __future__ import annotations

import json
import unittest

from bugpatrol.backfill import attachments_from_lark_message
from bugpatrol.lark_events import lark_message_from_event


class LarkEventsTest(unittest.TestCase):
    def test_lark_message_from_event_parses_text_message(self) -> None:
        message = lark_message_from_event(
            {
                "schema": "2.0",
                "event": {
                    "sender": {"sender_type": "user", "id": {"open_id": "ou_1"}},
                    "message": {
                        "message_id": "om_1",
                        "chat_id": "oc_1",
                        "root_id": "",
                        "msg_type": "text",
                        "create_time": "1000",
                        "body": {"content": json.dumps({"text": "hello"})},
                    },
                },
            }
        )

        self.assertEqual(message.message_id, "om_1")
        self.assertEqual(message.root_id, "om_1")
        self.assertEqual(message.sender_open_id, "ou_1")
        self.assertEqual(message.text, "hello")

    def test_lark_message_from_event_preserves_image_content_for_attachment_normalization(self) -> None:
        message = lark_message_from_event(
            {
                "event": {
                    "message": {
                        "message_id": "om_image",
                        "chat_id": "oc_1",
                        "msg_type": "image",
                        "body": {"content": json.dumps({"image_key": "img_v2_abc"})},
                        "sender": {"sender_type": "user", "id": {"open_id": "ou_1"}},
                    }
                },
            }
        )

        attachments = attachments_from_lark_message(message)

        self.assertEqual(message.msg_type, "image")
        self.assertEqual(message.raw_content, json.dumps({"image_key": "img_v2_abc"}))
        self.assertEqual(attachments[0].url, "lark://message/om_image/image/img_v2_abc")

    def test_lark_message_from_event_rejects_missing_message(self) -> None:
        with self.assertRaisesRegex(ValueError, "event.message"):
            lark_message_from_event({"event": {}})


if __name__ == "__main__":
    unittest.main()
