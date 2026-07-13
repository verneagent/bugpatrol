from __future__ import annotations

import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Protocol


class TopicReplier(Protocol):
    def reply_to_message(self, *, chat_id: str, message_id: str, text: str) -> None: ...


def format_elapsed(seconds: float) -> str:
    """Human elapsed like ``12m03s`` (drop the minutes segment under a minute)."""
    total = max(0, int(seconds))
    minutes, secs = divmod(total, 60)
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def render_progress_message(
    *, issue_number: int, phase: str, elapsed_seconds: float, runner: str = ""
) -> str:
    """One liveness line: which issue, current phase, elapsed time, which runner."""
    text = f"⏳ 修复 #{issue_number} 仍在进行：{phase}（已用 {format_elapsed(elapsed_seconds)}）"
    if runner:
        text += f"\n执行机：{runner}"
    return text


@dataclass
class ProgressReporter:
    """Periodic liveness pings to the reporter's Lark topic during a long run.

    One background daemon thread wakes every ``interval_seconds`` and posts the
    current phase + elapsed time, so a human watching the bug topic can see the
    run is alive (and where it is) instead of a 10-30 min silence before the
    terminal notification. Bounded by ``max_beats`` so a hung run can't spam the
    topic forever.

    Best-effort by contract: it must never fail the fix run. A withdrawn source
    message is swallowed via ``swallow`` (same tolerance as the other topic
    pings); the thread loop logs any other error to stderr and keeps going
    rather than crashing a background heartbeat.
    """

    replier: TopicReplier | None
    chat_id: str
    message_id: str
    issue_number: int
    interval_seconds: float
    runner: str = ""
    max_beats: int = 12
    # Return True to swallow a Lark error (e.g. a withdrawn source message).
    swallow: Callable[[Exception], bool] | None = None
    _clock: Callable[[], float] = time.monotonic
    _start: float = field(init=False, default=0.0)
    _phase: str = field(init=False, default="准备中")
    _beats: int = field(init=False, default=0)
    _lock: threading.Lock = field(init=False, default_factory=threading.Lock)
    _stop: threading.Event = field(init=False, default_factory=threading.Event)
    _thread: "threading.Thread | None" = field(init=False, default=None)

    def __post_init__(self) -> None:
        # Anchor T0 at construction: execute_fix_run builds the reporter right at
        # the run's start, so elapsed is meaningful even for a test calling beat()
        # without spinning the thread.
        self._start = self._clock()

    @property
    def enabled(self) -> bool:
        return (
            self.replier is not None
            and bool(self.chat_id)
            and bool(self.message_id)
            and self.interval_seconds > 0
            and self.max_beats > 0
        )

    def set_phase(self, phase: str) -> None:
        with self._lock:
            self._phase = phase

    def start(self) -> None:
        if not self.enabled or self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._loop, name=f"fix-progress-{self.issue_number}", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=5.0)
            self._thread = None

    def __enter__(self) -> "ProgressReporter":
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()

    def _loop(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            try:
                if not self.beat():
                    return
            except Exception as error:  # noqa: BLE001 - background best-effort ping
                # Log, don't crash the heartbeat thread; the fix run itself is
                # unaffected by a failed progress ping.
                print(f"bugpatrol: progress heartbeat failed: {error}", file=sys.stderr)

    def beat(self) -> bool:
        """Post one heartbeat. Returns False once the beat cap is reached.

        Separated from the thread loop so emit/cap/render is unit testable
        without spinning a real thread.
        """
        with self._lock:
            if self._beats >= self.max_beats:
                return False
            self._beats += 1
            phase = self._phase
        assert self.replier is not None  # enabled guards the thread; direct callers must set it
        text = render_progress_message(
            issue_number=self.issue_number,
            phase=phase,
            elapsed_seconds=self._clock() - self._start,
            runner=self.runner,
        )
        try:
            self.replier.reply_to_message(
                chat_id=self.chat_id, message_id=self.message_id, text=text
            )
        except Exception as error:
            if self.swallow is not None and self.swallow(error):
                return True
            raise
        return True
