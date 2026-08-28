"""macOS platform port: window geometry, capture sources, Quartz input, hotkeys.

Instance based, with no module-global engine binding and no authoritative held
input state - both belong to :class:`~prospector_engine.input_authority.InputAuthority`
(bugs B1, B7, B8, B10).

**Coordinate discipline.** Everything this module exchanges with macOS is in
*logical points*: ``CGWindowListCopyWindowInfo`` bounds, Accessibility position
and size, CGEvent locations, and ScreenCaptureKit source rects. Device pixels
appear only as the ``backing_scale`` recorded in
:class:`~prospector_engine.geometry.DisplayInfo` and as the dimensions of a
captured raster. Handing a device-pixel rectangle to any of these APIs is the
bug that captured the desktop instead of the game (DECISIONS.md D-017).

The client area is derived from the window frame minus a title-bar height that
is *measured* from the window's own traffic lights rather than assumed, with a
documented fallback when Accessibility cannot see them.
"""

from __future__ import annotations

import contextlib
import os
import sys
import threading
from collections.abc import Callable
from typing import Any

import numpy as np

if sys.platform != "darwin" and not os.environ.get("TREASURE_ALLOW_CROSS_PLATFORM_IMPORT"):
    raise ImportError(
        "prospector_engine.platform_mac may only be imported on macOS. "
        "Set TREASURE_ALLOW_CROSS_PLATFORM_IMPORT=1 with mocked Quartz modules "
        "to run the opposite-OS import contract test (plan 16.3)."
    )

try:
    import Quartz
except ImportError as exc:  # pragma: no cover - install-time failure
    raise ImportError(
        "prospector_engine.platform_mac requires pyobjc-framework-Quartz "
        f"({exc}). Install with: pip install -e '.[dev]'"
    ) from exc

from prospector_engine.capture import normalize_into_canonical
from prospector_engine.contracts import (
    FocusState,
    InputKey,
    InputVocabulary,
    IntentType,
    MouseButton,
    PinResult,
    Provenance,
    RawFrame,
    RuntimeIntent,
    monotonic_s,
)
from prospector_engine.geometry import (
    CANONICAL_SIZE_PX,
    DisplayInfo,
    LogicalRect,
    ViewportGeometry,
    ViewportState,
    WindowIdentity,
)

__all__ = [
    "MAC_HOTKEY_BINDINGS",
    "TITLE_BAR_FALLBACK_PT",
    "MacHotkeySource",
    "MacPlatformPort",
    "MacQuartzWindowSource",
    "MacReleaseOnlyPort",
    "MacScreenCaptureKitSource",
    "screencapturekit_available",
]

# macOS ANSI virtual keycodes for the whole input vocabulary. These are the
# same numbers the legacy V3_KEYCODES table used; only the lookup moved.
_MAC_KEYCODES: dict[InputKey, int] = {
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

_MAC_BUTTON_EVENTS: dict[MouseButton, tuple[int, int, int, int]] = {
    # (down, up, dragged, CGMouseButton)
    MouseButton.LEFT: (
        Quartz.kCGEventLeftMouseDown,
        Quartz.kCGEventLeftMouseUp,
        Quartz.kCGEventLeftMouseDragged,
        Quartz.kCGMouseButtonLeft,
    ),
    MouseButton.RIGHT: (
        Quartz.kCGEventRightMouseDown,
        Quartz.kCGEventRightMouseUp,
        Quartz.kCGEventRightMouseDragged,
        Quartz.kCGMouseButtonRight,
    ),
    MouseButton.MIDDLE: (
        Quartz.kCGEventOtherMouseDown,
        Quartz.kCGEventOtherMouseUp,
        Quartz.kCGEventOtherMouseDragged,
        Quartz.kCGMouseButtonCenter,
    ),
}

TITLE_BAR_FALLBACK_PT = 28.0
"""Provisional standard-window title-bar height in points.

Only used when Accessibility cannot expose the window's close button. The
measured value for the live Roblox client on the development Mac
(2026-08-27, macOS 25.4, 2x display) was exactly 28.0 pt, derived as
``2 * (close_button_y - frame_y) + close_button_height``.
"""

TITLE_BAR_PROVENANCE = Provenance(
    status=__import__(
        "prospector_engine.contracts", fromlist=["EvidenceStatus"]
    ).EvidenceStatus.PROVISIONAL,
    source="platform_mac.MacPlatformPort._measure_title_bar_pt fallback",
    note="measured 28.0 pt on the dev Mac; E-VIEW on macOS is PENDING",
)

MAC_HOTKEY_BINDINGS: dict[str, IntentType] = {
    "f1": IntentType.START_LIVE,
    "f2": IntentType.STOP,
    "f3": IntentType.PIXEL_INFO,
    "f4": IntentType.RESET_CHARACTER,
    "f5": IntentType.PAN_SWAP_TEST,
    "f6": IntentType.DIG_LOOP,
}
"""F1-F6, all actually bound (bug B4: F5 was advertised but bound nowhere).

F6 is the standalone dig loop (DECISIONS.md D-015).
"""

_MAC_HOTKEY_VK: dict[str, int] = {
    "f1": 122,
    "f2": 120,
    "f3": 99,
    "f4": 118,
    "f5": 96,
    "f6": 97,
}


def _post(event: Any) -> None:
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)


