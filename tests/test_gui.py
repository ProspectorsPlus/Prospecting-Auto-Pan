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


def test_the_shadow_view_explains_what_the_overlay_shows(dashboard: Any) -> None:
    joined = " ".join(_collect_label_text(dashboard.root))
    assert "assumed player-forward reference" in joined
    assert "E-FORWARD PENDING" in joined
    assert "desired map-arrow direction" in joined
    assert "rejected candidates" in joined


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
    dashboard._render_status(snapshot)
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


# ---------------------------------------------------------------------------
# The Shadow overlay
# ---------------------------------------------------------------------------


def _observation(**overrides: Any) -> Any:
    """A synthetic observation with a real frame behind it."""
    from prospector_engine.contracts import (
        ArrowObservation,
        CueReading,
        DiagnosticObservation,
        DirectionObservation,
        NavigationPhase,
        RuntimeKey,
    )
    from tests.fakes import make_frame

    frame = overrides.pop("frame", None) or make_frame(7, captured_at_s=0.0)
    arrow = overrides.pop(
        "arrow",
        ArrowObservation(
            profile_id="yellow_map_v0",
            track_id=3,
            bbox_px=(600, 300, 60, 90),
            centroid_px=(630.0, 345.0),
            tip_px=(630.0, 300.0),
            axis_unit_xy=(0.0, -1.0),
            confidence=0.82,
            valid=True,
        ),
    )
    direction = overrides.pop(
        "direction",
        DirectionObservation(
            error_deg=32.0,
            confidence=0.7,
            cue_id="fusion",
            cue_disagreement_deg=4.0,
            valid=True,
        ),
    )
    defaults: dict[str, Any] = {
        "frame": frame,
        "processed_at_s": 0.005,
        "published_at_s": 0.006,
        "key": RuntimeKey(
            run_id="test",
            coordinator_generation=1,
            mode_session_id=1,
            source_epoch=1,
            geometry_revision=1,
            profile_revision=1,
            frame_sequence=frame.sequence,
            content_id=frame.content_id,
        ),
        "profile_id": "yellow_map_v0",
        "profile_status": "pending",
        "strategy_id": "fusion",
        "arrow": arrow,
        "candidates": (),
        "contour_px": ((600, 300), (660, 300), (660, 390), (600, 390)),
        "anchor_px": (640.0, 430.0),
        "forward_deg": 0.0,
        "forward_source": "assumed: screen-up (E-FORWARD PENDING)",
        "desired_deg": 32.0,
        "direction": direction,
        "cues": (
            CueReading("tail_to_head", 32.0, 0.8, 0.8, valid=True),
            CueReading("notch_to_tip", 30.0, 0.9, 0.9, valid=True),
            CueReading("pca_axis", -150.0, 0.4, 0.0, valid=False, note="outlier"),
        ),
        "motion": None,
        "arrival": None,
        "phase": NavigationPhase.ALIGN,
        "command": None,
        "abstain_reason": None,
        "capture_ms": 4.0,
        "perception_ms": 5.0,
        "decision_ms": 0.1,
    }
    defaults.update(overrides)
    return DiagnosticObservation(**defaults)


def _canvas_items(dashboard: Any) -> dict[str, Any]:
    diagnostics = dashboard._diagnostics
    return diagnostics._items


def _visible(dashboard: Any, name: str) -> bool:
    """Whether an overlay element is on screen. Never created counts as not."""
    item = _canvas_items(dashboard).get(name)
    if item is None:
        return False
    return bool(dashboard.canvas.itemcget(item, "state") != "hidden")


def test_the_overlay_draws_both_direction_arms_and_the_angle(dashboard: Any) -> None:
    dashboard.root.update_idletasks()
    diagnostics = dashboard._diagnostics

    assert diagnostics.render(_observation()) is True

    for name in ("forward_arm", "desired_arm", "arc", "angle_text", "anchor_dot"):
        assert _visible(dashboard, name), name
    assert "+32.0" in dashboard.canvas.itemcget(_canvas_items(dashboard)["angle_text"], "text")


def test_the_overlay_draws_the_arrow_geometry(dashboard: Any) -> None:
    dashboard.root.update_idletasks()
    dashboard._diagnostics.render(_observation())

    for name in ("contour", "bbox", "centroid", "tip"):
        assert _visible(dashboard, name), name


