"""Dashboard construction and the focus-safety rules baked into its layout.

The dashboard's most important property is a *negative* one: it must not offer
a clickable control that would only work if clicking it did not steal focus
from Roblox. That is asserted here rather than left to review.

These tests build a real Tk window and skip cleanly where one cannot be opened.
They do not start capture or the coordinator, so no screen recording permission
and no background threads are involved.
"""

from __future__ import annotations

import contextlib
import tkinter as tk
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


def _tk_root() -> tk.Tk:
    try:
        root = tk.Tk()
    except tk.TclError as exc:  # pragma: no cover - headless environment
        pytest.skip(f"no Tk display available: {exc}")
    root.withdraw()
    return root


@pytest.fixture
def dashboard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("TREASURE_DATA_DIR", str(tmp_path / "data"))
    from treasure_gui import Dashboard, build_application

    application = build_application()
    root = _tk_root()
    root.geometry("1100x760")
    dash = Dashboard(root, application)
    root.update_idletasks()
    yield dash
    root.destroy()
    application.deadman.close()


def _button_texts(widget: tk.Misc) -> list[str]:
    texts: list[str] = []
    for child in widget.winfo_children():
        cls = child.winfo_class()
        if cls in ("TButton", "Button"):
            with_text = str(child.cget("text"))
            texts.append(with_text)
        texts.extend(_button_texts(child))
    return texts


def test_the_dashboard_builds_and_reports_off(dashboard: Any) -> None:
    assert dashboard.mode_var.get() == "OFF"
    assert dashboard.recorder is None


def test_there_is_no_clickable_start_live_reset_or_pan_test(dashboard: Any) -> None:
    """Clicking Tk removes Roblox focus, so those must not be buttons (plan 11.2)."""
    texts = [text.lower() for text in _button_texts(dashboard.root)]
    assert not any(text.startswith("start live") for text in texts)
    assert not any(text == "reset" or text.startswith("reset character") for text in texts)
    assert not any(text.startswith("pan test") or text.startswith("pan swap") for text in texts)


def test_the_offered_buttons_are_the_focus_safe_ones(dashboard: Any) -> None:
    texts = set(_button_texts(dashboard.root))
    assert "Pin Window" in texts
    assert "Start Shadow" in texts
    assert "Arm Live..." in texts
    assert any(text.startswith("Record") for text in texts)
    assert any(text.startswith("STOP") for text in texts)


def test_live_and_service_hotkeys_are_shown_as_guidance(dashboard: Any) -> None:
    assert "F1" in dashboard.live_guide.cget("text")
    labels = _collect_label_text(dashboard.root)
    joined = " ".join(labels)
    assert "F4" in joined and "F5" in joined
    assert "Focus Roblox" in joined


def _collect_label_text(widget: tk.Misc) -> list[str]:
    texts: list[str] = []
    for child in widget.winfo_children():
        if child.winfo_class() in ("TLabel", "Label"):
            # Labels driven purely by a textvariable have no static text.
            with contextlib.suppress(tk.TclError):
                texts.append(str(child.cget("text")))
        texts.extend(_collect_label_text(child))
    return texts


def test_the_pending_pixel_status_is_visible_in_the_ui(dashboard: Any) -> None:
    joined = " ".join(_collect_label_text(dashboard.root))
    assert "PENDING reverification" in joined


def test_automatic_profile_classification_is_shown_as_disabled(dashboard: Any) -> None:
    joined = " ".join(_collect_label_text(dashboard.root))
    assert "Automatic classification is DISABLED" in joined
    assert "E-PROF" in joined


def test_the_recovery_button_is_hidden_until_release_is_uncertain(dashboard: Any) -> None:
    assert not dashboard.recover_button.winfo_ismapped()

    dashboard.app.authority.latch_release_uncertain("test")
    dashboard.root.update_idletasks()
    snapshot = dashboard.app.coordinator.snapshot()
    if snapshot is None:
        dashboard.app.coordinator._publish_telemetry()
        snapshot = dashboard.app.coordinator.snapshot()
    assert snapshot is not None
    dashboard._render(snapshot)
    dashboard.root.update_idletasks()

    assert dashboard.recover_button.winfo_manager() == "grid"


def test_the_window_is_resizable_and_the_body_expands(dashboard: Any) -> None:
    root = dashboard.root
    assert root.resizable() == (True, True)
    # Row 3 is the body; it must take the slack when the window grows.
    assert int(root.grid_rowconfigure(3)["weight"]) == 1
    assert int(root.grid_columnconfigure(0)["weight"]) == 1
    root.geometry("1600x1000")
    root.update_idletasks()
    root.geometry("960x640")
    root.update_idletasks()


def test_every_readiness_card_has_a_label(dashboard: Any) -> None:
    expected = {
        "viewport",
        "focus",
        "capture",
        "watchdog",
        "deadman",
        "ledger",
        "release",
        "arm",
        "pixels",
    }
    assert set(dashboard.readiness_labels) == expected
