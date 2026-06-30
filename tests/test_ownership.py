from __future__ import annotations

import unittest
from pathlib import Path

from bugpatrol.ownership import load_codeowners, parse_codeowners, resolve_first_owner, resolve_owners


class OwnershipTest(unittest.TestCase):
    def test_parse_codeowners_ignores_comments_and_invalid_lines(self) -> None:
        rules = parse_codeowners(
            """
            # comment
            *
            * @fallback
            /src/todo/ @todo-owner @backup
            !ignored @nobody
            """
        )

        self.assertEqual(len(rules), 2)
        self.assertEqual(rules[0].pattern, "*")
        self.assertEqual(rules[1].owners, ("@todo-owner", "@backup"))

    def test_later_matching_rule_wins(self) -> None:
        rules = parse_codeowners(
            """
            * @fallback
            /src/todo/ @todo-owner
            """
        )

        self.assertEqual(resolve_owners("src/todo/list.ts", rules), ("@todo-owner",))
        self.assertEqual(resolve_owners("README.md", rules), ("@fallback",))

    def test_glob_matches_prd_files(self) -> None:
        rules = parse_codeowners(
            """
            * @fallback
            /docs/prd/todo-* @todo-owner
            /docs/prd/notifications.md @notify-owner
            """
        )

        self.assertEqual(resolve_first_owner("docs/prd/todo-list.md", rules), "todo-owner")
        self.assertEqual(resolve_first_owner("docs/prd/notifications.md", rules), "notify-owner")

    def test_loads_sandbox_codeowners(self) -> None:
        rules = load_codeowners(Path("../bugpatrol-todo-sandbox"))

        self.assertEqual(resolve_first_owner("src/todo/list.ts", rules), "garlanddiego")
        self.assertEqual(resolve_first_owner("src/notifications/reminders.ts", rules), "verneagent")


if __name__ == "__main__":
    unittest.main()

