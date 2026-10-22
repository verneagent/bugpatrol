from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from bugpatrol.duplicate_evidence import classify_duplicate_evidence, parse_duplicate_evidence


class DuplicateEvidenceTest(unittest.TestCase):
    def evidence(self) -> dict:
        return json.loads(Path("tests/fixtures/duplicate_delivery.json").read_text())

    def classify(self, data: dict | None) -> str:
        return classify_duplicate_evidence(parse_duplicate_evidence(data))

    def test_ordered_five_outcomes(self) -> None:
        regression = self.evidence()
        incomplete = copy.deepcopy(regression)
        incomplete["occurred_at"] = "2026-10-21T23:00:00Z"
        incomplete["components"][1]["current_delivery"] = "missing"
        pre_fix = copy.deepcopy(regression)
        pre_fix["occurred_at"] = "2026-10-21T23:00:00Z"
        for component in pre_fix["components"]:
            component["reported_fix"] = "missing"
        old_build = copy.deepcopy(regression)
        old_build["components"][0]["reported_fix"] = "missing"
        old_build["components"][0]["build_predates_fix"] = True
        unknown = copy.deepcopy(regression)
        unknown["components"][0]["build_evidence"] = ""
        for data, expected in (
            (incomplete, "delivery_incomplete"),
            (pre_fix, "pre_fix_duplicate"),
            (regression, "confirmed_regression"),
            (old_build, "old_build_duplicate"),
            (unknown, "needs_verification"),
        ):
            with self.subTest(expected=expected):
                self.assertEqual(self.classify(data), expected)

    def test_revert_is_delivery_gap_even_when_occurrence_predates_fix(self) -> None:
        data = self.evidence()
        data["occurred_at"] = "2026-10-21T23:00:00Z"
        data["components"][0]["current_delivery"] = "reverted"
        self.assertEqual(self.classify(data), "delivery_incomplete")

    def test_squash_and_cherry_pick_need_actual_content_not_ancestry_or_patch_id(self) -> None:
        for weak_check in ("ancestry", "patch_id", "unknown"):
            data = self.evidence()
            data["components"][0]["content_check"] = weak_check
            with self.subTest(check=weak_check):
                self.assertEqual(self.classify(data), "needs_verification")
        for actual_build in ("squash-build", "cherry-pick-build"):
            data = self.evidence()
            data["components"][0]["reported_build"] = actual_build
            self.assertEqual(self.classify(data), "confirmed_regression")

    def test_cross_repo_requires_every_component_and_matching_repository(self) -> None:
        for change in ("absent", "unknown", "wrong_repo", "duplicate"):
            data = self.evidence()
            if change == "absent":
                data["components"].pop()
            elif change == "unknown":
                data["components"][1]["current_delivery"] = "unknown"
            elif change == "wrong_repo":
                data["components"][1]["repository"] = "example/other"
            else:
                data["components"].append(copy.deepcopy(data["components"][0]))
            with self.subTest(change=change):
                self.assertEqual(self.classify(data), "needs_verification")

    def test_unknown_report_environment_cannot_use_main_revert_as_report_delivery_gap(self) -> None:
        data = self.evidence()
        data["environment"] = ""
        data["components"][0]["current_delivery"] = "reverted"
        self.assertEqual(self.classify(data), "needs_verification")

    def test_missing_conflicting_or_invalid_evidence_stays_unverified(self) -> None:
        self.assertEqual(self.classify(None), "needs_verification")
        for field in ("environment_evidence", "occurrence_evidence", "occurred_at"):
            data = self.evidence()
            data[field] = ""
            with self.subTest(field=field):
                self.assertEqual(self.classify(data), "needs_verification")
        for value in ("not-a-date", "2026-10-22T02:00:00", "2026-10-22T00:30:00Z"):
            data = self.evidence()
            data["occurred_at"] = value
            self.assertEqual(self.classify(data), "needs_verification")
        data = self.evidence()
        data["conflicts"] = ["https://evidence.test/conflicting-build"]
        self.assertEqual(self.classify(data), "needs_verification")

    def test_reported_revert_and_inconsistent_effective_build_stay_unverified(self) -> None:
        data = self.evidence()
        data["components"][0]["reported_fix"] = "reverted"
        self.assertEqual(self.classify(data), "needs_verification")
        data = self.evidence()
        data["occurred_at"] = "2026-10-21T23:00:00Z"
        self.assertEqual(self.classify(data), "needs_verification")

    def test_parser_rejects_untyped_evidence(self) -> None:
        for value in ([], "confirmed_regression", {"components": "all"}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_duplicate_evidence(value)


if __name__ == "__main__":
    unittest.main()
