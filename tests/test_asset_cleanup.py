from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from bugpatrol.asset_cleanup import cleanup_asset_repo


class AssetCleanupTest(unittest.TestCase):
    def test_dry_run_lists_matching_asset_paths_without_deleting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            checkout = Path(tmp)
            base = checkout / ".github" / "issue-assets"
            (base / "om_test_1").mkdir(parents=True)
            (base / "om_keep").mkdir()
            (base / "om_test_1" / "bug.png").write_bytes(b"png")

            result = cleanup_asset_repo(
                checkout_path=checkout,
                base_path=".github/issue-assets",
                message_id_prefix="om_test",
            )

            self.assertEqual(result.scanned, 2)
            self.assertEqual(result.matched, 1)
            self.assertEqual(result.deleted, 0)
            self.assertTrue((base / "om_test_1" / "bug.png").exists())

    def test_delete_removes_matching_asset_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            checkout = Path(tmp)
            base = checkout / ".github" / "issue-assets"
            (base / "om_test_1").mkdir(parents=True)
            (base / "om_keep").mkdir()

            result = cleanup_asset_repo(
                checkout_path=checkout,
                base_path=".github/issue-assets",
                message_id_prefix="om_test",
                delete=True,
            )

            self.assertEqual(result.scanned, 2)
            self.assertEqual(result.matched, 1)
            self.assertEqual(result.deleted, 1)
            self.assertFalse((base / "om_test_1").exists())
            self.assertTrue((base / "om_keep").exists())

    def test_delete_with_push_commits_and_pushes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            checkout = Path(tmp)
            base = checkout / ".github" / "issue-assets"
            (base / "om_test_1").mkdir(parents=True)
            run = Mock()
            run.return_value.returncode = 0
            run.return_value.stdout = ""
            run.return_value.stderr = ""

            with patch("bugpatrol.asset_cleanup.subprocess.run", run):
                cleanup_asset_repo(
                    checkout_path=checkout,
                    base_path=".github/issue-assets",
                    message_id_prefix="om_test",
                    delete=True,
                    push=True,
                    branch="main",
                    remote_url="origin",
                )

            commands = [call.args[0] for call in run.call_args_list]
            self.assertEqual(commands[0], ["git", "-C", str(checkout), "add", "-A"])
            self.assertEqual(commands[1][:5], ["git", "-C", str(checkout), "commit", "--no-verify"])
            self.assertEqual(commands[2], ["git", "-C", str(checkout), "push", "--no-verify", "origin", "main"])


if __name__ == "__main__":
    unittest.main()
