"""Tests for scripts/issue_daily_report.py (daily per-reporter issue count)."""
import datetime as dt
import unittest
from unittest import mock
from zoneinfo import ZoneInfo

import scripts.issue_daily_report as m

META = '<!-- BUGPATROL_INTAKE_META:{"source":"lark","reporter_open_id":"ou_abc","chat_id":"oc_x"} -->'
SINCE = dt.datetime(2026, 8, 18, tzinfo=ZoneInfo("Asia/Shanghai"))


class ParseIntakeMetaTest(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(m.parse_intake_meta(f"body\n{META}")["reporter_open_id"], "ou_abc")

    def test_missing(self):
        self.assertIsNone(m.parse_intake_meta("no marker here"))

    def test_malformed_json(self):
        self.assertIsNone(m.parse_intake_meta("<!-- BUGPATROL_INTAKE_META:{broken} -->"))

    def test_escaped_comment_variant(self):
        # Bodies may store the comment with HTML-escaped brackets.
        escaped = META.replace("<!--", "&lt;!--").replace("-->", "--&gt;")
        self.assertEqual(m.parse_intake_meta(escaped)["reporter_open_id"], "ou_abc")


class ReporterOfTest(unittest.TestCase):
    def test_meta_wins_over_author(self):
        issue = {"number": 1, "user": "sobit-bot[bot]", "body": META}
        self.assertEqual(m.reporter_of(issue), ("ou_abc", "meta"))

    def test_native_user_as_string(self):
        issue = {"number": 2, "user": "alice", "body": "no meta"}
        self.assertEqual(m.reporter_of(issue), ("alice", "github"))

    def test_native_user_as_dict(self):
        issue = {"number": 3, "user": {"login": "bob"}, "body": "no meta"}
        self.assertEqual(m.reporter_of(issue), ("bob", "github"))

    def test_no_reporter(self):
        issue = {"number": 4, "user": None, "body": "no meta"}
        self.assertEqual(m.reporter_of(issue), (None, "none"))


class DisplayNameTest(unittest.TestCase):
    def test_sender_names_priority(self):
        cfg = {"sender_names": {"ou_abc": "张三"}, "user_open_ids": {}}
        self.assertEqual(m.display_name("ou_abc", "meta", cfg), "张三")

    def test_reverse_user_open_ids(self):
        cfg = {"sender_names": {}, "user_open_ids": {"alice": "ou_abc"}}
        self.assertEqual(m.display_name("ou_abc", "meta", cfg), "alice")

    def test_github_login_passthrough(self):
        self.assertEqual(m.display_name("alice", "github", {}), "alice")

    def test_fallback_raw_id(self):
        self.assertEqual(m.display_name("ou_unknown", "meta", {}), "ou_unknown")


class AggregateTest(unittest.TestCase):
    def _issue(self, number, created_utc, user, body="", is_pr=False):
        # GitHub API returns naive UTC with a trailing Z.
        return {
            "number": number,
            "created_at": created_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "user": user,
            "body": body,
            "is_pr": is_pr,
        }

    @mock.patch.object(m, "fetch_issues")
    def test_counts_prs_excluded_window_filtered(self, fetch):
        window_start = SINCE.astimezone(dt.timezone.utc)  # 2026-08-17 16:00 UTC
        fetch.return_value = [
            self._issue(1, window_start + dt.timedelta(hours=1), "sobit-bot[bot]", META),            # meta -> ou_abc
            self._issue(2, window_start + dt.timedelta(hours=2), "alice", "no meta"),                  # native -> alice
            self._issue(3, window_start + dt.timedelta(hours=3), "sobit-bot[bot]", META),              # meta -> ou_abc
            self._issue(4, window_start + dt.timedelta(hours=4), "bob", "no meta", is_pr=True),        # PR excluded
            self._issue(5, window_start - dt.timedelta(hours=1), "alice", "no meta"),                  # before window
            self._issue(6, SINCE.astimezone(dt.timezone.utc) + dt.timedelta(days=1, hours=1), "alice", "no meta"),  # after window
        ]
        cfg = {"sender_names": {"ou_abc": "张三"}, "user_open_ids": {"alice": "ou_alice"}}
        result = m.aggregate("o/r", SINCE, "gh", cfg)
        counts = {c["reporter"]: (c["count"], sorted(c["issues"])) for c in result["counts"]}
        # alice appears once (issue 2); ou_alice's reverse map is not used here.
        self.assertEqual(counts, {"张三": (2, [1, 3]), "alice": (1, [2])})

    @mock.patch.object(m, "fetch_issues")
    def test_sorted_by_count_desc(self, fetch):
        window_start = SINCE.astimezone(dt.timezone.utc)
        fetch.return_value = [
            self._issue(1, window_start + dt.timedelta(hours=1), "sobit-bot[bot]", META),
            self._issue(2, window_start + dt.timedelta(hours=2), "sobit-bot[bot]", META),
            self._issue(3, window_start + dt.timedelta(hours=3), "alice", "no meta"),
        ]
        cfg = {"sender_names": {}, "user_open_ids": {}}
        result = m.aggregate("o/r", SINCE, "gh", cfg)
        self.assertEqual([c["reporter"] for c in result["counts"]], ["ou_abc", "alice"])
        self.assertEqual([c["count"] for c in result["counts"]], [2, 1])


class FetchIssuesGuardTest(unittest.TestCase):
    def _fake(self, stdout):
        fake = mock.Mock()
        fake.returncode = 0
        fake.stdout = stdout
        fake.stderr = ""
        return fake

    def test_missing_projection_key_fails_loud(self):
        # A filter key absent from the projection must not silently die
        # (the PR-sneaks-in-as-issue bug).
        fake = self._fake('{"number":1,"created_at":"2026-08-18T00:00:00Z","user":"x","body":""}\n')
        with mock.patch("subprocess.run", return_value=fake):
            with self.assertRaisesRegex(RuntimeError, "projection missing"):
                m.fetch_issues("o/r", "2026-08-18T00:00:00+00:00", "gh")

    def test_valid_shape_passes(self):
        fake = self._fake('{"number":1,"created_at":"2026-08-18T00:00:00Z","user":"x","body":"","is_pr":false}\n')
        with mock.patch("subprocess.run", return_value=fake):
            issues = m.fetch_issues("o/r", "2026-08-18T00:00:00+00:00", "gh")
        self.assertEqual([i["number"] for i in issues], [1])


if __name__ == "__main__":
    unittest.main()
