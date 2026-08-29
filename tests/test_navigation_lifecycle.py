"""Can the navigator still move after the stages that prove it can move?

Every test here uses the **real** :class:`InputAuthority` and the **real**
:class:`NavigationInputSession`. That is the whole point of the file.

The defect it exists to prevent was invisible to the existing tests because
those tests fake ``release_navigation`` as "lift the keys". In production it is
``InputAuthority.release_navigation`` -> ``release_all`` -> ``_admission_open =
False`` **and** ``MovementActuator.disarm``. Admission is reopened in exactly
one place, ``activate_generation``, and only on a mode transition. So a fake
that merely lifts keys reports a healthy run for a code path that has muted the
session for good.

The chain that ran on the owner's machine, on the success path of every Live
start (``stop-epoch4-1914449166.jsonl``)::

    _LiveControlPort.release_forward("acceptance-probe-complete")
    _LiveControlPort.release_forward("acceptance-complete")
    _LiveControlPort.release_turn()
    _LiveControlPort.release_forward("prologue-complete")
      -> NavigationInputSession.release_navigation
        -> InputAuthority.release_navigation
          -> InputAuthority.release_all
            -> MovementActuator.disarm            # <- the follower is now mute

Four full release floors, all of them on the way to "the camera turns, go and
navigate". These tests assert on the one thing that matters afterwards: a ``W``
down edge reaching the port.
"""

from __future__ import annotations

from typing import Any

import pytest

from prospector_engine.contracts import (
    InputKey,
    SafetyFault,
    SafetyFaultKind,
)
from prospector_engine.input_authority import (
    AuthorityConfig,
    HealthSources,
    InputAuthority,
)
from prospector_engine.movement import DesiredMovement
from tests.fakes import FakeDeadmanClient, FakePlatformPort, VirtualClock


@pytest.fixture
def authority(clock: VirtualClock, port: FakePlatformPort, deadman: FakeDeadmanClient) -> Any:
    made = InputAuthority(
        port,
        deadman=deadman,
        health=HealthSources(
            focus=port.focus_state,
            client_rect=port.window_geometry,
            capture_age_s=lambda: 0.005,
        ),
        config=AuthorityConfig(),
        run_id="lifecycle-test",
    )
    made.start_watchdog()
    made.activate_generation(
        1, emits_input=True, requires_capture=True, pinned_rect=port.window_geometry()
    )
    yield made
    made.stop_watchdog(timeout_s=0.5)


def _down_edges(port: FakePlatformPort, key: InputKey) -> int:
    code = port.key_code(key)
    return sum(1 for op, args in port.ops() if op == "key_down" and args == (code,))


# ---------------------------------------------------------------------------
# 1-4: the prologue's cleanup must not be a lifecycle event
# ---------------------------------------------------------------------------


def test_the_acceptance_probes_release_leaves_the_session_able_to_press(
    authority: Any, port: FakePlatformPort
) -> None:
    """``stop_moving`` after a pulse; the very next frame may press again."""
    session = authority.navigation_session(1)
    session.move(DesiredMovement(forward=1, reason="acceptance pulse"))
    assert InputKey.W in session.movement.held

    session.stop_moving("acceptance-probe-complete")

    assert session.movement.held == frozenset(), "the pulse is over; W must be up"
    assert session.movement.armed, "an ordinary stop must not disarm the actuator"
    assert authority.ledger_empty()

    before = _down_edges(port, InputKey.W)
    outcome = session.move(DesiredMovement(forward=1, reason="following the arrow"))

    assert outcome.block.value == "", outcome.block.value
    assert InputKey.W in outcome.held
    assert _down_edges(port, InputKey.W) == before + 1, "no W edge reached the port"


def test_a_turn_probes_release_leaves_the_session_able_to_press(
    authority: Any, port: FakePlatformPort
) -> None:
    """The same rule for the camera half, which releases after every probe."""
    session = authority.navigation_session(1)
    session.move(DesiredMovement(turn=1, reason="setup probe: arrow keys +200"))
    assert InputKey.RIGHT in session.movement.held

    session.stop_moving("setup probe complete")

    assert session.movement.armed
    before = _down_edges(port, InputKey.W)
    session.move(DesiredMovement(forward=1, reason="following the arrow"))

    assert _down_edges(port, InputKey.W) == before + 1


def test_the_whole_prologue_cleanup_sequence_leaves_movement_armed(
    authority: Any, port: FakePlatformPort
) -> None:
    """Every release the prologue performs, in production order, then a press.

    This is the regression in its exact shape. If any one of these four
    reverts to ``release_navigation``, the final assertion fails - which is
    what the fake-backed prologue test could not see.
    """
    session = authority.navigation_session(1)
    session.move(DesiredMovement(forward=1, reason="input acceptance"))
    session.stop_moving("acceptance-probe-complete")
    session.stop_moving("acceptance-complete")
    session.move(DesiredMovement(turn=-1, reason="setup probe"))
    session.stop_moving("setup probe complete")
    session.stop_moving("prologue-complete")

    assert session.movement.armed
    assert session.movement.held == frozenset()

    before = _down_edges(port, InputKey.W)
    outcome = session.move(DesiredMovement(forward=1, reason="navigating"))

    assert InputKey.W in outcome.held
    assert _down_edges(port, InputKey.W) == before + 1


