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
from typing import Protocol, runtime_checkable

from prospector_engine.contracts import (
    ClientRectPhysicalPx,
    FocusState,
    InputKey,
    InputVocabulary,
    MouseButton,
    PinResult,
    RuntimeIntent,
)

__all__ = [
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

    def is_running(self) -> bool: ...


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

    def find_client_rect(self) -> ClientRectPhysicalPx | None: ...

    def pin_client_rect(self, size_px: tuple[int, int]) -> PinResult: ...

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

    # -- read-only diagnostics -------------------------------------------
    def cursor_client_px(self) -> tuple[int, int] | None:
        """Cursor position in client-relative physical pixels, or ``None``.

        Read-only, added for the calibration read-out and the F3 pixel probe
        (DECISIONS.md D-003). It emits nothing, so it is safe outside an input
        generation; it returns ``None`` when the client rect is unknown or the
        cursor is outside it, rather than reporting a coordinate that would
        paste into a config file wrong.
        """
        ...

    # -- hotkeys ----------------------------------------------------------
    def create_hotkey_source(self, submit: Callable[[RuntimeIntent], None]) -> HotkeySource: ...


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
