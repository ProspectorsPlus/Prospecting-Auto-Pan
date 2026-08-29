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

import os
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path

from prospector_engine.contracts import monotonic_s

__all__ = ["PlainLine", "PlainLog", "Topic", "Verdict"]


class Verdict(Enum):
    """How a line should read, and whether it needs an action.

    Six, not three. The three it had could not distinguish the two things the
    owner most needed to see - *an edge went out* and *the mode changed* - from
    ordinary commentary, and it had no way at all to say "this is wrong and
    nothing stopped because of it". A cadence downshift written as ``fail``
    reads as a refusal; written as ``info`` it disappears. ``WARN`` is the one
    that was missing, and the run that refused Ctrl+N seven times with
    ``cadence:cooldown`` is what it is for.
    """

    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"
    INFO = "info"
    #: An OS edge: a key or button going down or up, or a pointer delta.
    INPUT = "input"
    #: A lifecycle change: a mode transition, a worker starting or ending.
    STATE = "state"

    @property
    def mark(self) -> str:
        return {
            "pass": "PASS ",
            "warn": "WARN ",
            "fail": "FAIL ",
            "info": "INFO ",
            "input": "INPUT",
            "state": "STATE",
        }[self.value]

    @property
    def needs_action(self) -> bool:
        return self is Verdict.FAIL


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
    #: Mode and worker transitions. Once-per-change, and the rows a failed
    #: session is read out of.
    STATE = "state"
    #: One OS edge: a key or button going down or up. Deliberately **not** a
    #: per-frame topic - the actuator is level-triggered, so an edge is a rare
    #: event and losing one to frame chatter would lose the only record that
    #: something was actually pressed.
    INPUT = "input"
    #: Which mechanism edges are going out through, once it is chosen.
    BACKEND = "backend"
    #: The adaptive cadence tier. Reported, never obeyed.
    CADENCE = "cadence"
    #: What the pursuit controller is doing - FOLLOW, CORRECT, COAST, SEARCH -
    #: and what is actually held while it does it. Deliberately **not** a
    #: per-frame topic even though it is computed every frame: the navigator
    #: only writes a line when the state, the held keys or the recovery rung
    #: change, and those are rare and are the lines a run is read out of.
    PURSUIT = "pursuit"
    #: One obstacle-recovery episode: which rung, which side, and how it ended.
    RECOVERY = "recovery"

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
    #: Wall-clock seconds, for a stamp a person can match against a screen
    #: recording or a memory of when they pressed the key. The monotonic
    #: ``at_s`` remains the one used for ordering and for elapsed time.
    wall_at_s: float = 0.0

    def stamp(self, started_at_s: float) -> str:
        elapsed = max(0.0, self.at_s - started_at_s)
        return f"+{int(elapsed) // 60}:{int(elapsed) % 60:02d}"

    def clock(self) -> str:
        """``[HH:MM:SS.mmm]`` - the stamp on every rendered line."""
        if not self.wall_at_s:
            return "[--:--:--.---]"
        whole = time.localtime(self.wall_at_s)
        millis = int((self.wall_at_s % 1.0) * 1000.0)
        return f"[{time.strftime('%H:%M:%S', whole)}.{millis:03d}]"

    def render(self, started_at_s: float) -> str:
        repeat = f"  (x{self.count})" if self.count > 1 else ""
        return f"{self.clock()} {self.verdict.mark} {self.text}{repeat}"

    def render_elapsed(self, started_at_s: float) -> str:
        """The same line stamped with elapsed time instead of the clock."""
        repeat = f"  (x{self.count})" if self.count > 1 else ""
        return f"{self.stamp(started_at_s)} {self.verdict.mark} {self.text}{repeat}"


