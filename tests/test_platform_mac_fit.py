"""The macOS Fit & Verify path against a scripted Accessibility layer.

These run only on macOS (the module imports Quartz at import time) and never
touch a real window: every Accessibility call goes to a fake, so the cases
the field produced can be replayed exactly - several Roblox windows where
the first one is not the game, a resize the OS clamps, a size attribute that
is not settable, a denied move, and a missing Accessibility grant.

Native evidence for E-VIEW still needs the owner: a fake cannot prove what
Roblox does to a resize request.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Any

import pytest

from prospector_engine.geometry import LogicalRect, ViewportGeometry, WindowIdentity
from tests.fakes import make_geometry

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="macOS Accessibility path")


@dataclass
class _Point:
    x: float
    y: float


@dataclass
class _Size:
    width: float
    height: float


class _FakeWindow:
    def __init__(self, title: str, frame: tuple[float, float, float, float]) -> None:
        self.title = title
        self.frame = frame
        self.set_sizes: list[tuple[float, float]] = []
        self.set_positions: list[tuple[float, float]] = []


class _FakeAX:
    """Just enough of ApplicationServices for ``pin_client_rect``."""

    kAXErrorSuccess = 0
    kAXWindowsAttribute = "AXWindows"
    kAXPositionAttribute = "AXPosition"
    kAXSizeAttribute = "AXSize"
    kAXValueCGPointType = 1
    kAXValueCGSizeType = 2

    def __init__(
        self,
        windows: list[_FakeWindow],
        *,
        settable: bool = True,
        refuse_size: bool = False,
        clamp_to: tuple[float, float] | None = None,
    ) -> None:
        self.windows = windows
        self.settable = settable
        self.refuse_size = refuse_size
        self.clamp_to = clamp_to

    def AXUIElementCreateApplication(self, pid: int) -> str:
        return f"app:{pid}"

    def AXUIElementCopyAttributeValue(
        self, element: Any, name: str, _out: Any
    ) -> tuple[int, Any]:
        if name == self.kAXWindowsAttribute:
            return (0, list(self.windows))
        if isinstance(element, _FakeWindow):
            if name == self.kAXPositionAttribute:
                return (0, ("point", element.frame[0], element.frame[1]))
            if name == self.kAXSizeAttribute:
                return (0, ("size", element.frame[2], element.frame[3]))
            if name == "AXTitle":
                return (0, element.title)
            if name == "AXFullScreen":
                return (0, False)
        return (-25205, None)

    def AXValueGetValue(self, value: Any, kind: int, _out: Any) -> tuple[bool, Any]:
        if value[0] == "point":
            return (True, _Point(value[1], value[2]))
        return (True, _Size(value[1], value[2]))

    def AXUIElementIsAttributeSettable(
        self, element: Any, name: str, _out: Any
    ) -> tuple[int, bool]:
        return (0, self.settable)

    def AXValueCreate(self, kind: int, value: Any) -> tuple[str, Any]:
        return ("size" if kind == self.kAXValueCGSizeType else "point", value)

    def AXUIElementSetAttributeValue(self, element: Any, name: str, value: Any) -> int:
        if name == self.kAXSizeAttribute:
            if self.refuse_size:
                return -25200
            width, height = float(value[1].width), float(value[1].height)
            if self.clamp_to is not None:
                width, height = max(width, self.clamp_to[0]), max(height, self.clamp_to[1])
            element.set_sizes.append((width, height))
            element.frame = (element.frame[0], element.frame[1], width, height)
            return 0
        if name == self.kAXPositionAttribute:
            return -25200  # every move is denied; a resize must not care
        return -25205


def _port(
    monkeypatch: pytest.MonkeyPatch,
    services: _FakeAX,
    selected: _FakeWindow,
    *,
    trusted: bool = True,
) -> Any:
    from prospector_engine import platform_mac

    port = platform_mac.MacPlatformPort()
    identity = WindowIdentity(
        window_id=77, process_id=4242, owner="Roblox", title=selected.title
    )
    x, y, w, h = selected.frame
    monkeypatch.setattr(port, "_app_services", lambda: services)
    monkeypatch.setattr(port, "accessibility_trusted", lambda: trusted)
    monkeypatch.setattr(port, "_scan_roblox", lambda: (identity, LogicalRect(x, y, w, h)))
    monkeypatch.setattr(port, "_measure_title_bar_pt", lambda window: 28.0)

    def window_geometry() -> ViewportGeometry:
        fx, fy, fw, fh = selected.frame
        return make_geometry(origin=(fx, fy + 28.0), size=(fw, fh - 28.0), backing_scale=2.0)

    monkeypatch.setattr(port, "window_geometry", window_geometry)
    return port


def test_the_ax_window_is_correlated_with_the_captured_cg_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``windows[0]`` was a crash-handler dialog; the game is the second window."""
    dialog = _FakeWindow("Roblox Crash Handler", (300.0, 300.0, 400.0, 200.0))
    game = _FakeWindow("Roblox", (0.0, 39.0, 1800.0, 1082.0))
    services = _FakeAX([dialog, game])
    port = _port(monkeypatch, services, game)

    result = port.pin_client_rect((1280.0, 720.0))

    assert result.ok, result.message
    assert game.set_sizes == [(1280.0, 748.0)], (
        "the game window was resized, title bar included"
    )
    assert dialog.set_sizes == [], "the first AX window was left alone"
    assert "frame match" in result.mechanism


