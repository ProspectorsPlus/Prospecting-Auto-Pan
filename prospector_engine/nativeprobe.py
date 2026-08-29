"""Bounded native input diagnostics: does Roblox actually act on what we post?

Why this module exists
----------------------
Every earlier claim that "movement works" was made from evidence that cannot
support it. ``CGEventPost`` returning cleanly proves the call did not raise.
An inert keycode (F13) read back through ``CGEventSourceKeyState`` proves this
process can reach WindowServer. A unit test with a fake port proves the code
under it is wired the way the test wired it. **None of the three is a claim
about Roblox**, and the runtime trace on the owner's machine
(``stop-epoch4-1914449166.jsonl``) is the proof: ``W`` was physically down for
322.7 ms, the down and up edges both went out, and the run was still failed -
on a key-state loopback that was never evidence of game receipt in the first
place.

What this module does is the one thing none of those did: it posts an edge and
then **looks at the game**. It is deliberately free of everything that could
explain away a negative result - no arrow detector, no profile, no automatic
setup, no cadence governor, no Shadow qualification, no follower, no
coordinator, no input authority. A platform port, a capture service, and a
frame-difference measurement.

**Every path releases.** Each trial posts its edges inside a ``try`` whose
``finally`` lifts them, and the runner's own ``finally`` sweeps the entire
vocabulary regardless of what any trial did. A probe that returned a verdict
while holding a key would be worse than no probe.

The measurement
---------------
Two numbers per frame pair, both on the raw client pixels:

``mad``
    Mean absolute difference between consecutive greyscale frames. It is a
    scalar "how much of the picture changed", and it is what separates a
    character that is walking from one that is standing in an animated scene.

``dx``/``dy``
    Phase-correlated translation, accumulated over the window. Walking forward
    produces mostly ``dy``; turning the camera produces a large signed ``dx``,
    which is what makes a *turn* distinguishable from a *step* rather than just
    "something changed".

Both are compared against an idle window measured **immediately before the
same trial**, for the same duration, at the same place in the world. Roblox
scenes are never perfectly still - water, foliage, other players - so a
constant threshold would be either useless or a lie. The comparison is a
multiple of the idle level, with an absolute floor underneath so a perfectly
still baseline cannot make a flicker into a step.
"""

from __future__ import annotations

import contextlib
import itertools
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import numpy as np

from prospector_engine.contracts import (
    CapturedFrame,
    EvidenceStatus,
    InputKey,
    MouseButton,
    Provenance,
    monotonic_s,
)

__all__ = [
    "CameraBackend",
    "FrameTap",
    "NativeProbeConfig",
    "ProbeReport",
    "SceneMotion",
    "TrialResult",
    "camera_ladder",
    "keyboard_ladder",
    "measure_scene",
    "run_camera_trial",
    "run_key_trial",
    "run_scroll_trial",
]


# ---------------------------------------------------------------------------
# Measurement
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SceneMotion:
    """How much the picture changed over one window, and in what direction."""

    frames: int
    span_s: float
    #: Mean absolute greyscale difference per frame pair, on a 0-255 scale.
    mad: float
    #: Accumulated phase-correlated translation over the window, canonical px.
    dx_px: float
    dy_px: float
    #: The largest single-pair ``mad``, so one big change is visible behind a
    #: mean that several still pairs would otherwise hide.
    peak_mad: float = 0.0
    #: Mean phase-correlation response, 0-1. Low means the shift is not to be
    #: trusted, which is different from a shift of zero.
    response: float = 0.0

    @property
    def usable(self) -> bool:
        return self.frames >= 2

    def describe(self) -> str:
        return (
            f"{self.frames} frames over {self.span_s * 1000:.0f} ms, "
            f"mad {self.mad:.2f} (peak {self.peak_mad:.2f}), "
            f"shift {self.dx_px:+.1f},{self.dy_px:+.1f} px"
        )


def _grey(frame: CapturedFrame, downscale: int) -> Any:
    import cv2

    bgr = np.asarray(frame.bgr)
    if downscale > 1:
        bgr = bgr[::downscale, ::downscale]
    return cv2.cvtColor(np.ascontiguousarray(bgr), cv2.COLOR_BGR2GRAY)


