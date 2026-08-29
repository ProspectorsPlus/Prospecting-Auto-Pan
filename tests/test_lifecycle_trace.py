"""The named-stage record, and that it survives into the file people read.

The stop trace this exists to fix held only ``frame``, ``preview`` and
``governor`` rows. A session that entered Live, failed to prove the game was
receiving input and stopped looked, from the exported file, exactly like a
session that never armed at all - which is why the last one took a guess and a
log timestamp to diagnose.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from prospector_engine.contracts import InputKey, IntentType, monotonic_s
from prospector_engine.lifecycle import (
    FORWARD_PULSE_PATH,
    LifecycleJournal,
    LifecycleStage,
)
from prospector_engine.telemetry import AppPaths
from tests.test_runtime_concurrency import Harness


@pytest.fixture
def harness() -> Any:
    """A fully wired coordinator over fakes. Shared with the concurrency
    tests, redeclared here so this module reads on its own."""
    rig = Harness()
    yield rig
    rig.close()


# ---------------------------------------------------------------------------
# The journal itself
# ---------------------------------------------------------------------------


def test_the_first_missing_stage_is_the_one_to_show() -> None:
    """ "Live stopped" is not a diagnosis. "The OS accepted the edge and no
    frame after it showed movement" is."""
    journal = LifecycleJournal()
    for stage in (
        LifecycleStage.W_REQUESTED,
        LifecycleStage.OS_EDGE_POSTED,
        LifecycleStage.OS_EDGE_LOOPBACK_OBSERVED,
    ):
        journal.note(stage)
    assert journal.first_missing() is LifecycleStage.LEASE_HELD


def test_a_complete_path_has_nothing_missing() -> None:
    journal = LifecycleJournal()
    for stage in FORWARD_PULSE_PATH:
        journal.note(stage)
    assert journal.first_missing() is None


def test_only_stages_after_a_mark_are_counted() -> None:
    """A previous attempt's success must not paper over this attempt's failure."""
    journal = LifecycleJournal()
    for stage in FORWARD_PULSE_PATH:
        journal.note(stage)
    mark = monotonic_s()
    journal.note(LifecycleStage.W_REQUESTED)
    assert journal.first_missing(since_s=mark) is LifecycleStage.OS_EDGE_POSTED


def test_the_ring_is_bounded() -> None:
    journal = LifecycleJournal(capacity=8)
    for _ in range(50):
        journal.note(LifecycleStage.W_REQUESTED)
    assert len(journal.events()) == 8


# ---------------------------------------------------------------------------
# The authority is the only honest source for the edge and the lease
# ---------------------------------------------------------------------------


def test_the_authority_records_the_edge_the_lease_and_the_release(harness: Any) -> None:
    harness.start()
    harness.authority.activate_generation(1, emits_input=True, requires_capture=False)
    session = harness.authority.service_session(1)
    lease = session.hold_key(InputKey.W, 200)
    assert lease is not None

    stages = [event.stage for event in harness.authority.lifecycle.events()]
    assert LifecycleStage.OS_EDGE_POSTED in stages
    assert LifecycleStage.LEASE_HELD in stages

    session.release_all("test")
    stages = [event.stage for event in harness.authority.lifecycle.events()]
    assert LifecycleStage.W_RELEASE_POSTED in stages
    assert LifecycleStage.LEDGER_EMPTY in stages


def test_a_post_that_raises_is_recorded_as_not_posted(harness: Any) -> None:
    """ "The post call returned" and "the post call raised" are different facts,
    and only one of them is any evidence at all."""
    harness.start()
    harness.authority.activate_generation(1, emits_input=True, requires_capture=False)
    harness.port.fail("key_down")
    session = harness.authority.service_session(1)
    assert session.hold_key(InputKey.W, 200) is None

    posted = [
        event
        for event in harness.authority.lifecycle.events()
        if event.stage is LifecycleStage.OS_EDGE_POSTED
    ]
    assert posted and posted[-1].fields["posted"] is False


# ---------------------------------------------------------------------------
# ...and it reaches the exported file
# ---------------------------------------------------------------------------


def test_the_stop_trace_carries_the_lifecycle_and_the_raw_events(
    harness: Any, tmp_path: Path
) -> None:
    harness.coordinator._paths = AppPaths(tmp_path).ensure()
    harness.start()
    harness.authority.lifecycle.note(LifecycleStage.LIVE_WORKER_ENTERED, "live-1")
    harness.authority.lifecycle.note(
        LifecycleStage.GAME_MOTION_NOT_CONFIRMED, "median +0.001 vs threshold 0.020"
    )
    harness.coordinator.events.add("live.armed", "token-abc")

    harness.submit(IntentType.STOP)
    assert harness.wait_for(lambda: list((tmp_path / "logs").glob("stop-*.jsonl")))

    written = sorted((tmp_path / "logs").glob("stop-*.jsonl"))[-1]
    rows = [json.loads(line) for line in written.read_text().splitlines() if line.strip()]
    kinds = {row["kind"] for row in rows}
    assert "lifecycle" in kinds, f"the stop trace still has only {kinds}"
    assert "event" in kinds

    stages = {row["stage"] for row in rows if row["kind"] == "lifecycle"}
    assert LifecycleStage.LIVE_WORKER_ENTERED.value in stages
    assert LifecycleStage.GAME_MOTION_NOT_CONFIRMED.value in stages
    names = {row["name"] for row in rows if row["kind"] == "event"}
    assert "live.armed" in names
    assert "intent.stop" in names


