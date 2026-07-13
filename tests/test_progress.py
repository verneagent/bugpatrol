from __future__ import annotations

import unittest

from bugpatrol.progress import (
    ProgressReporter,
    format_elapsed,
    render_progress_message,
)


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


class RecordingReplier:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self._error = error

    def reply_to_message(self, *, chat_id: str, message_id: str, text: str) -> None:
        self.calls.append((chat_id, message_id, text))
        if self._error is not None:
            raise self._error


def _reporter(replier: RecordingReplier | None, **kwargs: object) -> ProgressReporter:
    params: dict[str, object] = dict(
        replier=replier,
        chat_id="oc_1",
        message_id="om_1",
        issue_number=7,
        interval_seconds=5.0,
    )
    params.update(kwargs)
    return ProgressReporter(**params)  # type: ignore[arg-type]


class FormatTest(unittest.TestCase):
    def test_sub_minute_drops_minutes_segment(self) -> None:
        self.assertEqual(format_elapsed(42), "42s")

    def test_zero_pads_seconds_over_a_minute(self) -> None:
        self.assertEqual(format_elapsed(125), "2m05s")

    def test_clamps_negative_to_zero(self) -> None:
        self.assertEqual(format_elapsed(-3), "0s")


class RenderTest(unittest.TestCase):
    def test_includes_issue_phase_elapsed_and_runner(self) -> None:
        text = render_progress_message(
            issue_number=7, phase="跑验证门", elapsed_seconds=125, runner="macstudio-bugpatrol"
        )
        self.assertIn("#7", text)
        self.assertIn("跑验证门", text)
        self.assertIn("2m05s", text)
        self.assertIn("macstudio-bugpatrol", text)

    def test_omits_runner_line_when_blank(self) -> None:
        text = render_progress_message(issue_number=1, phase="x", elapsed_seconds=0)
        self.assertNotIn("执行机", text)


class BeatTest(unittest.TestCase):
    def test_reports_current_phase_and_elapsed(self) -> None:
        clock = FakeClock()
        replier = RecordingReplier()
        reporter = _reporter(replier, _clock=clock)
        reporter.set_phase("跑验证门")
        clock.t = 125.0
        self.assertTrue(reporter.beat())
        self.assertEqual(len(replier.calls), 1)
        chat_id, message_id, text = replier.calls[0]
        self.assertEqual((chat_id, message_id), ("oc_1", "om_1"))
        self.assertIn("#7", text)
        self.assertIn("跑验证门", text)
        self.assertIn("2m05s", text)

    def test_stops_posting_at_beat_cap(self) -> None:
        replier = RecordingReplier()
        reporter = _reporter(replier, max_beats=2)
        self.assertTrue(reporter.beat())
        self.assertTrue(reporter.beat())
        self.assertFalse(reporter.beat())  # cap reached -> no further post
        self.assertEqual(len(replier.calls), 2)

    def test_swallows_error_when_swallow_returns_true(self) -> None:
        replier = RecordingReplier(error=RuntimeError("withdrawn"))
        reporter = _reporter(replier, swallow=lambda _e: True)
        self.assertTrue(reporter.beat())  # does not raise

    def test_reraises_error_when_not_swallowed(self) -> None:
        replier = RecordingReplier(error=RuntimeError("boom"))
        reporter = _reporter(replier, swallow=lambda _e: False)
        with self.assertRaises(RuntimeError):
            reporter.beat()


class EnabledTest(unittest.TestCase):
    def test_disabled_without_replier(self) -> None:
        reporter = _reporter(None)
        self.assertFalse(reporter.enabled)
        reporter.start()  # no-op, no thread
        self.assertIsNone(reporter._thread)

    def test_disabled_when_interval_zero(self) -> None:
        self.assertFalse(_reporter(RecordingReplier(), interval_seconds=0).enabled)

    def test_disabled_when_topic_missing(self) -> None:
        self.assertFalse(_reporter(RecordingReplier(), chat_id="").enabled)
        self.assertFalse(_reporter(RecordingReplier(), message_id="").enabled)

    def test_enabled_with_full_config(self) -> None:
        self.assertTrue(_reporter(RecordingReplier()).enabled)


class ThreadLifecycleTest(unittest.TestCase):
    def test_start_spawns_thread_and_stop_joins(self) -> None:
        # Large interval so the loop parks on _stop.wait() and fires no beats;
        # stop() sets the event so wait() returns immediately (no real delay).
        reporter = _reporter(RecordingReplier(), interval_seconds=60.0)
        reporter.start()
        assert reporter._thread is not None
        self.assertTrue(reporter._thread.is_alive())
        reporter.stop()
        self.assertIsNone(reporter._thread)


if __name__ == "__main__":
    unittest.main()