def measure_scene(frames: Sequence[CapturedFrame], *, downscale: int = 4) -> SceneMotion:
    """Reduce a run of frames to one :class:`SceneMotion`.

    Consecutive pairs only. A pair whose shapes disagree - the window was
    resized mid-window - is skipped rather than guessed at.
    """
    import cv2

    usable = [frame for frame in frames if np.asarray(frame.bgr).size]
    if len(usable) < 2:
        return SceneMotion(len(frames), 0.0, 0.0, 0.0, 0.0)
    frames = usable
    greys = [_grey(frame, downscale).astype(np.float32) for frame in frames]
    window: Any = None
    mads: list[float] = []
    responses: list[float] = []
    dx_total = 0.0
    dy_total = 0.0
    for previous, current in itertools.pairwise(greys):
        if previous.shape != current.shape or previous.size == 0:
            continue
        mads.append(float(np.abs(current - previous).mean()))
        if window is None:
            window = cv2.createHanningWindow((previous.shape[1], previous.shape[0]), cv2.CV_32F)
        # Copies, and not for tidiness. ``cv2.phaseCorrelate`` multiplies its
        # arguments by the window **in place**: pass a frame to it and the
        # array is left windowed. Every frame here is the ``current`` of one
        # pair and the ``previous`` of the next, so without the copies the
        # second comparison of every pair is a windowed image against a raw
        # one - and a run of six identical frames reported a mean absolute
        # difference of 40.9 out of 255.
        (dx, dy), response = cv2.phaseCorrelate(previous.copy(), current.copy(), window)
        # Reported in canonical pixels, so the number means the same thing
        # whatever downscale this ran at.
        dx_total += float(dx) * downscale
        dy_total += float(dy) * downscale
        responses.append(float(response))
    if not mads:
        return SceneMotion(len(frames), 0.0, 0.0, 0.0, 0.0)
    return SceneMotion(
        frames=len(frames),
        span_s=frames[-1].captured_at_s - frames[0].captured_at_s,
        mad=float(np.mean(mads)),
        dx_px=dx_total,
        dy_px=dy_total,
        peak_mad=float(max(mads)),
        response=float(np.mean(responses)) if responses else 0.0,
    )


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NativeProbeConfig:
    """Bounds and thresholds for the probe. Provisional, with provenance."""

    #: How long one key is held. The mission's window; long enough that Roblox
    #: starts the walk animation and several frames land on real motion.
    hold_ms: int = 600
    #: Hard ceiling on any single hold, whatever a caller asks for.
    max_hold_ms: int = 1200
    #: Settling pause between the idle window and the edge, and between trials.
    settle_s: float = 0.45
    #: How long to wait for one frame before giving up on it.
    frame_timeout_s: float = 0.3
    #: Movement must beat this multiple of the idle window's ``mad``...
    mad_multiple: float = 2.5
    #: ...and this absolute floor on a 0-255 greyscale, so a still baseline
    #: cannot promote noise.
    min_mad: float = 0.45
    #: A turn must additionally shift the scene horizontally by this many
    #: canonical px, and beat this multiple of the idle window's own drift.
    min_turn_px: float = 25.0
    turn_multiple: float = 3.0
    #: Frames are compared at this downscale. Purely a cost choice; every
    #: reported displacement is scaled back to canonical px.
    downscale: int = 4
    #: How long after the down edge to read the OS key state. Reading it in the
    #: same breath as the post is what produced ``NO_LOOPBACK`` on a key that
    #: was demonstrably held for 322 ms.
    loopback_delay_ms: int = 80
    provenance: Provenance = field(
        default_factory=lambda: Provenance(
            status=EvidenceStatus.PROVISIONAL,
            source="prospector_engine/nativeprobe.py",
            note=(
                "chosen bounds for a diagnostic, not measurements. The "
                "discriminating number is the idle window measured immediately "
                "before each trial; the floors only stop a perfectly still "
                "baseline from promoting noise."
            ),
        )
    )


# ---------------------------------------------------------------------------
# Frames
# ---------------------------------------------------------------------------


