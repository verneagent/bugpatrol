from __future__ import annotations

import unittest
from pathlib import Path

from bugpatrol.chat_discovery import (
    CachedBranchChatDiscoverer,
    apply_branch_chats,
    discover_branch_chats,
)
from bugpatrol.config import load_project_config


CONFIG = load_project_config(Path("projects/todo-sandbox.toml"))
MAIN_CHAT_ID = CONFIG.lark.chat_id


class FakeChatLister:
    def __init__(self, chats: tuple[tuple[str, str], ...]) -> None:
        self.chats = chats
        self.calls = 0

    def list_bot_chats(self) -> tuple[tuple[str, str], ...]:
        self.calls += 1
        return self.chats


class FakeBranchLister:
    def __init__(self, branches: tuple[str, ...]) -> None:
        self.branches = branches

    def list_remote_branch_names(self, *, repo: str) -> tuple[str, ...]:
        del repo
        return self.branches


class DiscoverBranchChatsTest(unittest.TestCase):
    def test_chat_named_after_a_live_branch_is_adopted(self) -> None:
        discovery = discover_branch_chats(
            lark=FakeChatLister((("oc_moments", "feature-moments"),)),
            github=FakeBranchLister(("main", "feature-moments")),
            config=CONFIG,
        )

        self.assertEqual(discovery.branch_chats, {"oc_moments": "feature-moments"})

    def test_chat_whose_branch_is_gone_is_not_adopted(self) -> None:
        discovery = discover_branch_chats(
            lark=FakeChatLister((("oc_stale", "feature-deleted"),)),
            github=FakeBranchLister(("main", "feature-moments")),
            config=CONFIG,
        )

        self.assertEqual(discovery.branch_chats, {})
        self.assertEqual(discovery.unmatched_chats, ("feature-deleted",))

    def test_unrelated_chats_and_the_main_chat_are_never_adopted(self) -> None:
        discovery = discover_branch_chats(
            lark=FakeChatLister(
                (
                    (MAIN_CHAT_ID, "main"),
                    ("oc_random", "Agent 体验反馈"),
                    ("oc_mainish", "main"),
                )
            ),
            github=FakeBranchLister(("main", "feature-moments")),
            config=CONFIG,
        )

        self.assertEqual(discovery.branch_chats, {})

    def test_chat_name_is_matched_after_stripping_whitespace(self) -> None:
        discovery = discover_branch_chats(
            lark=FakeChatLister((("oc_moments", " feature-moments "),)),
            github=FakeBranchLister(("feature-moments",)),
            config=CONFIG,
        )

        self.assertEqual(discovery.branch_chats, {"oc_moments": "feature-moments"})


class ApplyBranchChatsTest(unittest.TestCase):
    def test_discovered_chats_are_added_to_the_scanned_set(self) -> None:
        updated = apply_branch_chats(CONFIG, {"oc_moments": "feature-moments"})

        self.assertIn("oc_moments", updated.lark.all_chat_ids())
        self.assertEqual(updated.lark.branch_for_chat("oc_moments"), "feature-moments")
        self.assertEqual(CONFIG.lark.branch_for_chat("oc_moments"), "main")

    def test_configured_override_wins_over_discovery(self) -> None:
        configured_chat = next(iter(CONFIG.lark.branch_chats))

        updated = apply_branch_chats(CONFIG, {configured_chat: "feature-guessed"})

        self.assertEqual(
            updated.lark.branch_for_chat(configured_chat),
            CONFIG.lark.branch_for_chat(configured_chat),
        )

    def test_applying_to_the_base_config_drops_chats_that_stopped_matching(self) -> None:
        with_chat = apply_branch_chats(CONFIG, {"oc_moments": "feature-moments"})

        without_chat = apply_branch_chats(CONFIG, {})

        self.assertIn("oc_moments", with_chat.lark.all_chat_ids())
        self.assertNotIn("oc_moments", without_chat.lark.all_chat_ids())


class CachedBranchChatDiscovererTest(unittest.TestCase):
    def test_refetches_only_after_the_ttl_expires(self) -> None:
        lark = FakeChatLister((("oc_moments", "feature-moments"),))
        now = [0.0]
        discoverer = CachedBranchChatDiscoverer(
            lark=lark,
            github=FakeBranchLister(("feature-moments",)),
            config=CONFIG,
            ttl_seconds=300,
            clock=lambda: now[0],
        )

        discoverer.resolve()
        now[0] = 299
        discoverer.resolve()
        self.assertEqual(lark.calls, 1)

        now[0] = 301
        discoverer.resolve()
        self.assertEqual(lark.calls, 2)


if __name__ == "__main__":
    unittest.main()
