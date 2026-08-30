"""Phase 0A characterization: the bounded services still do what worked.

``tests/fixtures/legacy_sequences.json`` is an ordered transcript of the
pre-navigator engine's dig, dequip, pan-swap, and reset sequences, recorded
with every OS edge and every sleep intercepted (see the fixture's own
``_provenance`` block).

These tests assert that the rewritten services emit the **same input edges in
the same order with the same coordinates**. They deliberately do *not* preserve
the defects: B2 (unbounded dequip), B3 (cleanup skipped on cancel), B5 (Stop
released only the left button), and B6 (concurrent pan swaps) each get their
own test proving the old behavior is gone.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from prospector_engine import engine
from prospector_engine.capture import EvidenceRegistry
from prospector_engine.contracts import (
    DigOutcome,
    InputKey,
    MouseButton,
    PanSwapOutcome,
    ResetOutcome,
)
from tests.fakes import FakeCancellation, FakeFrameSource, make_frame

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "legacy_sequences.json").read_text())
LEGACY = FIXTURE["sequences"]

#: The legacy transcript recorded its own screen samples as events. The new
#: services read one coherent frame instead of grabbing per-pixel (bug B12), so
#: sampling is not an emitted edge any more and is excluded from the comparison.
NON_INPUT_OPS = ("sample_rgb",)


def legacy_ops(name: str) -> list[tuple[str, tuple[Any, ...]]]:
    return [
        (event["op"], tuple(int(a) if isinstance(a, float) else a for a in event["args"]))
        for event in LEGACY[name]
        if event["op"] not in NON_INPUT_OPS
    ]


def _frames(context: engine.ServiceContext, clock: Any, script: list[dict[Any, Any]]) -> None:
    """Load a scripted sequence of pixel states into the context's frame source."""
    registry = EvidenceRegistry("characterization")
    source = FakeFrameSource()
    for index, pixels in enumerate(script, start=1):
        source.push(
            registry.envelope_for(make_frame(index, captured_at_s=clock.now(), pixels=pixels))
        )
    context.frames = source


PROMPT_SHOWING = {
    engine.DEFAULT_PIXELS.pan_start_check_px: engine.DEFAULT_PIXELS.pan_start_check_rgb
}
PROMPT_CLEARED = {engine.DEFAULT_PIXELS.pan_start_check_px: (0, 0, 0)}
PAN_CONFIRMED = {engine.DEFAULT_PIXELS.pan_check_px: engine.DEFAULT_PIXELS.pan_check_rgb}


# ---------------------------------------------------------------------------
# Ordering equivalence with the legacy build
# ---------------------------------------------------------------------------


def test_dig_tap_matches_legacy_edges(rig: Any, service_context: engine.ServiceContext) -> None:
    pixels = engine.DEFAULT_PIXELS
    _frames(
        service_context,
        rig.clock,
        [
            {
                pixels.dig_spot_a_px: pixels.dig_spot_a_rgb,
                pixels.dig_spot_b_px: pixels.dig_spot_b_rgb,
                pixels.capacity_px: (10, 10, 10),
            }
        ],
    )
    result = engine.run_dig_at_current_spot(service_context)

    assert result.outcome is DigOutcome.DIG_PROGRESS
    assert result.evidence.on_dig_spot is True
    assert result.evidence.capacity_full is False
    assert rig.port.ops() == legacy_ops("dig_tap")


def test_dequip_pan_matches_legacy_edges(
    rig: Any, service_context: engine.ServiceContext
) -> None:
    # Same script the legacy transcript used: prompt visible twice, then clear.
    _frames(service_context, rig.clock, [PROMPT_SHOWING, PROMPT_SHOWING, PROMPT_CLEARED])
    cleared, detail, attempts = engine.run_dequip_pan(service_context)

    assert cleared is True
    assert attempts == 3, detail
    assert rig.port.ops() == legacy_ops("dequip_pan")


