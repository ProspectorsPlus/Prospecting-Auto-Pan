"""Who owns control during Live, and what the log says about it.

The coordinator half of the same question ``test_navigation_lifecycle`` asks of
the authority. These run a real :class:`RuntimeCoordinator` over the fake
platform, because the properties being asserted - exactly one worker owns
input, a terminal fault visibly leaves LIVE, the readable log narrates the
whole path - are properties of the wiring rather than of any one object.

Everything here is driven through the physical-chord capability, exactly as a
listener drives it. Nothing pre-authorizes anything.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from prospector_engine.capture import GovernorState
from prospector_engine.contracts import (
    InputKey,
    IntentType,
    ModeResult,
    ModeResultKind,
    RunMode,
    SafetyFaultKind,
    monotonic_s,
)
from prospector_engine.coordinator import WorkerContext
from prospector_engine.movement import DesiredMovement
from prospector_engine.plainlog import Topic, Verdict
from tests.fakes import settle_cadence_for_live
from tests.test_runtime_concurrency import Harness


@pytest.fixture
def harness() -> Any:
    made = Harness()
    # The one wire ``build_application`` closes and the bare harness did not:
    # without it a terminal fault releases every edge and the coordinator goes
    # on believing it is in Live.
    made.authority._on_safety_fault = made.coordinator.submit_fault
    yield made
    made.close()


def _walking_worker(entered: threading.Event, stop: threading.Event) -> Any:
    """A Live worker that holds W and waits, like the follower does."""

    def worker(context: WorkerContext) -> ModeResult:
        session = context.navigation
        assert session is not None
        session.move(DesiredMovement(forward=1, reason="following the arrow"))
        entered.set()
        while not stop.is_set() and not context.cancellation.is_cancelled():
            time.sleep(0.005)
        return ModeResult(ModeResultKind.COMPLETED, "done")

    return worker


# ---------------------------------------------------------------------------
# 5: cadence is not an authorization
# ---------------------------------------------------------------------------


def test_a_cooling_down_governor_does_not_refuse_the_chord(harness: Any) -> None:
    """The exact state the retired gate refused seven times in one trace.

    ``stop-epoch6-1915252479.jsonl`` recorded ``live_refused`` with
    ``cadence:cooldown at 30 Hz`` while frames were arriving 24 ms apart. A
    cooldown is the governor deciding not to probe a higher tier for a while.
    It is not a statement about whether the picture is usable.
    """
    harness.register(
        IntentType.START_LIVE, "live", lambda ctx: ModeResult(ModeResultKind.COMPLETED, "ok")
    )
    harness.start()
    settle_cadence_for_live(harness.capture)
    governor = harness.capture.governor
    governor._state = GovernorState.COOLDOWN
    governor._cooldown_until_s = monotonic_s() + 60.0
    assert not harness.capture.metrics().live_eligible
    assert harness.coordinator.readiness().capture_fresh

    intent = harness.coordinator.chord_authority().intent(IntentType.START_LIVE, "Ctrl+N")
    harness.coordinator.submit(intent)

    assert harness.wait_for(lambda: harness.started == ["live"])


# ---------------------------------------------------------------------------
# 9-10: one worker owns control across the transition
# ---------------------------------------------------------------------------


def test_shadow_to_live_leaves_exactly_one_control_worker(harness: Any) -> None:
    """Shadow is cancelled and joined before the Live worker is created."""
    shadow_running = threading.Event()
    shadow_release = threading.Event()

    def shadow(context: WorkerContext) -> ModeResult:
        shadow_running.set()
        while not context.cancellation.is_cancelled():
            time.sleep(0.005)
        shadow_release.set()
        return ModeResult(ModeResultKind.CANCELLED, "cancelled")

    live_entered = threading.Event()
    live_stop = threading.Event()
    harness.register(IntentType.START_SHADOW, "shadow", shadow)
    harness.register(IntentType.START_LIVE, "live", _walking_worker(live_entered, live_stop))
    harness.start()

    harness.submit(IntentType.START_SHADOW)
    assert shadow_running.wait(2.0)
    shadow_worker_id = harness.coordinator._worker_id

    harness.chord(IntentType.START_LIVE)
    assert live_entered.wait(2.0)
    live_worker_id = harness.coordinator._worker_id

    try:
        assert harness.coordinator.mode is RunMode.LIVE
        assert shadow_release.is_set(), "the Shadow worker was never cancelled"
        assert live_worker_id != shadow_worker_id
        assert harness.started == ["shadow", "live"]
        assert harness.finished == ["shadow"], "Shadow must have exited before Live acts"

        # Both ids are in the readable log, which is where a person looks.
        story = " ".join(line.text for line in harness.coordinator.plain.lines())
        assert shadow_worker_id in story and live_worker_id in story
    finally:
        live_stop.set()


def test_a_straggling_shadow_worker_cannot_release_live_input(harness: Any) -> None:
    """A worker that outlives its join deadline is contained, not trusted.

    It is holding a session from a superseded generation, and both the press
    and the release paths refuse for one that is not current - so the worst it
    can do is run its ``finally`` into a refusal that is written down.
    """
    straggler_may_exit = threading.Event()
    straggler_done = threading.Event()

    def shadow(context: WorkerContext) -> ModeResult:
        # Blocked, exactly as a worker inside a native screen grab is blocked:
        # it does not see the cancellation until long after the join deadline.
        straggler_may_exit.wait(5.0)
        session = context.navigation or context.observer
        if session is not None:
            session.release_navigation("worker-exit")
        straggler_done.set()
        return ModeResult(ModeResultKind.CANCELLED, "late")

    live_entered = threading.Event()
    live_stop = threading.Event()
    harness.register(IntentType.START_LIVE, "live", _walking_worker(live_entered, live_stop))
    harness.coordinator.register_worker(
        IntentType.START_SHADOW, harness.worker("shadow", shadow)
    )
    harness.start()

    harness.submit(IntentType.START_SHADOW)
    time.sleep(0.05)
    harness.chord(IntentType.START_LIVE)
    assert live_entered.wait(3.0)
    assert InputKey.W in harness.authority.movement.held

    try:
        straggler_may_exit.set()
        assert straggler_done.wait(3.0)
        time.sleep(0.05)

        assert InputKey.W in harness.authority.movement.held, (
            "the straggler released the key the Live worker is holding"
        )
        assert harness.authority.movement.armed
        assert harness.coordinator.mode is RunMode.LIVE
    finally:
        live_stop.set()


# ---------------------------------------------------------------------------
# 11-12: faults and Stop
# ---------------------------------------------------------------------------


def test_a_terminal_fault_releases_and_visibly_exits_live(harness: Any) -> None:
    """No zombie: the key comes up *and* the dashboard stops saying LIVE.

    The fault path used to end at an event-log line. Everything downstream of
    the authority - the actuator's armed flag, the release floor - did its job,
    and the one object that owns ``RunMode`` was never told, so the header read
    LIVE over a runtime whose next command could only answer "the navigator is
    stopped".
    """
    entered = threading.Event()
    stop = threading.Event()
    harness.register(IntentType.START_LIVE, "live", _walking_worker(entered, stop))
    harness.start()
    harness.chord(IntentType.START_LIVE)
    assert entered.wait(2.0)
    assert InputKey.W in harness.authority.movement.held
    harness.port.transcript.clear()

    harness.port.set_focus(False)
    fault = harness.authority.poll_safety()

    try:
        assert fault is not None and fault.kind is SafetyFaultKind.FOCUS_LOST
        assert ("key_up", (harness.port.key_code(InputKey.W),)) in harness.port.ops()
        assert harness.wait_for(lambda: harness.coordinator.mode is not RunMode.LIVE), (
            "LIVE never exited: the dashboard would still say it is navigating"
        )
        assert harness.authority.ledger_empty()
        assert not harness.authority.movement.armed
    finally:
        stop.set()


def test_stop_releases_every_movement_input_within_the_deadline(harness: Any) -> None:
    entered = threading.Event()
    stop = threading.Event()

    def worker(context: WorkerContext) -> ModeResult:
        session = context.navigation
        assert session is not None
        session.move(DesiredMovement(forward=1, strafe=-1, turn=1, jump=True))
        entered.set()
        while not stop.is_set() and not context.cancellation.is_cancelled():
            time.sleep(0.005)
        return ModeResult(ModeResultKind.CANCELLED, "stopped")

    harness.register(IntentType.START_LIVE, "live", worker)
    harness.start()
    harness.chord(IntentType.START_LIVE)
    assert entered.wait(2.0)
    harness.port.transcript.clear()

    began = monotonic_s()
    harness.submit(IntentType.STOP)
    assert harness.wait_for(lambda: harness.coordinator.mode is RunMode.IDLE)
    elapsed_ms = (monotonic_s() - began) * 1000.0
    stop.set()

    lifted = {args[0] for op, args in harness.port.ops() if op == "key_up"}
    for key in (
        InputKey.W,
        InputKey.A,
        InputKey.S,
        InputKey.D,
        InputKey.LEFT,
        InputKey.RIGHT,
        InputKey.SPACE,
    ):
        assert harness.port.key_code(key) in lifted, f"{key.value} survived Stop"
    assert {op for op, _ in harness.port.ops() if op.endswith("mb_up")} == {
        "lmb_up",
        "rmb_up",
        "mmb_up",
    }
    assert harness.authority.ledger_empty()
    assert elapsed_ms < 2000.0, f"Stop took {elapsed_ms:.0f} ms"


# ---------------------------------------------------------------------------
# 13: the readable log narrates the whole path
# ---------------------------------------------------------------------------


def test_the_readable_log_narrates_authorization_input_and_fault(harness: Any) -> None:
    """One run, read end to end in the log a person is actually shown.

    The complaint this answers is not "there is no output". There were sixteen
    lifecycle stages and two event rings. It is that none of them said, in one
    place and in order: you pressed the chord, the frame was this fresh, the
    cadence was warming up and that did not matter, the mode changed, this key
    went down through this mechanism, it was held this long, it came up, and
    this is why everything stopped.
    """
    entered = threading.Event()
    stop = threading.Event()
    harness.register(IntentType.START_LIVE, "live", _walking_worker(entered, stop))
    harness.start()
    plain = harness.coordinator.plain

    harness.chord(IntentType.START_LIVE)
    assert entered.wait(2.0)
    time.sleep(0.05)
    harness.port.set_focus(False)
    harness.authority.poll_safety()
    assert harness.wait_for(lambda: harness.coordinator.mode is not RunMode.LIVE)
    stop.set()

    story = "\n".join(plain.rendered(400))
    lines = plain.lines(400)

    assert "You pressed Ctrl+N." in story
    assert "Latest frame" in story and "limit" in story, "the freshness check and its bound"
    assert "ENTER LIVE" in story and "worker live-" in story
    assert "key=W DOWN" in story and "backend=" in story
    assert "HOLD W" in story and "key=W UP" in story
    assert "LIVE -> " in story, "the mode change a person watches for"

    verdicts = {line.verdict for line in lines}
    assert Verdict.STATE in verdicts and Verdict.INPUT in verdicts
    assert {Topic.CHORD, Topic.GATE, Topic.STATE, Topic.INPUT} <= {line.topic for line in lines}
    # A control line is never lost to frame telemetry, whatever else happened.
    assert any(line.topic is Topic.CHORD for line in lines)


def test_an_advisory_cadence_is_narrated_as_a_warning_not_a_refusal(harness: Any) -> None:
    harness.register(
        IntentType.START_LIVE, "live", lambda ctx: ModeResult(ModeResultKind.COMPLETED, "ok")
    )
    harness.start()
    assert not harness.capture.metrics().live_eligible

    intent = harness.coordinator.chord_authority().intent(IntentType.START_LIVE, "Ctrl+N")
    harness.coordinator.submit(intent)
    assert harness.wait_for(lambda: harness.started == ["live"])

    warnings = [
        line for line in harness.coordinator.plain.lines(200) if line.verdict is Verdict.WARN
    ]
    assert any("adaptive only, not blocking" in line.text for line in warnings), "\n".join(
        harness.coordinator.plain.rendered(200)
    )
