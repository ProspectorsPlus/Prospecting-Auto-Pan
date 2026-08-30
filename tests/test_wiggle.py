"""Wiggle service tests (D-088).

``run_wiggle`` ports the legacy standalone macro's ``wiggleMove()`` onto the
bounded-service pattern: the same local-frame rotation and duty-cycle math,
realized as leased ``hold_key``/``renew``/``release`` calls through
``ServiceInputSession`` instead of raw ``key_down``/``key_up``. These tests
check the two things that mattered about that change - every key it ever
presses gets released, on both the happy path and cancellation - plus the
pure rotation/weighting/phase math the timing is built from.
"""

from __future__ import annotations

import itertools
from typing import Any

import pytest

from prospector_engine import engine
from prospector_engine.capture import EvidenceRegistry
from prospector_engine.contracts import InputKey, WiggleOutcome
from tests.fakes import FakeCancellation, FakeFrameSource, make_frame

WASD_AND_SPACE = (InputKey.W, InputKey.A, InputKey.S, InputKey.D, InputKey.SPACE)


def _push_frames(
    context: engine.ServiceContext, clock: Any, pixel_scripts: list[dict[Any, Any]]
) -> None:
    """Load a scripted sequence of pixel states; the last one repeats once exhausted."""
    registry = EvidenceRegistry("wiggle-to-chest-test")
    source = FakeFrameSource()
    for index, pixels in enumerate(pixel_scripts, start=1):
        source.push(
            registry.envelope_for(make_frame(index, captured_at_s=clock.now(), pixels=pixels))
        )
    context.frames = source


def test_wiggle_holds_space_once_for_the_whole_run(
    rig: Any, service_context: engine.ServiceContext
) -> None:
    result = engine.run_wiggle(service_context, degree=0.0, forward_s=0.08, backward_s=0.04)

    assert result.outcome is WiggleOutcome.SUCCESS
    space_code = rig.port.key_code(InputKey.SPACE)
    space_downs = [
        args for op, args in rig.port.ops() if op == "key_down" and args[0] == space_code
    ]
    space_ups = [
        args for op, args in rig.port.ops() if op == "key_up" and args[0] == space_code
    ]
    assert len(space_downs) == 1, "space is acquired once, never toggled mid-run"
    assert len(space_ups) == 1


def test_wiggle_releases_every_key_it_ever_pressed(
    rig: Any, service_context: engine.ServiceContext
) -> None:
    engine.run_wiggle(service_context, degree=37.0, forward_s=0.12, backward_s=0.05)

    downs = sorted(args[0] for op, args in rig.port.ops() if op == "key_down")
    ups = sorted(args[0] for op, args in rig.port.ops() if op == "key_up")
    assert downs == ups, "every key press has a matching release"
    assert rig.authority.ledger_empty()


def test_wiggle_never_commands_outside_wasd_and_space(
    rig: Any, service_context: engine.ServiceContext
) -> None:
    engine.run_wiggle(service_context, degree=123.0, forward_s=0.1, backward_s=0.05)

    allowed = {rig.port.key_code(key) for key in WASD_AND_SPACE}
    codes = {args[0] for op, args in rig.port.ops() if op in ("key_down", "key_up")}
    assert codes <= allowed


def test_wiggle_clips_to_its_configured_max_duration(
    rig: Any, service_context: engine.ServiceContext
) -> None:
    config = engine.WiggleConfig(max_forward_s=0.08, max_backward_s=0.04)

    result = engine.run_wiggle(
        service_context, degree=0.0, forward_s=10_000.0, backward_s=10_000.0, config=config
    )

    assert result.outcome is WiggleOutcome.SUCCESS
    assert result.elapsed_s < 2.0, "an unbounded caller request must not run unbounded"


def test_wiggle_degree_is_taken_mod_360(
    rig: Any, service_context: engine.ServiceContext
) -> None:
    result = engine.run_wiggle(service_context, degree=725.0, forward_s=0.02, backward_s=0.0)
    assert result.degree_deg == pytest.approx(5.0)