def test_pan_swap_matches_legacy_edges(
    rig: Any, service_context: engine.ServiceContext
) -> None:
    _frames(
        service_context,
        rig.clock,
        [PROMPT_SHOWING, PROMPT_SHOWING, PROMPT_CLEARED, PAN_CONFIRMED],
    )
    result = engine.run_pan_swap(service_context)

    assert result.outcome is PanSwapOutcome.SUCCESS
    assert result.attempts == 1
    assert rig.port.ops() == legacy_ops("pan_swap")


def test_reset_matches_legacy_edges(rig: Any, service_context: engine.ServiceContext) -> None:
    _frames(service_context, rig.clock, [PROMPT_SHOWING, PROMPT_SHOWING, PROMPT_CLEARED])
    result = engine.run_reset(service_context)

    assert result.outcome is ResetOutcome.SUCCESS
    assert rig.port.ops() == legacy_ops("reset_character")


def test_pan_swap_click_targets_are_the_calibrated_pixels() -> None:
    """The migrated coordinates are exactly the ones that worked, not new guesses."""
    moves = [tuple(e["args"]) for e in LEGACY["pan_swap"] if e["op"] == "move_abs_px"]
    pixels = engine.DEFAULT_PIXELS
    assert pixels.pan_menu_button_px in moves
    assert pixels.pan_first_slot_px in moves
    assert pixels.pan_equip_px in moves
    assert pixels.pan_bottom_slot_px in moves


# ---------------------------------------------------------------------------
# The defects are gone (they are recorded, not blessed)
# ---------------------------------------------------------------------------


def test_b2_dequip_is_bounded_when_the_prompt_never_clears(
    rig: Any, service_context: engine.ServiceContext
) -> None:
    """B2: the legacy loop pressed 1 forever if the prompt never cleared."""
    _frames(service_context, rig.clock, [PROMPT_SHOWING] * 200)
    cleared, detail, attempts = engine.run_dequip_pan(service_context)

    assert cleared is False
    assert attempts == engine.DEFAULT_TIMINGS.dequip_max_attempts
    assert "attempt cap" in detail


def test_b2_dequip_stops_at_the_deadline_before_the_attempt_cap(
    rig: Any, service_context: engine.ServiceContext
) -> None:
    _frames(service_context, rig.clock, [PROMPT_SHOWING] * 200)
    service_context.deadline_s = rig.clock.now() + 0.4  # under two poll intervals
    cleared, detail, attempts = engine.run_dequip_pan(service_context)

    assert cleared is False
    assert attempts < engine.DEFAULT_TIMINGS.dequip_max_attempts
    assert "deadline" in detail


def test_b3_reset_releases_the_camera_button_when_cancelled_mid_drag(
    rig: Any, service_context: engine.ServiceContext
) -> None:
    """B3: cancelling the legacy reset could leave state set and RMB held."""
    _frames(
        service_context, rig.clock, [PROMPT_SHOWING, PROMPT_CLEARED] + [PROMPT_CLEARED] * 200
    )
    cancellation = FakeCancellation(rig.clock, cancel_after_waits=40)
    service_context.cancel = cancellation
    rig.authority.activate_generation(
        1,
        emits_input=True,
        cancellation=cancellation,
        requires_capture=True,
        pinned_rect=rig.port.window_geometry(),
    )

    result = engine.run_reset(service_context)

    assert result.outcome is ResetOutcome.CANCELLED
    ops = [op for op, _ in rig.port.ops()]
    assert ops.count("rmb_down") == ops.count("rmb_up") == 1
    assert ops[-1] == "rmb_up" or "rmb_up" in ops
    assert rig.authority.ledger_empty()


def test_b5_release_all_lifts_every_key_not_just_the_left_button(rig: Any) -> None:
    """B5: Stop used to call ``mouse_up()`` and nothing else."""
    rig.activate()
    session = rig.session()
    assert session.hold_key(InputKey.W, 1000) is not None
    assert session.hold_key(InputKey.SHIFT, 1000) is not None
    assert session.hold_button(MouseButton.LEFT, 1000) is not None
    rig.port.transcript.clear()

    report = rig.authority.release_all("stop")

    lifted_keys = {args[0] for op, args in rig.port.ops() if op == "key_up"}
    assert lifted_keys == {rig.port.key_code(key) for key in InputKey}
    assert {"lmb_up", "rmb_up", "mmb_up"} <= {op for op, _ in rig.port.ops()}
    assert report.release_known_safe
    assert rig.authority.ledger_empty()


