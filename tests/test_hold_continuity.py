"""One down edge, one up edge, and a hold that survives an ordinary hiccup.

The failure these exist to stop is a *timing* one, not a platform one. The
macOS forward key was already posted through the same Quartz call and the same
virtual keycode Prospector Lite uses; what differed was how long the lease
covering it was granted for.

The lease horizon used to be whatever remained of the frame's own evidence
budget - ``max_evidence_age_ms`` minus the age the frame already had. A command
built from a 70 ms-old frame therefore asked for a 30 ms lease. The next frame
arrived 33 ms later, the lease had already expired, the watchdog lifted the
key, and the command built from *that* frame pressed it again. A hold meant to
be continuous came out as a rattle, and from inside the process every single
step of it looked correct.

So the two budgets are separated and both are asserted here:

* **evidence** - how stale a frame may be and still authorize a *new decision*.
  Unchanged, and still enforced in ``apply_navigation_command``.
* **hold** - how long a key already down may stay down with no fresh frame at
  all. This is the lease horizon and the watchdog's stall budget, and it is
  what a renewal is granted.

Nothing here emits OS input: the platform port is a fake that records edges.
"""

from __future__ import annotations

from typing import Any

import pytest

from prospector_engine.capture import EvidenceRegistry
from prospector_engine.contracts import (
    InputKey,
    NavigationApplyStatus,
    NavigationCommand,
    SafetyFaultKind,
)
from prospector_engine.input_authority import AuthorityConfig
from tests.fakes import make_frame

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class Walker:
    """Drives one navigation session frame by frame, like the live worker.

    Deliberately *not* a helper that reaches into the authority: it produces
    frames, mints evidence for them and applies commands, which is the whole of
    what the live loop does. If the rattle can be reproduced through this, it
    can be reproduced in production.
    """

    def __init__(self, rig: Any, *, generation: int = 1) -> None:
        self.rig = rig
        self.generation = generation
        rig.authority.activate_generation(
            generation,
            emits_input=True,
            cancellation=None,
            requires_capture=True,
            pinned_rect=rig.port.window_geometry(),
        )
        self.registry = EvidenceRegistry(
            rig.authority.run_id, on_token=rig.authority.register_evidence
        )
        self.registry.set_generation(generation)
        self.session = rig.authority.navigation_session(generation)
        self.sequence = 0

    def step(
        self,
        *,
        forward: int = 1,
        turn: int = 0,
        yaw_px: int = 0,
        frame_age_ms: float = 20.0,
        gap_ms: float = 33.0,
    ) -> Any:
        """Advance the clock by ``gap_ms``, capture a frame, apply a command."""
        self.rig.clock.advance(gap_ms / 1000.0)
        now = self.rig.clock.now()
        captured_at_s = now - frame_age_ms / 1000.0
        self.sequence += 1
        frame = make_frame(self.sequence, captured_at_s=captured_at_s)
        envelope = self.registry.envelope_for(frame)
        self.rig.set_capture_age(frame_age_ms / 1000.0)
        command = NavigationCommand(
            generation=self.generation,
            source_frame_sequence=frame.sequence,
            source_captured_at_s=captured_at_s,
            forward_axis=forward,  # type: ignore[arg-type]
            lateral_axis=0,
            jump=False,
            yaw_delta_px=yaw_px,
            turn_axis=turn,  # type: ignore[arg-type]
            issued_at_s=now,
            valid_until_s=max(now + 0.001, captured_at_s + 0.09),
            reason="walking",
        )
        return self.session.apply_navigation_command(command, envelope.evidence_token)

    def skip(self, gap_ms: float) -> None:
        """Time passes and no frame arrives. The watchdog still runs."""
        self.rig.clock.advance(gap_ms / 1000.0)
        age = self.rig.capture_age_s() or 0.0
        self.rig.set_capture_age(age + gap_ms / 1000.0)
        self.rig.authority.poll_safety()


def _edges(rig: Any, key: InputKey) -> tuple[int, int]:
    code = rig.port.key_code(key)
    downs = sum(1 for op, args in rig.port.ops() if op == "key_down" and args == (code,))
    ups = sum(1 for op, args in rig.port.ops() if op == "key_up" and args == (code,))
    return (downs, ups)


# ---------------------------------------------------------------------------
# One press, one release
# ---------------------------------------------------------------------------


def test_forward_held_over_many_frames_posts_exactly_one_down_edge(rig: Any) -> None:
    """Thirty frames of "keep walking" is one key press, not thirty."""
    walker = Walker(rig)

    for _ in range(30):
        assert walker.step().status is NavigationApplyStatus.APPLIED

    downs, ups = _edges(rig, InputKey.W)
    assert downs == 1, f"{downs} down edges for one continuous hold"
    assert ups == 0, "the hold was interrupted"
    assert rig.authority.held_targets() == ("w",)
    assert rig.authority.hold_lapses == {}


def test_a_sustained_hold_is_reported_as_one_unbroken_duration(rig: Any) -> None:
    walker = Walker(rig)
    for _ in range(20):
        walker.step(gap_ms=33.0)

    held_s = rig.authority.forward_held_s(rig.clock.now())

    # Twenty frames at 33 ms is a two-thirds of a second walk, and the hold
    # duration must reflect the *press*, not the age of the newest lease.
    assert held_s == pytest.approx(20 * 0.033, abs=0.05)


def test_releasing_forward_posts_exactly_one_up_edge(rig: Any) -> None:
    walker = Walker(rig)
    for _ in range(10):
        walker.step(forward=1)

    walker.step(forward=0)

    downs, ups = _edges(rig, InputKey.W)
    assert (downs, ups) == (1, 1)
    assert rig.authority.forward_held_s(rig.clock.now()) == 0.0


