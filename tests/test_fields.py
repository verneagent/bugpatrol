from __future__ import annotations

import unittest

from bugpatrol.fields import NATIVE_ISSUE_TYPES, TRIAGE_OUTPUT_SCHEMA, validate_field_value


class FieldsTest(unittest.TestCase):
    def test_native_issue_type_is_not_an_issue_field(self) -> None:
        self.assertEqual(NATIVE_ISSUE_TYPES, ("Bug", "Feature", "Task"))
        self.assertNotIn("Issue Type", TRIAGE_OUTPUT_SCHEMA["properties"])

    def test_triage_schema_has_expected_enums(self) -> None:
        props = TRIAGE_OUTPUT_SCHEMA["properties"]

        self.assertEqual(props["issue_type"]["enum"], ["Bug", "Feature", "Task"])
        self.assertIn("代码 Bug", props["triage_verdict"]["enum"])
        self.assertIn("Needs info", props["triage_status"]["enum"])
        self.assertIn("Lark @mention", props["owner_reason"]["enum"])

    def test_validate_field_value(self) -> None:
        validate_field_value("Platform", "iOS")
        with self.assertRaisesRegex(ValueError, "invalid value"):
            validate_field_value("Platform", "watchOS")


if __name__ == "__main__":
    unittest.main()

