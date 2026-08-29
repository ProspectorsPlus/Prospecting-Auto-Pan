"""The plain log: what the navigator is doing, in sentences, in order.

The diagnostics drawer is the engineering half - lifecycle stages, lease
targets, governor transitions, frame traces - and the owner said plainly that
it is not readable. This is the other half, and it is not a summary of the
drawer: it is a running commentary written in the words a person would use, so
that "it is not moving" always has a next line under it saying why.

Three rules keep it readable while a character is walking:

**One line per change, never per frame.** Every line carries a topic. A topic
that says the same thing again updates its existing line's repeat count instead
of appending; only a genuinely new sentence appends. So thirty frames of
"holding W" is one line, and the line that matters - "the world is moving" -
is never pushed off the screen by it.

**Numbers are rounded until they stop changing.** Degrees to the nearest five,
durations to whole seconds, rates to whole numbers. A line that only differs in
its third decimal is the same line.

**Every failure names one physical action.** If there is nothing a person can
do about it, it is not a failure, it is information. That distinction is what
keeps the red lines worth reading.

Two rings, for the same reason :class:`~prospector_engine.telemetry.EventLog`
has two: a *story* ring that per-frame topics can never evict, and the visible
tail. It is deliberately **not** cleared on Stop - reading back why the last run
ended is most of what it is for - only on a new Start Navigator.

Nothing here decides anything. It renders sentences the engine composed.
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, replace
from enum import Enum

from prospector_engine.contracts import monotonic_s

__all__ = ["PlainLine", "PlainLog", "Topic", "Verdict"]


class Verdict(Enum):
    """How a line should read, and whether it needs an action."""

    PASS = "pass"
    FAIL = "fail"
    INFO = "info"

    @property
    def mark(self) -> str:
        return {"pass": "OK", "fail": "!!", "info": "  "}[self.value]


class Topic(Enum):
    """What a line is *about*. Repeats collapse within a topic, never across.

    Split finely enough that a per-frame topic - ``FORWARD``, ``TURN``,
    ``MOTION`` - can churn without ever evicting a once-per-session one.
    """

    WINDOW = "window"
    SIZE = "size"
    CAPTURE = "capture"
    MAP = "map"
    ARROW = "arrow"
    CHORD = "chord"
    GATE = "gate"
    FORWARD = "forward"
    TURN = "turn"
    MOTION = "motion"
    STOP = "stop"
    NOTE = "note"

    @property
    def per_frame(self) -> bool:
        """Topics that can fire every tick, and so may never evict the story."""
        return self in (Topic.FORWARD, Topic.TURN, Topic.MOTION, Topic.ARROW)


@dataclass(frozen=True)
class PlainLine:
    """One sentence, its verdict, and how many times it has repeated."""

    topic: Topic
    verdict: Verdict
    text: str
    at_s: float
    count: int = 1
    #: Set while a stage is still running; the line is rewritten in place when
    #: it resolves, rather than leaving "Looking for..." above "Found it".
    pending: bool = False

    def stamp(self, started_at_s: float) -> str:
        elapsed = max(0.0, self.at_s - started_at_s)
        return f"+{int(elapsed) // 60}:{int(elapsed) % 60:02d}"

    def render(self, started_at_s: float) -> str:
        repeat = f"  (x{self.count})" if self.count > 1 else ""
        return f"{self.stamp(started_at_s)} {self.verdict.mark} {self.text}{repeat}"


class PlainLog:
    """A bounded, thread-safe running commentary. Append-only, collapse-on-repeat."""

    #: Lines a per-frame topic may add per second across the whole log. Beyond
    #: it, a repeat updates the existing line rather than appending.
    MAX_LINES_PER_S = 3.0

    def __init__(self, capacity: int = 400) -> None:
        self._lock = threading.Lock()
        self._lines: deque[PlainLine] = deque(maxlen=capacity)
        self._started_at_s = monotonic_s()
        self._sequence = 0
        self._last_append_s = 0.0

    # -- writing ----------------------------------------------------------
    def say(
        self,
        topic: Topic,
        verdict: Verdict,
        text: str,
        *,
        pending: bool = False,
    ) -> None:
        """Record one sentence. Identical consecutive text collapses.

        "Identical" is judged on the topic and the rendered sentence, which is
        why callers round their numbers first: a line that differs only in a
        digit nobody reads is the same line, and should not push another off
        the screen.
        """
        now = monotonic_s()
        with self._lock:
            self._sequence += 1
            latest = self._find_topic(topic)
            if latest is not None and latest.text == text and latest.verdict is verdict:
                self._replace(latest, replace(latest, count=latest.count + 1, at_s=now))
                return
            if latest is not None and latest.pending:
                # A stage that was running has resolved: rewrite it in place
                # rather than leaving "Looking for..." above "Found it".
                self._replace(latest, PlainLine(topic, verdict, text, now, pending=pending))
                self._last_append_s = now
                return
            # Under the rate cap a per-frame topic updates its own line instead
            # of appending. The sentence still changes; the log just does not
            # grow three times a frame.
            capped = (now - self._last_append_s) < 1.0 / self.MAX_LINES_PER_S
            if topic.per_frame and capped and latest is not None:
                self._replace(
                    latest, PlainLine(topic, verdict, text, now, count=latest.count + 1)
                )
                return
            self._lines.append(PlainLine(topic, verdict, text, now, pending=pending))
            self._last_append_s = now

    def passed(self, topic: Topic, text: str) -> None:
        self.say(topic, Verdict.PASS, text)

    def failed(self, topic: Topic, text: str) -> None:
        self.say(topic, Verdict.FAIL, text)

    def note(self, topic: Topic, text: str) -> None:
        self.say(topic, Verdict.INFO, text)

    def working(self, topic: Topic, text: str) -> None:
        """A stage that is still running. Rewritten in place when it resolves."""
        self.say(topic, Verdict.INFO, text, pending=True)

    def restart(self) -> None:
        """A new Start Navigator. The only thing that clears the log."""
        with self._lock:
            self._lines.clear()
            self._started_at_s = monotonic_s()
            self._last_append_s = 0.0
            self._sequence += 1

    # -- reading ----------------------------------------------------------
    @property
    def sequence(self) -> int:
        """Bumped on every write, so a renderer can skip an unchanged log."""
        with self._lock:
            return self._sequence

    @property
    def started_at_s(self) -> float:
        return self._started_at_s

    def lines(self, limit: int = 200) -> tuple[PlainLine, ...]:
        with self._lock:
            return tuple(self._lines)[-limit:]

    def rendered(self, limit: int = 200) -> tuple[str, ...]:
        with self._lock:
            started = self._started_at_s
            rows = tuple(self._lines)[-limit:]
        return tuple(row.render(started) for row in rows)

    def failures(self) -> tuple[PlainLine, ...]:
        with self._lock:
            return tuple(row for row in self._lines if row.verdict is Verdict.FAIL)

    def as_rows(self) -> Iterable[dict[str, object]]:
        """For the stop trace, so the plain story is exported beside the numbers."""
        with self._lock:
            started = self._started_at_s
            rows = list(self._lines)
        return [
            {
                "kind": "plain",
                "at_s": round(row.at_s, 3),
                "elapsed": row.stamp(started),
                "topic": row.topic.value,
                "verdict": row.verdict.value,
                "text": row.text,
                "count": row.count,
            }
            for row in rows
        ]

    # -- internals --------------------------------------------------------
    def _find_topic(self, topic: Topic) -> PlainLine | None:
        for row in reversed(self._lines):
            if row.topic is topic:
                return row
        return None

    def _replace(self, old: PlainLine, new: PlainLine) -> None:
        for index in range(len(self._lines) - 1, -1, -1):
            if self._lines[index] is old:
                self._lines[index] = new
                return
