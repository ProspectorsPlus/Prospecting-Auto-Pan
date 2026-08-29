"""The named stages between a physical keypress and a character that moved.

The failure this module exists to end: *"it says APPLIED and the character does
not move."* Several very different faults produce that sentence, and every one
of them used to look identical from outside, because the only thing recorded
was the conclusion.

So the path is named end to end, every stage is recorded with the moment it
happened, and the record is persisted with the frame trace. The next failure is
then read out of one file rather than guessed at:

===============================  ==========================================
``PHYSICAL_EDGE_RECEIVED``       the listener saw a real key edge
``CHORD_RECOGNIZED``             it completed a chord
``CHORD_REFUSED``                and policy - focus, readiness - refused it
``INTENT_QUEUED``                the coordinator accepted the intent
``HOTKEY_CHORD_RECEIVED``        the coordinator loop took it off the queue
``LIVE_AUTHORIZATION_CREATED``   the chord minted its one-use authorization
``LIVE_AUTHORIZATION_CONSUMED``  and spent it on this transition
``LIVE_ENTERED``                 the mode is LIVE
``LIVE_REFUSED``                 it is not, and this is the exact reason
``LIVE_WORKER_ENTERED``          the live worker is running
``W_REQUESTED``                  the controller asked for forward
``OS_EDGE_POSTED``               the post call returned without raising
``OS_EDGE_LOOPBACK_OBSERVED``    the OS agrees the key is down
``LEASE_HELD``                   the authority holds the lease it reports
``HOLD_LAPSED``                  a held key came up and had to be pressed again
``W_HOLD_CONFIRMED``             it stayed down, with the measured duration
``POST_EDGE_FRAME_OBSERVED``     a frame captured *after* the down edge exists
``GAME_MOTION_CONFIRMED``        that frame shows the world moved
``W_RELEASE_POSTED``             the up edge went out
``LEDGER_EMPTY``                 nothing is held
``TURN_REQUESTED``               a signed camera correction was asked for
``TURN_POSTED``                  the authority accepted and emitted it
``TURN_OBSERVED``                the heading afterwards, measured
``TURN_BACKEND_SELECTED``        which actuator this run turns with
``TURN_UNAVAILABLE``             and why none could be chosen
===============================  ==========================================

**Naming is exact and the stages do not merge.** ``OS_EDGE_POSTED`` means
``CGEventPost`` returned; it is not evidence that anything received the event.
``LEASE_HELD`` means the authority accepted it; it is not evidence that the
game did. Only ``GAME_MOTION_CONFIRMED`` is success, and it requires visual
evidence from a frame captured after the down edge. No earlier stage may ever
be reported as proof of movement.

Nothing here emits input, reads a pixel, or touches an OS API. It records.
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from prospector_engine.contracts import monotonic_s

__all__ = [
    "FORWARD_PULSE_PATH",
    "LIVE_ENTRY_PATH",
    "NATIVE_MOTION_PATH",
    "LifecycleEvent",
    "LifecycleJournal",
    "LifecycleStage",
    "merge_rows",
]


class LifecycleStage(Enum):
    """One named step. Ordered, and never merged with its neighbours."""

    PHYSICAL_EDGE_RECEIVED = "physical_edge_received"
    CHORD_RECOGNIZED = "chord_recognized"
    CHORD_REFUSED = "chord_refused"
    INTENT_QUEUED = "intent_queued"
    #: The coordinator took a start chord off its own queue and is about to
    #: decide on it. Distinct from CHORD_RECOGNIZED, which happens on the
    #: listener thread: a chord that is recognized and never received is a
    #: queue or coordinator fault, and the two stages are what tell them apart.
    HOTKEY_CHORD_RECEIVED = "hotkey_chord_received"
    LIVE_AUTHORIZATION_CREATED = "live_authorization_created"
    LIVE_AUTHORIZATION_CONSUMED = "live_authorization_consumed"
    LIVE_ENTERED = "live_entered"
    LIVE_REFUSED = "live_refused"
    LIVE_WORKER_ENTERED = "live_worker_entered"
    W_REQUESTED = "w_requested"
    OS_EDGE_POSTED = "os_edge_posted"
    OS_EDGE_LOOPBACK_OBSERVED = "os_edge_loopback_observed"
    OS_EDGE_LOOPBACK_MISSING = "os_edge_loopback_missing"
    LEASE_HELD = "lease_held"
    HOLD_LAPSED = "hold_lapsed"
    #: A forward hold that stayed physically down for a measured duration,
    #: with the down-to-up milliseconds attached. It is not evidence that the
    #: game moved; only GAME_MOTION_CONFIRMED is.
    W_HOLD_CONFIRMED = "w_hold_confirmed"
    POST_EDGE_FRAME_OBSERVED = "post_edge_frame_observed"
    GAME_MOTION_CONFIRMED = "game_motion_confirmed"
    GAME_MOTION_NOT_CONFIRMED = "game_motion_not_confirmed"
    W_RELEASE_POSTED = "w_release_posted"
    LEDGER_EMPTY = "ledger_empty"
    #: Camera. Requested and posted are separate for the same reason the
    #: forward stages are: a delta the authority refused is not a delta the
    #: game saw, and a run that turns nowhere has to say which of the two it
    #: was. TURN_BACKEND_SELECTED carries the backend the characterizer chose;
    #: TURN_UNAVAILABLE carries why it could not choose one.
    TURN_REQUESTED = "turn_requested"
    TURN_POSTED = "turn_posted"
    TURN_OBSERVED = "turn_observed"
    TURN_BACKEND_SELECTED = "turn_backend_selected"
    TURN_UNAVAILABLE = "turn_unavailable"

    @property
    def label(self) -> str:
        return self.value.replace("_", " ")


#: The stages a genuine Ctrl+N must walk before any input exists at all. A run
#: that stops inside this path never reached the input backend, so nothing
#: about Quartz, focus, camera calibration or hold timing can be to blame.
LIVE_ENTRY_PATH: tuple[LifecycleStage, ...] = (
    LifecycleStage.CHORD_RECOGNIZED,
    LifecycleStage.INTENT_QUEUED,
    LifecycleStage.HOTKEY_CHORD_RECEIVED,
    LifecycleStage.LIVE_AUTHORIZATION_CREATED,
    LifecycleStage.LIVE_AUTHORIZATION_CONSUMED,
    LifecycleStage.LIVE_ENTERED,
    LifecycleStage.LIVE_WORKER_ENTERED,
)


#: The whole causal chain a native movement check has to walk, from the
#: physical chord to an empty ledger. ``first_missing`` over this is the one
#: sentence a blocked owner is shown, and it names a transition rather than
#: saying "movement failed".
NATIVE_MOTION_PATH: tuple[LifecycleStage, ...] = (
    *LIVE_ENTRY_PATH,
    LifecycleStage.W_REQUESTED,
    LifecycleStage.OS_EDGE_POSTED,
    LifecycleStage.LEASE_HELD,
    LifecycleStage.W_HOLD_CONFIRMED,
    LifecycleStage.GAME_MOTION_CONFIRMED,
    LifecycleStage.W_RELEASE_POSTED,
    LifecycleStage.LEDGER_EMPTY,
)


#: The stages a bounded forward pulse must reach, in order. The first one
#: missing from a run is the thing to show a person.
FORWARD_PULSE_PATH: tuple[LifecycleStage, ...] = (
    LifecycleStage.W_REQUESTED,
    LifecycleStage.OS_EDGE_POSTED,
    LifecycleStage.OS_EDGE_LOOPBACK_OBSERVED,
    LifecycleStage.LEASE_HELD,
    LifecycleStage.POST_EDGE_FRAME_OBSERVED,
    LifecycleStage.GAME_MOTION_CONFIRMED,
    LifecycleStage.W_RELEASE_POSTED,
    LifecycleStage.LEDGER_EMPTY,
)


@dataclass(frozen=True)
class LifecycleEvent:
    """One stage, when it happened, and the numbers that justify it."""

    stage: LifecycleStage
    at_s: float
    detail: str = ""
    fields: Mapping[str, Any] = field(default_factory=dict)

    def as_row(self) -> dict[str, Any]:
        return {
            "stage": self.stage.value,
            "at_s": round(self.at_s, 6),
            "detail": self.detail,
            **dict(self.fields),
        }

    def describe(self) -> str:
        head = self.stage.label.upper()
        return f"{head}: {self.detail}" if self.detail else head


class LifecycleJournal:
    """A bounded, thread-safe ring of lifecycle events.

    Bounded by construction, like every other ring in this application: a
    ``deque(maxlen=...)``, one dataclass and one append under a lock per event,
    which is what makes it safe to leave on in production.
    """

    def __init__(self, capacity: int = 512) -> None:
        self._events: deque[LifecycleEvent] = deque(maxlen=capacity)
        self._lock = threading.Lock()

    def note(self, stage: LifecycleStage, detail: str = "", /, **fields: Any) -> LifecycleEvent:
        """Record one stage. Never raises, whatever it is handed.

        ``stage`` and ``detail`` are positional-only, so a caller that passes
        ``detail=`` as a *field* lands in ``fields`` instead of raising
        ``TypeError`` for "multiple values for argument 'detail'". That is a bad place for an
        exception: journalling happens on the coordinator thread, inside the
        handler for the thing being journalled, so a mistyped diagnostic
        safe-stopped the run it was trying to describe. The collision is
        absorbed instead - the field wins and the positional is kept beside
        it - because a wrong label is a smaller failure than a stopped
        session, and both end up in the row either way.
        """
        extra = dict(fields)
        collided = extra.pop("detail", None)
        if collided is not None:
            extra["note"] = detail
            detail = str(collided)
        event = LifecycleEvent(stage, monotonic_s(), detail, extra)
        with self._lock:
            self._events.append(event)
        return event

    def events(self) -> tuple[LifecycleEvent, ...]:
        with self._lock:
            return tuple(self._events)

    def clear(self) -> None:
        with self._lock:
            self._events.clear()

    def rows(self) -> tuple[dict[str, Any], ...]:
        return tuple(event.as_row() for event in self.events())

    def reached(self, stage: LifecycleStage, *, since_s: float = 0.0) -> bool:
        return any(e.stage is stage and e.at_s >= since_s for e in self.events())

    def first_missing(
        self, path: Sequence[LifecycleStage] = FORWARD_PULSE_PATH, *, since_s: float = 0.0
    ) -> LifecycleStage | None:
        """The earliest stage in ``path`` that never happened.

        This is what a blocked user is shown. "Live stopped" after thirty
        seconds is not a diagnosis; "the OS accepted the edge and no frame
        after it showed movement" is.
        """
        for stage in path:
            if not self.reached(stage, since_s=since_s):
                return stage
        return None

    def describe(self, limit: int = 24) -> tuple[str, ...]:
        return tuple(event.describe() for event in self.events()[-limit:])


def merge_rows(journals: Iterable[LifecycleJournal]) -> list[dict[str, Any]]:
    """Every journal's rows in one time-ordered list, for the JSONL export."""
    rows = [row for journal in journals for row in journal.rows()]
    rows.sort(key=lambda row: float(row.get("at_s", 0.0)))
    return rows
