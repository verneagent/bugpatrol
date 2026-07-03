from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bugpatrol.intake import Attachment, IntakeRecord
from bugpatrol.triage_queue import (
    CommandTriageDispatcher,
    TriageRequestQueue,
    classify_triage_signal,
)


def make_record(**overrides: object) -> IntakeRecord:
    values = {
        "reporter_name": "Diego",
        "reporter_open_id": "ou_reporter",
        "created_at": "2026-07-03T10:00:00Z",
        "chat_id": "oc_test",
        "root_id": "om_root",
        "message_id": "om_msg",
        "original_text": "补充：安卓也会卡住",
        "attachments": (),
    }
    values.update(overrides)
    return IntakeRecord(**values)  # type: ignore[arg-type]


class TriageQueueTest(unittest.TestCase):
    def test_classifies_new_issue_as_triage_material(self) -> None:
        signal = classify_triage_signal("created", make_record())

        self.assertTrue(signal.should_enqueue)
        self.assertEqual(signal.reason, "intake_created")
        self.assertEqual(signal.material_message_ids, ("om_msg",))

    def test_classifies_acknowledgement_as_non_actionable(self) -> None:
        signal = classify_triage_signal("updated", make_record(original_text="收到"))

        self.assertFalse(signal.should_enqueue)
        self.assertEqual(signal.reason, "acknowledgement")

    def test_classifies_attachment_followup_as_material(self) -> None:
        signal = classify_triage_signal(
            "updated",
            make_record(
                attachments=(Attachment(kind="image", url="https://assets.test/s.png"),),
            ),
        )

        self.assertTrue(signal.should_enqueue)
        self.assertEqual(signal.reason, "material_followup")
        self.assertEqual(signal.asset_urls, ("https://assets.test/s.png",))

    def test_queue_coalesces_issue_requests_and_extends_due_at(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            queue_path = Path(temp) / "triage-queue.json"
            queue = TriageRequestQueue.load(queue_path)

            first = queue.enqueue(
                issue_number=7,
                signal=classify_triage_signal("created", make_record(message_id="om_1")),
                quiet_seconds=60,
                now=100,
            )
            second = queue.enqueue(
                issue_number=7,
                signal=classify_triage_signal("updated", make_record(message_id="om_2")),
                quiet_seconds=60,
                now=120,
            )

            self.assertIsNotNone(first)
            self.assertIsNotNone(second)
            self.assertEqual(second.due_at, 180)
            self.assertEqual(second.material_message_ids, ("om_1", "om_2"))
            self.assertEqual(queue.due_requests(now=179), ())
            self.assertEqual(queue.due_requests(now=180), (second,))

            loaded = TriageRequestQueue.load(queue_path)
            self.assertEqual(loaded.due_requests(now=180)[0].trigger_fingerprint, second.trigger_fingerprint)

    def test_dispatcher_formats_command_placeholders(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            queue = TriageRequestQueue.load(Path(temp) / "triage-queue.json")
            request = queue.enqueue(
                issue_number=9,
                signal=classify_triage_signal("created", make_record(message_id="om_9")),
                quiet_seconds=0,
                now=100,
            )
            self.assertIsNotNone(request)

            dispatcher = CommandTriageDispatcher(
                [
                    "gh",
                    "workflow",
                    "run",
                    "bugpatrol-triage.yml",
                    "-f",
                    "issue_number={issue_number}",
                    "-f",
                    "trigger_fingerprint={trigger_fingerprint}",
                    "-f",
                    "reason={reason}",
                ]
            )
            with patch("bugpatrol.triage_queue.subprocess.run") as run:
                run.return_value.returncode = 0
                result = dispatcher.dispatch(request)

            self.assertEqual(result.issue_number, 9)
            self.assertEqual(run.call_args.args[0][5], "issue_number=9")
            self.assertEqual(run.call_args.args[0][7], f"trigger_fingerprint={request.trigger_fingerprint}")
            self.assertEqual(run.call_args.args[0][9], "reason=intake_created")


if __name__ == "__main__":
    unittest.main()
