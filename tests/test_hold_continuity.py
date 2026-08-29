"""The same properties as ``test_movement.py``, through the real wiring.

``test_movement.py`` drives a bare :class:`MovementActuator` over a recording
port. This drives the **real** :class:`InputAuthority` and the real
:class:`NavigationInputSession` that the live worker is handed, because the
seam between them is where the last defect lived and a unit test on either
side of it could not have found it.

The defect, for the record: ``release_navigation`` was wired to
``release_all``, which closed admission; admission is reopened only by
``activate_generation``, which only fires on a mode transition. So the first
ordinary "stop walking" - an arrival candidate, a deadband hold, one occluded
frame - muted the session permanently, and the prologue's own tidy-up did it on
the success path of every healthy start before the navigation loop read its
first frame (D-067).
"""

from __future__ import annotations

import time
from typing import Any

from prospector_engine.contracts import InputKey
from prospector_engine.movement import IDLE, DesiredMovement


def _session(rig: Any, generation: int = 1) -> Any:
    rig.authority.start_watchdog()
    rig.authority.activate_generation(
        generation,
        emits_input=True,
        cancellation=None,
        requires_capture=True,
        pinned_rect=rig.port.window_geometry(),
    )
    return rig.authority.navigation_session(generation)


def _edges(rig: Any, key: InputKey) -> tuple[int, int]:
    code = rig.port.key_code(key)
    ops = rig.port.ops()
    downs = sum(1 for op, args in ops if op == "key_down" and args == (code,))
    ups = sum(1 for op, args in ops if op == "key_up" and args == (code,))
    return (downs, ups)


# ---------------------------------------------------------------------------
# The regression
# ---------------------------------------------------------------------------


def test_an_ordinary_stop_does_not_mute_the_session(rig: Any) -> None:
    """The whole defect, in five lines, at the seam it lived on.

    Every gate downstream of this was healthy - focus, viewport, capture age,
    the helper, the ledger - and the session still could not press, because
    stopping and disarming were the same verb.
    """
    session = _session(rig)
    try:
        session.move(DesiredMovement(forward=1, reason="walking"))
        assert _edges(rig, InputKey.W)[0] == 1

        session.stop_moving("arrival candidate")
        outcome = session.move(DesiredMovement(forward=1, reason="walking again"))

        assert not outcome.block.blocking, f"the session was muted: {outcome.block.value}"
        assert _edges(rig, InputKey.W)[0] == 2
    finally:
        rig.authority.release_all("test")
        rig.authority.stop_watchdog()


def test_many_ticks_of_the_same_intent_are_one_press(rig: Any) -> None:
    session = _session(rig)
    try:
        for _ in range(40):
            session.move(DesiredMovement(forward=1))
        downs, ups = _edges(rig, InputKey.W)
    finally:
        rig.authority.release_all("test")
        rig.authority.stop_watchdog()

    assert downs == 1, f"{downs} presses for one continuous hold"
    assert ups == 0


def test_the_hold_survives_a_long_gap_between_ticks(rig: Any) -> None:
    """No frame, no capture, no evidence - the key is simply still down.

    This is what could not be expressed before: a hold was a chain of commands
    each bounded by one frame's evidence age, so a gap longer than that budget
    lifted the key and the next command pressed it again.
    """
    session = _session(rig)
    try:
        session.move(DesiredMovement(forward=1))
        time.sleep(0.25)
        session.move(DesiredMovement(forward=1))
        downs, ups = _edges(rig, InputKey.W)
    finally:
        rig.authority.release_all("test")
        rig.authority.stop_watchdog()

    assert (downs, ups) == (1, 0), f"a quarter second gap rattled the key: {downs}/{ups}"


def test_letting_go_is_one_release_and_the_ledger_agrees(rig: Any) -> None:
    session = _session(rig)
    try:
        for _ in range(5):
            session.move(DesiredMovement(forward=1))
        session.move(IDLE)

        assert _edges(rig, InputKey.W) == (1, 1)
        assert session.movement.empty
    finally:
        rig.authority.release_all("test")
        rig.authority.stop_watchdog()


# ---------------------------------------------------------------------------
# Everything still releases
# ---------------------------------------------------------------------------


def test_the_authority_release_floor_covers_the_actuator(rig: Any) -> None:
    """Stop must lift a key the *actuator* holds, not only a lease."""
    session = _session(rig)
    try:
        session.move(DesiredMovement(forward=1, turn=1))
        assert not session.movement.empty

        report = rig.authority.release_all("stop:test")

        assert report.ledger_empty and report.release_known_safe
        assert session.movement.empty
        assert _edges(rig, InputKey.W)[1] >= 1
        assert _edges(rig, InputKey.RIGHT)[1] >= 1
    finally:
        rig.authority.stop_watchdog()


def test_a_stop_disarms_so_a_racing_tick_cannot_press_again(rig: Any) -> None:
    """The property that makes Stop mean stop, kept while the mute was removed."""
    session = _session(rig)
    try:
        session.move(DesiredMovement(forward=1))
        rig.authority.release_all("stop:test")

        outcome = session.move(DesiredMovement(forward=1))

        assert outcome.block.blocking, "a tick landed a press after Stop"
        assert session.movement.empty
    finally:
        rig.authority.stop_watchdog()


def test_observing_cannot_press_at_all(rig: Any) -> None:
    """Shadow activates with ``emits_input=False`` and the actuator disarms."""
    rig.authority.start_watchdog()
    rig.authority.activate_generation(
        1,
        emits_input=False,
        cancellation=None,
        requires_capture=True,
        pinned_rect=rig.port.window_geometry(),
    )
    session = rig.authority.navigation_session(1)
    try:
        outcome = session.move(DesiredMovement(forward=1))
    finally:
        rig.authority.stop_watchdog()

    assert outcome.block.blocking
    assert _edges(rig, InputKey.W)[0] == 0


def test_a_signed_yaw_reaches_the_platform_in_both_directions(rig: Any) -> None:
    session = _session(rig)
    try:
        session.move(DesiredMovement(forward=1, yaw_px=12))
        session.move(DesiredMovement(forward=1, yaw_px=-12))

        deltas = [args[0] for op, args in rig.port.ops() if op == "drag_delta"]
        assert deltas == [12, -12]
        assert _edges(rig, InputKey.W) == (1, 0), "yaw disturbed the forward hold"
    finally:
        rig.authority.release_all("test")
        rig.authority.stop_watchdog()