def test_pressing_again_after_a_deliberate_release_is_a_second_press(rig: Any) -> None:
    """A real change of intention *should* produce a second edge."""
    walker = Walker(rig)
    for _ in range(4):
        walker.step(forward=1)
    walker.step(forward=0)
    for _ in range(4):
        walker.step(forward=1)

    downs, ups = _edges(rig, InputKey.W)
    assert (downs, ups) == (2, 1)


# ---------------------------------------------------------------------------
# The hold survives an ordinary hiccup
# ---------------------------------------------------------------------------


def test_a_late_frame_inside_the_hold_budget_does_not_lift_the_key(rig: Any) -> None:
    """One slow frame is not a safety event.

    This is the regression. Before the budgets were separated, a gap longer
    than what remained of the evidence window expired the lease, the watchdog
    lifted W, and the next frame pressed it again.
    """
    budget_ms = AuthorityConfig().max_capture_stall_ms
    walker = Walker(rig)
    for _ in range(5):
        walker.step()

    walker.skip(budget_ms - 40)  # a long frame, still inside the budget

    assert rig.authority.held_targets() == ("w",), "a late frame lifted the key"
    assert _edges(rig, InputKey.W) == (1, 0)

    # ...and the next frame renews rather than re-pressing.
    assert walker.step(frame_age_ms=20.0, gap_ms=1.0).applied
    assert _edges(rig, InputKey.W) == (1, 0)
    assert rig.authority.hold_lapses == {}


def test_several_dropped_frames_inside_the_budget_do_not_rattle_the_key(
    rig: Any,
) -> None:
    walker = Walker(rig)
    walker.step()

    for _ in range(6):
        # Each gap is longer than the evidence budget and shorter than the
        # hold budget: exactly the shape that used to produce a rattle.
        walker.skip(120.0)
        walker.step(frame_age_ms=15.0, gap_ms=1.0)

    downs, ups = _edges(rig, InputKey.W)
    assert (downs, ups) == (1, 0), f"{downs} presses and {ups} releases for one walk"


def test_a_stall_past_the_hold_budget_releases_everything(rig: Any) -> None:
    """The budget is a bound, not a suggestion."""
    budget_ms = AuthorityConfig().max_capture_stall_ms
    walker = Walker(rig)
    walker.step()

    walker.skip(budget_ms + 200)

    downs, ups = _edges(rig, InputKey.W)
    assert rig.authority.ledger_empty()
    assert downs == 1 and ups >= 1


def test_the_evidence_rule_for_a_new_decision_is_unchanged(rig: Any) -> None:
    """A hold surviving a hiccup must not let a stale frame decide anything."""
    walker = Walker(rig)
    walker.step()

    over_budget = AuthorityConfig().max_evidence_age_ms + 40.0
    result = walker.step(frame_age_ms=over_budget)

    assert result.status is NavigationApplyStatus.REJECTED_EVIDENCE


# ---------------------------------------------------------------------------
# Everything releases, on every path
# ---------------------------------------------------------------------------


def test_losing_focus_releases_the_hold(rig: Any) -> None:
    walker = Walker(rig)
    walker.step()

    rig.port.set_focus(False)
    fault = rig.authority.poll_safety()

    downs, ups = _edges(rig, InputKey.W)
    assert fault is not None and fault.kind is SafetyFaultKind.FOCUS_LOST
    assert rig.authority.ledger_empty()
    assert downs == 1 and ups >= 1


def test_an_unhealthy_deadman_releases_the_hold(rig: Any) -> None:
    walker = Walker(rig)
    walker.step()

    rig.deadman.set_healthy(False)
    fault = rig.authority.poll_safety()

    assert fault is not None and fault.kind is SafetyFaultKind.DEADMAN_UNHEALTHY
    assert rig.authority.ledger_empty()


def test_stop_releases_the_hold_and_the_ledger_is_empty(rig: Any) -> None:
    walker = Walker(rig)
    walker.step()

    report = rig.authority.release_all("stop:test")

    downs, ups = _edges(rig, InputKey.W)
    assert report.ledger_empty and report.release_known_safe
    assert downs == 1 and ups >= 1, "the release floor did not lift forward"


def test_a_worker_exception_cannot_leave_a_key_held(rig: Any) -> None:
    """The live worker's ``finally`` is what this stands in for."""
    walker = Walker(rig)
    try:
        walker.step()
        raise RuntimeError("the worker blew up mid-walk")
    except RuntimeError:
        walker.session.release_navigation("worker-exit")

    downs, ups = _edges(rig, InputKey.W)
    assert rig.authority.ledger_empty()
    assert downs == 1 and ups >= 1


# ---------------------------------------------------------------------------
# Turning
# ---------------------------------------------------------------------------


def test_a_signed_yaw_request_posts_the_same_sign(rig: Any) -> None:
    walker = Walker(rig)

    walker.step(yaw_px=12)
    walker.step(yaw_px=-12)

    deltas = [args[0] for op, args in rig.port.ops() if op == "drag_delta"]
    assert deltas == [12, -12]
    assert rig.authority.last_yaw()[0] == -12


def test_the_two_turn_keys_can_never_be_held_at_once(rig: Any) -> None:
    walker = Walker(rig)

    walker.step(forward=0, turn=1)
    assert rig.authority.held_targets() == ("right",)

    walker.step(forward=0, turn=-1)
    assert rig.authority.held_targets() == ("left",)
    assert _edges(rig, InputKey.RIGHT) == (1, 1)
