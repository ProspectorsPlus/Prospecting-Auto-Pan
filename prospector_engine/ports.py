"""Platform-port protocols and the one factory that selects an implementation.

This module must stay import-clean on **both** operating systems: it imports
no Quartz, no ctypes/wintypes, no Tk. That is what lets the Windows contract
tests run on macOS and vice versa (plan 13.1, bug B1).

``PlatformPort`` is private to :mod:`prospector_engine.input_authority`,
capture's viewport management, and the deadman's release-only backend. Feature
code never receives one - it gets a narrow capability session instead
(plan 4.2).
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable

from prospector_engine.bindings import ChordEvent, HotkeyHealth
from prospector_engine.contracts import (
    FocusState,
    InputKey,
    InputVocabulary,
    IntentType,
    MouseButton,
    PinResult,
    RawFrame,
    RuntimeIntent,
)
from prospector_engine.geometry import ViewportGeometry

__all__ = [
    "BufferPool",
    "CaptureSource",
    "HotkeySource",
    "PlatformPort",
    "PlatformUnavailable",
    "ReleaseOnlyPort",
    "create_platform_port",
    "create_release_only_port",
    "current_platform_name",
]


class PlatformUnavailable(RuntimeError):
    """Raised when the running OS has no supported port implementation."""


@runtime_checkable
class HotkeySource(Protocol):
    """A named daemon listener that submits intents and nothing else.

    It must never call input, mutate engine state, or run feature logic on its
    own thread (plan 3.1, bug B4).
    """

    def start(self) -> None: ...

    def stop(self) -> None: ...

    def is_running(self) -> bool:
        """Whether a chord pressed *now* would be heard.

        Not "a thread object exists". A listener whose event source the OS has
        disabled is alive and deaf, and reporting that as running is what let
        the dashboard claim hotkeys were ready while every chord vanished.
        """
        ...

    def health(self) -> HotkeyHealth:
        """The full reading: state, backend, counts, last edge, last error."""
        ...

    def clear_held_keys(self, reason: str = "focus-change") -> None:
        """Quarantine everything held, on a focus transition.

        A modifier the user is holding in another application must be released
        and pressed again before it can complete a chord here.
        """
        ...


@runtime_checkable
class BufferPool(Protocol):
    """Bounded supply of reusable canonical-raster buffers.

    Capture backends draw from this instead of allocating per frame, so memory
    stays flat as the frame rate rises. A buffer returns to the pool when the
    frame holding it is released.
    """

    def acquire(self, height: int, width: int) -> Any: ...

    def release(self, buffer: Any) -> None: ...


@runtime_checkable
class CaptureSource(Protocol):
    """A window-specific frame source.

    Two shapes, both supported, because the good backends are push-based and
    the portable ones are not:

    * **push** - the OS delivers frames on its own thread (ScreenCaptureKit,
      Windows Graphics Capture). ``is_pushing`` is True and ``on_frame`` is
      called; ``poll`` is never used.
    * **pull** - the caller asks for a frame (Quartz window images, mss).
      ``is_pushing`` is False and the service paces ``poll`` itself.

    Either way the source delivers frames already normalized into the canonical
    raster, because every backend can crop and scale far more cheaply than a
    per-frame CPU resize can.
    """

    @property
    def name(self) -> str: ...

    @property
    def is_pushing(self) -> bool: ...

    def start(
        self,
        geometry: ViewportGeometry,
        pool: BufferPool,
        on_frame: Callable[[RawFrame], None],
    ) -> None: ...

    def stop(self) -> None: ...

    def set_target_fps(self, fps: int) -> None:
        """Hint the source's delivery cadence. Pull sources may ignore it."""
        ...

    def poll(self) -> RawFrame | None:
        """Pull sources only. Returns ``None`` when no frame is available."""
        ...

    def health(self) -> str | None:
        """A problem description, or ``None`` when the source is healthy."""
        ...


@runtime_checkable
class ReleaseOnlyPort(Protocol):
    """The strict subset the out-of-process deadman is allowed to hold.

    There is deliberately no ``down`` method anywhere in this protocol: the
    helper can lift an input but has no code path that presses one
    (plan 4.5).
    """

    @property
    def vocabulary(self) -> InputVocabulary: ...

    def key_code(self, key: InputKey) -> int: ...

    def raw_key_up(self, code: int) -> None: ...

    def raw_button_up(self, button: MouseButton) -> None: ...