def test_b12_one_dig_decision_reads_exactly_one_frame(
    rig: Any, service_context: engine.ServiceContext
) -> None:
    """B12: the legacy tick sampled three independently captured instants."""
    pixels = engine.DEFAULT_PIXELS
    _frames(
        service_context,
        rig.clock,
        [
            {
                pixels.dig_spot_a_px: pixels.dig_spot_a_rgb,
                pixels.dig_spot_b_px: pixels.dig_spot_b_rgb,
                pixels.capacity_px: (10, 10, 10),
            }
        ],
    )
    source = service_context.frames
    engine.run_dig_at_current_spot(service_context)
    assert isinstance(source, FakeFrameSource)
    assert source.reads == 1


@pytest.mark.parametrize(
    ("capacity_rgb", "expected"),
    [
        (engine.DEFAULT_PIXELS.capacity_rgb, DigOutcome.PAN_FULL),
        ((100, 100, 100), DigOutcome.CUE_LOST),
    ],
)
def test_dig_gating_preserves_legacy_colour_semantics(
    rig: Any,
    service_context: engine.ServiceContext,
    capacity_rgb: tuple[int, int, int],
    expected: DigOutcome,
) -> None:
    pixels = engine.DEFAULT_PIXELS
    _frames(
        service_context,
        rig.clock,
        [{pixels.dig_spot_a_px: (255, 255, 255), pixels.capacity_px: capacity_rgb}],
    )
    result = engine.run_dig_at_current_spot(service_context)
    assert result.outcome is expected


def test_color_close_keeps_its_threshold_semantics() -> None:
    assert engine.color_close((51.0, 51.0, 51.0), (51.0, 51.0, 51.0), 10.0)
    assert engine.color_close((51.0, 51.0, 51.0), (70.0, 40.0, 60.0), 10.0)
    assert not engine.color_close((51.0, 51.0, 51.0), (110.0, 51.0, 51.0), 10.0)


def test_migrated_pixels_are_marked_pending_not_validated() -> None:
    """The old numbers came from a different geometry basis (plan 4.1)."""
    from prospector_engine.contracts import EvidenceStatus

    assert engine.DEFAULT_PIXELS.status is EvidenceStatus.PENDING
    assert engine.DEFAULT_PIXELS.provenance.status is EvidenceStatus.PENDING
    shifted = engine.DEFAULT_PIXELS.from_legacy_window_frame(56)
    assert shifted.dig_spot_a_px == (554, 603 - 56)
    assert shifted.provenance.status is EvidenceStatus.PENDING


# ---------------------------------------------------------------------------
# The standalone dig loop (DECISIONS.md D-015)
# ---------------------------------------------------------------------------


def test_the_dig_loop_taps_while_the_spot_matches(
    rig: Any, service_context: engine.ServiceContext
) -> None:
    pixels = engine.DEFAULT_PIXELS
    diggable = {
        pixels.dig_spot_a_px: pixels.dig_spot_a_rgb,
        pixels.dig_spot_b_px: pixels.dig_spot_b_rgb,
        pixels.capacity_px: (10, 10, 10),
    }
    lost = {pixels.dig_spot_a_px: (0, 0, 0), pixels.capacity_px: (10, 10, 10)}
    _frames(service_context, rig.clock, [diggable, diggable, diggable, lost])

    result = engine.run_dig_loop(service_context)

    assert result.taps == 3
    assert result.outcome is DigOutcome.CUE_LOST
    ops = [op for op, _ in rig.port.ops()]
    assert ops.count("lmb_down") == ops.count("lmb_up") == 3


