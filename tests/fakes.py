"""Deterministic fakes: virtual clock, recording platform port, synthetic frames.

No test in this repository emits real OS input. Everything the production code
would send to Quartz or SendInput lands in :class:`FakePlatformPort.transcript`
instead, stamped with the virtual clock, which is what makes before/after input
ordering comparable across the Phase 0 rewrite.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from numpy.typing import NDArray

from prospector_engine.contracts import (
    CapturedFrame,
    ClientRectPhysicalPx,
    FocusState,
    FrameEnvelope,
    InputKey,
    InputVocabulary,
    MouseButton,
    PinResult,
    RuntimeIntent,
    freeze_array,
)
from prospector_engine.input_authority import DeadmanClient

__all__ = [
    "MAC_KEYCODES",
    "FakeCancellation",
    "FakeCaptureBackend",
    "FakeDeadmanClient",
    "FakeFrameSource",
    "FakeHotkeySource",
    "FakePlatformPort",
    "VirtualClock",
    "install_virtual_clock",
    "make_frame",
    "make_rect",
]

#: The real macOS virtual keycodes, so transcripts line up with the legacy
#: fixture recorded from the pre-navigator engine.
MAC_KEYCODES: dict[InputKey, int] = {
    InputKey.W: 13,
    InputKey.A: 0,
    InputKey.S: 1,
    InputKey.D: 2,
    InputKey.SPACE: 49,
    InputKey.SHIFT: 56,
    InputKey.ESCAPE: 53,
    InputKey.DIGIT_1: 18,
    InputKey.DIGIT_2: 19,
}


class VirtualClock:
    """Monotonic time the test advances explicitly. Never sleeps."""

    def __init__(self, start_s: float = 1000.0) -> None:
        self._now_s = start_s
        self._lock = threading.Lock()

    def now(self) -> float:
        with self._lock:
            return self._now_s

    def advance(self, seconds: float) -> float:
        with self._lock:
            self._now_s += max(0.0, seconds)
            return self._now_s

    def ms_since(self, origin_s: float) -> float:
        return (self.now() - origin_s) * 1000.0


def install_virtual_clock(monkeypatch: Any, clock: VirtualClock, *modules: str) -> None:
    """Point every named module's ``monotonic_s`` at the virtual clock."""
    import importlib

    for name in modules:
        module = importlib.import_module(name)
        monkeypatch.setattr(module, "monotonic_s", clock.now)


class FakeCancellation:
    """Cancellation whose ``wait`` advances virtual time instead of sleeping.

    ``cancel_after_waits`` injects a stop request in the middle of a bounded
    sequence, which is how the cancellation tests reach every wait point.
    """

    def __init__(self, clock: VirtualClock, cancel_after_waits: int | None = None) -> None:
        self._clock = clock
        self._cancelled = False
        self._waits = 0
        self._cancel_after = cancel_after_waits

    def cancel(self) -> None:
        self._cancelled = True

    def is_cancelled(self) -> bool:
        return self._cancelled

    @property
    def waits(self) -> int:
        return self._waits

    def wait(self, timeout_s: float) -> bool:
        self._waits += 1
        if self._cancel_after is not None and self._waits > self._cancel_after:
            self._cancelled = True
        if self._cancelled:
            return True
        self._clock.advance(timeout_s)
        return False


def make_rect(
    *,
    origin_px: tuple[int, int] = (0, 0),
    size_px: tuple[int, int] = (1280, 720),
    scale: float = 1.0,
    display_id: str = "fake-display",
    valid: bool = True,
    verified_at_s: float = 0.0,
) -> ClientRectPhysicalPx:
    return ClientRectPhysicalPx(
        origin_px=origin_px,
        size_px=size_px,
        scale=scale,
        verified_at_s=verified_at_s,
        display_id=display_id,
        valid=valid,
        invalid_reason=None if valid else "test",
    )