class PlainLog:
    """A bounded, thread-safe running commentary. Append-only, collapse-on-repeat."""

    #: Lines a per-frame topic may add per second across the whole log. Beyond
    #: it, a repeat updates the existing line rather than appending.
    MAX_LINES_PER_S = 3.0

    def __init__(self, capacity: int = 400, *, run_id: str | None = None) -> None:
        self._lock = threading.Lock()
        #: A list rather than a bounded deque, because *which* line is dropped
        #: when it is full is the whole point. A ``deque(maxlen=)`` drops the
        #: oldest, which on a busy run is always a control line - the chord,
        #: the refusal, the transition - because those happen once and the
        #: per-frame topics happen sixty times a second. :meth:`_evict` drops
        #: the oldest *per-frame* line instead, and only falls back to the
        #: oldest line when there is no frame telemetry left to drop.
        self._lines: list[PlainLine] = []
        self._capacity = max(20, capacity)
        self._started_at_s = monotonic_s()
        self._started_wall_s = time.time()
        self._sequence = 0
        self._last_append_s = 0.0
        #: One id per run, minted here at process start and re-minted by
        #: :meth:`restart`. Every export names it, so two runs' stories can
        #: never be read as one.
        self._run_id = run_id or os.urandom(4).hex()

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
                self._replace(
                    latest,
                    PlainLine(
                        topic, verdict, text, now, pending=pending, wall_at_s=time.time()
                    ),
                )
                self._last_append_s = now
                return
            # Under the rate cap a per-frame topic updates its own line instead
            # of appending - but only when the new sentence is a *variation* of
            # the one already there, judged on its first word.
            #
            # That qualifier matters. "Facing 40 degrees off" becoming "Facing
            # 45 degrees off" is one fact with a new number and should not cost
            # a line. "Holding W" becoming "Released W" is two different events
            # and collapsing them loses the half the reader came for. Without
            # this, a press and the release that followed it inside a third of
            # a second showed up as the release alone.
            capped = (now - self._last_append_s) < 1.0 / self.MAX_LINES_PER_S
            variation = (
                latest is not None
                and latest.verdict is verdict
                and latest.text.split(" ", 1)[0] == text.split(" ", 1)[0]
            )
            if topic.per_frame and capped and variation and latest is not None:
                self._replace(
                    latest,
                    PlainLine(
                        topic,
                        verdict,
                        text,
                        now,
                        count=latest.count + 1,
                        wall_at_s=time.time(),
                    ),
                )
                return
            self._append(
                PlainLine(topic, verdict, text, now, pending=pending, wall_at_s=time.time())
            )
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

    def restart(self, *, run_id: str | None = None) -> None:
        """A new Start Navigator. The only thing that clears the log.

        It also mints a new run id, so an export taken after this cannot carry
        a line from before it.
        """
        with self._lock:
            self._lines.clear()
            self._started_at_s = monotonic_s()
            self._started_wall_s = time.time()
            self._last_append_s = 0.0
            self._sequence += 1
            self._run_id = run_id or os.urandom(4).hex()

    # -- reading ----------------------------------------------------------
    @property
    def sequence(self) -> int:
        """Bumped on every write, so a renderer can skip an unchanged log."""
        with self._lock:
            return self._sequence

    @property
    def started_at_s(self) -> float:
        return self._started_at_s

    @property
    def run_id(self) -> str:
        """The id of the run these lines belong to. Never reused."""
        return self._run_id

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

    def write_text(self, directory: Path, *, label: str = "run") -> Path | None:
        """Write this run's story as plain text beside the JSONL trace.

        One file per run, named with the run id, so a reader never has to work
        out which lines belong to which session - which is the failure the id
        exists to prevent, and the one an appended log makes inevitable.
        """
        with self._lock:
            started = self._started_at_s
            rows = list(self._lines)
            run_id = self._run_id
            wall = self._started_wall_s
        try:
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f"{label}-{run_id}.log"
            header = (
                f"Treasure Navigator run {run_id}\n"
                f"started {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(wall))}\n"
                f"{len(rows)} lines\n\n"
            )
            path.write_text(
                header + "\n".join(row.render(started) for row in rows) + "\n",
                encoding="utf-8",
            )
        except OSError:
            return None
        return path

    def as_rows(self) -> Iterable[dict[str, object]]:
        """For the stop trace, so the plain story is exported beside the numbers."""
        with self._lock:
            started = self._started_at_s
            rows = list(self._lines)
        return [
            {
                "kind": "plain",
                "run_id": self._run_id,
                "at_s": round(row.at_s, 3),
                "clock": row.clock(),
                "elapsed": row.stamp(started),
                "topic": row.topic.value,
                "verdict": row.verdict.value,
                "text": row.text,
                "count": row.count,
            }
            for row in rows
        ]

    # -- internals --------------------------------------------------------
    def _append(self, line: PlainLine) -> None:
        self._lines.append(line)
        while len(self._lines) > self._capacity:
            self._evict()

    def _evict(self) -> None:
        """Drop one line, preferring frame telemetry over the story."""
        for index, row in enumerate(self._lines):
            if row.topic.per_frame:
                del self._lines[index]
                return
        del self._lines[0]

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