class FrameTap:
    """A pull interface over a running capture service. Reads only."""

    def __init__(self, service: Any, *, timeout_s: float = 0.3) -> None:
        self._service = service
        self._timeout_s = timeout_s
        self._sequence = 0

    def drain(self) -> None:
        """Adopt the newest frame without returning anything, so the next
        :meth:`collect` starts from now rather than from a backlog."""
        latest = self._service.latest()
        if latest is not None:
            self._sequence = latest.frame.sequence

    def next_frame(self, timeout_s: float | None = None) -> CapturedFrame | None:
        envelope = self._service.wait_for_new(
            self._sequence, self._timeout_s if timeout_s is None else timeout_s
        )
        if envelope is None:
            return None
        frame: CapturedFrame = envelope.frame
        self._sequence = frame.sequence
        return frame

    def collect(self, duration_s: float) -> list[CapturedFrame]:
        """Every frame that arrives in the next ``duration_s``."""
        deadline = monotonic_s() + duration_s
        frames: list[CapturedFrame] = []
        while monotonic_s() < deadline:
            frame = self.next_frame(min(self._timeout_s, max(0.005, deadline - monotonic_s())))
            if frame is not None:
                frames.append(frame)
        return frames

    def latest_age_ms(self) -> float | None:
        latest = self._service.latest()
        if latest is None:
            return None
        return float(latest.frame.age_s(monotonic_s())) * 1000.0


# ---------------------------------------------------------------------------
# Trials
# ---------------------------------------------------------------------------


class CameraBackend(Enum):
    """The candidate ways to turn the Roblox camera, in ladder order."""

    ARROW_KEYS = "arrow-keys"
    MOUSE_DELTA = "mouse-delta"
    RIGHT_DRAG = "right-drag"

    @property
    def label(self) -> str:
        return {
            CameraBackend.ARROW_KEYS: "Left/Right arrow keys",
            CameraBackend.MOUSE_DELTA: "bare relative mouse delta",
            CameraBackend.RIGHT_DRAG: "right button held, drag deltas",
        }[self]


@dataclass(frozen=True)
class TrialResult:
    """One bounded pulse, and what the game did about it."""

    label: str
    backend: str
    target: str
    posted: bool
    hold_ms: float
    idle: SceneMotion
    during: SceneMotion
    moved: bool
    detail: str
    #: A second still window, measured *after* the key came up. It is the
    #: control: something that was changing throughout the trial - a page
    #: loading tiles, a cutscene, another player walking past - shows up here
    #: too, and a verdict is only earned by beating both.
    after: SceneMotion | None = None
    #: Diagnostic only. Never a reason to fail a trial - see the module note.
    loopback: bool | None = None
    frame_age_ms: float | None = None
    error: str = ""

    @property
    def mad_ratio(self) -> float:
        return self.during.mad / self.idle.mad if self.idle.mad > 1e-9 else float("inf")

    def describe(self) -> str:
        verdict = "MOVED" if self.moved else "no motion"
        ratio = "inf" if self.mad_ratio == float("inf") else f"{self.mad_ratio:.1f}x"
        control = "" if self.after is None else f"/after {self.after.mad:5.2f}"
        return (
            f"{self.label:<28} {verdict:<9} held {self.hold_ms:5.0f} ms  "
            f"mad {self.during.mad:6.2f} vs still {self.idle.mad:5.2f}{control} "
            f"({ratio})  "
            f"shift {self.during.dx_px:+7.1f},{self.during.dy_px:+7.1f} px  "
            f"loopback={self.loopback}"
        )

    def as_row(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "backend": self.backend,
            "target": self.target,
            "posted": self.posted,
            "hold_ms": round(self.hold_ms, 1),
            "moved": self.moved,
            "detail": self.detail,
            "loopback": self.loopback,
            "frame_age_ms": None if self.frame_age_ms is None else round(self.frame_age_ms, 1),
            "error": self.error,
            "idle": {
                "frames": self.idle.frames,
                "mad": round(self.idle.mad, 4),
                "dx_px": round(self.idle.dx_px, 2),
                "dy_px": round(self.idle.dy_px, 2),
            },
            "during": {
                "frames": self.during.frames,
                "mad": round(self.during.mad, 4),
                "peak_mad": round(self.during.peak_mad, 4),
                "dx_px": round(self.during.dx_px, 2),
                "dy_px": round(self.during.dy_px, 2),
                "response": round(self.during.response, 3),
            },
            "after": None
            if self.after is None
            else {
                "frames": self.after.frames,
                "mad": round(self.after.mad, 4),
                "dx_px": round(self.after.dx_px, 2),
                "dy_px": round(self.after.dy_px, 2),
            },
        }


