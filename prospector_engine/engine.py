"""Bounded Treasure services and the detectors they read.

What changed from the pre-navigator engine, and why:

* ``State``, the global run loop, and the direct ``mouse_*`` / ``key_*`` calls
  are gone. Ownership belongs to the coordinator and the input authority
  (bugs B1, B7).
* Every service takes a cancellation token **and** a monotonic deadline, and
  returns a typed outcome carrying the evidence it decided on (bugs B2, B3).
* Detectors read one coherent :class:`~prospector_engine.contracts.CapturedFrame`
  instead of three independently captured instants (bug B12).
* Coordinates are client-relative physical pixels (bug B11).

The pixel constants below are the ones that worked in the legacy build, but
they were calibrated against the old macOS *window-frame* basis with the frame
pinned to 1280x720. The canonical viewport pins the **client** to 1280x720, so
they are carried over as ``EvidenceStatus.PENDING`` and must be re-derived with
``treasure.py --calibrate`` before any unattended run (plan 4.1).
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Protocol, runtime_checkable

from prospector_engine.contracts import (
    CancellationToken,
    CapturedFrame,
    DigEvidence,
    DigHandoffResult,
    DigOutcome,
    EvidenceStatus,
    FrameEnvelope,
    InputKey,
    LeaseHandle,
    MouseButton,
    PanSwapOutcome,
    PanSwapResult,
    Provenance,
    ResetOutcome,
    ResetResult,
    WiggleOutcome,
    WiggleResult,
    monotonic_s,
)
from prospector_engine.input_authority import ServiceInputSession

__all__ = [
    "DEFAULT_CHEST_PIXEL",
    "DEFAULT_PIXELS",
    "DEFAULT_TIMINGS",
    "DEFAULT_WIGGLE_CONFIG",
    "DEFAULT_WIGGLE_TO_CHEST_LIMITS",
    "ChestPixel",
    "FrameSource",
    "ServiceContext",
    "ServiceTimings",
    "TreasurePixels",
    "WiggleConfig",
    "WiggleToChestLimits",
    "capacity_full",
    "color_close",
    "is_white",
    "is_yellow",
    "on_chest_spot",
    "on_dig_spot",
    "run_dequip_pan",
    "run_dig_at_current_spot",
    "run_dig_loop",
    "run_pan_swap",
    "run_reset",
    "run_wiggle",
    "run_wiggle_to_chest",
    "sample_client_pixel",
]

Point = tuple[int, int]
Rgb = tuple[float, float, float]


@runtime_checkable
class FrameSource(Protocol):
    """Anything that can hand back the newest coherent frame envelope."""

    def latest(self) -> FrameEnvelope | None: ...


# ---------------------------------------------------------------------------
# Colour tests (adapted from the legacy engine, semantics preserved)
# ---------------------------------------------------------------------------


def is_white(rgb: Rgb, white_min: float) -> bool:
    """Diagnostic only. Dig gating uses two calibrated terrain points instead."""
    return all(channel >= white_min for channel in rgb)


def is_yellow(rgb: Rgb, yellow_min: float, blue_gap: float) -> bool:
    """The capacity-bar full test: strong red and green, suppressed blue."""
    r, g, b = rgb
    return r >= yellow_min and g >= yellow_min and b <= min(r, g) - blue_gap


def color_close(rgb: Rgb, target: Rgb, tolerance_pct: float) -> bool:
    """Every channel within ``tolerance_pct`` % of the 0-255 range of target."""
    tolerance = tolerance_pct / 100.0 * 255.0
    return all(abs(a - t) <= tolerance for a, t in zip(rgb, target, strict=True))


# ---------------------------------------------------------------------------
# Calibrated pixels
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TreasurePixels:
    """Calibrated sample points and click targets, client-relative physical px.

    Two terrain check points gate digging rather than one brightness cue: a
    single UI-cue pixel's brightness drifted with lighting and animation state
    and read both far under and far over its own threshold on the same
    coordinate. Two points, both required, calibrated on a confirmed valid dig
    spot, held up where one threshold did not.
    """

    dig_spot_a_px: Point = (559, 614)
    dig_spot_a_rgb: Rgb = (51.0, 51.0, 51.0)
    dig_spot_b_px: Point = (556, 607)
    dig_spot_b_rgb: Rgb = (201.0, 201.0, 201.0)
    dig_spot_tolerance_pct: float = 10.0

    capacity_px: Point = (799, 542)
    yellow_min: float = 140.0
    yellow_blue_gap: float = 45.0
    white_min: float = 175.0

    sample_box_px: int = 6

    pan_menu_button_px: Point = (442, 195)
    pan_first_slot_px: Point = (508, 255)
    pan_bottom_slot_px: Point = (514, 531)
    pan_equip_px: Point = (769, 515)
    pan_check_px: Point = (500, 547)
    pan_check_rgb: Rgb = (140.0, 140.0, 140.0)
    pan_check_tolerance_pct: float = 5.0
    pan_start_check_px: Point = (540, 523)
    pan_start_check_rgb: Rgb = (194.0, 55.0, 27.0)
    pan_start_check_tolerance_pct: float = 10.0

    reset_menu_px: Point = (641, 630)
    reset_confirm_px: Point = (530, 380)

    status: EvidenceStatus = EvidenceStatus.PENDING
    provenance: Provenance = field(
        default_factory=lambda: Provenance(
            status=EvidenceStatus.PENDING,
            source="legacy macOS window-frame basis, 1280x720 outer frame",
            note=(
                "carried over unchanged; the canonical viewport pins the CLIENT to "
                "1280x720, so these must be re-derived with --calibrate and manually "
                "reverified before unattended use (plan 4.1)"
            ),
        )
    )

    def from_legacy_window_frame(self, title_bar_px: int) -> TreasurePixels:
        """Shift every point from the old window-frame basis into client space.

        This is offered as the mechanical part of the migration. It does *not*
        make the values validated: the client area is also a different size
        now, so anything anchored to a screen edge still needs re-deriving.
        """
        shift = -title_bar_px

        def moved(point: Point) -> Point:
            return (point[0], point[1] + shift)

        return replace(
            self,
            dig_spot_a_px=moved(self.dig_spot_a_px),
            dig_spot_b_px=moved(self.dig_spot_b_px),
            capacity_px=moved(self.capacity_px),
            pan_menu_button_px=moved(self.pan_menu_button_px),
            pan_first_slot_px=moved(self.pan_first_slot_px),
            pan_bottom_slot_px=moved(self.pan_bottom_slot_px),
            pan_equip_px=moved(self.pan_equip_px),
            pan_check_px=moved(self.pan_check_px),
            pan_start_check_px=moved(self.pan_start_check_px),
            reset_menu_px=moved(self.reset_menu_px),
            reset_confirm_px=moved(self.reset_confirm_px),
            provenance=Provenance(
                status=EvidenceStatus.PENDING,
                source=f"legacy window-frame basis shifted by -{title_bar_px} px",
                note="mechanical transform only; manual reverification still required",
            ),
        )


DEFAULT_PIXELS = TreasurePixels()


@dataclass(frozen=True)
class ChestPixel:
    """The two X_MARKS_THE_SPOT sample points that stop ``run_wiggle_to_chest``.

    *Both* points must match for the stop condition to fire. Owner-supplied
    client-relative physical pixels and target colours, not yet re-derived
    with ``treasure.py --calibrate``.
    """

    point_px: Point = (542, 563)
    target_rgb: Rgb = (81.0, 124.0, 65.0)
    tolerance_pct: float = 5.0
    point_b_px: Point = (553, 564)
    target_b_rgb: Rgb = (78.0, 116.0, 56.0)
    tolerance_b_pct: float = 5.0
    sample_box_px: int = 6
    poll_s: float = 0.1

    status: EvidenceStatus = EvidenceStatus.PENDING
    provenance: Provenance = field(
        default_factory=lambda: Provenance(
            status=EvidenceStatus.PENDING,
            source="owner-supplied X_MARKS_THE_SPOT sample",
            note="not yet re-derived with --calibrate",
        )
    )


DEFAULT_CHEST_PIXEL = ChestPixel()


@dataclass(frozen=True)
class DigLoopLimits:
    """Bounds for the standalone dig loop (D-015).

    The pre-navigator build looped forever; this one has both an attempt cap
    and a monotonic deadline, like every other retry path here.
    """

    max_taps: int = 5000
    max_pan_swaps: int = 20
    deadline_ms: int = 30 * 60 * 1000
    poll_ms: int = 10
    provenance: Provenance = field(
        default_factory=lambda: Provenance(
            status=EvidenceStatus.PROVISIONAL,
            source="DECISIONS.md D-015",
            note="bounds chosen so a session ends on its own; E-DIG is PENDING",
        )
    )


@dataclass(frozen=True)
class DigLoopResult:
    outcome: DigOutcome
    taps: int
    pan_swaps: int
    elapsed_s: float
    detail: str
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class ServiceTimings:
    """Every delay the legacy sequences used, named and bounded.

    These reproduce the timings that worked in the legacy build; they are
    provisional configuration, not measurements.
    """

    dig_hold_ms: int = 1
    click_pre_ms: int = 100
    click_post_ms: int = 100
    click_settle_ms: int = 100

    dequip_poll_ms: int = 250
    dequip_max_attempts: int = 12
    dequip_deadline_ms: int = 8000

    pan_scroll_count: int = 20
    pan_scroll_duration_ms: int = 250
    pan_max_attempts: int = 5
    pan_attempt_deadline_ms: int = 30000
    pan_after_slot_ms: int = 500
    pan_after_two_ms: int = 750
    pan_after_one_ms: int = 1000

    reset_post_escape_ms: int = 500
    reset_menu_pre_ms: int = 200
    reset_menu_post_ms: int = 200
    reset_confirm_pre_ms: int = 500
    reset_confirm_post_ms: int = 0
    reset_post_enter_ms: int = 8000
    reset_pre_drag_ms: int = 400
    reset_button_settle_ms: int = 200
    reset_drag_total_ms: int = 1000
    reset_drag_step_ms: int = 16
    reset_drag_step_px: int = 12
    reset_post_drag_ms: int = 500
    reset_deadline_ms: int = 30000

    provenance: Provenance = field(
        default_factory=lambda: Provenance(
            status=EvidenceStatus.PROVISIONAL,
            source="legacy engine.py sequence constants, commit 5b81120",
            note="reproduces the timings that worked; E-DIG/E-LIFECYCLE are PENDING",
        )
    )


DEFAULT_TIMINGS = ServiceTimings()


# ---------------------------------------------------------------------------
# Detectors over one coherent frame
# ---------------------------------------------------------------------------


def sample_client_pixel(frame: CapturedFrame, point_px: Point, box_px: int) -> Rgb:
    return frame.sample_mean_rgb(point_px, box_px)


def on_dig_spot(frame: CapturedFrame, pixels: TreasurePixels = DEFAULT_PIXELS) -> bool:
    """True when *both* terrain check points match, read from one frame."""
    a = sample_client_pixel(frame, pixels.dig_spot_a_px, pixels.sample_box_px)
    b = sample_client_pixel(frame, pixels.dig_spot_b_px, pixels.sample_box_px)
    return color_close(a, pixels.dig_spot_a_rgb, pixels.dig_spot_tolerance_pct) and color_close(
        b, pixels.dig_spot_b_rgb, pixels.dig_spot_tolerance_pct
    )


def capacity_full(frame: CapturedFrame, pixels: TreasurePixels = DEFAULT_PIXELS) -> bool:
    rgb = sample_client_pixel(frame, pixels.capacity_px, pixels.sample_box_px)
    return is_yellow(rgb, pixels.yellow_min, pixels.yellow_blue_gap)


def on_chest_spot(frame: CapturedFrame, chest: ChestPixel = DEFAULT_CHEST_PIXEL) -> bool:
    """True when ``X_MARKS_THE_SPOT`` reads at *both* chest pixels, from one frame."""
    a = sample_client_pixel(frame, chest.point_px, chest.sample_box_px)
    b = sample_client_pixel(frame, chest.point_b_px, chest.sample_box_px)
    return color_close(a, chest.target_rgb, chest.tolerance_pct) and color_close(
        b, chest.target_b_rgb, chest.tolerance_b_pct
    )


# ---------------------------------------------------------------------------
# Service context
# ---------------------------------------------------------------------------


class ServiceCancelled(Exception):
    """Internal unwind for a cancelled or expired bounded service.

    Never escapes the service function that catches it; callers see a typed
    ``CANCELLED``/``TIMEOUT`` outcome instead (bug B3: the legacy reset left
    state set when this unwound past its cleanup).
    """


@dataclass
class ServiceContext:
    """Everything a bounded service is allowed to touch."""

    frames: FrameSource
    session: ServiceInputSession
    cancel: CancellationToken
    deadline_s: float
    pixels: TreasurePixels = DEFAULT_PIXELS
    timings: ServiceTimings = DEFAULT_TIMINGS
    on_status: Callable[[str], None] | None = None

    def status(self, message: str) -> None:
        if self.on_status is not None:
            self.on_status(message)

    def expired(self) -> bool:
        return monotonic_s() >= self.deadline_s

    def check(self) -> None:
        if self.cancel.is_cancelled():
            raise ServiceCancelled("cancelled")
        if self.expired():
            raise ServiceCancelled("deadline")

    def sleep_ms(self, milliseconds: float) -> None:
        """Cancellable wait, clipped to the remaining deadline."""
        self.check()
        remaining_s = min(milliseconds / 1000.0, max(0.0, self.deadline_s - monotonic_s()))
        if self.cancel.wait(remaining_s):
            raise ServiceCancelled("cancelled")
        self.check()

    def frame(self) -> CapturedFrame:
        self.check()
        envelope = self.frames.latest()
        if envelope is None:
            raise ServiceCancelled("no-frame")
        return envelope.frame

    def sample(self, point_px: Point) -> Rgb:
        return sample_client_pixel(self.frame(), point_px, self.pixels.sample_box_px)

    def tap(self, key: InputKey) -> None:
        self.check()
        self.session.tap_key(key, hold_ms=0)

    def click_buffered(self, point_px: Point, pre_ms: int, post_ms: int) -> None:
        """Move-nudge-click, preserving the legacy ordering exactly.

        The one-pixel pre-move plus settle is what makes Roblox register the
        pointer as having *entered* the control before the click lands; a
        single warp straight onto the target was unreliable.
        """
        self.sleep_ms(pre_ms)
        self.session.pointer_move_client((point_px[0] - 1, point_px[1] - 1))
        self.sleep_ms(self.timings.click_settle_ms)
        self.session.pointer_move_client(point_px)
        self.session.tap_button(MouseButton.LEFT, hold_ms=0)
        self.sleep_ms(post_ms)


# ---------------------------------------------------------------------------
# Services
# ---------------------------------------------------------------------------


def run_dequip_pan(ctx: ServiceContext) -> tuple[bool, str, int]:
    """Press ``1`` until the pan-swap prompt clears - bounded (bug B2).

    The legacy loop had no attempt cap and no deadline, so a prompt that never
    cleared meant pressing ``1`` forever. Returns ``(cleared, detail, attempts)``;
    cancellation and deadline expiry are ordinary results here, not exceptions,
    because both callers need to report them as typed outcomes.
    """
    timings = ctx.timings
    pixels = ctx.pixels
    deadline_s = min(ctx.deadline_s, monotonic_s() + timings.dequip_deadline_ms / 1000.0)
    attempts = 0
    try:
        for attempt in range(1, timings.dequip_max_attempts + 1):
            if monotonic_s() >= deadline_s:
                return False, f"dequip deadline after {attempts} attempts", attempts
            attempts = attempt
            ctx.tap(InputKey.DIGIT_1)
            ctx.sleep_ms(timings.dequip_poll_ms)
            rgb = ctx.sample(pixels.pan_start_check_px)
            rounded = tuple(round(c) for c in rgb)
            ctx.status(f"dequip attempt {attempt}: start-gate rgb={rounded}")
            cleared = not color_close(
                rgb, pixels.pan_start_check_rgb, pixels.pan_start_check_tolerance_pct
            )
            if cleared:
                return True, f"cleared after {attempt} attempts", attempt
    except ServiceCancelled as stop:
        return False, f"dequip {stop} after {attempts} attempts", attempts
    return (
        False,
        f"dequip attempt cap ({timings.dequip_max_attempts}) reached",
        timings.dequip_max_attempts,
    )


def run_pan_swap(ctx: ServiceContext) -> PanSwapResult:
    """Swap a full pan for an empty one and confirm it re-equipped.

    Bounded on both axes: at most ``pan_max_attempts`` attempts and never past
    the context deadline. Retries are a loop, not recursion, so a long retry
    chain cannot deepen the stack (bug B2).
    """
    started_s = monotonic_s()
    timings = ctx.timings
    pixels = ctx.pixels
    evidence: list[str] = []
    attempts = 0
    try:
        for attempt in range(1, timings.pan_max_attempts + 1):
            attempts = attempt
            ctx.check()
            ctx.status(f"[pan swap] attempt {attempt}: waiting for prompt to clear")
            cleared, detail, _ = run_dequip_pan(ctx)
            evidence.append(f"attempt{attempt}:dequip:{detail}")
            if not cleared:
                return PanSwapResult(
                    PanSwapOutcome.FAILED,
                    attempt,
                    monotonic_s() - started_s,
                    detail,
                    tuple(evidence),
                )

            ctx.status(f"[pan swap] attempt {attempt}: press 2")
            ctx.tap(InputKey.DIGIT_2)
            ctx.sleep_ms(timings.pan_after_slot_ms)

            def click(point: Point) -> None:
                ctx.click_buffered(point, timings.click_pre_ms, timings.click_post_ms)

            ctx.status(f"[pan swap] attempt {attempt}: open pans menu")
            click(pixels.pan_menu_button_px)

            ctx.status(f"[pan swap] attempt {attempt}: select first pan, scroll, equip")
            click(pixels.pan_first_slot_px)
            _scroll_down(ctx, timings.pan_scroll_count, timings.pan_scroll_duration_ms)
            click(pixels.pan_equip_px)

            ctx.status(f"[pan swap] attempt {attempt}: select bottom pan, equip")
            click(pixels.pan_bottom_slot_px)
            click(pixels.pan_equip_px)

            ctx.sleep_ms(timings.pan_after_slot_ms)
            ctx.status(f"[pan swap] attempt {attempt}: press 2")
            ctx.tap(InputKey.DIGIT_2)
            ctx.sleep_ms(timings.pan_after_two_ms)
            ctx.status(f"[pan swap] attempt {attempt}: press 1")
            ctx.tap(InputKey.DIGIT_1)
            ctx.sleep_ms(timings.pan_after_one_ms)

            rgb = ctx.sample(pixels.pan_check_px)
            confirmed = color_close(rgb, pixels.pan_check_rgb, pixels.pan_check_tolerance_pct)
            rounded = tuple(round(c) for c in rgb)
            evidence.append(f"attempt{attempt}:check_rgb={rounded}:confirmed={confirmed}")
            if confirmed:
                ctx.status("[pan swap] pan confirmed out - done")
                return PanSwapResult(
                    PanSwapOutcome.SUCCESS,
                    attempt,
                    monotonic_s() - started_s,
                    "pan confirmed re-equipped",
                    tuple(evidence),
                )
            ctx.status(f"[pan swap] attempt {attempt}: pan not confirmed out")
        return PanSwapResult(
            PanSwapOutcome.FAILED,
            attempts,
            monotonic_s() - started_s,
            f"gave up after {timings.pan_max_attempts} attempts",
            tuple(evidence),
        )
    except ServiceCancelled as stop:
        outcome = (
            PanSwapOutcome.CANCELLED if str(stop) == "cancelled" else PanSwapOutcome.TIMEOUT
        )
        return PanSwapResult(
            outcome, attempts, monotonic_s() - started_s, str(stop), tuple(evidence)
        )


def _scroll_down(ctx: ServiceContext, count: int, duration_ms: int) -> None:
    if count <= 0:
        return
    step_ms = duration_ms / count
    for _ in range(count):
        ctx.check()
        ctx.session.scroll_lines(-1)
        ctx.sleep_ms(step_ms)


def run_reset(ctx: ServiceContext) -> ResetResult:
    """Reset the character and normalize the camera to a known heading.

    The dequip runs *inside* the guarded block so a cancellation between it
    and the rest of the sequence still reaches the cleanup path (bug B3), and
    the right mouse button is released unconditionally on every exit.
    """
    started_s = monotonic_s()
    timings = ctx.timings
    pixels = ctx.pixels
    evidence: list[str] = []
    right_button = None
    try:
        cleared, detail, _ = run_dequip_pan(ctx)
        evidence.append(f"dequip:{detail}")
        if not cleared:
            return ResetResult(
                ResetOutcome.FAILED, monotonic_s() - started_s, detail, tuple(evidence)
            )

        ctx.tap(InputKey.ESCAPE)
        ctx.sleep_ms(timings.reset_post_escape_ms)
        ctx.click_buffered(
            pixels.reset_menu_px, timings.reset_menu_pre_ms, timings.reset_menu_post_ms
        )
        ctx.click_buffered(
            pixels.reset_confirm_px, timings.reset_confirm_pre_ms, timings.reset_confirm_post_ms
        )
        ctx.sleep_ms(timings.reset_post_enter_ms)

        canonical = ctx.frame().canonical_size_px
        centre = (canonical[0] // 2, canonical[1] // 2)
        ctx.session.pointer_move_client(centre)
        ctx.sleep_ms(timings.reset_pre_drag_ms)

        # Roblox reads raw HID deltas for mouselook: a held button plus delta
        # drag events turns the camera; an absolute move does nothing.
        steps = max(1, timings.reset_drag_total_ms // timings.reset_drag_step_ms)
        right_button = ctx.session.hold_button(
            MouseButton.RIGHT,
            max_hold_ms=timings.reset_button_settle_ms
            + timings.reset_drag_total_ms
            + timings.reset_post_drag_ms,
        )
        if right_button is None:
            return ResetResult(
                ResetOutcome.FAILED,
                monotonic_s() - started_s,
                "camera button lease refused",
                tuple(evidence),
            )
        ctx.sleep_ms(timings.reset_button_settle_ms)
        for _ in range(steps):
            ctx.check()
            ctx.session.renew(right_button, timings.reset_drag_step_ms * 4)
            ctx.session.pointer_delta(0, timings.reset_drag_step_px)
            ctx.sleep_ms(timings.reset_drag_step_ms)
        ctx.session.release(right_button)
        right_button = None
        ctx.sleep_ms(timings.reset_post_drag_ms)
        ctx.session.pointer_move_client(centre)

        evidence.append(f"drag_steps={steps}")
        return ResetResult(
            ResetOutcome.SUCCESS, monotonic_s() - started_s, "reset complete", tuple(evidence)
        )
    except ServiceCancelled as stop:
        outcome = ResetOutcome.CANCELLED if str(stop) == "cancelled" else ResetOutcome.TIMEOUT
        return ResetResult(outcome, monotonic_s() - started_s, str(stop), tuple(evidence))
    finally:
        # Never leave the camera button stuck down, on any exit path.
        if right_button is not None:
            ctx.session.release(right_button)


def run_dig_at_current_spot(ctx: ServiceContext, max_attempts: int = 1) -> DigHandoffResult:
    """One bounded dig pass, decided from a single coherent frame.

    Returns ``PAN_FULL`` before ever tapping when the capacity bar reads full,
    so digging cannot continue into a full pan.
    """
    started_s = monotonic_s()
    attempts = 0
    try:
        while attempts < max_attempts:
            attempts += 1
            frame = ctx.frame()
            pixels = ctx.pixels
            a = sample_client_pixel(frame, pixels.dig_spot_a_px, pixels.sample_box_px)
            b = sample_client_pixel(frame, pixels.dig_spot_b_px, pixels.sample_box_px)
            capacity_rgb = sample_client_pixel(frame, pixels.capacity_px, pixels.sample_box_px)
            full = is_yellow(capacity_rgb, pixels.yellow_min, pixels.yellow_blue_gap)
            diggable = color_close(
                a, pixels.dig_spot_a_rgb, pixels.dig_spot_tolerance_pct
            ) and color_close(b, pixels.dig_spot_b_rgb, pixels.dig_spot_tolerance_pct)
            evidence = DigEvidence(
                frame_sequence=frame.sequence,
                dig_spot_a_rgb=a,
                dig_spot_b_rgb=b,
                capacity_rgb=capacity_rgb,
                on_dig_spot=diggable,
                capacity_full=full,
            )
            if full:
                return DigHandoffResult(
                    DigOutcome.PAN_FULL,
                    evidence,
                    monotonic_s() - started_s,
                    attempts,
                    "pan full",
                )
            if not diggable:
                return DigHandoffResult(
                    DigOutcome.CUE_LOST,
                    evidence,
                    monotonic_s() - started_s,
                    attempts,
                    "dig-spot check points do not match",
                )
            ctx.session.tap_button(MouseButton.LEFT, hold_ms=ctx.timings.dig_hold_ms)
            return DigHandoffResult(
                DigOutcome.DIG_PROGRESS,
                evidence,
                monotonic_s() - started_s,
                attempts,
                "dig tap issued",
            )
        return DigHandoffResult(
            DigOutcome.TIMEOUT,
            DigEvidence(-1, None, None, None, False, False),
            monotonic_s() - started_s,
            attempts,
            "attempt cap reached",
        )
    except ServiceCancelled as stop:
        outcome = DigOutcome.CANCELLED if str(stop) == "cancelled" else DigOutcome.TIMEOUT
        return DigHandoffResult(
            outcome,
            DigEvidence(-1, None, None, None, False, False),
            monotonic_s() - started_s,
            attempts,
            str(stop),
        )


def run_dig_loop(ctx: ServiceContext, limits: DigLoopLimits | None = None) -> DigLoopResult:
    """Dig at the current spot until something says stop (D-015).

    This is the pre-navigator behavior, rebuilt on the bounded services: tap
    while both terrain check points match, run a pan swap when the capacity bar
    reads full, and stop on anything else. Unlike the old loop it has an attempt
    cap, a monotonic deadline, and a cancellation token that lands within one
    wait slice.

    It is *not* navigation. It assumes the character is already standing on a
    dig spot, which is exactly what the old build assumed.
    """
    bounds = limits or DigLoopLimits()
    started_s = monotonic_s()
    deadline_s = min(ctx.deadline_s, started_s + bounds.deadline_ms / 1000.0)
    # ServiceContext is mutable by design so a nested service inherits the
    # tighter of the two deadlines rather than the caller's.
    loop_ctx = ctx
    loop_ctx.deadline_s = deadline_s
    taps = 0
    swaps = 0
    evidence: list[str] = []
    try:
        while taps < bounds.max_taps:
            result = run_dig_at_current_spot(loop_ctx)
            if result.outcome is DigOutcome.DIG_PROGRESS:
                taps += 1
                loop_ctx.sleep_ms(bounds.poll_ms)
                continue
            if result.outcome is DigOutcome.PAN_FULL:
                if swaps >= bounds.max_pan_swaps:
                    return DigLoopResult(
                        DigOutcome.TIMEOUT,
                        taps,
                        swaps,
                        monotonic_s() - started_s,
                        f"pan-swap cap ({bounds.max_pan_swaps}) reached",
                        tuple(evidence),
                    )
                ctx.status("capacity full - running pan swap")
                swap = run_pan_swap(loop_ctx)
                swaps += 1
                evidence.append(f"pan_swap#{swaps}:{swap.outcome.name}:{swap.detail}")
                if swap.outcome is not PanSwapOutcome.SUCCESS:
                    return DigLoopResult(
                        DigOutcome.FAILED
                        if swap.outcome is PanSwapOutcome.FAILED
                        else DigOutcome.CANCELLED,
                        taps,
                        swaps,
                        monotonic_s() - started_s,
                        f"pan swap {swap.outcome.name.lower()}: {swap.detail}",
                        tuple(evidence),
                    )
                continue
            # CUE_LOST, TIMEOUT, CANCELLED, FAILED: report it and stop rather
            # than tapping into an unknown state.
            evidence.append(f"stopped_on:{result.outcome.name}:{result.detail}")
            return DigLoopResult(
                result.outcome,
                taps,
                swaps,
                monotonic_s() - started_s,
                result.detail,
                tuple(evidence),
            )
        return DigLoopResult(
            DigOutcome.TIMEOUT,
            taps,
            swaps,
            monotonic_s() - started_s,
            f"tap cap ({bounds.max_taps}) reached",
            tuple(evidence),
        )
    except ServiceCancelled as stop:
        outcome = DigOutcome.CANCELLED if str(stop) == "cancelled" else DigOutcome.TIMEOUT
        return DigLoopResult(
            outcome, taps, swaps, monotonic_s() - started_s, str(stop), tuple(evidence)
        )


# ---------------------------------------------------------------------------
# Wiggle: a directional strafe-wiggle test (D-088)
#
# Ported from the legacy standalone macro's wiggleMove(). Everything is
# planned in a LOCAL frame - forward is (0, 1), right is (1, 0) - then rotated
# clockwise by ``degree`` into a world-space vector, decomposed into per-key
# duty-cycle weights, and realized as leased key holds rather than raw
# key_down/key_up: a stall or cancellation here releases through the same
# input authority as every other service, instead of a raw Quartz/pynput call
# that nothing would ever lift (bugs B1, B7).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WiggleConfig:
    """Cadence and bounds for the strafe-wiggle test service.

    ``forward_half_ms``/``backward_half_ms`` reproduce the legacy
    ``wiggleMove()`` timing exactly: a 250ms half-phase bookending the forward
    run, 125ms bookending the mirrored backward one. ``lease_horizon_ms`` is
    kept short and renewed every tick, so a stalled tick loop can never hold a
    key past one horizon after the stall (the authority's own ceiling is a
    second bound behind this one, per ``AuthorityConfig.max_hold_ms``).
    """

    tick_ms: int = 20
    forward_half_ms: float = 250.0
    backward_half_ms: float = 125.0
    lease_horizon_ms: int = 250
    max_forward_s: float = 30.0
    max_backward_s: float = 10.0
    provenance: Provenance = field(
        default_factory=lambda: Provenance(
            status=EvidenceStatus.PROVISIONAL,
            source="legacy engine.py wiggleMove(), commit 5b81120",
            note="reproduces the timing that worked on raw key events; not "
            "re-measured on the leased input authority",
        )
    )


DEFAULT_WIGGLE_CONFIG = WiggleConfig()


def _rotate_cw(x: float, y: float, degree: float) -> tuple[float, float]:
    """Clockwise rotation: 0 deg leaves ``(x, y)`` unchanged, 90 deg turns
    forward ``(0, 1)`` into right ``(1, 0)``."""
    theta = math.radians(degree)
    return (
        x * math.cos(theta) + y * math.sin(theta),
        -x * math.sin(theta) + y * math.cos(theta),
    )


def _wiggle_key_weights(x: float, y: float) -> dict[InputKey, float]:
    """A world-space vector, decomposed into W/A/S/D weights in ``[0, 1]``,
    normalized so the dominant axis is always fully held."""
    weights = {
        InputKey.D: max(x, 0.0),
        InputKey.A: max(-x, 0.0),
        InputKey.W: max(y, 0.0),
        InputKey.S: max(-y, 0.0),
    }
    peak = max(weights.values())
    if peak <= 1e-9:
        return weights
    return {key: value / peak for key, value in weights.items()}


def _wiggle_side_phases(total_ms: float, target_half_ms: float) -> list[tuple[int, float]]:
    """``[(side_sign, duration_ms), ...]``: half, full, full, ..., full, half -
    symmetric, starting and ending on ``side_sign = -1``. The number of full
    alternating swings is chosen (forced odd, so the pattern starts and ends
    on the same side) to fit ``total_ms`` around the requested half-phase
    length as closely as an integer count allows.
    """
    if total_ms <= 0:
        return []
    swings = max(1, round((total_ms / target_half_ms - 2) / 2))
    if swings % 2 == 0:
        swings += 1
    half_ms = total_ms / (2 + 2 * swings)
    full_ms = 2 * half_ms
    phases: list[tuple[int, float]] = [(-1, half_ms)]
    sign = 1
    for _ in range(swings):
        phases.append((sign, full_ms))
        sign *= -1
    phases.append((-1, half_ms))
    return phases


def _wiggle_weights_at(
    degree: float, forward_sign: int, side_sign: int
) -> dict[InputKey, float]:
    forward_x, forward_y = _rotate_cw(0.0, float(forward_sign), degree)
    side_x, side_y = _rotate_cw(float(side_sign), 0.0, degree)
    return _wiggle_key_weights(forward_x + side_x, forward_y + side_y)


@dataclass
class _DutyKeyState:
    """One key's held lease and its fractional duty-cycle accumulator."""

    lease: LeaseHandle | None = None
    accumulator: float = 0.0