def test_cancelling_wiggle_mid_run_still_releases_every_key(
    rig: Any, service_context: engine.ServiceContext
) -> None:
    """Same class of bug as B1/B7/B3: a cancellation must never leave a
    direction key or space stuck down."""
    cancellation = FakeCancellation(rig.clock, cancel_after_waits=3)
    service_context.cancel = cancellation
    rig.authority.activate_generation(
        1,
        emits_input=True,
        cancellation=cancellation,
        requires_capture=True,
        pinned_rect=rig.port.window_geometry(),
    )

    result = engine.run_wiggle(service_context, degree=0.0, forward_s=5.0, backward_s=0.5)

    assert result.outcome is WiggleOutcome.CANCELLED
    downs = sorted(args[0] for op, args in rig.port.ops() if op == "key_down")
    ups = sorted(args[0] for op, args in rig.port.ops() if op == "key_up")
    assert downs == ups
    assert rig.authority.ledger_empty()


def test_wiggle_refuses_cleanly_when_the_space_lease_is_refused(
    rig: Any, service_context: engine.ServiceContext
) -> None:
    """No generation is active, so the very first acquire (space) is refused."""
    unarmed = engine.ServiceContext(
        frames=service_context.frames,
        session=rig.authority.service_session(999),
        cancel=service_context.cancel,
        deadline_s=service_context.deadline_s,
    )

    result = engine.run_wiggle(unarmed, degree=0.0, forward_s=0.1, backward_s=0.05)

    assert result.outcome is WiggleOutcome.FAILED
    assert rig.port.ops() == []
    assert rig.authority.ledger_empty()


# ---------------------------------------------------------------------------
# Pure math: rotation, key weights, phase shape
# ---------------------------------------------------------------------------


def test_rotate_cw_leaves_the_vector_unchanged_at_zero_degrees() -> None:
    assert engine._rotate_cw(0.0, 1.0, 0.0) == pytest.approx((0.0, 1.0))


def test_rotate_cw_turns_forward_into_right_at_90_degrees() -> None:
    x, y = engine._rotate_cw(0.0, 1.0, 90.0)
    assert (x, y) == pytest.approx((1.0, 0.0), abs=1e-9)


def test_wiggle_key_weights_normalizes_to_the_dominant_axis() -> None:
    weights = engine._wiggle_key_weights(0.5, 1.0)
    assert weights[InputKey.W] == pytest.approx(1.0)
    assert weights[InputKey.D] == pytest.approx(0.5)
    assert weights[InputKey.A] == 0.0
    assert weights[InputKey.S] == 0.0


def test_wiggle_key_weights_is_all_zero_for_the_zero_vector() -> None:
    weights = engine._wiggle_key_weights(0.0, 0.0)
    assert all(value == 0.0 for value in weights.values())


def test_wiggle_side_phases_start_and_end_on_the_same_side() -> None:
    phases = engine._wiggle_side_phases(2000.0, 250.0)
    assert phases[0][0] == -1
    assert phases[-1][0] == -1
    assert sum(duration for _, duration in phases) == pytest.approx(2000.0)


def test_wiggle_side_phases_alternate_sign_between_the_bookends() -> None:
    phases = engine._wiggle_side_phases(2000.0, 250.0)
    inner = [side for side, _ in phases[1:-1]]
    assert all(a != b for a, b in itertools.pairwise(inner))


def test_wiggle_side_phases_is_empty_for_zero_or_negative_duration() -> None:
    assert engine._wiggle_side_phases(0.0, 250.0) == []
    assert engine._wiggle_side_phases(-10.0, 250.0) == []


# ---------------------------------------------------------------------------
# on_chest_spot: the X_MARKS_THE_SPOT detector (D-092)
# ---------------------------------------------------------------------------

CHEST = engine.DEFAULT_CHEST_PIXEL


def _inverted(rgb: engine.Rgb) -> tuple[int, int, int]:
    """A colour guaranteed far outside tolerance of ``rgb``, whatever it is."""
    return tuple(255 - int(c) for c in rgb)  # type: ignore[return-value]