def _classify_motion(
    idle: SceneMotion,
    during: SceneMotion,
    config: NativeProbeConfig,
    *,
    after: SceneMotion | None = None,
) -> tuple[bool, str]:
    """Did the picture change *because of the hold*, rather than merely during it?

    The threshold is a multiple of the **larger** of the two still windows -
    the one before the edge and the one after the key came up. One window alone
    is not enough, and the failure is not hypothetical: a Roblox home page sits
    at a measured 0.00, so the absolute floor is the only guard, and a client
    that lazily loads a row of thumbnails part-way through a 600 ms hold beats
    a floor of 0.45 without any key having done anything. Whatever is changing
    on its own is still changing a moment later; a character that was walking
    has stopped.
    """
    if not during.usable:
        return False, f"only {during.frames} frames during the hold; cannot judge"
    baseline = idle.mad if after is None else max(idle.mad, after.mad)
    threshold = max(config.mad_multiple * baseline, config.min_mad)
    moved = during.mad > threshold
    control = "" if after is None else f", after {after.mad:.3f}"
    return moved, (
        f"mad {during.mad:.3f} vs threshold {threshold:.3f} "
        f"(still {baseline:.3f} x{config.mad_multiple:g}, floor {config.min_mad:g}"
        f"{control}) over {during.frames} frames"
    )


def _classify_shift(
    idle: SceneMotion,
    during: SceneMotion,
    config: NativeProbeConfig,
    *,
    axis: str,
    sign: int,
) -> tuple[bool, str]:
    """Judge a trial on *directed scene translation* rather than on change.

    ``mad`` is the right measure for "is the character walking", because a walk
    changes the whole picture. It is the wrong measure for a camera turn or a
    scrolled page in a busy scene: a Roblox home page with animated tiles sits
    at ``mad`` 28-37 all by itself, so nothing a probe does can beat a multiple
    of it, and a page that visibly scrolled 68 px would be reported as still.
    Phase correlation answers the question that was actually asked - *which way
    and how far did the picture move* - and it is not fooled by things changing
    in place.
    """
    if not during.usable:
        return False, f"only {during.frames} frames during the hold; cannot judge"
    observed = during.dx_px if axis == "x" else during.dy_px
    drift = idle.dx_px if axis == "x" else idle.dy_px
    threshold = max(config.min_turn_px, config.turn_multiple * abs(drift))
    moved = abs(observed) > threshold
    name = "horizontal" if axis == "x" else "vertical"
    return moved, (
        f"{name} shift {observed:+.1f} px vs threshold {threshold:.1f} "
        f"(idle drift {drift:+.1f} px), requested sign {sign:+d}, "
        f"directed {observed * sign:+.1f}, {during.frames} frames"
    )


