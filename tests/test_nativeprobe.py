"""The bounded native diagnostic: does it measure honestly, and does it let go?

The probe is the one thing in the repository whose whole job is to press a key
at the real game, so the two properties that matter are that it cannot lie
about what happened and that no path through it leaves a key down.

The port here records edges and never touches an OS. The frames are synthetic
and are wiring stress, not validation: nothing about the detector is asserted
from them, and no gate is passed on their output (plan 7.2).
"""

from __future__ import annotations

from typing import Any, ClassVar

import numpy as np
import pytest

from prospector_engine.contracts import InputKey, MouseButton, freeze_array, monotonic_s
from prospector_engine.nativeprobe import (
    CameraBackend,
    FrameTap,
    NativeProbeConfig,
    keyboard_ladder,
    measure_scene,
    released_afterwards,
    run_camera_trial,
    run_key_trial,
)
from tests.fakes import make_frame

FAST = NativeProbeConfig(
    hold_ms=60, settle_s=0.0, frame_timeout_s=0.02, downscale=2, loopback_delay_ms=5
)


#: One fixed textured world, cropped rather than rolled. A rolled image wraps,
#: and a wrap is a discontinuity that phase correlation reads as a second peak -
#: which underestimates the very shift the test is asserting on. Cropping a
#: window out of a larger canvas is a true translation.
_WORLD = np.clip(np.random.default_rng(11).normal(128.0, 48.0, (128, 320, 3)), 0, 255).astype(
    np.uint8
)


def _frame(sequence: int, *, shift_px: int = 0, noise: float = 0.0, at_s: float) -> Any:
    """A window onto :data:`_WORLD`, optionally translated, optionally speckled."""
    # Wrapped into the canvas so a long run of frames never walks off it.
    left = 96 + int(shift_px) % 128
    base = _WORLD[32:96, left : left + 96].copy()
    if noise:
        rng = np.random.default_rng(sequence)
        base = np.clip(
            base.astype(np.int16) + rng.normal(0.0, noise, base.shape).astype(np.int16),
            0,
            255,
        ).astype(np.uint8)
    frame = make_frame(sequence, captured_at_s=at_s)
    object.__setattr__(frame, "bgr", freeze_array(base))
    return frame


class Scene:
    """A capture service stand-in whose picture moves only when a key is down."""

    def __init__(self, *, responds: bool = True, shift_per_frame: int = 8) -> None:
        self.responds = responds
        self.shift_per_frame = shift_per_frame
        self.held: set[str] = set()
        self.buttons: set[str] = set()
        self._sequence = 0
        self._offset = 0
        self._latest: Any = None
        self.raise_on_frame: int | None = None

    # -- the FrameTap interface -------------------------------------------
    def latest(self) -> Any:
        return self._latest

    def wait_for_new(self, after_sequence: int, timeout_s: float) -> Any:
        del after_sequence, timeout_s
        self._sequence += 1
        # Only while something is held: the point is a failure *during* the
        # hold, which is the path that could leave a key down.
        if (
            self.raise_on_frame is not None
            and (self.held or self.buttons)
            and self._sequence >= self.raise_on_frame
        ):
            raise RuntimeError("the capture source fell over mid-hold")
        if self.responds and (self.held or self.buttons):
            self._offset += self.shift_per_frame
        frame = _frame(self._sequence, shift_px=self._offset, noise=1.0, at_s=monotonic_s())
        self._latest = _Envelope(frame)
        return self._latest


class _Envelope:
    def __init__(self, frame: Any) -> None:
        self.frame = frame


