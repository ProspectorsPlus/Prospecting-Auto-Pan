"""Canonical viewport guard, one coherent frame source, and the evidence registry.

One logical decision reads exactly one stamped frame (bug B12). The UI never
captures independently; it renders from the same published envelope the
controller used, so a diagnostic overlay can never disagree with the decision
it is drawing.

Freshness is computed by the consumer from ``captured_at_s`` - the *start* of
acquisition - so a frame that waited in a queue cannot look young (plan 5).
"""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

import numpy as np
from numpy.typing import NDArray

from prospector_engine.contracts import (
    EVIDENCE_MINT_KEY,
    CapturedFrame,
    ClientRectPhysicalPx,
    EvidenceStatus,
    EvidenceToken,
    FrameEnvelope,
    Provenance,
    freeze_array,
    monotonic_s,
)
from prospector_engine.ports import PlatformPort

__all__ = [
    "CaptureBackend",
    "CaptureConfig",
    "CaptureService",
    "EvidenceRegistry",
    "MssCaptureBackend",
    "ViewportGuard",
    "ViewportStatus",
]


@dataclass(frozen=True)
class CaptureConfig:
    """Cadence and freshness budget - provisional configuration, not results."""

    target_interval_ms: int = 50
    max_capture_duration_ms: int = 40
    max_frame_age_ms: int = 100
    stall_after_ms: int = 500
    duplicate_budget: int = 3
    viewport_recheck_interval_ms: int = 250
    provenance: Provenance = field(
        default_factory=lambda: Provenance(
            status=EvidenceStatus.PROVISIONAL,
            source="TREASURE_NAVIGATION_PLAN.md section 7.4 E-PERF",
            note="20 Hz controller target; E-PERF measurement is PENDING",
        )
    )


@runtime_checkable
class CaptureBackend(Protocol):
    """A screen-grab backend. Recreated wholesale when it starts failing."""

    def grab_client(self, rect: ClientRectPhysicalPx) -> NDArray[np.uint8]:
        """Return an HxWx3 BGR array of the client area in physical pixels."""
        ...

    def close(self) -> None: ...


class MssCaptureBackend:
    """The production backend. ``mss`` handles are per-thread by design."""

    def __init__(self) -> None:
        import mss

        factory = getattr(mss, "MSS", None) or mss.mss
        self._sct: Any = factory()

    def grab_client(self, rect: ClientRectPhysicalPx) -> NDArray[np.uint8]:
        region = {
            "left": rect.origin_px[0],
            "top": rect.origin_px[1],
            "width": rect.width_px,
            "height": rect.height_px,
        }
        raw = np.asarray(self._sct.grab(region))
        return np.ascontiguousarray(raw[:, :, :3])  # BGRA -> BGR

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._sct.close()


@dataclass(frozen=True)
class ViewportStatus:
    rect: ClientRectPhysicalPx | None
    pinned: ClientRectPhysicalPx | None
    valid: bool
    reason: str

    @property
    def identity_matches(self) -> bool:
        if self.rect is None or self.pinned is None:
            return False
        return self.rect.identity() == self.pinned.identity()


class ViewportGuard:
    """Owns the pinned client rect and decides when it stops being true.

    A size error above one physical pixel, a display/DPI change, a resize, a
    move, a fullscreen transition, or a lost client identity all invalidate
    the contract; invalidation releases input *before* reacquisition
    (plan 4.1). The release itself is the coordinator's job - this class only
    reports the truth.
    """

    def __init__(
        self, port: PlatformPort, requested_size_px: tuple[int, int] = (1280, 720)
    ) -> None:
        self._port = port
        self._requested_size_px = requested_size_px
        self._lock = threading.Lock()
        self._pinned: ClientRectPhysicalPx | None = None
        self._last_status = ViewportStatus(None, None, False, "not-pinned")

    @property
    def requested_size_px(self) -> tuple[int, int]:
        return self._requested_size_px

    @property
    def pinned(self) -> ClientRectPhysicalPx | None:
        with self._lock:
            return self._pinned

    def pin(self) -> tuple[bool, str, ClientRectPhysicalPx | None]:
        result = self._port.pin_client_rect(self._requested_size_px)
        with self._lock:
            self._pinned = result.rect if result.ok else None
        self.check()
        return result.ok, result.message, result.rect

    def adopt_current(self) -> ClientRectPhysicalPx | None:
        """Adopt the client rect as-is, without moving the window.

        Shadow observation and recording are useful before the owner is ready
        to have the window moved, so adoption is offered explicitly rather
        than making ``pin`` a silent side effect of starting.
        """
        rect = self._port.find_client_rect()
        with self._lock:
            self._pinned = rect if rect is not None and rect.valid else None
        self.check()
        return rect

    def invalidate(self, reason: str) -> None:
        with self._lock:
            self._pinned = None
            self._last_status = ViewportStatus(None, None, False, reason)

    def check(self) -> ViewportStatus:
        rect = self._port.find_client_rect()
        with self._lock:
            pinned = self._pinned
            if rect is None:
                status = ViewportStatus(None, pinned, False, "client-not-found")
            elif not rect.valid:
                status = ViewportStatus(rect, pinned, False, rect.invalid_reason or "invalid")
            elif pinned is None:
                status = ViewportStatus(rect, None, False, "not-pinned")
            elif rect.identity() != pinned.identity():
                status = ViewportStatus(
                    rect, pinned, False, f"identity-changed:{rect.identity()}"
                )
            else:
                status = ViewportStatus(rect, pinned, True, "ok")
            self._last_status = status
            return status

    def last_status(self) -> ViewportStatus:
        with self._lock:
            return self._last_status