def run_key_trial(
    *,
    port: Any,
    tap: FrameTap,
    key: InputKey,
    config: NativeProbeConfig,
    label: str | None = None,
    backend_name: str = "",
    on_progress: Callable[[str], None] = lambda _text: None,
) -> TrialResult:
    """One key: idle window, one down edge, a bounded hold, one up edge.

    The up edge is in a ``finally``. Everything between the two - collecting
    frames, reading the OS key state, measuring - can raise, be interrupted, or
    return nonsense, and the key still comes up.
    """
    hold_s = min(config.max_hold_ms, max(1, config.hold_ms)) / 1000.0
    name = label or f"{key.value.upper()} ({backend_name or 'default'})"
    on_progress(f"{name}: measuring the still scene")
    tap.drain()
    idle = measure_scene(tap.collect(hold_s), downscale=config.downscale)
    time.sleep(config.settle_s)
    tap.drain()

    code = port.key_code(key)
    posted = False
    error = ""
    loopback: bool | None = None
    during_frames: list[CapturedFrame] = []
    began = monotonic_s()
    ended = began
    try:
        on_progress(f"{name}: pressing")
        port.raw_key_down(code)
        posted = True
        began = monotonic_s()
        deadline = began + hold_s
        loopback_at = began + config.loopback_delay_ms / 1000.0
        loopback_read = False
        while monotonic_s() < deadline:
            frame = tap.next_frame(min(0.1, max(0.005, deadline - monotonic_s())))
            if frame is not None and frame.captured_at_s > began:
                during_frames.append(frame)
            if not loopback_read and monotonic_s() >= loopback_at:
                loopback_read = True
                with contextlib.suppress(Exception):
                    loopback = port.key_state(key)
    except Exception as exc:  # a probe must never leave a key down
        error = repr(exc)
    finally:
        ended = monotonic_s()
        with contextlib.suppress(Exception):
            port.raw_key_up(code)
    during = measure_scene(during_frames, downscale=config.downscale)
    # The control window. Measured after the key is up and after the same
    # settle, so a scene that changes on its own is measured in the same
    # conditions the verdict is being drawn from.
    time.sleep(config.settle_s)
    tap.drain()
    after = measure_scene(tap.collect(hold_s), downscale=config.downscale)
    moved, detail = _classify_motion(idle, during, config, after=after)
    if error:
        detail = f"{detail}; the trial raised: {error}"
    return TrialResult(
        label=name,
        backend=backend_name,
        target=key.value,
        posted=posted,
        hold_ms=(ended - began) * 1000.0 if posted else 0.0,
        idle=idle,
        during=during,
        moved=moved and not error,
        detail=detail,
        loopback=loopback,
        frame_age_ms=tap.latest_age_ms(),
        error=error,
        after=after,
    )


def run_camera_trial(
    *,
    port: Any,
    tap: FrameTap,
    backend: CameraBackend,
    sign: int,
    config: NativeProbeConfig,
    magnitude_px: int = 40,
    on_progress: Callable[[str], None] = lambda _text: None,
) -> TrialResult:
    """One camera candidate, one direction, released on every path.

    ``RIGHT_DRAG`` is the mechanism the repository's own camera reset already
    uses against Roblox - centre the cursor, hold the right button, emit drag
    deltas, release in a ``finally`` - rather than a bare mouse-moved event,
    which Roblox ignores while no button is down.
    """
    hold_s = min(config.max_hold_ms, max(1, config.hold_ms)) / 1000.0
    direction = "right" if sign > 0 else "left"
    name = f"{backend.value} {direction}"
    on_progress(f"{name}: measuring the still scene")
    tap.drain()
    idle = measure_scene(tap.collect(hold_s), downscale=config.downscale)
    time.sleep(config.settle_s)
    tap.drain()

    key = InputKey.RIGHT if sign > 0 else InputKey.LEFT
    posted = False
    error = ""
    loopback: bool | None = None
    during_frames: list[CapturedFrame] = []
    began = monotonic_s()
    ended = began
    held_button: MouseButton | None = None
    try:
        if backend is CameraBackend.ARROW_KEYS:
            port.raw_key_down(port.key_code(key))
        elif backend is CameraBackend.RIGHT_DRAG:
            _centre_cursor(port)
            port.raw_button_down(MouseButton.RIGHT)
            held_button = MouseButton.RIGHT
        posted = True
        began = monotonic_s()
        deadline = began + hold_s
        loopback_at = began + config.loopback_delay_ms / 1000.0
        loopback_read = False
        step = int(sign * abs(magnitude_px))
        while monotonic_s() < deadline:
            if backend is CameraBackend.MOUSE_DELTA:
                port.raw_pointer_delta(step, 0, None)
            elif backend is CameraBackend.RIGHT_DRAG:
                port.raw_pointer_delta(step, 0, MouseButton.RIGHT)
            frame = tap.next_frame(min(0.05, max(0.005, deadline - monotonic_s())))
            if frame is not None and frame.captured_at_s > began:
                during_frames.append(frame)
            if not loopback_read and monotonic_s() >= loopback_at:
                loopback_read = True
                if backend is CameraBackend.ARROW_KEYS:
                    with contextlib.suppress(Exception):
                        loopback = port.key_state(key)
    except Exception as exc:
        error = repr(exc)
    finally:
        ended = monotonic_s()
        if backend is CameraBackend.ARROW_KEYS:
            with contextlib.suppress(Exception):
                port.raw_key_up(port.key_code(key))
        if held_button is not None:
            with contextlib.suppress(Exception):
                port.raw_button_up(held_button)
    during = measure_scene(during_frames, downscale=config.downscale)
    turned, detail = _classify_shift(idle, during, config, axis="x", sign=sign)
    if error:
        detail = f"{detail}; the trial raised: {error}"
    time.sleep(config.settle_s)
    return TrialResult(
        label=name,
        backend=backend.value,
        target=f"turn:{direction}",
        posted=posted,
        hold_ms=(ended - began) * 1000.0 if posted else 0.0,
        idle=idle,
        during=during,
        moved=turned and not error,
        detail=detail,
        loopback=loopback,
        frame_age_ms=tap.latest_age_ms(),
        error=error,
    )