def run_wiggle(
    ctx: ServiceContext,
    degree: float,
    forward_s: float,
    backward_s: float,
    config: WiggleConfig = DEFAULT_WIGGLE_CONFIG,
) -> WiggleResult:
    """Strafe-wiggle toward ``degree`` (0 = forward/W, clockwise), then a
    smaller mirrored wiggle back, holding space throughout.

    ``degree`` is taken mod 360, so any float is accepted. ``forward_s`` and
    ``backward_s`` are clipped to ``config.max_forward_s`` / ``max_backward_s``
    so a bad caller cannot ask for an unbounded hold. Every key this acquires
    is released on every exit path, cancelled or not (bug B1/B7 class, same as
    every other service here): nothing here can leave a direction key stuck
    down the way the legacy raw ``key_down``/``key_up`` calls could.
    """
    started_s = monotonic_s()
    degree = degree % 360.0
    forward_s = max(0.0, min(forward_s, config.max_forward_s))
    backward_s = max(0.0, min(backward_s, config.max_backward_s))
    evidence: list[str] = [
        f"degree={degree:.1f}",
        f"forward_s={forward_s:.2f}",
        f"backward_s={backward_s:.2f}",
    ]

    duty: dict[InputKey, _DutyKeyState] = {
        key: _DutyKeyState() for key in (InputKey.W, InputKey.A, InputKey.S, InputKey.D)
    }
    space_lease: LeaseHandle | None = None

    def release_all() -> None:
        nonlocal space_lease
        for state in duty.values():
            if state.lease is not None:
                ctx.session.release(state.lease)
                state.lease = None
        if space_lease is not None:
            ctx.session.release(space_lease)
            space_lease = None

    def apply_weights(weights: dict[InputKey, float]) -> None:
        for key, weight in weights.items():
            state = duty[key]
            if weight >= 0.999:
                if state.lease is None:
                    state.lease = ctx.session.hold_key(key, config.lease_horizon_ms)
                else:
                    ctx.session.renew(state.lease, config.lease_horizon_ms)
                state.accumulator = 0.0
                continue
            if weight <= 0.001:
                if state.lease is not None:
                    ctx.session.release(state.lease)
                    state.lease = None
                state.accumulator = 0.0
                continue
            state.accumulator += weight
            if state.accumulator >= 1.0:
                state.accumulator -= 1.0
                if state.lease is None:
                    state.lease = ctx.session.hold_key(key, config.lease_horizon_ms)
                else:
                    ctx.session.renew(state.lease, config.lease_horizon_ms)
            elif state.lease is not None:
                ctx.session.release(state.lease)
                state.lease = None

    def run_phase(total_s: float, forward_sign: int, target_half_ms: float) -> None:
        total_ms = total_s * 1000.0
        for side_sign, duration_ms in _wiggle_side_phases(total_ms, target_half_ms):
            phase_deadline_s = monotonic_s() + duration_ms / 1000.0
            weights = _wiggle_weights_at(degree, forward_sign, side_sign)
            while monotonic_s() < phase_deadline_s:
                ctx.check()
                apply_weights(weights)
                if space_lease is not None:
                    ctx.session.renew(space_lease, config.lease_horizon_ms)
                ctx.sleep_ms(config.tick_ms)

    try:
        space_lease = ctx.session.hold_key(InputKey.SPACE, config.lease_horizon_ms)
        if space_lease is None:
            return WiggleResult(
                WiggleOutcome.FAILED,
                degree,
                monotonic_s() - started_s,
                "space lease refused",
                tuple(evidence),
            )
        run_phase(forward_s, +1, config.forward_half_ms)
        run_phase(backward_s, -1, config.backward_half_ms)
        return WiggleResult(
            WiggleOutcome.SUCCESS,
            degree,
            monotonic_s() - started_s,
            "wiggle complete",
            tuple(evidence),
        )
    except ServiceCancelled as stop:
        outcome = WiggleOutcome.CANCELLED if str(stop) == "cancelled" else WiggleOutcome.TIMEOUT
        return WiggleResult(
            outcome, degree, monotonic_s() - started_s, str(stop), tuple(evidence)
        )
    finally:
        release_all()