class Port:
    """Records every edge. The whole platform the probe is allowed to see."""

    CODES: ClassVar[dict[str, int]] = {
        "w": 13,
        "a": 0,
        "s": 1,
        "d": 2,
        "left": 123,
        "right": 124,
        "space": 49,
    }

    def __init__(self, scene: Scene, *, raises: str = "") -> None:
        self.scene = scene
        self.raises = raises
        self.edges: list[tuple[str, str]] = []
        self.event_backend = "hid"
        self.selected: list[str] = []

    def key_code(self, key: InputKey) -> int:
        return self.CODES[key.value]

    def _name(self, code: int) -> str:
        return {value: name for name, value in self.CODES.items()}[code]

    def raw_key_down(self, code: int) -> None:
        if self.raises == "down":
            raise RuntimeError("the post call failed")
        name = self._name(code)
        self.edges.append(("down", name))
        self.scene.held.add(name)

    def raw_key_up(self, code: int) -> None:
        name = self._name(code)
        self.edges.append(("up", name))
        self.scene.held.discard(name)

    def raw_button_down(self, button: MouseButton) -> None:
        self.edges.append(("down", f"mouse:{button.value}"))
        self.scene.buttons.add(button.value)

    def raw_button_up(self, button: MouseButton) -> None:
        self.edges.append(("up", f"mouse:{button.value}"))
        self.scene.buttons.discard(button.value)

    def raw_pointer_delta(self, dx: int, dy: int, held: Any = None) -> None:
        del dx, dy, held

    def raw_pointer_move_client(self, point_px: tuple[int, int]) -> None:
        del point_px

    def raw_scroll_lines(self, lines: int) -> None:
        del lines

    def key_state(self, key: InputKey) -> bool | None:
        return key.value in self.scene.held

    def window_geometry(self) -> Any:
        from tests.fakes import make_geometry

        return make_geometry()

    def set_event_backend(self, name: str) -> bool:
        self.selected.append(name)
        if name not in ("hid", "session", "pid"):
            return False
        self.event_backend = name
        return True


def _held(port: Port) -> set[str]:
    """What is still down, from the edges alone."""
    down: set[str] = set()
    for edge, target in port.edges:
        down.add(target) if edge == "down" else down.discard(target)
    return down


# ---------------------------------------------------------------------------
# Measurement
# ---------------------------------------------------------------------------


def test_a_still_scene_and_a_moving_one_are_told_apart() -> None:
    now = monotonic_s()
    still = [_frame(i, at_s=now + i * 0.016) for i in range(6)]
    moving = [_frame(i, shift_px=i * 8, at_s=now + i * 0.016) for i in range(6)]

    assert measure_scene(still, downscale=2).mad < 0.5
    walked = measure_scene(moving, downscale=2)
    assert walked.mad > 5.0
    assert abs(walked.dx_px) > 10.0, "a translating scene must report a translation"


def test_fewer_than_two_frames_cannot_be_judged() -> None:
    reading = measure_scene([_frame(1, at_s=monotonic_s())])
    assert not reading.usable
    assert reading.mad == 0.0


# ---------------------------------------------------------------------------
# Trials
# ---------------------------------------------------------------------------


def test_a_game_that_moves_is_reported_as_moved() -> None:
    scene = Scene(responds=True)
    port = Port(scene)
    trial = run_key_trial(
        port=port, tap=FrameTap(scene, timeout_s=0.02), key=InputKey.W, config=FAST
    )

    assert trial.moved
    assert trial.posted
    assert trial.hold_ms >= FAST.hold_ms * 0.5
    assert _held(port) == set(), "the probe returned with a key down"
    assert port.edges.count(("down", "w")) == 1, "exactly one down edge per trial"
    assert port.edges.count(("up", "w")) == 1


def test_a_game_that_ignores_the_key_is_reported_as_no_motion() -> None:
    """Posted, held, and nothing happened. Not "the post failed"."""
    scene = Scene(responds=False)
    port = Port(scene)
    trial = run_key_trial(
        port=port, tap=FrameTap(scene, timeout_s=0.02), key=InputKey.W, config=FAST
    )

    assert trial.posted, "the edge went out; that is a different fact"
    assert not trial.moved
    assert "threshold" in trial.detail
    assert _held(port) == set()


def test_a_probe_that_raises_still_releases(monkeypatch: Any) -> None:
    """The property that makes this safe to run against a real client."""
    scene = Scene(responds=True)
    scene.raise_on_frame = 1
    port = Port(scene)

    trial = run_key_trial(
        port=port, tap=FrameTap(scene, timeout_s=0.02), key=InputKey.W, config=FAST
    )

    assert trial.error, "the failure must be reported, not swallowed"
    assert not trial.moved
    assert _held(port) == set(), "an exception left a key down"