def run_scroll_trial(
    *,
    port: Any,
    tap: FrameTap,
    config: NativeProbeConfig,
    backend_name: str = "",
    lines_per_step: int = -3,
    on_progress: Callable[[str], None] = lambda _text: None,
) -> TrialResult:
    """Does Roblox act on *anything* we post? Answered without a character.

    A keyboard trial cannot distinguish "our events never arrive" from "the
    game has nothing to move" - and a Roblox client sitting on its home page,
    or at a loading screen, or in a menu, is in the second state. Every W
    trial then reports "no motion", which looks exactly like a broken input
    backend and is not one.

    Scrolling is the one thing a Roblox window will visibly do in *any* state.
    It emits no key, presses no button, clicks nothing and activates nothing;
    it moves the pointer over the client first because macOS routes a scroll by
    where the pointer is rather than by which application is frontmost. If the
    picture changes, this process's CGEvents are reaching Roblox and Roblox is
    acting on them, which is the half of "does input work" that does not need a
    world to stand in.

    It is deliberately **not** evidence that ``W`` moves a character. Scroll is
    routed by hit-testing and keys by focus, and only a character walking
    settles the keyboard question.
    """
    hold_s = min(config.max_hold_ms, max(1, config.hold_ms)) / 1000.0
    name = f"scroll ({backend_name or 'default'})"
    on_progress(f"{name}: measuring the still scene")
    tap.drain()
    idle = measure_scene(tap.collect(hold_s), downscale=config.downscale)
    time.sleep(config.settle_s)
    _centre_cursor(port)
    tap.drain()

    posted = False
    error = ""
    during_frames: list[CapturedFrame] = []
    began = monotonic_s()
    ended = began
    try:
        began = monotonic_s()
        deadline = began + hold_s
        posted = True
        while monotonic_s() < deadline:
            port.raw_scroll_lines(int(lines_per_step))
            frame = tap.next_frame(min(0.06, max(0.005, deadline - monotonic_s())))
            if frame is not None and frame.captured_at_s > began:
                during_frames.append(frame)
    except Exception as exc:
        error = repr(exc)
    finally:
        ended = monotonic_s()
    during = measure_scene(during_frames, downscale=config.downscale)
    # Vertical translation, not ``mad``: a scrolling page moves the picture,
    # and the home page it is most useful on changes 28-37 units per frame
    # while sitting perfectly still.
    moved, detail = _classify_shift(
        idle, during, config, axis="y", sign=-1 if lines_per_step < 0 else 1
    )
    if error:
        detail = f"{detail}; the trial raised: {error}"
    time.sleep(config.settle_s)
    return TrialResult(
        label=name,
        backend=backend_name,
        target="scroll",
        posted=posted,
        hold_ms=(ended - began) * 1000.0,
        idle=idle,
        during=during,
        moved=moved and not error,
        detail=detail,
        frame_age_ms=tap.latest_age_ms(),
        error=error,
    )


