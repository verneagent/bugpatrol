from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from bugpatrol.ledger import JsonMessageLedger


class LedgerTest(unittest.TestCase):
    def test_json_message_ledger_persists_processed_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "processed.json"
            ledger = JsonMessageLedger.load(path)

            self.assertFalse(ledger.is_processed("om_1"))
            ledger.mark_processed("om_1")

            loaded = JsonMessageLedger.load(path)
            self.assertTrue(loaded.is_processed("om_1"))
            self.assertFalse(loaded.is_processed("om_2"))


if __name__ == "__main__":
    unittest.main()