# ---------------------------------------------------------------------------
# Wiggle-to-chest: repeat the wiggle until X_MARKS_THE_SPOT is on screen
# (D-092)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WiggleToChestLimits:
    """Bounds for ``run_wiggle_to_chest`` (D-092), same shape as ``DigLoopLimits``.

    ``max_passes`` exists so an unmet stop condition ends the loop on its own
    rather than wiggling forever; ``ctx.deadline_s`` bounds it independently
    on wall-clock time, so both an attempt cap and a monotonic deadline hold
    at once, like every other retry loop here.
    """

    max_passes: int = 2000
    provenance: Provenance = field(
        default_factory=lambda: Provenance(
            status=EvidenceStatus.PROVISIONAL,
            source="DECISIONS.md D-092",
            note="pass cap chosen so an unmet stop condition ends on its own; not measured",
        )
    )


DEFAULT_WIGGLE_TO_CHEST_LIMITS = WiggleToChestLimits()


def run_wiggle_to_chest(
    ctx: ServiceContext,
    angle_source: Callable[[], float],
    chest: ChestPixel = DEFAULT_CHEST_PIXEL,
    forward_s: float = 3.0,
    backward_s: float = 0.5,
    config: WiggleConfig = DEFAULT_WIGGLE_CONFIG,
    limits: WiggleToChestLimits = DEFAULT_WIGGLE_TO_CHEST_LIMITS,
) -> WiggleResult:
    """Ctrl+4: repeat the strafe-wiggle until ``X_MARKS_THE_SPOT`` is found.

    Each pass reads ``angle_source()`` fresh rather than once at the start, so
    this tracks the degree monitor (Ctrl+5) live while it is armed and simply
    holds its last value - 0.0 if Ctrl+5 has never been pressed - while it is
    not; whether the monitor is armed never changes how this loop behaves,
    only what angle it sees. The chest pixel is polled at least once every
    ``chest.poll_s`` throughout every phase of every pass, not just between
    passes, so a spot that appears mid-swing is not missed. As soon as it is
    found every key this holds - W/A/S/D and space - is released before this
    returns, the same unconditional release every other exit path here uses
    (bug B1/B7 class).
    """
    started_s = monotonic_s()
    evidence: list[str] = []

    duty: dict[InputKey, _DutyKeyState] = {
        key: _DutyKeyState() for key in (InputKey.W, InputKey.A, InputKey.S, InputKey.D)
    }
    space_lease: LeaseHandle | None = None
    last_poll_s = -math.inf
    found = False
    degree = 0.0

    def release_all() -> None:
        nonlocal space_lease
        for state in duty.values():
            if state.lease is not None:
                ctx.session.release(state.lease)
                state.lease = None
        if space_lease is not None:
            ctx.session.release(space_lease)
            space_lease = None

    def apply_weights(weights: dict[InputKey, float]) -> None:
        for key, weight in weights.items():
            state = duty[key]
            if weight >= 0.999:
                if state.lease is None:
                    state.lease = ctx.session.hold_key(key, config.lease_horizon_ms)
                else:
                    ctx.session.renew(state.lease, config.lease_horizon_ms)
                state.accumulator = 0.0
                continue
            if weight <= 0.001:
                if state.lease is not None:
                    ctx.session.release(state.lease)
                    state.lease = None
                state.accumulator = 0.0
                continue
            state.accumulator += weight
            if state.accumulator >= 1.0:
                state.accumulator -= 1.0
                if state.lease is None:
                    state.lease = ctx.session.hold_key(key, config.lease_horizon_ms)
                else:
                    ctx.session.renew(state.lease, config.lease_horizon_ms)
            elif state.lease is not None:
                ctx.session.release(state.lease)
                state.lease = None

    def chest_spotted() -> bool:
        """At most one frame read/sample per ``chest.poll_s``, never more often."""
        nonlocal last_poll_s
        now = monotonic_s()
        if now - last_poll_s < chest.poll_s:
            return False
        last_poll_s = now
        spotted = on_chest_spot(ctx.frame(), chest)
        if spotted:
            evidence.append(f"chest_spotted@{now - started_s:.2f}s")
        return spotted

    def run_phase(total_s: float, forward_sign: int, target_half_ms: float) -> bool:
        """One forward or backward phase. Returns True the instant the chest is found."""
        total_ms = total_s * 1000.0
        for side_sign, duration_ms in _wiggle_side_phases(total_ms, target_half_ms):
            phase_deadline_s = monotonic_s() + duration_ms / 1000.0
            weights = _wiggle_weights_at(degree, forward_sign, side_sign)
            while monotonic_s() < phase_deadline_s:
                ctx.check()
                if chest_spotted():
                    return True
                apply_weights(weights)
                if space_lease is not None:
                    ctx.session.renew(space_lease, config.lease_horizon_ms)
                ctx.sleep_ms(config.tick_ms)
        return False

    try:
        space_lease = ctx.session.hold_key(InputKey.SPACE, config.lease_horizon_ms)
        if space_lease is None:
            return WiggleResult(
                WiggleOutcome.FAILED,
                degree,
                monotonic_s() - started_s,
                "space lease refused",
                tuple(evidence),
            )
        passes = 0
        while not found:
            ctx.check()
            if passes >= limits.max_passes:
                return WiggleResult(
                    WiggleOutcome.TIMEOUT,
                    degree,
                    monotonic_s() - started_s,
                    f"pass cap ({limits.max_passes}) reached without finding the chest",
                    tuple(evidence),
                )
            passes += 1
            degree = angle_source() % 360.0
            evidence.append(f"pass{passes}:degree={degree:.1f}")
            found = run_phase(forward_s, +1, config.forward_half_ms) or run_phase(
                backward_s, -1, config.backward_half_ms
            )
        return WiggleResult(
            WiggleOutcome.SUCCESS,
            degree,
            monotonic_s() - started_s,
            f"X_MARKS_THE_SPOT found after {passes} pass(es)",
            tuple(evidence),
        )
    except ServiceCancelled as stop:
        outcome = WiggleOutcome.CANCELLED if str(stop) == "cancelled" else WiggleOutcome.TIMEOUT
        return WiggleResult(
            outcome, degree, monotonic_s() - started_s, str(stop), tuple(evidence)
        )
    finally:
        release_all()