def _cursor_point_pt() -> Any:
    """Cursor location in **logical points**, which is what CGEvent reports."""
    return Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))


def screencapturekit_available() -> bool:
    """Whether the async window-capture backend can be used on this machine."""
    try:
        import CoreMedia  # noqa: F401
        import ScreenCaptureKit  # noqa: F401
    except Exception:
        return False
    return True


class MacReleaseOnlyPort:
    """The strict release-only backend the deadman helper holds.

    Deliberately has no ``down`` method: there is no code path in this class
    that can press anything (plan 4.5).
    """

    def __init__(self) -> None:
        self._vocabulary = InputVocabulary()

    @property
    def vocabulary(self) -> InputVocabulary:
        return self._vocabulary

    def key_code(self, key: InputKey) -> int:
        return _MAC_KEYCODES[key]

    def raw_key_up(self, code: int) -> None:
        _post(Quartz.CGEventCreateKeyboardEvent(None, code, False))

    def raw_button_up(self, button: MouseButton) -> None:
        _, up, _drag, btn = _MAC_BUTTON_EVENTS[button]
        _post(Quartz.CGEventCreateMouseEvent(None, up, _cursor_point_pt(), btn))


# ===========================================================================
# Window-specific capture sources
# ===========================================================================


class MacQuartzWindowSource:
    """Synchronous window capture through ``CGWindowListCreateImage``.

    It captures the Roblox window itself, not a desktop rectangle that happens
    to contain it, so an overlapping window cannot contaminate a frame.

    Measured on the development Mac (2x display, 1280x720 client): ~13 ms per
    frame including the copy - about a 75 Hz ceiling with no headroom left for
    perception. It is the dependency-light fallback; ScreenCaptureKit is
    preferred (DECISIONS.md D-018).
    """

    def __init__(self) -> None:
        self._geometry: ViewportGeometry | None = None
        self._pool: Any = None
        self._error: str | None = None
        self._counter = 0

    @property
    def name(self) -> str:
        return "quartz-window"

    @property
    def is_pushing(self) -> bool:
        return False

    def start(
        self, geometry: ViewportGeometry, pool: Any, on_frame: Callable[[RawFrame], None]
    ) -> None:
        del on_frame  # pull source
        self._geometry = geometry
        self._pool = pool
        self._error = None

    def set_target_fps(self, fps: int) -> None:
        del fps  # paced by the service

    def stop(self) -> None:
        self._geometry = None

    def health(self) -> str | None:
        return self._error

    def poll(self) -> RawFrame | None:
        geometry = self._geometry
        if geometry is None or geometry.window is None or geometry.client_logical is None:
            return None
        source = geometry.client_rect_in_window_logical
        started = monotonic_s()
        # Nominal resolution: the canonical raster is defined in logical units,
        # so asking for backing pixels would only pay to downscale them again.
        image = Quartz.CGWindowListCreateImage(
            Quartz.CGRectMake(source.x, source.y, source.width, source.height),
            Quartz.kCGWindowListOptionIncludingWindow,
            geometry.window.window_id,
            Quartz.kCGWindowImageBoundsIgnoreFraming | Quartz.kCGWindowImageNominalResolution,
        )
        if image is None:
            self._error = "window image unavailable (minimized, closed, or on another Space)"
            return None
        width = int(Quartz.CGImageGetWidth(image))
        height = int(Quartz.CGImageGetHeight(image))
        bytes_per_row = int(Quartz.CGImageGetBytesPerRow(image))
        data = Quartz.CGDataProviderCopyData(Quartz.CGImageGetDataProvider(image))
        if data is None or width <= 0 or height <= 0:
            self._error = "window image had no pixel data"
            return None
        raw = np.frombuffer(data, dtype=np.uint8)
        needed = bytes_per_row * height
        if raw.size < needed:
            self._error = f"short image buffer: {raw.size} < {needed}"
            return None
        bgra = raw[:needed].reshape(height, bytes_per_row // 4, 4)[:, :width, :3]
        normalize_started = monotonic_s()
        canonical = normalize_into_canonical(bgra, geometry, self._pool)
        if canonical is None:
            self._error = "frame buffer pool exhausted"
            return None
        normalize_ms = (monotonic_s() - normalize_started) * 1000.0
        self._counter += 1
        self._error = None
        return RawFrame(
            bgr=canonical,
            geometry=geometry,
            captured_at_s=started,
            presented_at_s=monotonic_s(),
            content_id=None,
            backend=self.name,
            normalize_ms=normalize_ms,
        )


class MacScreenCaptureKitSource:
    """Asynchronous, window-specific capture through ScreenCaptureKit.

    The preferred macOS backend. The OS pushes frames on its own queue, crops
    the client area and scales to the canonical raster **on the GPU** through
    ``sourceRect``/``destinationRect``, and keeps delivering while another
    application is frontmost - which is exactly the case that matters, because
    the dashboard itself takes focus.

    Measured on the development Mac: 110 unique fps at 1280x720 against a
    120 Hz request, versus ~75 Hz for the Quartz fallback.

    Two pyobjc details this depends on, both easy to get wrong:

    * ScreenCaptureKit keeps only a **weak** reference to a stream output, so
      the delegate must be retained here or frames silently stop arriving.
    * Completion handlers are typed ``void``; returning a value from one raises
      inside the callback and takes the process down.
    """

    #: Bounded wait for the async shareable-content query.
    CONTENT_TIMEOUT_S = 5.0
    START_TIMEOUT_S = 6.0

    def __init__(self) -> None:
        self._stream: Any = None
        self._output: Any = None
        self._geometry: ViewportGeometry | None = None
        self._pool: Any = None
        self._on_frame: Callable[[RawFrame], None] | None = None
        self._error: str | None = None
        self._target_fps = 60
        self._idle_frames = 0
        self._content_counter = 0
        self._lock = threading.Lock()
        self._reconfiguring = False

    @property
    def name(self) -> str:
        return "screencapturekit"

    @property
    def idle_frames(self) -> int:
        """Surfaces ScreenCaptureKit redelivered unchanged, and we skipped."""
        with self._lock:
            return self._idle_frames

    @property
    def is_pushing(self) -> bool:
        return True

    def poll(self) -> RawFrame | None:
        return None  # push source

    def health(self) -> str | None:
        with self._lock:
            return self._error

    @property
    def reconfiguring(self) -> bool:
        """A frame-interval change has been requested and not yet acknowledged.

        The governor waits for this to clear before judging the new tier:
        ScreenCaptureKit applies a configuration asynchronously, and frames
        delivered in between still belong to the old interval.
        """
        with self._lock:
            return self._reconfiguring

    def set_target_fps(self, fps: int) -> None:
        self._target_fps = max(1, fps)
        stream = self._stream
        if stream is None:
            return
        import CoreMedia

        configuration = self._configuration()
        if configuration is None:
            return
        configuration.setMinimumFrameInterval_(CoreMedia.CMTimeMake(1, self._target_fps))

        def _updated(error: Any) -> None:
            with self._lock:
                self._reconfiguring = False
                if error is not None:
                    self._error = f"reconfigure failed: {error}"

        with self._lock:
            self._reconfiguring = True
        try:
            stream.updateConfiguration_completionHandler_(configuration, _updated)
        except Exception as exc:
            with self._lock:
                self._reconfiguring = False
                self._error = f"reconfigure failed: {exc!r}"

    # -- construction -----------------------------------------------------
    def _find_window(self, window_id: int) -> Any:
        import ScreenCaptureKit as SCK

        done = threading.Event()
        found: dict[str, Any] = {}

        def _received(content: Any, error: Any) -> None:
            found["content"] = content
            found["error"] = error
            done.set()

        SCK.SCShareableContent.getShareableContentWithCompletionHandler_(_received)
        if not done.wait(self.CONTENT_TIMEOUT_S):
            raise TimeoutError("ScreenCaptureKit shareable content query timed out")
        if found.get("error") is not None:
            raise RuntimeError(f"shareable content error: {found['error']}")
        content = found.get("content")
        if content is None:
            raise RuntimeError("ScreenCaptureKit returned no shareable content")
        for window in content.windows():
            if int(window.windowID()) == int(window_id):
                return window
        raise LookupError(f"window {window_id} is not shareable (closed or minimized?)")

    def _configuration(self) -> Any:
        import CoreMedia
        import ScreenCaptureKit as SCK

        geometry = self._geometry
        if geometry is None or geometry.client_logical is None:
            return None
        width, height = geometry.canonical_px
        source = geometry.client_rect_in_window_logical
        inner_x, inner_y, inner_w, inner_h = geometry.canonical_letterbox_px()

        configuration = SCK.SCStreamConfiguration.alloc().init()
        configuration.setWidth_(width)
        configuration.setHeight_(height)
        configuration.setPixelFormat_(0x42475241)  # 'BGRA'
        configuration.setMinimumFrameInterval_(CoreMedia.CMTimeMake(1, self._target_fps))
        configuration.setQueueDepth_(3)
        configuration.setShowsCursor_(False)
        # Crop to the client and place it letterboxed inside the canonical
        # raster, both on the GPU. The same rectangle is what
        # ViewportGeometry inverts, so overlay coordinates stay exact.
        configuration.setSourceRect_(
            Quartz.CGRectMake(source.x, source.y, source.width, source.height)
        )
        with contextlib.suppress(Exception):
            configuration.setScalesToFit_(False)
            configuration.setDestinationRect_(
                Quartz.CGRectMake(inner_x, inner_y, inner_w, inner_h)
            )
        with contextlib.suppress(Exception):
            configuration.setIgnoreShadowsSingleWindow_(True)
        return configuration

    def start(
        self, geometry: ViewportGeometry, pool: Any, on_frame: Callable[[RawFrame], None]
    ) -> None:
        import ScreenCaptureKit as SCK

        if geometry.window is None or geometry.client_logical is None:
            raise ValueError("ScreenCaptureKit needs a resolved window geometry")
        self._geometry = geometry
        self._pool = pool
        self._on_frame = on_frame

        window = self._find_window(geometry.window.window_id)
        configuration = self._configuration()
        if configuration is None:
            raise ValueError("could not build a stream configuration")
        content_filter = SCK.SCContentFilter.alloc().initWithDesktopIndependentWindow_(window)
        stream = SCK.SCStream.alloc().initWithFilter_configuration_delegate_(
            content_filter, configuration, None
        )
        output = _make_stream_output(self._deliver)
        ok, error = stream.addStreamOutput_type_sampleHandlerQueue_error_(
            output, SCK.SCStreamOutputTypeScreen, None, None
        )
        if not ok:
            raise RuntimeError(f"addStreamOutput failed: {error}")

        started = threading.Event()
        outcome: dict[str, Any] = {}

        def _started(error: Any) -> None:
            outcome["error"] = error
            started.set()

        stream.startCaptureWithCompletionHandler_(_started)
        if not started.wait(self.START_TIMEOUT_S):
            raise TimeoutError("ScreenCaptureKit start timed out")
        if outcome.get("error") is not None:
            raise RuntimeError(f"ScreenCaptureKit start failed: {outcome['error']}")

        # Retained deliberately: SCK holds the output weakly.
        self._stream = stream
        self._output = output
        with self._lock:
            self._error = None

    @staticmethod
    def _frame_status(sample_buffer: Any) -> tuple[int, int | None]:
        """``(status, display_time)`` from ScreenCaptureKit's own attachments.

        This is the authoritative uniqueness signal: ``SCFrameStatusComplete``
        means new content was composited, ``SCFrameStatusIdle`` means the same
        surface was redelivered. Using it means the pipeline never inflates its
        frame rate by counting a redelivered surface, and never pays for a copy
        it will discard (mission section 6).
        """
        import CoreMedia
        import ScreenCaptureKit as SCK

        try:
            attachments = CoreMedia.CMSampleBufferGetSampleAttachmentsArray(
                sample_buffer, False
            )
            if not attachments or len(attachments) == 0:
                return (int(SCK.SCFrameStatusComplete), None)
            info = attachments[0]
            status = info.get(SCK.SCStreamFrameInfoStatus)
            display_time = info.get(SCK.SCStreamFrameInfoDisplayTime)
            return (
                int(SCK.SCFrameStatusComplete) if status is None else int(status),
                None if display_time is None else int(display_time),
            )
        except Exception:
            return (int(SCK.SCFrameStatusComplete), None)

    def _deliver(self, sample_buffer: Any) -> None:
        """Called on ScreenCaptureKit's queue. Copies once and returns."""
        import CoreMedia
        import ScreenCaptureKit as SCK

        on_frame = self._on_frame
        geometry = self._geometry
        if on_frame is None or geometry is None:
            return
        status, display_time = self._frame_status(sample_buffer)
        if status != int(SCK.SCFrameStatusComplete):
            with self._lock:
                self._idle_frames += 1
            return  # redelivered surface: skip the copy entirely
        started = monotonic_s()
        try:
            pixel_buffer = CoreMedia.CMSampleBufferGetImageBuffer(sample_buffer)
            if pixel_buffer is None:
                return
            Quartz.CVPixelBufferLockBaseAddress(pixel_buffer, 1)
            try:
                width = int(Quartz.CVPixelBufferGetWidth(pixel_buffer))
                height = int(Quartz.CVPixelBufferGetHeight(pixel_buffer))
                stride = int(Quartz.CVPixelBufferGetBytesPerRow(pixel_buffer))
                address = Quartz.CVPixelBufferGetBaseAddress(pixel_buffer)
                if address is None or width <= 0 or height <= 0:
                    return
                source = np.frombuffer(
                    address.as_buffer(stride * height), dtype=np.uint8
                ).reshape(height, stride // 4, 4)[:, :width, :3]
                target = self._pool.acquire(height, width) if self._pool is not None else None
                if target is None:
                    with self._lock:
                        self._error = "frame buffer pool exhausted"
                    return
                np.copyto(target, source)
            finally:
                Quartz.CVPixelBufferUnlockBaseAddress(pixel_buffer, 1)
        except Exception as exc:
            with self._lock:
                self._error = f"sample delivery failed: {exc!r}"
            return
        with self._lock:
            self._content_counter += 1
            content_id = display_time if display_time is not None else self._content_counter
        on_frame(
            RawFrame(
                bgr=target,
                geometry=geometry,
                captured_at_s=started,
                presented_at_s=monotonic_s(),
                content_id=content_id,
                backend=self.name,
            )
        )

    def stop(self) -> None:
        stream = self._stream
        self._stream = None
        self._output = None
        self._on_frame = None
        if stream is None:
            return
        stopped = threading.Event()

        def _stopped(error: Any) -> None:
            stopped.set()

        with contextlib.suppress(Exception):
            stream.stopCaptureWithCompletionHandler_(_stopped)
            stopped.wait(2.0)


_STREAM_OUTPUT_CLASS: Any = None
_STREAM_OUTPUT_LOCK = threading.Lock()


def _stream_output_class() -> Any:
    """The ``SCStreamOutput`` delegate class, defined exactly once.

    Objective-C class names are process-global, so defining this per stream
    raises "overriding existing Objective-C class" the second time a capture
    session starts - which is every tier change and every reacquisition. The
    class is therefore cached and the per-stream callback lives on the
    instance.
    """
    global _STREAM_OUTPUT_CLASS
    with _STREAM_OUTPUT_LOCK:
        if _STREAM_OUTPUT_CLASS is not None:
            return _STREAM_OUTPUT_CLASS

        import objc
        import ScreenCaptureKit as SCK

        protocols = []
        with contextlib.suppress(Exception):
            protocols = [objc.protocolNamed("SCStreamOutput")]
        base: Any = objc.lookUpClass("NSObject")

        class _TreasureStreamOutput(base, protocols=protocols):  # type: ignore[call-arg,misc]
            def initWithDeliver_(self, deliver: Any) -> Any:
                instance = objc.super(_TreasureStreamOutput, self).init()
                if instance is None:
                    return None
                instance._deliver_callback = deliver
                return instance

            def stream_didOutputSampleBuffer_ofType_(
                self, stream: Any, sample_buffer: Any, output_type: Any
            ) -> None:
                if output_type != SCK.SCStreamOutputTypeScreen:
                    return
                callback = getattr(self, "_deliver_callback", None)
                if callback is not None:
                    callback(sample_buffer)

        _STREAM_OUTPUT_CLASS = _TreasureStreamOutput
        return _STREAM_OUTPUT_CLASS


def _make_stream_output(deliver: Callable[[Any], None]) -> Any:
    instance: Any = _stream_output_class().alloc().initWithDeliver_(deliver)
    return instance


# ===========================================================================
# Platform port
# ===========================================================================


class MacPlatformPort:
    """Full macOS port. Only ``InputAuthority`` and capture may hold one."""

    def __init__(self, *, window_owner_substring: str = "roblox") -> None:
        self._vocabulary = InputVocabulary()
        self._owner_substring = window_owner_substring
        self._lock = threading.Lock()
        self._geometry = ViewportGeometry.unpinned()
        self._title_bar_pt: float = TITLE_BAR_FALLBACK_PT
        self._title_bar_measured = False

    # -- identity ---------------------------------------------------------
    @property
    def name(self) -> str:
        return "macos"

    @property
    def vocabulary(self) -> InputVocabulary:
        return self._vocabulary

    def key_code(self, key: InputKey) -> int:
        return _MAC_KEYCODES[key]

    @property
    def title_bar_pt(self) -> float:
        return self._title_bar_pt

    @property
    def title_bar_measured(self) -> bool:
        """True when the inset came from AX geometry rather than the fallback."""
        return self._title_bar_measured

    # -- accessibility ----------------------------------------------------
    @staticmethod
    def _app_services() -> Any:
        import ApplicationServices

        return ApplicationServices

    def accessibility_trusted(self) -> bool:
        try:
            return bool(self._app_services().AXIsProcessTrusted())
        except Exception:
            return False

    # -- displays ---------------------------------------------------------
    @staticmethod
    def _scale_for_display(display_id: int) -> float:
        """Backing pixels per logical point for one display."""
        mode = Quartz.CGDisplayCopyDisplayMode(display_id)
        if mode is None:
            return 1.0
        points_wide = float(Quartz.CGDisplayModeGetWidth(mode))
        pixels_wide = float(Quartz.CGDisplayModeGetPixelWidth(mode))
        return pixels_wide / points_wide if points_wide else 1.0

    def _display_for_rect(self, rect: LogicalRect) -> DisplayInfo:
        """The display a window sits on, found by its centre.

        Using the centre rather than the origin means a window straddling two
        displays resolves to the one showing most of it, and a window that
        migrates is noticed because the display id is part of the viewport
        identity.
        """
        centre_x = rect.x + rect.width / 2.0
        centre_y = rect.y + rect.height / 2.0
        display_id = int(Quartz.CGMainDisplayID())
        with contextlib.suppress(Exception):
            error, displays, count = Quartz.CGGetDisplaysWithPoint(
                Quartz.CGPoint(centre_x, centre_y), 1, None, None
            )
            if not error and count:
                display_id = int(displays[0])
        bounds = Quartz.CGDisplayBounds(display_id)
        return DisplayInfo(
            display_id=str(display_id),
            bounds_logical=LogicalRect(
                float(bounds.origin.x),
                float(bounds.origin.y),
                float(bounds.size.width),
                float(bounds.size.height),
            ),
            backing_scale=self._scale_for_display(display_id),
        )

    # -- window lookup ----------------------------------------------------
    def _scan_roblox(self) -> tuple[WindowIdentity, LogicalRect] | None:
        """Largest on-screen Roblox (not Studio) window frame, in POINTS."""
        try:
            options = (
                Quartz.kCGWindowListOptionOnScreenOnly
                | Quartz.kCGWindowListExcludeDesktopElements
            )
            windows = Quartz.CGWindowListCopyWindowInfo(options, Quartz.kCGNullWindowID)
        except Exception:
            return None
        best: tuple[WindowIdentity, LogicalRect] | None = None
        best_area = 0.0
        for window in windows or []:
            owner = str(window.get("kCGWindowOwnerName", ""))
            lowered = owner.lower()
            if self._owner_substring not in lowered or "studio" in lowered:
                continue
            if int(window.get("kCGWindowLayer", 0) or 0) != 0:
                continue  # menu bars, panels, and shadows are not the game
            bounds = window.get("kCGWindowBounds") or {}
            width = float(bounds.get("Width", 0.0))
            height = float(bounds.get("Height", 0.0))
            if width < 320 or height < 240 or width * height <= best_area:
                continue
            best_area = width * height
            best = (
                WindowIdentity(
                    window_id=int(window.get("kCGWindowNumber") or 0),
                    process_id=int(window.get("kCGWindowOwnerPID") or 0),
                    owner=owner,
                    title=str(window.get("kCGWindowName") or ""),
                ),
                LogicalRect(
                    float(bounds.get("X", 0.0)),
                    float(bounds.get("Y", 0.0)),
                    width,
                    height,
                ),
            )
        return best

    def roblox_on_another_space(self) -> bool:
        """A Roblox window exists but is off-screen - the fullscreen signature."""
        with contextlib.suppress(Exception):
            windows = Quartz.CGWindowListCopyWindowInfo(
                Quartz.kCGWindowListOptionAll, Quartz.kCGNullWindowID
            )
            for window in windows or []:
                owner = str(window.get("kCGWindowOwnerName", "")).lower()
                if self._owner_substring in owner and "studio" not in owner:
                    return True
        return False

    def _ax_window(self, pid: int) -> Any | None:
        services = self._app_services()
        app = services.AXUIElementCreateApplication(int(pid))
        error, windows = services.AXUIElementCopyAttributeValue(
            app, services.kAXWindowsAttribute, None
        )
        if error != services.kAXErrorSuccess or not windows:
            return None
        return windows[0]

    def _measure_title_bar_pt(self, window: Any) -> float | None:
        """Derive the title-bar height from the window's own traffic lights.

        ``2 * (close_button_y - frame_y) + close_button_height`` because the
        buttons are vertically centred in the bar. Returns ``None`` when
        Accessibility does not expose them, and the caller then uses the
        documented fallback and says which it used.
        """
        services = self._app_services()
        try:
            error, frame_value = services.AXUIElementCopyAttributeValue(
                window, services.kAXPositionAttribute, None
            )
            if error != services.kAXErrorSuccess:
                return None
            ok, frame_point = services.AXValueGetValue(
                frame_value, services.kAXValueCGPointType, None
            )
            if not ok:
                return None
            error, button = services.AXUIElementCopyAttributeValue(
                window, "AXCloseButton", None
            )
            if error != services.kAXErrorSuccess or button is None:
                return None
            error, position_value = services.AXUIElementCopyAttributeValue(
                button, "AXPosition", None
            )
            if error != services.kAXErrorSuccess:
                return None
            error, size_value = services.AXUIElementCopyAttributeValue(button, "AXSize", None)
            if error != services.kAXErrorSuccess:
                return None
            ok_position, button_point = services.AXValueGetValue(
                position_value, services.kAXValueCGPointType, None
            )
            ok_size, button_size = services.AXValueGetValue(
                size_value, services.kAXValueCGSizeType, None
            )
            if not (ok_position and ok_size):
                return None
            inset = float(button_point.y) - float(frame_point.y)
            measured = 2.0 * inset + float(button_size.height)
            return measured if 12.0 <= measured <= 80.0 else None
        except Exception:
            return None

    # -- PlatformPort: viewport ------------------------------------------
    def focus_state(self) -> FocusState:
        """True/False/None per plan 4.3; ``None`` means genuinely unknown."""
        try:
            from AppKit import NSWorkspace  # bundled with pyobjc-framework-Cocoa
        except Exception:
            return None
        try:
            frontmost = NSWorkspace.sharedWorkspace().frontmostApplication()
        except Exception:
            return None
        if frontmost is None:
            return None
        name = str(frontmost.localizedName() or "").lower()
        if "studio" in name:
            return False
        return self._owner_substring in name

    def window_geometry(self) -> ViewportGeometry:
        found = self._scan_roblox()
        if found is None:
            detail = (
                "Roblox is running but not on this Space - exit native fullscreen"
                if self.roblox_on_another_space()
                else "Roblox window not found"
            )
            geometry = ViewportGeometry.invalid(detail)
            with self._lock:
                self._geometry = geometry
            return geometry

        identity, frame_logical = found
        title_bar_pt = TITLE_BAR_FALLBACK_PT
        measured = False
        if identity.process_id and self.accessibility_trusted():
            window = self._ax_window(identity.process_id)
            if window is not None:
                candidate = self._measure_title_bar_pt(window)
                if candidate is not None:
                    title_bar_pt, measured = candidate, True

        client_logical = frame_logical.inset(top=title_bar_pt)
        display = self._display_for_rect(frame_logical)

        if client_logical.width <= 0 or client_logical.height <= 0:
            geometry = ViewportGeometry.invalid(
                f"non-positive client size {client_logical.size}"
            )
        else:
            canonical_w, canonical_h = CANONICAL_SIZE_PX
            is_canonical = (
                abs(client_logical.width - canonical_w) <= 1.0
                and abs(client_logical.height - canonical_h) <= 1.0
            )
            geometry = ViewportGeometry(
                state=(
                    ViewportState.CANONICAL_VERIFIED
                    if is_canonical
                    else ViewportState.ADOPTED_NONCANONICAL
                ),
                window=identity,
                display=display,
                frame_logical=frame_logical,
                client_logical=client_logical,
                canonical_px=CANONICAL_SIZE_PX,
                verified_at_s=monotonic_s(),
                detail=(
                    "client matches the canonical size"
                    if is_canonical
                    else f"client is {client_logical.width:g}x{client_logical.height:g} pt, "
                    f"not the canonical {canonical_w}x{canonical_h}"
                ),
            )
        with self._lock:
            self._title_bar_pt = title_bar_pt
            self._title_bar_measured = measured
            self._geometry = geometry
        return geometry

    def pin_client_rect(self, size_logical: tuple[float, float]) -> PinResult:
        """Resize Roblox so its **client content** is ``size_logical`` POINTS.

        Accessibility sets the *frame*, so the title bar is added back before
        asking. macOS may legally refuse the position or clamp the size, so the
        achieved geometry is read back and reported rather than retried.
        """
        services = self._app_services()
        if not self.accessibility_trusted():
            return PinResult(
                False,
                "Accessibility permission not granted. System Settings > Privacy & "
                "Security > Accessibility - enable it for this app or terminal, then retry.",
                self.window_geometry(),
                size_logical,
            )
        found = self._scan_roblox()
        if found is None:
            return PinResult(
                False,
                "Roblox window not found. Open Roblox, not minimized, and not in "
                "native fullscreen.",
                self.window_geometry(),
                size_logical,
            )
        identity, _frame = found
        window = self._ax_window(identity.process_id)
        if window is None:
            return PinResult(
                False,
                "Could not reach Roblox's window through Accessibility.",
                self.window_geometry(),
                size_logical,
            )

        error, is_fullscreen = services.AXUIElementCopyAttributeValue(
            window, "AXFullScreen", None
        )
        if error == services.kAXErrorSuccess and bool(is_fullscreen):
            return PinResult(
                False,
                "Roblox is in native fullscreen - exit fullscreen and retry.",
                self.window_geometry(),
                size_logical,
            )

        measured = self._measure_title_bar_pt(window)
        title_bar_pt = measured if measured is not None else TITLE_BAR_FALLBACK_PT
        want_width = float(size_logical[0])
        want_height = float(size_logical[1]) + title_bar_pt

        position = services.AXValueCreate(
            services.kAXValueCGPointType, Quartz.CGPoint(0.0, 0.0)
        )
        size = services.AXValueCreate(
            services.kAXValueCGSizeType, Quartz.CGSize(want_width, want_height)
        )
        error_position = services.AXUIElementSetAttributeValue(
            window, services.kAXPositionAttribute, position
        )
        error_size = services.AXUIElementSetAttributeValue(
            window, services.kAXSizeAttribute, size
        )
        if error_position != services.kAXErrorSuccess or error_size != services.kAXErrorSuccess:
            return PinResult(
                False,
                f"Failed to move or resize Roblox (AX errors {error_position}/{error_size}).",
                self.window_geometry(),
                size_logical,
            )

        geometry = self.window_geometry()
        if not geometry.valid or geometry.client_logical is None:
            return PinResult(
                False,
                "Pinned, but the client rect could not be read back.",
                geometry,
                size_logical,
            )
        client = geometry.client_logical
        delta_w = abs(client.width - size_logical[0])
        delta_h = abs(client.height - size_logical[1])
        if delta_w > 1.0 or delta_h > 1.0:
            # Reported once, then accepted as a truthful non-canonical state.
            # Roblox enforces a minimum window size, so a request below it is
            # clamped and retrying would loop forever (DECISIONS.md D-017).
            return PinResult(
                False,
                f"Requested a {size_logical[0]:g}x{size_logical[1]:g} pt client but the OS "
                f"gave {client.width:g}x{client.height:g} pt "
                f"(off by {delta_w:g}x{delta_h:g}). Running non-canonical: observation and "
                f"recording work, calibrated pixel constants do not.",
                geometry.with_state(
                    ViewportState.ADOPTED_NONCANONICAL, "clamped by the OS or the application"
                ),
                size_logical,
            )
        source = "measured" if self._title_bar_measured else "provisional fallback"
        return PinResult(
            True,
            f"Client pinned to {client.width:g}x{client.height:g} pt at "
            f"({client.x:g},{client.y:g}); backing {geometry.client_backing_px[0]}x"
            f"{geometry.client_backing_px[1]} px at {geometry.backing_scale:g}x, "
            f"title bar {title_bar_pt:g} pt ({source}).",
            geometry,
            size_logical,
        )

    def create_capture_source(self) -> Any:
        """ScreenCaptureKit when available, otherwise the Quartz fallback."""
        if screencapturekit_available():
            return MacScreenCaptureKitSource()
        return MacQuartzWindowSource()

    # -- PlatformPort: raw edges -----------------------------------------
    def raw_key_down(self, code: int) -> None:
        _post(Quartz.CGEventCreateKeyboardEvent(None, code, True))

    def raw_key_up(self, code: int) -> None:
        _post(Quartz.CGEventCreateKeyboardEvent(None, code, False))

    def raw_button_down(self, button: MouseButton) -> None:
        down, _up, _drag, btn = _MAC_BUTTON_EVENTS[button]
        _post(Quartz.CGEventCreateMouseEvent(None, down, _cursor_point_pt(), btn))

    def raw_button_up(self, button: MouseButton) -> None:
        _down, up, _drag, btn = _MAC_BUTTON_EVENTS[button]
        _post(Quartz.CGEventCreateMouseEvent(None, up, _cursor_point_pt(), btn))

    def raw_pointer_move_client(self, point_px: tuple[int, int]) -> None:
        """Move to a point in **canonical** coordinates.

        The canonical -> display-logical transform lives on the geometry, so
        this is one composed mapping rather than an ad-hoc division by the
        display scale.
        """
        with self._lock:
            geometry = self._geometry
        if not geometry.valid:
            geometry = self.window_geometry()
        if not geometry.valid:
            return
        x_pt, y_pt = geometry.display_logical_from_canonical.apply(
            float(point_px[0]), float(point_px[1])
        )
        with contextlib.suppress(Exception):
            Quartz.CGWarpMouseCursorPosition((x_pt, y_pt))
        _post(
            Quartz.CGEventCreateMouseEvent(
                None, Quartz.kCGEventMouseMoved, (x_pt, y_pt), Quartz.kCGMouseButtonLeft
            )
        )

    def raw_pointer_delta(
        self, dx: int, dy: int, held_button: MouseButton | None = None
    ) -> None:
        """Relative HID motion - the only thing that drives Roblox's camera.

        Roblox reads ``kCGMouseEventDeltaX/Y``; a plain absolute move does
        nothing even while a button is genuinely held. When the authority's
        ledger holds a button, the delta rides that button's *dragged* event.
        """
        if held_button is not None:
            _down, _up, dragged, btn = _MAC_BUTTON_EVENTS[held_button]
            event = Quartz.CGEventCreateMouseEvent(None, dragged, _cursor_point_pt(), btn)
        else:
            event = Quartz.CGEventCreateMouseEvent(
                None, Quartz.kCGEventMouseMoved, _cursor_point_pt(), Quartz.kCGMouseButtonLeft
            )
        Quartz.CGEventSetIntegerValueField(event, Quartz.kCGMouseEventDeltaX, int(dx))
        Quartz.CGEventSetIntegerValueField(event, Quartz.kCGMouseEventDeltaY, int(dy))
        _post(event)

    def raw_scroll_lines(self, lines: int) -> None:
        _post(
            Quartz.CGEventCreateScrollWheelEvent(
                None, Quartz.kCGScrollEventUnitLine, 1, int(lines)
            )
        )

    def cursor_client_px(self) -> tuple[int, int] | None:
        """Cursor position in **canonical** coordinates, or ``None``."""
        geometry = self.window_geometry()
        if not geometry.valid:
            return None
        point = _cursor_point_pt()
        canonical = geometry.display_logical_from_canonical.inverse().apply(
            float(point.x), float(point.y)
        )
        x, y = round(canonical[0]), round(canonical[1])
        width, height = geometry.canonical_px
        if not (0 <= x < width and 0 <= y < height):
            return None
        return (x, y)

    # -- PlatformPort: hotkeys -------------------------------------------
    def create_hotkey_source(self, submit: Callable[[RuntimeIntent], None]) -> MacHotkeySource:
        return MacHotkeySource(submit, focus_probe=self.focus_state)


class MacHotkeySource:
    """Global F1-F5 listener that submits intents and nothing else.

    Input-emitting intents are submitted only while Roblox is positively
    focused (plan 11.2). ``STOP`` is always accepted - a stop that needed
    focus would be useless exactly when it matters.
    """

    #: Intents that never require focus.
    ALWAYS_ALLOWED = frozenset({IntentType.STOP, IntentType.SHUTDOWN, IntentType.PIXEL_INFO})

    def __init__(
        self,
        submit: Callable[[RuntimeIntent], None],
        *,
        focus_probe: Callable[[], FocusState],
    ) -> None:
        self._submit = submit
        self._focus_probe = focus_probe
        self._listener: Any | None = None
        self._sequence = 0
        self._lock = threading.Lock()

    def start(self) -> None:
        try:
            from pynput import keyboard
        except ImportError as exc:  # pragma: no cover - install-time failure
            raise ImportError(f"Global hotkeys need pynput ({exc}).") from exc

        vk_to_intent = {
            _MAC_HOTKEY_VK[name]: intent for name, intent in MAC_HOTKEY_BINDINGS.items()
        }

        def on_press(key: Any) -> None:
            vk = getattr(key, "vk", None)
            if vk is None:
                vk = getattr(getattr(key, "value", None), "vk", None)
            if not isinstance(vk, int):
                return
            intent_type = vk_to_intent.get(vk)
            if intent_type is None:
                return
            self.fire(intent_type)

        listener = keyboard.Listener(on_press=on_press)
        listener.daemon = True
        listener.name = "treasure-hotkeys"
        listener.start()
        self._listener = listener

    def fire(self, intent_type: IntentType) -> bool:
        """Submit one hotkey intent if policy allows. Exposed for tests."""
        if intent_type not in self.ALWAYS_ALLOWED and self._focus_probe() is not True:
            return False
        with self._lock:
            self._sequence += 1
            sequence = self._sequence
        self._submit(
            RuntimeIntent(
                sequence=sequence,
                intent_type=intent_type,
                source="hotkey",
                created_at_s=monotonic_s(),
            )
        )
        return True

    def stop(self) -> None:
        listener = self._listener
        self._listener = None
        if listener is not None:
            with contextlib.suppress(Exception):
                listener.stop()

    def is_running(self) -> bool:
        listener = self._listener
        return bool(listener is not None and listener.is_alive())