def test_a_recognized_chord_is_recorded_before_policy_runs(harness: Any) -> None:
    """ "The chord never arrived" and "the chord arrived and was refused" have
    completely different remedies, so they are different rows."""
    from prospector_engine.bindings import (
        ChordDisposition,
        ChordEvent,
        Modifier,
        binding_for_intent,
    )

    binding = binding_for_intent(IntentType.START_LIVE)
    assert binding is not None
    harness.coordinator.note_hotkey_edge(
        ChordEvent(ChordDisposition.RECOGNIZED, "n", frozenset({Modifier.CTRL}), binding)
    )
    stages = [event.stage for event in harness.authority.lifecycle.events()]
    assert LifecycleStage.CHORD_RECOGNIZED in stages


def test_ordinary_typing_is_not_recorded(harness: Any) -> None:
    """Every keystroke on the machine reaches the listener. Recording them all
    would push the events that matter out of a bounded ring in seconds."""
    from prospector_engine.bindings import ChordDisposition, ChordEvent

    before = len(harness.authority.lifecycle.events())
    for _ in range(200):
        harness.coordinator.note_hotkey_edge(
            ChordEvent(ChordDisposition.UNKNOWN_KEY, None, frozenset())
        )
    assert len(harness.authority.lifecycle.events()) == before


def test_a_near_miss_is_recorded_because_it_is_the_useful_one(harness: Any) -> None:
    from prospector_engine.bindings import ChordDisposition, ChordEvent, Modifier

    harness.coordinator.note_hotkey_edge(
        ChordEvent(ChordDisposition.NO_CHORD, "n", frozenset({Modifier.CTRL, Modifier.ALT}))
    )
    events = [
        event
        for event in harness.authority.lifecycle.events()
        if event.stage is LifecycleStage.PHYSICAL_EDGE_RECEIVED
    ]
    assert events and events[-1].fields["key"] == "n"


# ---------------------------------------------------------------------------
# A hold that comes up and is pressed again is a rattle, and it is counted
# ---------------------------------------------------------------------------


def test_a_hold_that_lapses_and_is_re_pressed_is_counted_and_named(harness: Any) -> None:
    """The difference between walking and shuffling, made visible.

    A single command's lease may not outlive its evidence, so the window it can
    ask for is the budget minus the age the frame already had. If frames arrive
    further apart than that, the lease expires before its renewal, the watchdog
    lifts the key, and the next command presses it again - a hold that is meant
    to be continuous comes out as a rattle.

    Lengthening the lease would be weakening a safety bound. Counting it is
    not, and a count that climbs during a run is the symptom named.
    """
    harness.start()
    harness.authority.activate_generation(1, emits_input=True, requires_capture=False)
    session = harness.authority.service_session(1)

    lease = session.hold_key(InputKey.W, 200)
    assert lease is not None
    assert harness.authority.hold_lapses == {}
    session.release(lease)

    # Pressed again immediately: this is the same hold, interrupted.
    again = session.hold_key(InputKey.W, 200)
    assert again is not None
    assert harness.authority.hold_lapses.get("w") == 1
    assert "re-pressed after lapsing" in harness.authority.describe_holds()

    lapsed = [
        event
        for event in harness.authority.lifecycle.events()
        if event.stage is LifecycleStage.HOLD_LAPSED
    ]
    assert lapsed and lapsed[-1].fields["target"] == "w"
    assert lapsed[-1].fields["gap_ms"] >= 0.0
    session.release_all("test")


def test_a_deliberate_stop_and_start_is_not_counted_as_a_lapse(harness: Any) -> None:
    """Only a re-press *soon* after a release is a hold that broke."""
    harness.start()
    harness.authority.activate_generation(1, emits_input=True, requires_capture=False)
    session = harness.authority.service_session(1)
    authority = harness.authority

    lease = session.hold_key(InputKey.W, 200)
    assert lease is not None
    session.release(lease)
    # Pretend the release happened well outside the window.
    authority._released_at_s["w"] = monotonic_s() - authority.HOLD_LAPSE_WINDOW_S - 1.0
    again = session.hold_key(InputKey.W, 200)
    assert again is not None
    assert authority.hold_lapses == {}
    session.release_all("test")