def make_frame(
    sequence: int = 1,
    *,
    captured_at_s: float = 0.0,
    duration_ms: float = 5.0,
    rect: ClientRectPhysicalPx | None = None,
    pixels: dict[tuple[int, int], tuple[int, int, int]] | None = None,
    fill_rgb: tuple[int, int, int] = (0, 0, 0),
    duplicate: bool = False,
) -> CapturedFrame:
    """A synthetic frame with specific RGB values painted at specific points.

    Each requested point is painted as a 7x7 block - exactly covering the
    detectors' 6x6 sample box - so mean-of-box sampling returns the exact value
    and two points 7 px apart (as the dig-spot pair is) stay independent.
    """
    rect = rect or make_rect()
    bgr = np.zeros((rect.height_px, rect.width_px, 3), dtype=np.uint8)
    bgr[:, :, 0] = fill_rgb[2]
    bgr[:, :, 1] = fill_rgb[1]
    bgr[:, :, 2] = fill_rgb[0]
    for (x, y), (r, g, b) in (pixels or {}).items():
        top, left = max(0, y - 3), max(0, x - 3)
        bgr[top : top + 7, left : left + 7] = (b, g, r)
    return CapturedFrame(
        sequence=sequence,
        captured_at_s=captured_at_s,
        completed_at_s=captured_at_s + duration_ms / 1000.0,
        duration_ms=duration_ms,
        client_rect=rect,
        bgr=freeze_array(bgr),
        duplicate=duplicate,
        capture_error=None,
    )


class FakeFrameSource:
    """A frame source with a scripted sequence of frames.

    The last frame repeats once the script is exhausted, so a service that
    polls more often than the script anticipated still gets coherent data
    instead of ``None``.
    """

    def __init__(self, envelopes: list[FrameEnvelope] | None = None) -> None:
        self._envelopes: list[FrameEnvelope] = list(envelopes or [])
        self._index = 0
        self.reads = 0

    def push(self, envelope: FrameEnvelope) -> None:
        self._envelopes.append(envelope)

    def latest(self) -> FrameEnvelope | None:
        self.reads += 1
        if not self._envelopes:
            return None
        envelope = self._envelopes[min(self._index, len(self._envelopes) - 1)]
        self._index += 1
        return envelope


@dataclass
class FakeCaptureBackend:
    """Capture backend that returns scripted arrays and can be made to fail."""

    frames: list[NDArray[np.uint8]] = field(default_factory=list)
    fail_times: int = 0
    grabs: int = 0
    closed: int = 0

    def grab_client(self, rect: ClientRectPhysicalPx) -> NDArray[np.uint8]:
        self.grabs += 1
        if self.fail_times > 0:
            self.fail_times -= 1
            raise OSError("scripted capture failure")
        if not self.frames:
            return np.zeros((rect.height_px, rect.width_px, 3), dtype=np.uint8)
        return self.frames[min(self.grabs - 1, len(self.frames) - 1)]

    def close(self) -> None:
        self.closed += 1