class EvidenceRegistry:
    """Mints the opaque tokens that authorize acting on a specific frame.

    The mint key lives in :mod:`prospector_engine.contracts` and only this
    class holds it, so feature code cannot construct a token; the authority
    additionally checks object identity against what was registered
    (plan 5).
    """

    def __init__(
        self, run_id: str, on_token: Callable[[EvidenceToken], None] | None = None
    ) -> None:
        self._run_id = run_id
        self._on_token = on_token
        self._generation = 0
        self._lock = threading.Lock()

    def set_generation(self, generation: int) -> None:
        with self._lock:
            self._generation = generation

    def envelope_for(self, frame: CapturedFrame) -> FrameEnvelope:
        with self._lock:
            generation = self._generation
        token = EvidenceToken(
            run_id=self._run_id,
            generation=generation,
            frame_sequence=frame.sequence,
            captured_at_s=frame.captured_at_s,
            duration_ms=frame.duration_ms,
            viewport_identity=frame.client_rect.identity(),
            _mint_key=EVIDENCE_MINT_KEY,
        )
        if self._on_token is not None:
            self._on_token(token)
        return FrameEnvelope(frame=frame, evidence_token=token)


class CaptureService:
    """One daemon thread publishing the latest coherent frame envelope.

    The published slot is size one and drop-oldest: a slow consumer sees the
    newest truth rather than a backlog, and the producer never blocks
    (bug B13).
    """

    def __init__(
        self,
        guard: ViewportGuard,
        registry: EvidenceRegistry,
        *,
        config: CaptureConfig | None = None,
        backend_factory: Callable[[], CaptureBackend] = MssCaptureBackend,
        on_frame: Callable[[FrameEnvelope], None] | None = None,
    ) -> None:
        self._guard = guard
        self._registry = registry
        self._config = config or CaptureConfig()
        self._backend_factory = backend_factory
        self._on_frame = on_frame

        self._lock = threading.Lock()
        self._latest: FrameEnvelope | None = None
        self._sequence = 0
        self._duplicate_run = 0
        self._last_signature: int | None = None
        self._last_success_s: float | None = None
        self._last_error: str | None = None
        self._backend: CaptureBackend | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    @property
    def config(self) -> CaptureConfig:
        return self._config

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="treasure-capture", daemon=True)
        self._thread.start()

    def stop(self, timeout_s: float = 1.0) -> bool:
        self._stop.set()
        thread = self._thread
        self._thread = None
        if thread is not None:
            thread.join(timeout_s)
            if thread.is_alive():
                return False
        backend = self._backend
        self._backend = None
        if backend is not None:
            backend.close()
        return True

    @property
    def running(self) -> bool:
        thread = self._thread
        return bool(thread is not None and thread.is_alive())

    def latest(self) -> FrameEnvelope | None:
        with self._lock:
            return self._latest

    def latest_age_s(self) -> float | None:
        with self._lock:
            envelope = self._latest
        if envelope is None:
            return None
        return envelope.frame.age_s(monotonic_s())

    def last_error(self) -> str | None:
        with self._lock:
            return self._last_error

    def stalled(self) -> bool:
        with self._lock:
            last = self._last_success_s
        if last is None:
            return self.running
        return (monotonic_s() - last) * 1000.0 > self._config.stall_after_ms

    def capture_once(self) -> FrameEnvelope | None:
        """One coherent acquisition. Exposed so tests can drive it directly."""
        status = self._guard.check()
        rect = status.rect
        if rect is None or not status.valid:
            self._record_error(f"viewport:{status.reason}")
            return None
        if self._backend is None:
            self._backend = self._backend_factory()
        started_s = monotonic_s()
        try:
            bgr = self._backend.grab_client(rect)
        except Exception as exc:
            self._record_error(f"backend:{exc!r}")
            with contextlib.suppress(Exception):
                self._backend.close()
            self._backend = None  # recreate on the next pass
            return None
        completed_s = monotonic_s()

        signature = int(bgr[:: max(1, bgr.shape[0] // 16), :: max(1, bgr.shape[1] // 16)].sum())
        with self._lock:
            duplicate = signature == self._last_signature
            self._duplicate_run = self._duplicate_run + 1 if duplicate else 0
            self._last_signature = signature
            self._sequence += 1
            sequence = self._sequence
            self._last_success_s = completed_s
            self._last_error = None

        frame = CapturedFrame(
            sequence=sequence,
            captured_at_s=started_s,
            completed_at_s=completed_s,
            duration_ms=(completed_s - started_s) * 1000.0,
            client_rect=rect,
            bgr=freeze_array(bgr),
            duplicate=duplicate,
            capture_error=None,
        )
        envelope = self._registry.envelope_for(frame)
        with self._lock:
            self._latest = envelope
        if self._on_frame is not None:
            with contextlib.suppress(Exception):
                self._on_frame(envelope)
        return envelope

    def duplicate_run(self) -> int:
        with self._lock:
            return self._duplicate_run

    def over_duplicate_budget(self) -> bool:
        return self.duplicate_run() > self._config.duplicate_budget

    def _record_error(self, message: str) -> None:
        with self._lock:
            self._last_error = message

    def _loop(self) -> None:
        interval_s = self._config.target_interval_ms / 1000.0
        while not self._stop.is_set():
            started = monotonic_s()
            with contextlib.suppress(Exception):
                self.capture_once()
            elapsed = monotonic_s() - started
            if self._stop.wait(max(0.0, interval_s - elapsed)):
                return