def test_a_denied_move_does_not_fail_a_successful_resize(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    game = _FakeWindow("Roblox", (120.0, 80.0, 1800.0, 1082.0))
    port = _port(monkeypatch, _FakeAX([game]), game)

    result = port.pin_client_rect((1280.0, 720.0))

    assert result.ok
    assert game.set_positions == [], "the origin is preserved; no move is requested"
    assert game.frame[0] == 120.0 and game.frame[1] == 80.0


def test_an_os_clamp_is_reported_as_an_answer_not_a_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    game = _FakeWindow("Roblox", (0.0, 39.0, 1800.0, 1082.0))
    port = _port(monkeypatch, _FakeAX([game], clamp_to=(1400.0, 800.0)), game)

    result = port.pin_client_rect((1280.0, 720.0))

    assert result.ok, "a clamp is not an API refusal"
    assert result.clamped
    assert "answered" in result.message
    assert result.geometry is not None and result.geometry.valid


def test_an_ignored_resize_is_not_ok_false_either(monkeypatch: pytest.MonkeyPatch) -> None:
    """The request was accepted and nothing changed: the guard's settle
    classifies that as clamped-to-current, not as a refusal."""
    game = _FakeWindow("Roblox", (0.0, 39.0, 1800.0, 1082.0))
    port = _port(monkeypatch, _FakeAX([game], clamp_to=(1800.0, 1082.0)), game)

    result = port.pin_client_rect((1280.0, 720.0))

    assert result.ok and result.clamped


def test_an_unsettable_size_is_a_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    game = _FakeWindow("Roblox", (0.0, 39.0, 1800.0, 1082.0))
    port = _port(monkeypatch, _FakeAX([game], settable=False), game)

    result = port.pin_client_rect((1280.0, 720.0))

    assert not result.ok
    assert "not settable" in result.message
    assert game.set_sizes == []


def test_a_refused_set_is_a_refusal_with_the_permission_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    game = _FakeWindow("Roblox", (0.0, 39.0, 1800.0, 1082.0))
    port = _port(monkeypatch, _FakeAX([game], refuse_size=True), game)

    result = port.pin_client_rect((1280.0, 720.0))

    assert not result.ok
    assert "Accessibility" in result.message


def test_a_missing_accessibility_grant_is_a_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    game = _FakeWindow("Roblox", (0.0, 39.0, 1800.0, 1082.0))
    port = _port(monkeypatch, _FakeAX([game]), game, trusted=False)

    result = port.pin_client_rect((1280.0, 720.0))

    assert not result.ok
    assert "Accessibility permission" in result.message
    assert game.set_sizes == []


def test_title_match_is_the_fallback_when_frames_disagree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A window mid-animation reports a different frame; the title still matches."""
    other = _FakeWindow("Settings", (10.0, 10.0, 500.0, 400.0))
    game = _FakeWindow("Roblox", (0.0, 39.0, 1790.0, 1070.0))
    services = _FakeAX([other, game])
    port = _port(monkeypatch, services, game)
    monkeypatch.setattr(
        port,
        "_scan_roblox",
        lambda: (
            WindowIdentity(window_id=77, process_id=4242, owner="Roblox", title="Roblox"),
            LogicalRect(0.0, 39.0, 1800.0, 1082.0),
        ),
    )

    result = port.pin_client_rect((1280.0, 720.0))

    assert result.ok
    assert "title match" in result.mechanism
    assert other.set_sizes == []