def _centre_cursor(port: Any) -> None:
    """Put the pointer in the middle of the client area before a drag."""
    with contextlib.suppress(Exception):
        geometry = port.window_geometry()
        if geometry.valid:
            width, height = geometry.canonical_px
            port.raw_pointer_move_client((width // 2, height // 2))


# ---------------------------------------------------------------------------
# Ladders
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProbeReport:
    """Everything a run of the probe observed, and what it chose."""

    trials: tuple[TrialResult, ...]
    selected_input_backend: str | None
    selected_camera_backend: str | None
    notes: tuple[str, ...] = ()

    def as_payload(self) -> dict[str, Any]:
        return {
            "selected_input_backend": self.selected_input_backend,
            "selected_camera_backend": self.selected_camera_backend,
            "notes": list(self.notes),
            "trials": [trial.as_row() for trial in self.trials],
        }


def keyboard_ladder(
    *,
    port: Any,
    tap: FrameTap,
    backends: Sequence[str],
    key: InputKey,
    config: NativeProbeConfig,
    trials_per_backend: int = 2,
    on_progress: Callable[[str], None] = lambda _text: None,
) -> tuple[list[TrialResult], str | None]:
    """Try each backend in order; stop at the first that *repeatedly* moves.

    "Repeatedly" is the whole point of the ladder: one trial that happens to
    coincide with a passing cloud is not a backend, so a backend is selected
    only when every one of its trials moved.
    """
    results: list[TrialResult] = []
    selected: str | None = None
    for backend in backends:
        if not _select_backend(port, backend):
            on_progress(f"backend {backend}: not supported on this port; skipped")
            continue
        moved_all = True
        for index in range(max(1, trials_per_backend)):
            trial = run_key_trial(
                port=port,
                tap=tap,
                key=key,
                config=config,
                label=f"{key.value.upper()} via {backend} #{index + 1}",
                backend_name=backend,
                on_progress=on_progress,
            )
            results.append(trial)
            on_progress(trial.describe())
            moved_all = moved_all and trial.moved
        if moved_all:
            selected = backend
            break
    _select_backend(port, selected or (backends[0] if backends else ""))
    return results, selected


def camera_ladder(
    *,
    port: Any,
    tap: FrameTap,
    config: NativeProbeConfig,
    candidates: Sequence[CameraBackend] = tuple(CameraBackend),
    on_progress: Callable[[str], None] = lambda _text: None,
) -> tuple[list[TrialResult], str | None]:
    """Try each camera candidate in both directions; the first that turns wins.

    Both directions are required, and the *signs must differ*: a candidate that
    shifts the scene the same way whichever key is pressed is measuring
    something other than the camera.
    """
    results: list[TrialResult] = []
    selected: str | None = None
    for backend in candidates:
        pair: list[TrialResult] = []
        for sign in (1, -1):
            trial = run_camera_trial(
                port=port,
                tap=tap,
                backend=backend,
                sign=sign,
                config=config,
                on_progress=on_progress,
            )
            pair.append(trial)
            results.append(trial)
            on_progress(trial.describe())
        if all(trial.moved for trial in pair) and (
            pair[0].during.dx_px * pair[1].during.dx_px < 0
        ):
            selected = backend.value
            break
    return results, selected


def _select_backend(port: Any, name: str) -> bool:
    """Ask the port to use ``name``. ``True`` if it took effect."""
    if not name:
        return True
    setter = getattr(port, "set_event_backend", None)
    if setter is None:
        return name in ("", "default")
    try:
        return bool(setter(name))
    except Exception:
        return False


@contextlib.contextmanager
def released_afterwards(port: Any) -> Iterator[None]:
    """Sweep every key and button this probe can press, whatever happened."""
    try:
        yield
    finally:
        for key in (
            InputKey.W,
            InputKey.A,
            InputKey.S,
            InputKey.D,
            InputKey.LEFT,
            InputKey.RIGHT,
            InputKey.SPACE,
        ):
            with contextlib.suppress(Exception):
                port.raw_key_up(port.key_code(key))
        for button in (MouseButton.LEFT, MouseButton.RIGHT, MouseButton.MIDDLE):
            with contextlib.suppress(Exception):
                port.raw_button_up(button)