class FakePlatformPort:
    """Records every edge it is asked to emit; never touches the OS."""

    def __init__(
        self,
        clock: VirtualClock,
        *,
        rect: ClientRectPhysicalPx | None = None,
        focus: FocusState = True,
        journal: list[str] | None = None,
    ) -> None:
        self._clock = clock
        self.journal = journal if journal is not None else []
        self._rect = rect if rect is not None else make_rect()
        self._focus: FocusState = focus
        self._vocabulary = InputVocabulary()
        self.transcript: list[dict[str, Any]] = []
        self.cursor_px: tuple[int, int] | None = (100, 100)
        self.fail_ops: set[str] = set()
        self.pin_calls = 0
        self._lock = threading.Lock()

    # -- test controls ----------------------------------------------------
    def set_focus(self, focus: FocusState) -> None:
        self._focus = focus

    def set_rect(self, rect: ClientRectPhysicalPx | None) -> None:
        self._rect = rect  # type: ignore[assignment]

    def fail(self, *ops: str) -> None:
        self.fail_ops.update(ops)

    def ops(self, *, exclude: tuple[str, ...] = ()) -> list[tuple[str, tuple[Any, ...]]]:
        return [
            (entry["op"], tuple(entry["args"]))
            for entry in self.transcript
            if entry["op"] not in exclude
        ]

    def _record(self, op: str, *args: Any) -> None:
        if op in self.fail_ops:
            raise OSError(f"scripted failure: {op}")
        with self._lock:
            self.transcript.append(
                {"t_ms": round(self._clock.now() * 1000.0, 3), "op": op, "args": list(args)}
            )
            self.journal.append(f"port:{op}")

    # -- PlatformPort -----------------------------------------------------
    @property
    def name(self) -> str:
        return "fake"

    @property
    def vocabulary(self) -> InputVocabulary:
        return self._vocabulary

    def key_code(self, key: InputKey) -> int:
        return MAC_KEYCODES[key]

    def focus_state(self) -> FocusState:
        return self._focus

    def find_client_rect(self) -> ClientRectPhysicalPx | None:
        return self._rect

    def pin_client_rect(self, size_px: tuple[int, int]) -> PinResult:
        self.pin_calls += 1
        if self._rect is None:
            return PinResult(False, "no window")
        return PinResult(True, f"pinned {size_px}", self._rect)

    def raw_key_down(self, code: int) -> None:
        self._record("key_down", code)

    def raw_key_up(self, code: int) -> None:
        self._record("key_up", code)

    def raw_button_down(self, button: MouseButton) -> None:
        self._record(
            {"left": "lmb_down", "right": "rmb_down", "middle": "mmb_down"}[button.value]
        )

    def raw_button_up(self, button: MouseButton) -> None:
        self._record({"left": "lmb_up", "right": "rmb_up", "middle": "mmb_up"}[button.value])

    def raw_pointer_move_client(self, point_px: tuple[int, int]) -> None:
        self._record("move_abs_px", int(point_px[0]), int(point_px[1]))

    def raw_pointer_delta(
        self, dx: int, dy: int, held_button: MouseButton | None = None
    ) -> None:
        self._record("drag_delta", int(dx), int(dy))

    def raw_scroll_lines(self, lines: int) -> None:
        self._record("scroll", int(lines))

    def cursor_client_px(self) -> tuple[int, int] | None:
        return self.cursor_px

    def create_hotkey_source(self, submit: Callable[[RuntimeIntent], None]) -> FakeHotkeySource:
        return FakeHotkeySource(submit)


class FakeHotkeySource:
    def __init__(self, submit: Callable[[RuntimeIntent], None]) -> None:
        self.submit = submit
        self.started = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.started = False

    def is_running(self) -> bool:
        return self.started


class FakeDeadmanClient(DeadmanClient):
    """In-process stand-in for the helper, with failure injection.

    It answers the same protocol the real subprocess does, so the ACK-before-
    down ordering can be asserted without spawning a process. The real helper
    is exercised separately, end to end, through its file sink.
    """

    def __init__(self, *, healthy: bool = True, journal: list[str] | None = None) -> None:
        super().__init__(token="fake", argv=["-"])
        self._healthy = healthy
        self.journal = journal if journal is not None else []
        self.calls: list[tuple[str, Any]] = []
        self.registered: dict[int, str] = {}
        self.refuse_register = False
        self.refuse_renew = False
        self.refuse_release_all = False
        self.release_all_count = 0

    def start(self) -> None:
        self._healthy = True

    def set_healthy(self, healthy: bool) -> None:
        self._healthy = healthy

    def register(self, lease_id: int, generation: int, target: str, expires_in_ms: int) -> bool:
        self.calls.append(("register", (lease_id, generation, target, expires_in_ms)))
        self.journal.append(f"deadman:register:{target}")
        if self.refuse_register or not self._healthy:
            return False
        self.registered[lease_id] = target
        return True

    def renew(self, lease_id: int, generation: int, expires_in_ms: int) -> bool:
        self.calls.append(("renew", (lease_id, generation, expires_in_ms)))
        self.journal.append("deadman:renew")
        return not self.refuse_renew and self._healthy and lease_id in self.registered

    def forget(self, lease_id: int) -> bool:
        self.calls.append(("forget", lease_id))
        self.journal.append("deadman:forget")
        self.registered.pop(lease_id, None)
        return True

    def release_all(self, reason: str) -> bool:
        self.calls.append(("release_all", reason))
        self.journal.append("deadman:release_all")
        self.release_all_count += 1
        self.registered.clear()
        return not self.refuse_release_all

    def ping(self) -> bool:
        return self._healthy

    def close(self, timeout_s: float = 1.0) -> None:
        self._healthy = False

    def ops(self) -> Iterator[str]:
        return (name for name, _ in self.calls)