def _channel_limits(tolerance: engine.Tolerance) -> tuple[float, float, float]:
    """Mirrors ``color_close``'s own tolerance resolution (D-100)."""
    return tolerance if isinstance(tolerance, tuple) else (tolerance / 100.0 * 255.0,) * 3


CHEST_FOUND = {CHEST.point_px: CHEST.target_rgb, CHEST.point_b_px: CHEST.target_b_rgb}
CHEST_NOT_FOUND = {
    CHEST.point_px: _inverted(CHEST.target_rgb),
    CHEST.point_b_px: _inverted(CHEST.target_b_rgb),
}


def test_on_chest_spot_true_when_both_points_match() -> None:
    frame = make_frame(1, pixels=CHEST_FOUND)
    assert engine.on_chest_spot(frame) is True


def test_on_chest_spot_false_when_only_the_first_point_matches() -> None:
    frame = make_frame(
        1,
        pixels={
            CHEST.point_px: CHEST.target_rgb,
            CHEST.point_b_px: _inverted(CHEST.target_b_rgb),
        },
    )
    assert engine.on_chest_spot(frame) is False


def test_on_chest_spot_false_when_only_the_second_point_matches() -> None:
    frame = make_frame(
        1,
        pixels={
            CHEST.point_px: _inverted(CHEST.target_rgb),
            CHEST.point_b_px: CHEST.target_b_rgb,
        },
    )
    assert engine.on_chest_spot(frame) is False


def test_on_chest_spot_true_within_tolerance() -> None:
    r, g, b = CHEST.target_rgb
    rb, gb, bb = CHEST.target_b_rgb
    half_r, _, half_b = (limit / 2.0 for limit in _channel_limits(CHEST.tolerance))
    half_rb, _, half_bb = (limit / 2.0 for limit in _channel_limits(CHEST.tolerance_b_pct))
    frame = make_frame(
        1,
        pixels={
            CHEST.point_px: (int(r + half_r), int(g), int(max(0.0, b - half_b))),
            CHEST.point_b_px: (int(rb + half_rb), int(gb), int(max(0.0, bb - half_bb))),
        },
    )
    assert engine.on_chest_spot(frame) is True


def test_on_chest_spot_false_outside_tolerance() -> None:
    r, g, b = CHEST.target_rgb
    limit_r = _channel_limits(CHEST.tolerance)[0]
    frame = make_frame(
        1,
        pixels={
            CHEST.point_px: (int(r + limit_r + 5), int(g), int(b)),
            CHEST.point_b_px: _inverted(CHEST.target_b_rgb),
        },
    )
    assert engine.on_chest_spot(frame) is False


def test_on_chest_spot_false_on_unrelated_colour() -> None:
    frame = make_frame(1, pixels=CHEST_NOT_FOUND)
    assert engine.on_chest_spot(frame) is False


# ---------------------------------------------------------------------------
# run_wiggle_to_chest: Ctrl+4's repeat-until-found behaviour (D-092)
# ---------------------------------------------------------------------------


def test_wiggle_to_chest_stops_before_moving_if_already_found(
    rig: Any, service_context: engine.ServiceContext
) -> None:
    """Chest is checked before movement is applied each tick, so a spot
    already on screen when Ctrl+4 fires must produce zero W/A/S/D presses."""
    _push_frames(service_context, rig.clock, [CHEST_FOUND])

    result = engine.run_wiggle_to_chest(service_context, angle_source=lambda: 0.0)

    assert result.outcome is WiggleOutcome.SUCCESS
    assert "found after 1 pass" in result.detail
    directional = {
        rig.port.key_code(key) for key in (InputKey.W, InputKey.A, InputKey.S, InputKey.D)
    }
    codes = {args[0] for op, args in rig.port.ops() if op in ("key_down", "key_up")}
    assert not (codes & directional), "no directional key should ever be pressed"