def test_a_failed_post_is_not_reported_as_a_held_key() -> None:
    scene = Scene(responds=False)
    port = Port(scene, raises="down")

    trial = run_key_trial(
        port=port, tap=FrameTap(scene, timeout_s=0.02), key=InputKey.W, config=FAST
    )

    assert not trial.posted
    assert not trial.moved
    assert _held(port) == set()


def test_a_camera_trial_releases_its_button_on_every_path() -> None:
    scene = Scene(responds=True)
    port = Port(scene)

    trial = run_camera_trial(
        port=port,
        tap=FrameTap(scene, timeout_s=0.02),
        backend=CameraBackend.RIGHT_DRAG,
        sign=1,
        config=FAST,
    )

    assert trial.posted
    assert ("down", "mouse:right") in port.edges
    assert ("up", "mouse:right") in port.edges
    assert _held(port) == set()


def test_the_loopback_is_recorded_and_never_decides() -> None:
    """A trial's verdict comes from the frames. The reading is reported."""
    scene = Scene(responds=True)
    port = Port(scene)
    trial = run_key_trial(
        port=port, tap=FrameTap(scene, timeout_s=0.02), key=InputKey.W, config=FAST
    )
    assert trial.loopback is True
    assert trial.moved

    # The same game, with a window server that never reports the key as down.
    scene2 = Scene(responds=True)
    port2 = Port(scene2)
    port2.key_state = lambda key: False  # type: ignore[assignment,method-assign]
    trial2 = run_key_trial(
        port=port2, tap=FrameTap(scene2, timeout_s=0.02), key=InputKey.W, config=FAST
    )
    assert trial2.loopback is False
    assert trial2.moved, "a false loopback must not overturn observed motion"


# ---------------------------------------------------------------------------
# The ladder
# ---------------------------------------------------------------------------


def test_the_ladder_stops_at_the_first_backend_that_repeatedly_moves() -> None:
    scene = Scene(responds=True)
    port = Port(scene)

    results, selected = keyboard_ladder(
        port=port,
        tap=FrameTap(scene, timeout_s=0.02),
        backends=("hid", "session", "pid"),
        key=InputKey.W,
        config=FAST,
        trials_per_backend=2,
    )

    assert selected == "hid"
    assert len(results) == 2, "it must not go on testing after one has won"
    assert "session" not in port.selected[:2]


def test_a_backend_that_moves_once_by_luck_is_not_selected() -> None:
    """Two trials, and both must move. One is a coincidence, not a backend."""
    scene = Scene(responds=True)
    port = Port(scene)
    calls = {"n": 0}

    def flaky(code: int) -> None:
        calls["n"] += 1
        scene.responds = calls["n"] == 1
        Port.raw_key_down(port, code)

    port.raw_key_down = flaky  # type: ignore[method-assign]

    _results, selected = keyboard_ladder(
        port=port,
        tap=FrameTap(scene, timeout_s=0.02),
        backends=("hid",),
        key=InputKey.W,
        config=FAST,
        trials_per_backend=2,
    )

    assert selected is None


def test_an_unsupported_backend_is_skipped_rather_than_silently_aliased() -> None:
    """A ladder that cannot tell which backend it used selects the wrong winner."""
    scene = Scene(responds=True)
    port = Port(scene)

    _results, selected = keyboard_ladder(
        port=port,
        tap=FrameTap(scene, timeout_s=0.02),
        backends=("nonsense", "hid"),
        key=InputKey.W,
        config=FAST,
        trials_per_backend=1,
    )

    assert selected == "hid"


def test_the_runner_sweeps_the_vocabulary_however_it_exits() -> None:
    scene = Scene(responds=True)
    port = Port(scene)

    with pytest.raises(RuntimeError), released_afterwards(port):
        port.raw_key_down(port.key_code(InputKey.W))
        port.raw_button_down(MouseButton.RIGHT)
        raise RuntimeError("the run fell over")

    lifted = {target for edge, target in port.edges if edge == "up"}
    assert {"w", "a", "s", "d", "left", "right", "space"} <= lifted
    assert {"mouse:left", "mouse:right", "mouse:middle"} <= lifted
    assert _held(port) == set()