@runtime_checkable
class PlatformPort(Protocol):
    """Every OS-specific operation the application is allowed to perform."""

    @property
    def name(self) -> str: ...

    @property
    def vocabulary(self) -> InputVocabulary: ...

    def key_code(self, key: InputKey) -> int:
        """Native code for a vocabulary key (macOS virtual key, Windows scancode)."""
        ...

    # -- window / viewport ------------------------------------------------
    def focus_state(self) -> FocusState: ...

    def window_geometry(self) -> ViewportGeometry:
        """The current, fully-described viewport.

        Always returns a value: an absent or unusable window is reported as a
        geometry in the ``INVALID`` state with a reason, never as ``None``, so
        every consumer reads the same authoritative result (plan 4.1).
        """
        ...

    def pin_client_rect(self, size_logical: tuple[float, float]) -> PinResult:
        """Resize so the **client content** is ``size_logical`` LOGICAL units.

        Logical, not device pixels: the window-management APIs on both
        platforms speak logical units, and dividing a device-pixel target by
        the display scale is what produced a 640x360-point request that Roblox
        clamped (DECISIONS.md D-017).
        """
        ...

    def create_capture_source(self) -> CaptureSource:
        """Build the best window-specific capture source for this OS."""
        ...

    # -- raw edges (InputAuthority only) ----------------------------------
    def raw_key_down(self, code: int) -> None: ...

    def raw_key_up(self, code: int) -> None: ...

    def raw_button_down(self, button: MouseButton) -> None: ...

    def raw_button_up(self, button: MouseButton) -> None: ...

    def raw_pointer_move_client(self, point_px: tuple[int, int]) -> None:
        """Move to a *client-relative* physical-pixel point.

        The conversion to desktop coordinates happens inside the port, using
        the currently verified client origin, so feature code never handles a
        screen-absolute coordinate (plan 4.3).
        """
        ...

    def raw_pointer_delta(
        self, dx: int, dy: int, held_button: MouseButton | None = None
    ) -> None:
        """Emit a relative HID motion delta - the only thing that drives yaw.

        Deviation from the plan's sketch signature, recorded in DECISIONS.md
        (D-001): the ledger lives in ``InputAuthority`` (bug B8), so the port
        cannot know whether a button is held. The authority passes that
        context, which is what lets macOS ride the delta on the correct
        ``*MouseDragged`` event as plan 12's yaw note requires.
        """
        ...

    def raw_scroll_lines(self, lines: int) -> None: ...

    # -- which mechanism edges go out through -----------------------------
    @property
    def event_backend(self) -> str:
        """The named mechanism outgoing edges currently use.

        Every OS has at least one; macOS has three genuinely different ones
        (see ``platform_mac.EVENT_BACKENDS``) and which of them Roblox acts on
        is an empirical question, so the name is part of the evidence every
        movement claim carries.
        """
        ...

    def set_event_backend(self, name: str) -> bool:
        """Select a mechanism by name. ``False`` if this port has no such one.

        A port that cannot honour a name must say so rather than silently
        continue on the previous one: an A/B ladder that cannot tell "I used
        the backend you asked for" from "I used the old one" selects the wrong
        winner and calls it evidence.
        """
        ...

    # -- read-only diagnostics -------------------------------------------
    def key_state(self, key: InputKey) -> bool | None:
        """Whether the OS believes ``key`` is down right now. ``None`` if unknown.

        Read-only, and the one cheap answer to a question ``CGEventPost``
        returning cannot answer: *did the edge reach the window server at all?*
        A post that returned and a key the OS does not believe is down are
        different faults with different remedies, and until they were separated
        the only available diagnosis was a guess.
        """
        ...

    def cursor_client_px(self) -> tuple[int, int] | None:
        """Cursor position in **canonical** client coordinates, or ``None``.

        Read-only, added for the calibration read-out and the F3 pixel probe
        (DECISIONS.md D-003). It emits nothing, so it is safe outside an input
        generation; it returns ``None`` when the client rect is unknown or the
        cursor is outside it, rather than reporting a coordinate that would
        paste into a config file wrong.
        """
        ...

    # -- hotkeys ----------------------------------------------------------
    def create_hotkey_source(
        self,
        submit: Callable[[RuntimeIntent], None],
        *,
        on_edge: Callable[[ChordEvent], None] | None = None,
        mint: Callable[[IntentType, str], RuntimeIntent] | None = None,
    ) -> HotkeySource:
        """Build the listener. ``on_edge`` sees every normalized edge, whatever
        policy later does with it - which is the only way "the chord never
        arrived" and "the chord arrived and was refused" stay distinguishable.

        ``mint`` is the coordinator's physical-chord capability. A listener
        built without one still submits every intent it recognizes, and none of
        them can start Live: that is deliberate, and it is what makes
        ``--hotkey-test`` safe to leave running while a person watches it."""
        ...


def current_platform_name() -> str:
    return "windows" if sys.platform == "win32" else "macos"


def create_platform_port(platform_name: str | None = None) -> PlatformPort:
    """Import and construct the port for this OS.

    The per-OS module is imported lazily *inside* the branch so that importing
    :mod:`prospector_engine.ports` never pulls Quartz or ctypes.wintypes onto
    the wrong platform.
    """
    target = platform_name or current_platform_name()
    if target == "windows":
        from prospector_engine.platform_win import WindowsPlatformPort

        return WindowsPlatformPort()
    if target == "macos":
        from prospector_engine.platform_mac import MacPlatformPort

        return MacPlatformPort()
    raise PlatformUnavailable(f"No platform port for {target!r}")


def create_release_only_port(platform_name: str | None = None) -> ReleaseOnlyPort:
    """Construct the minimal release-only backend used by ``deadman.py``.

    Kept separate from :func:`create_platform_port` so the helper process never
    imports window lookup, hotkeys, or anything able to press a key.
    """
    target = platform_name or current_platform_name()
    if target == "windows":
        from prospector_engine.platform_win import WindowsReleaseOnlyPort

        return WindowsReleaseOnlyPort()
    if target == "macos":
        from prospector_engine.platform_mac import MacReleaseOnlyPort

        return MacReleaseOnlyPort()
    raise PlatformUnavailable(f"No release-only port for {target!r}")
