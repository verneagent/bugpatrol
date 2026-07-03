from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from bugpatrol.lease import FileLease, LeaseHeldError, read_lease_info


class LeaseTest(unittest.TestCase):
    def test_file_lease_acquire_refresh_and_release(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "watch.lock"
            lease = FileLease(path, ttl_seconds=60, owner="test-owner")

            info = lease.acquire(now=100)
            self.assertEqual(info.owner, "test-owner")
            self.assertTrue(path.exists())

            refreshed = lease.refresh(now=120)
            self.assertEqual(refreshed.expires_at, 180)
            self.assertEqual(read_lease_info(path), refreshed)

            lease.release()
            self.assertFalse(path.exists())

    def test_file_lease_rejects_active_holder(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "watch.lock"
            first = FileLease(path, ttl_seconds=60, owner="first")
            second = FileLease(path, ttl_seconds=60, owner="second")

            first.acquire(now=100)

            with self.assertRaisesRegex(LeaseHeldError, "first"):
                second.acquire(now=120)

    def test_file_lease_can_take_over_expired_holder(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "watch.lock"
            first = FileLease(path, ttl_seconds=60, owner="first")
            second = FileLease(path, ttl_seconds=60, owner="second")

            first.acquire(now=100)
            info = second.acquire(now=161)

            self.assertEqual(info.owner, "second")


if __name__ == "__main__":
    unittest.main()