def test_wiggle_to_chest_stops_mid_pass_once_found_and_releases_every_key(
    rig: Any, service_context: engine.ServiceContext
) -> None:
    _push_frames(service_context, rig.clock, [CHEST_NOT_FOUND, CHEST_FOUND])

    result = engine.run_wiggle_to_chest(
        service_context, angle_source=lambda: 0.0, forward_s=1.0, backward_s=0.5
    )

    assert result.outcome is WiggleOutcome.SUCCESS
    downs = sorted(args[0] for op, args in rig.port.ops() if op == "key_down")
    ups = sorted(args[0] for op, args in rig.port.ops() if op == "key_up")
    assert downs == ups, "every key press has a matching release"
    assert rig.authority.ledger_empty()
    w_code = rig.port.key_code(InputKey.W)
    assert w_code in downs, "degree=0 should have driven forward (W) before the spot was found"


def test_wiggle_to_chest_never_commands_outside_wasd_and_space(
    rig: Any, service_context: engine.ServiceContext
) -> None:
    _push_frames(service_context, rig.clock, [CHEST_NOT_FOUND, CHEST_FOUND])

    engine.run_wiggle_to_chest(
        service_context, angle_source=lambda: 123.0, forward_s=1.0, backward_s=0.5
    )

    allowed = {rig.port.key_code(key) for key in WASD_AND_SPACE}
    codes = {args[0] for op, args in rig.port.ops() if op in ("key_down", "key_up")}
    assert codes <= allowed


def test_wiggle_to_chest_reads_the_angle_source_fresh_on_every_pass(
    rig: Any, service_context: engine.ServiceContext
) -> None:
    """Whether Ctrl+5 is active must not matter to this loop - it just reads
    whatever ``angle_source`` reports, every pass, live."""
    _push_frames(service_context, rig.clock, [CHEST_NOT_FOUND])
    angles = iter([0.0, 90.0, 725.0])
    calls: list[float] = []

    def angle_source() -> float:
        value = next(angles)
        calls.append(value)
        return value

    limits = engine.WiggleToChestLimits(max_passes=3)
    result = engine.run_wiggle_to_chest(
        service_context,
        angle_source=angle_source,
        forward_s=0.1,
        backward_s=0.05,
        limits=limits,
    )

    assert result.outcome is WiggleOutcome.TIMEOUT
    assert calls == [0.0, 90.0, 725.0], "each pass must read a fresh angle, not a cached one"
    assert result.degree_deg == pytest.approx(5.0), "725 deg mod 360, from the final pass"
    assert "pass cap (3) reached" in result.detail


def test_wiggle_to_chest_releases_every_key_on_cancellation(
    rig: Any, service_context: engine.ServiceContext
) -> None:
    _push_frames(service_context, rig.clock, [CHEST_NOT_FOUND])
    cancellation = FakeCancellation(rig.clock, cancel_after_waits=3)
    service_context.cancel = cancellation
    rig.authority.activate_generation(
        1,
        emits_input=True,
        cancellation=cancellation,
        requires_capture=True,
        pinned_rect=rig.port.window_geometry(),
    )

    result = engine.run_wiggle_to_chest(
        service_context, angle_source=lambda: 0.0, forward_s=5.0, backward_s=0.5
    )

    assert result.outcome is WiggleOutcome.CANCELLED
    downs = sorted(args[0] for op, args in rig.port.ops() if op == "key_down")
    ups = sorted(args[0] for op, args in rig.port.ops() if op == "key_up")
    assert downs == ups
    assert rig.authority.ledger_empty()


def test_wiggle_to_chest_refuses_cleanly_when_the_space_lease_is_refused(
    rig: Any, service_context: engine.ServiceContext
) -> None:
    _push_frames(service_context, rig.clock, [CHEST_NOT_FOUND])
    unarmed = engine.ServiceContext(
        frames=service_context.frames,
        session=rig.authority.service_session(999),
        cancel=service_context.cancel,
        deadline_s=service_context.deadline_s,
    )

    result = engine.run_wiggle_to_chest(unarmed, angle_source=lambda: 0.0)

    assert result.outcome is WiggleOutcome.FAILED
    assert rig.port.ops() == []
    assert rig.authority.ledger_empty()