def test_an_abstaining_direction_hides_the_desired_arm_and_says_why(dashboard: Any) -> None:
    from prospector_engine.contracts import DirectionObservation

    dashboard.root.update_idletasks()
    abstained = DirectionObservation(
        error_deg=None,
        confidence=0.0,
        cue_id="fusion",
        cue_disagreement_deg=61.0,
        valid=False,
        abstain_reason="cues disagree",
    )
    observation = _observation(direction=abstained, desired_deg=None)

    dashboard._diagnostics.render(observation)

    assert not _visible(dashboard, "desired_arm")
    assert not _visible(dashboard, "arc")
    assert "cues disagree" in dashboard.canvas.itemcget(
        _canvas_items(dashboard)["no_desired"], "text"
    )
    # The reference arm still shows: it is configuration, not a measurement.
    assert _visible(dashboard, "forward_arm")


def test_an_abstaining_arrow_hides_its_geometry_but_keeps_the_caption(dashboard: Any) -> None:
    from prospector_engine.contracts import ArrowObservation

    dashboard.root.update_idletasks()
    missing = ArrowObservation(
        profile_id="yellow_map_v0",
        track_id=None,
        bbox_px=None,
        centroid_px=None,
        tip_px=None,
        axis_unit_xy=None,
        confidence=0.0,
        valid=False,
        abstain_reason="no-candidate",
    )
    dashboard._diagnostics.render(_observation(arrow=missing, contour_px=(), desired_deg=None))

    for name in ("contour", "bbox", "centroid", "tip"):
        assert not _visible(dashboard, name), name
    assert "no-candidate" in dashboard.canvas.itemcget(
        _canvas_items(dashboard)["caption"], "text"
    )


def test_the_caption_reports_confidence_profile_status_and_timings(dashboard: Any) -> None:
    dashboard.root.update_idletasks()
    dashboard._diagnostics.render(_observation())

    text = dashboard.canvas.itemcget(_canvas_items(dashboard)["caption"], "text")
    assert "yellow_map_v0" in text
    assert "[pending]" in text
    assert "conf 0.82" in text
    assert "perception" in text
    assert "E-FORWARD PENDING" in text


def test_the_same_observation_is_not_redrawn(dashboard: Any) -> None:
    """Redrawing an unchanged frame would burn the Tk thread for nothing."""
    dashboard.root.update_idletasks()
    observation = _observation()

    assert dashboard._diagnostics.render(observation) is True
    assert dashboard._diagnostics.render(observation) is False


def test_canvas_items_are_reused_rather_than_recreated(dashboard: Any) -> None:
    """No delete-and-rebuild per frame: the item ids must be stable."""
    from tests.fakes import make_frame

    dashboard.root.update_idletasks()
    dashboard._diagnostics.render(_observation())
    first = dict(_canvas_items(dashboard))
    total_before = len(dashboard.canvas.find_all())

    for sequence in range(8, 14):
        dashboard._diagnostics.render(
            _observation(frame=make_frame(sequence, captured_at_s=0.0))
        )

    assert dict(_canvas_items(dashboard)) == first
    assert len(dashboard.canvas.find_all()) == total_before


def test_rejected_candidates_are_drawn_and_then_hidden_again(dashboard: Any) -> None:
    from prospector_engine.contracts import ArrowCandidateRecord
    from tests.fakes import make_frame

    dashboard.root.update_idletasks()
    rejected = tuple(
        ArrowCandidateRecord(
            label=index,
            area_px=1000,
            bbox_px=(100 * index, 100, 40, 40),
            centroid_px=(100.0 * index + 20, 120.0),
            score=0.2,
            accepted=False,
            rejected_reason="not the best candidate",
        )
        for index in range(1, 4)
    )
    dashboard._diagnostics.render(_observation(candidates=rejected))
    reject_items = dashboard._diagnostics._reject_items
    assert len(reject_items) == 3
    assert all(dashboard.canvas.itemcget(i, "state") != "hidden" for i in reject_items)

    dashboard._diagnostics.render(
        _observation(frame=make_frame(21, captured_at_s=0.0), candidates=())
    )
    assert all(dashboard.canvas.itemcget(i, "state") == "hidden" for i in reject_items)


def test_the_metrics_panel_reports_the_pipeline_state(dashboard: Any) -> None:
    dashboard._render_metrics(dashboard.app.capture.metrics())
    text = dashboard.metrics_var.get()
    assert "unique" in text
    assert "processed" in text
    assert "preview" in text
    assert "rss" in text


def test_selecting_a_profile_changes_the_running_pipeline(dashboard: Any) -> None:
    """The selector must reach the worker, not just the next session."""
    library = dashboard.app.library
    other = next(
        profile
        for profile in library.all()
        if profile.profile_id != dashboard.app.pipeline.profile.profile_id
    )
    dashboard.profile_var.set(f"{other.profile_id} [{other.status.value}]")

    dashboard._on_profile_selected(None)

    assert dashboard.app.pipeline.profile.profile_id == other.profile_id
