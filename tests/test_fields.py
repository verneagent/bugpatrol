from __future__ import annotations

import unittest

from bugpatrol.fields import (
    NATIVE_ISSUE_TYPES,
    TRIAGE_OUTPUT_SCHEMA,
    triage_output_schema,
    validate_field_value,
)


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

    def test_triage_schema_is_strict_response_format_compatible(self) -> None:
        props = TRIAGE_OUTPUT_SCHEMA["properties"]

        self.assertEqual(set(TRIAGE_OUTPUT_SCHEMA["required"]), set(props))

    def test_triage_output_schema_uses_known_assignees_as_enum(self) -> None:
        schema = triage_output_schema(known_assignees=("AndyCokeZero", "garlanddiego"))

        self.assertEqual(
            schema["properties"]["assignee"]["enum"],
            ["AndyCokeZero", "garlanddiego"],
        )
        self.assertNotIn("enum", TRIAGE_OUTPUT_SCHEMA["properties"]["assignee"])

    def test_validate_field_value(self) -> None:
        validate_field_value("Platform", "iOS")
        with self.assertRaisesRegex(ValueError, "invalid value"):
            validate_field_value("Platform", "watchOS")


if __name__ == "__main__":
    unittest.main()