def test_release_navigation_is_still_a_full_disarm(authority: Any) -> None:
    """The floor is kept where it belongs: worker exit, Stop, safety.

    The fix is not "stop disarming"; it is "stop disarming on the success
    path". Everything that *should* reach the floor still does.
    """
    session = authority.navigation_session(1)
    session.move(DesiredMovement(forward=1, reason="navigating"))

    session.release_navigation("worker-exit")

    assert not session.movement.armed
    assert session.movement.held == frozenset()
    assert session.move(DesiredMovement(forward=1)).block.name == "STOPPED"


# ---------------------------------------------------------------------------
# 9-10: one owner across a transition
# ---------------------------------------------------------------------------


def test_a_superseded_session_cannot_press(authority: Any, port: FakePlatformPort) -> None:
    """A cancelled worker can outlive its cancellation by one tick."""
    stale = authority.navigation_session(1)
    authority.activate_generation(
        2, emits_input=True, requires_capture=True, pinned_rect=port.window_geometry()
    )
    live = authority.navigation_session(2)

    before = _down_edges(port, InputKey.W)
    outcome = stale.move(DesiredMovement(forward=1, reason="a straggler's last tick"))

    assert outcome.block.name == "STOPPED"
    assert _down_edges(port, InputKey.W) == before, "a stale worker posted an edge"
    assert live.move(DesiredMovement(forward=1)).held == frozenset({InputKey.W})


def test_a_superseded_session_cannot_release_live_input(
    authority: Any, port: FakePlatformPort
) -> None:
    """The one that made Live a zombie without leaving a fingerprint.

    ``release_navigation`` used to ``del generation``: the argument was
    accepted and thrown away, so a straggling Shadow worker unwinding through
    its ``finally`` ran the whole release floor against the Live mode that had
    replaced it - and nothing in the trace said which worker did it.
    """
    stale = authority.navigation_session(1)
    authority.activate_generation(
        2, emits_input=True, requires_capture=True, pinned_rect=port.window_geometry()
    )
    live = authority.navigation_session(2)
    live.move(DesiredMovement(forward=1, reason="navigating"))
    assert InputKey.W in live.movement.held

    report = stale.release_navigation("worker-exit")
    assert report.reason.startswith("superseded-generation:1")

    assert InputKey.W in live.movement.held, "a stale worker released Live's key"
    assert live.movement.armed

    # ...and its ordinary stop is refused for the same reason.
    assert stale.stop_moving("straggler stop") == ()
    assert InputKey.W in live.movement.held


# ---------------------------------------------------------------------------
# 11-12: faults and Stop
# ---------------------------------------------------------------------------


def test_a_terminal_fault_releases_and_is_reported_to_its_owner(
    authority: Any, port: FakePlatformPort
) -> None:
    """Held W, positive focus loss: the key comes up and somebody is told."""
    seen: list[SafetyFault] = []
    authority._on_safety_fault = seen.append
    session = authority.navigation_session(1)
    session.move(DesiredMovement(forward=1, reason="navigating"))
    port.transcript.clear()

    port.set_focus(False)
    fault = authority.poll_safety()

    assert fault is not None and fault.kind is SafetyFaultKind.FOCUS_LOST
    assert ("key_up", (port.key_code(InputKey.W),)) in port.ops()
    assert authority.ledger_empty()
    assert not session.movement.armed
    assert [f.kind for f in seen] == [SafetyFaultKind.FOCUS_LOST], (
        "the fault must reach its callback, which is what submits it to the "
        "coordinator so LIVE visibly exits"
    )


def test_stop_lifts_every_key_and_button_it_can_press(
    authority: Any, port: FakePlatformPort
) -> None:
    session = authority.navigation_session(1)
    session.move(DesiredMovement(forward=1, strafe=1, turn=1, jump=True))
    port.transcript.clear()

    authority.release_all("stop:test")

    lifted = {args[0] for op, args in port.ops() if op == "key_up"}
    for key in (
        InputKey.W,
        InputKey.A,
        InputKey.S,
        InputKey.D,
        InputKey.LEFT,
        InputKey.RIGHT,
        InputKey.SPACE,
    ):
        assert port.key_code(key) in lifted, f"{key.value} was never lifted"
    lifted_buttons = {op for op, _args in port.ops() if op.endswith("mb_up")}
    assert lifted_buttons == {"lmb_up", "rmb_up", "mmb_up"}, (
        "a held mouse button would survive Stop"
    )
    assert authority.ledger_empty()