def test_the_dig_loop_runs_a_pan_swap_when_capacity_reads_full(
    rig: Any, service_context: engine.ServiceContext
) -> None:
    """This is the legacy tick()'s behaviour, rebuilt on the bounded services."""
    pixels = engine.DEFAULT_PIXELS
    full = {
        pixels.dig_spot_a_px: pixels.dig_spot_a_rgb,
        pixels.dig_spot_b_px: pixels.dig_spot_b_rgb,
        pixels.capacity_px: pixels.capacity_rgb,
    }
    _frames(
        service_context,
        rig.clock,
        [
            full,
            PROMPT_SHOWING,
            PROMPT_CLEARED,
            PAN_CONFIRMED,
            {pixels.dig_spot_a_px: (0, 0, 0)},
        ],
    )

    result = engine.run_dig_loop(service_context)

    assert result.pan_swaps == 1
    assert "pan_swap#1:SUCCESS" in result.evidence[0]


def test_the_dig_loop_is_bounded_by_its_tap_cap(
    rig: Any, service_context: engine.ServiceContext
) -> None:
    """B2 again: the legacy loop had no cap of any kind."""
    pixels = engine.DEFAULT_PIXELS
    diggable = {
        pixels.dig_spot_a_px: pixels.dig_spot_a_rgb,
        pixels.dig_spot_b_px: pixels.dig_spot_b_rgb,
        pixels.capacity_px: (10, 10, 10),
    }
    _frames(service_context, rig.clock, [diggable] * 50)

    result = engine.run_dig_loop(service_context, engine.DigLoopLimits(max_taps=5))

    assert result.taps == 5
    assert result.outcome is DigOutcome.TIMEOUT
    assert "tap cap" in result.detail


def test_the_dig_loop_stops_at_its_deadline(
    rig: Any, service_context: engine.ServiceContext
) -> None:
    pixels = engine.DEFAULT_PIXELS
    diggable = {
        pixels.dig_spot_a_px: pixels.dig_spot_a_rgb,
        pixels.dig_spot_b_px: pixels.dig_spot_b_rgb,
        pixels.capacity_px: (10, 10, 10),
    }
    _frames(service_context, rig.clock, [diggable] * 5000)

    result = engine.run_dig_loop(
        service_context, engine.DigLoopLimits(max_taps=100000, deadline_ms=200)
    )

    assert result.outcome in (DigOutcome.TIMEOUT, DigOutcome.CANCELLED)
    assert result.taps < 100000


def test_the_dig_loop_cancels_within_one_wait_slice(
    rig: Any, service_context: engine.ServiceContext
) -> None:
    pixels = engine.DEFAULT_PIXELS
    diggable = {
        pixels.dig_spot_a_px: pixels.dig_spot_a_rgb,
        pixels.dig_spot_b_px: pixels.dig_spot_b_rgb,
        pixels.capacity_px: (10, 10, 10),
    }
    _frames(service_context, rig.clock, [diggable] * 500)
    cancellation = FakeCancellation(rig.clock, cancel_after_waits=4)
    service_context.cancel = cancellation
    rig.authority.activate_generation(
        1,
        emits_input=True,
        cancellation=cancellation,
        requires_capture=True,
        pinned_rect=rig.port.window_geometry(),
    )

    result = engine.run_dig_loop(service_context)

    assert result.outcome is DigOutcome.CANCELLED
    assert result.taps <= 6
    assert rig.authority.ledger_empty()


def test_the_dig_loop_never_taps_into_a_full_pan(
    rig: Any, service_context: engine.ServiceContext
) -> None:
    pixels = engine.DEFAULT_PIXELS
    full = {
        pixels.dig_spot_a_px: pixels.dig_spot_a_rgb,
        pixels.dig_spot_b_px: pixels.dig_spot_b_rgb,
        pixels.capacity_px: pixels.capacity_rgb,
    }
    _frames(service_context, rig.clock, [full] * 30)

    result = engine.run_dig_loop(
        service_context, engine.DigLoopLimits(max_taps=100, max_pan_swaps=0)
    )

    assert result.taps == 0
    assert "pan-swap cap" in result.detail
    assert "lmb_down" not in [op for op, _ in rig.port.ops()]
