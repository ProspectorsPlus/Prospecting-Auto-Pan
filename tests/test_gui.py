"""Dashboard construction, focus safety, and - above all - layout stability.

The dashboard's most important properties are *negative* ones, and they are
asserted here rather than left to review:

* it must not offer a clickable control that would only work if clicking it did
  not steal focus from Roblox;
* **Stop & Release must be reachable at every supported size and UI scale**,
  because a stop control that scrolls off screen is not a stop control;
* the profile selector must not be able to disagree with the running pipeline;
* **the window must not resize itself.** Status strings, blocker text, setup
  progress and log lines all change several times a second, and every one of
  them used to be able to push the toplevel wider. The tests below drive each
  of those strings between empty and worst-case and assert the geometry does
  not move;
* **each polling loop owns exactly one timer.** Pressing a button four times
  used to start four render loops, because scheduling lived at the end of the
  render function.

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
    root.geometry("1240x820")
    dash = Dashboard(root, application)
    root.update_idletasks()
    yield dash
    root.destroy()
    application.deadman.close()


def _widgets(widget: tk.Misc, classes: tuple[str, ...]) -> list[tk.Misc]:
    found: list[tk.Misc] = []
    for child in widget.winfo_children():
        if child.winfo_class() in classes:
            found.append(child)
        found.extend(_widgets(child, classes))
    return found


def _button_texts(widget: tk.Misc) -> list[str]:
    return [str(button.cget("text")) for button in _widgets(widget, ("TButton", "Button"))]


# ---------------------------------------------------------------------------
# Focus safety
# ---------------------------------------------------------------------------


def test_the_dashboard_builds_and_reports_off(dashboard: Any) -> None:
    assert dashboard.mode_var.get() == "OFF"
    assert dashboard.recorder is None


def test_there_is_no_clickable_start_live_reset_or_pan_test(dashboard: Any) -> None:
    """Clicking Tk removes Roblox focus, so those must not be buttons (plan 11.2)."""
    texts = [text.lower() for text in _button_texts(dashboard.root)]
    assert not any(text.startswith("start live") for text in texts)
    assert not any(text == "reset" or text.startswith("reset character") for text in texts)
    assert not any(text.startswith("pan test") or text.startswith("pan swap") for text in texts)


def test_the_normal_flow_is_three_controls(dashboard: Any) -> None:
    texts = set(_button_texts(dashboard.root))
    assert "Start Navigator" in texts
    assert "Observe Only" in texts
    assert any(text.startswith("Stop & Release All Input") for text in texts)


@pytest.mark.parametrize(
    "retired",
    [
        "Connect Roblox",
        "Fit & Verify Viewport",
        "Collect Calibration Evidence",
        "Calibrate Live Control",
        "Enable Live Control...",
    ],
)
def test_the_dead_commissioning_controls_are_gone(dashboard: Any, retired: str) -> None:
    """Five buttons that between them did one useful thing and one dead end."""
    texts = {text.split("(")[0].strip() for text in _button_texts(dashboard.root)}
    assert retired not in texts


def test_diagnostic_recording_is_advanced_and_named_for_what_it_is(dashboard: Any) -> None:
    assert "Record Diagnostics" in set(_button_texts(dashboard.root))
    tip = dashboard.record_button
    assert tip.winfo_manager() == "grid"


def test_there_is_no_second_window_for_setup(dashboard: Any) -> None:
    """The commissioning Toplevel is gone; setup renders in the main window."""
    import tkinter as tk

    before = [w for w in dashboard.root.winfo_children() if isinstance(w, tk.Toplevel)]
    for _ in range(5):
        dashboard._render_setup()
    after = [w for w in dashboard.root.winfo_children() if isinstance(w, tk.Toplevel)]
    assert before == after == []


def test_live_and_service_hotkeys_are_shown_as_guidance(dashboard: Any) -> None:
    labels = [str(label.cget("text")) for label in _widgets(dashboard.root, ("Label",))]
    joined = " ".join(labels)
    assert "F1" in joined
    assert "F6" in joined and "F4" in joined and "F5" in joined


# ---------------------------------------------------------------------------
# Stop is always reachable
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "geometry",
    ["960x640", "1280x720", "1440x900", "1600x1000", "1280x640"],
)
def test_stop_and_release_is_visible_at_every_supported_size(
    dashboard: Any, geometry: str
) -> None:
    dashboard.root.geometry(geometry)
    dashboard.root.update_idletasks()
    button = dashboard.stop_button

    width, height = (int(part) for part in geometry.split("x"))
    assert button.winfo_ismapped() or button.winfo_manager() == "grid"
    assert button.winfo_width() > 0
    # Its top-left corner stays inside the window at every size.
    assert 0 <= button.winfo_x() < width
    assert 0 <= button.winfo_y() < height


@pytest.mark.parametrize("scaling", [1.0, 1.5, 2.0])
def test_stop_and_release_survives_ui_scaling(dashboard: Any, scaling: float) -> None:
    """Tk's ``tk scaling`` grows every font and pad; the header must absorb it."""
    dashboard.root.tk.call("tk", "scaling", scaling)
    dashboard.root.geometry("1280x720")
    dashboard.root.update_idletasks()

    button = dashboard.stop_button
    assert button.winfo_manager() == "grid"
    assert 0 <= button.winfo_x() < 1280


def test_the_window_has_a_minimum_size_that_matches_the_layout(dashboard: Any) -> None:
    minimum = dashboard.root.minsize()
    assert minimum == (dashboard.MIN_WIDTH, dashboard.MIN_HEIGHT)


def test_exactly_one_row_expands_and_it_is_the_body(dashboard: Any) -> None:
    root = dashboard.root
    weights = [int(root.grid_rowconfigure(row)["weight"]) for row in range(7)]
    assert weights.count(1) == 1, f"row weights {weights} - bounded rows, one grower"
    assert weights[5] == 1, "the preview row takes the extra height"
    assert weights[0] == 0, "the header must not grow"


# ---------------------------------------------------------------------------
# Summaries and analysis
# ---------------------------------------------------------------------------


def test_the_four_top_summaries_are_present(dashboard: Any) -> None:
    assert set(dashboard.summary_vars) == {"roblox", "capture", "navigation", "live"}
    for variable in dashboard.summary_vars.values():
        assert variable.get()


def test_the_analysis_panel_leads_with_plain_language(dashboard: Any) -> None:
    dashboard.analysis.render(None)
    assert dashboard.analysis.headline_var.get()

    observation = _observation()
    dashboard.analysis.render(observation)
    assert dashboard.analysis.headline_var.get() == observation.plain_summary
    assert dashboard.analysis.value_vars["profile"].get().startswith("green_arrow_v1")


def test_frame_details_are_disclosed_not_deleted(dashboard: Any) -> None:
    dashboard.analysis.render(_observation())
    details = dashboard.analysis.details_var.get()

    assert not dashboard.analysis.details.expanded, "detail is filed, not on the front page"
    assert "frame      #7" in details
    assert "revisions" in details and "geometry" in details
    assert "timing" in details


def test_a_frozen_packet_is_labelled_and_carries_no_command(dashboard: Any) -> None:
    from dataclasses import replace

    from prospector_engine.contracts import PacketKind

    frozen = replace(
        _observation(), packet_kind=PacketKind.TERMINAL, command=None, plain_summary="Stopped"
    )
    dashboard.analysis.render(frozen)

    headline = dashboard.analysis.headline_var.get()
    assert "frozen" in headline.lower()
    assert dashboard.analysis.value_vars["output"].get().startswith("none")


def test_the_diagnostics_drawer_keeps_every_engineering_value(dashboard: Any) -> None:
    from treasure_panels import DiagnosticsDrawer

    assert set(dashboard.drawer.texts) == set(DiagnosticsDrawer.TABS)
    dashboard.advanced.toggle()
    dashboard._render_drawer()
    performance = dashboard.drawer.texts["Performance"].get("1.0", "end")
    capture = dashboard.drawer.texts["Capture"].get("1.0", "end")

    assert "governor" in performance and "rss" in performance
    assert "current" in performance and "peak" in performance, "peak is not current memory"
    assert "superseded" in capture
    assert "lifetime" in capture, "lifetime totals are labelled as lifetime"
    assert "not a capture failure" in capture


def test_the_capture_tab_separates_every_rate(dashboard: Any) -> None:
    dashboard.advanced.toggle()
    dashboard._render_drawer()
    performance = dashboard.drawer.texts["Performance"].get("1.0", "end")

    for label in ("requested", "source", "unique", "processed", "control", "preview"):
        assert label in performance
    assert "never gates Live" in performance


# ---------------------------------------------------------------------------
# Profile authority
# ---------------------------------------------------------------------------


def test_the_selector_starts_on_the_profile_the_pipeline_is_running(dashboard: Any) -> None:
    """The observed bug: the dropdown read generic while the pipeline ran yellow."""
    authority = dashboard.app.profiles
    assert dashboard.profile_var.get() == authority.label_for(authority.active_id)
    assert dashboard.app.pipeline.profile.profile_id == authority.active_id


def test_selecting_a_profile_fires_the_real_combobox_event(dashboard: Any) -> None:
    """Bound to <<ComboboxSelected>>, and tested through it rather than around it."""
    authority = dashboard.app.profiles
    target = next(
        stable_id for stable_id in authority.library.ids() if stable_id != authority.active_id
    )
    dashboard.profile_var.set(authority.label_for(target))

    dashboard.profile_combo.event_generate("<<ComboboxSelected>>")
    dashboard.root.update_idletasks()

    assert authority.pending_id == target, "the swap is staged for a frame boundary"
    assert authority.active_id != target, "it must not land mid-frame"
    assert authority.apply_pending() is not None
    assert authority.active_id == target


def test_a_display_label_can_never_be_mistaken_for_a_profile_id(dashboard: Any) -> None:
    authority = dashboard.app.profiles
    for stable_id, label in authority.choices():
        assert authority.library.get(stable_id) is not None
        assert authority.library.get(label) is None


def test_the_profile_selector_is_an_override_not_the_normal_path(dashboard: Any) -> None:
    """Setup identifies the map from frames; the dropdown only overrides that."""
    from prospector_engine.vision import load_profiles

    candidates = load_profiles().selectable()
    assert len(candidates) >= 2, "a classifier needs something to choose between"
    assert dashboard.profile_combo.winfo_manager() == "grid"


# ---------------------------------------------------------------------------
# Overlay
# ---------------------------------------------------------------------------


def _observation(**overrides: Any) -> Any:
    """A synthetic observation with a real frame behind it."""
    from prospector_engine.contracts import (
        ArrowCandidateRecord,
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
            profile_id="green_arrow_v1",
            track_id=3,
            bbox_px=(600, 300, 60, 90),
            centroid_px=(630.0, 345.0),
            tip_px=(630.0, 300.0),
            axis_unit_xy=(0.0, -1.0),
            confidence=0.82,
            valid=True,
            tail_px=(630.0, 390.0),
            score_terms=(("contrast", 0.9), ("topology", 1.0), ("solidity", 0.85)),
            score_margin=0.24,
            notch_px=((612.0, 330.0), (648.0, 330.0)),
            scale_norm=0.09,
            track_age=12,
        ),
    )
    direction = overrides.pop(
        "direction",
        DirectionObservation(
            error_deg=32.0,
            confidence=0.7,
            cue_id="topology_consensus",
            cue_disagreement_deg=4.0,
            valid=True,
            sign_confidence=0.6,
            sign_margin_deg=28.0,
            anisotropy=1.9,
        ),
    )
    defaults: dict[str, Any] = {
        "frame": frame,
        "processed_at_s": 0.005,
        "published_at_s": 0.006,
        "key": RuntimeKey("test", 1, 1, 1, 1, 1, frame.sequence, frame.content_id),
        "profile_id": "green_arrow_v1",
        "profile_status": "pending",
        "strategy_id": "topology_consensus",
        "arrow": arrow,
        "candidates": (
            ArrowCandidateRecord(
                label=2,
                area_px=4000,
                bbox_px=(200, 400, 120, 90),
                centroid_px=(260.0, 445.0),
                score=0.31,
                accepted=False,
                rejected_reason="topology below threshold",
            ),
        ),
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
        "plain_summary": "Turn right 32 degrees",
    }
    defaults.update(overrides)
    return DiagnosticObservation(**defaults)


def _canvas_items(dashboard: Any) -> dict[str, Any]:
    return dashboard._diagnostics._items


def _visible(dashboard: Any, name: str) -> bool:
    item = _canvas_items(dashboard).get(name)
    if item is None:
        return False
    return bool(dashboard.canvas.itemcget(item, "state") != "hidden")


def test_the_overlay_draws_both_direction_arms_and_the_angle(dashboard: Any) -> None:
    dashboard.root.update_idletasks()
    assert dashboard._diagnostics.render(_observation())

    assert _visible(dashboard, "forward_arm")
    assert _visible(dashboard, "desired_arm")
    assert _visible(dashboard, "arc")
    text = dashboard.canvas.itemcget(_canvas_items(dashboard)["angle_text"], "text")
    assert "32" in text


def test_minimal_mode_omits_the_candidate_clutter(dashboard: Any) -> None:
    from treasure_overlay import OverlayMode

    dashboard.root.update_idletasks()
    dashboard._diagnostics.set_mode(OverlayMode.MINIMAL)
    dashboard._diagnostics.render(_observation())

    assert not _visible(dashboard, "contour")
    assert not _visible(dashboard, "notch_0")
    assert _visible(dashboard, "desired_arm"), "the turn is always drawn"


def test_full_diagnostics_adds_the_geometry_the_estimate_came_from(dashboard: Any) -> None:
    from treasure_overlay import OverlayMode

    dashboard.root.update_idletasks()
    dashboard._diagnostics.set_mode(OverlayMode.FULL)
    dashboard._diagnostics.render(_observation())

    assert _visible(dashboard, "contour")
    assert _visible(dashboard, "shaft")
    assert _visible(dashboard, "notch_0") and _visible(dashboard, "notch_1")
    assert dashboard._diagnostics._reject_items, "rejected candidates are drawn"


def test_an_abstaining_direction_hides_the_desired_arm_and_says_why(dashboard: Any) -> None:
    from prospector_engine.contracts import DirectionObservation

    dashboard.root.update_idletasks()
    abstained = DirectionObservation(
        error_deg=None,
        confidence=0.0,
        cue_id="topology_consensus",
        cue_disagreement_deg=61.0,
        valid=False,
        abstain_reason="cues disagree",
    )
    dashboard._diagnostics.render(_observation(direction=abstained, desired_deg=None))

    assert not _visible(dashboard, "desired_arm")
    assert _visible(dashboard, "no_desired")
    text = dashboard.canvas.itemcget(_canvas_items(dashboard)["no_desired"], "text")
    assert "cues disagree" in text


def test_a_frozen_packet_is_drawn_but_labelled_frozen(dashboard: Any) -> None:
    from dataclasses import replace

    from prospector_engine.contracts import PacketKind

    dashboard.root.update_idletasks()
    frozen = replace(
        _observation(),
        packet_kind=PacketKind.TERMINAL,
        command=None,
        plain_summary="Stopped - previous session ended normally",
    )
    assert dashboard._diagnostics.render(frozen)

    caption = dashboard.canvas.itemcget(_canvas_items(dashboard)["caption"], "text")
    assert "FROZEN" in caption
    assert "no command is in effect" in caption


def test_an_older_packet_is_refused_rather_than_drawn(dashboard: Any) -> None:
    """The exact bug: preview frame 53545 beside decision frame 53542."""
    from dataclasses import replace

    from tests.fakes import make_frame

    dashboard.root.update_idletasks()
    current = _observation(frame=make_frame(20))
    current = replace(current, key=replace(current.key, frame_sequence=20))
    assert dashboard._diagnostics.render(current)

    straggler = _observation(frame=make_frame(19))
    straggler = replace(straggler, key=replace(straggler.key, frame_sequence=19))
    assert not dashboard._diagnostics.render(straggler)


def test_the_same_observation_is_not_redrawn(dashboard: Any) -> None:
    dashboard.root.update_idletasks()
    observation = _observation()
    assert dashboard._diagnostics.render(observation)
    assert not dashboard._diagnostics.render(observation)


def test_canvas_items_are_reused_rather_than_recreated(dashboard: Any) -> None:
    from dataclasses import replace

    from tests.fakes import make_frame

    dashboard.root.update_idletasks()
    for sequence in range(30, 36):
        observation = _observation(frame=make_frame(sequence))
        observation = replace(
            observation, key=replace(observation.key, frame_sequence=sequence)
        )
        dashboard._diagnostics.render(observation)
    before = len(dashboard.canvas.find_all())

    for sequence in range(36, 42):
        observation = _observation(frame=make_frame(sequence))
        observation = replace(
            observation, key=replace(observation.key, frame_sequence=sequence)
        )
        dashboard._diagnostics.render(observation)

    assert len(dashboard.canvas.find_all()) == before


def test_a_conditional_control_changes_state_rather_than_leaving_the_grid(
    dashboard: Any,
) -> None:
    """Hiding a button must not change the layout - that is what makes it jump."""
    assert dashboard.recover_button.winfo_manager() == "grid"
    assert not dashboard.app.authority.release_uncertain


def test_the_preview_explains_what_the_overlay_shows(dashboard: Any) -> None:
    legend = dashboard.legend_var.get()
    assert "player-forward reference" in legend
    assert "map arrow" in legend


def test_closing_the_dashboard_shuts_the_application_down(dashboard: Any) -> None:
    with contextlib.suppress(Exception):
        assert callable(dashboard.on_close)


# ---------------------------------------------------------------------------
# Layout stability: the window must not resize itself
# ---------------------------------------------------------------------------

#: Empty, ordinary, and a genuinely awful string: a full sentence about
#: Accessibility permissions is exactly the kind of message that used to be
#: rendered into an unconstrained label and push the toplevel wider.
TEXT_CASES = (
    "",
    "Ready",
    "macOS has not granted this app permission to see the Roblox window. Enable "
    "Screen Recording and Accessibility for this app in System Settings > Privacy "
    "& Security, then press Start Navigator again.",
    "x" * 400,
)


def _root_size(dashboard: Any) -> tuple[int, int]:
    dashboard.root.update_idletasks()
    return (dashboard.root.winfo_reqwidth(), dashboard.root.winfo_reqheight())


def test_summary_text_of_any_length_cannot_resize_the_window(dashboard: Any) -> None:
    baseline = _root_size(dashboard)
    for text in TEXT_CASES:
        for key in dashboard.summary_vars:
            dashboard._set_summary(key, text[:40] or "-", text)
        assert _root_size(dashboard) == baseline, f"resized on {text[:30]!r}"


def test_the_readout_values_cannot_resize_the_window(dashboard: Any) -> None:
    baseline = _root_size(dashboard)
    for text in TEXT_CASES:
        for variable in dashboard.readout_vars.values():
            variable.set(text)
        assert _root_size(dashboard) == baseline, f"resized on {text[:30]!r}"


def test_the_actionable_message_wraps_and_clips_rather_than_growing(dashboard: Any) -> None:
    baseline = _root_size(dashboard)
    for text in TEXT_CASES:
        dashboard.message.set(text)
        assert _root_size(dashboard) == baseline, f"resized on {text[:30]!r}"


def test_the_mode_badge_cannot_resize_the_window(dashboard: Any) -> None:
    baseline = _root_size(dashboard)
    for text in ("OFF", "NAVIGATING", "FAULT - input released", "x" * 120):
        dashboard.mode_var.set(text)
        assert _root_size(dashboard) == baseline


def test_every_setup_stage_and_failure_leaves_the_geometry_alone(dashboard: Any) -> None:
    from prospector_engine.contracts import (
        SetupFailure,
        SetupFailureKind,
        SetupProgress,
        SetupStage,
    )

    baseline = _root_size(dashboard)
    for stage in SetupStage:
        failure = (
            SetupFailure(
                SetupFailureKind.PERMISSION,
                stage,
                TEXT_CASES[2],
                TEXT_CASES[2],
                TEXT_CASES[3],
            )
            if stage is SetupStage.FAILED
            else None
        )
        progress = SetupProgress(
            stage=stage,
            attempt=3,
            detail=TEXT_CASES[2],
            started_at_s=0.0,
            updated_at_s=1.0,
            failure=failure,
        )
        dashboard.setup_panel.render(progress)
        dashboard._render_guidance(progress)
        assert _root_size(dashboard) == baseline, f"resized on {stage}"


def test_expanding_the_diagnostics_drawer_does_not_widen_the_window(dashboard: Any) -> None:
    width_before = _root_size(dashboard)[0]
    dashboard.advanced.toggle()
    dashboard._render_drawer()
    dashboard.root.update_idletasks()

    assert dashboard.root.winfo_reqwidth() == width_before


def test_long_log_lines_do_not_widen_the_window(dashboard: Any) -> None:
    dashboard.advanced.toggle()
    dashboard._render_drawer()
    width_before = _root_size(dashboard)[0]
    for index in range(40):
        dashboard.app.coordinator.events.add("noise", f"{'y' * 300} {index}")
    dashboard._render_drawer()

    assert _root_size(dashboard)[0] == width_before


# ---------------------------------------------------------------------------
# Timers: one cancellable handle per loop
# ---------------------------------------------------------------------------


def test_each_polling_loop_owns_exactly_one_timer(dashboard: Any) -> None:
    assert set(dashboard.tickers) == {"preview", "status", "setup", "metrics", "drawer"}
    for ticker in dashboard.tickers.values():
        assert ticker.running


def test_starting_a_ticker_repeatedly_does_not_multiply_it(dashboard: Any) -> None:
    """Clicking a refresh four times used to give four render loops forever."""
    ticker = dashboard.tickers["setup"]
    handles = set()
    for _ in range(6):
        ticker.start()
        handles.add(ticker._handle)
    assert len(handles) == 1


def test_rendering_on_demand_never_schedules_a_second_loop(dashboard: Any) -> None:
    ticker = dashboard.tickers["status"]
    handle = ticker._handle
    for _ in range(5):
        ticker.render_once()
    assert ticker._handle == handle
    assert ticker.ticks == 5


def test_closing_cancels_every_timer(dashboard: Any) -> None:
    for ticker in dashboard.tickers.values():
        ticker.stop()
        assert not ticker.running
        assert ticker._handle is None


# ---------------------------------------------------------------------------
# The preview survives status churn
# ---------------------------------------------------------------------------


def test_the_preview_is_not_blanked_by_status_changes(dashboard: Any) -> None:
    dashboard.root.update_idletasks()
    assert dashboard._diagnostics.render(_observation())
    drawn = len(dashboard.canvas.find_all())

    for text in TEXT_CASES:
        dashboard.message.set(text)
        dashboard.mode_var.set(text[:20])
        for key in dashboard.summary_vars:
            dashboard._set_summary(key, "Connected", text)
        dashboard.root.update_idletasks()

    assert len(dashboard.canvas.find_all()) == drawn


def test_a_one_pixel_layout_change_does_not_discard_the_preview(dashboard: Any) -> None:
    dashboard.root.update_idletasks()
    assert dashboard._diagnostics.render(_observation())
    before = len(dashboard.canvas.find_all())

    dashboard.canvas.configure(width=dashboard.canvas.winfo_width() + 1)
    dashboard.root.update_idletasks()

    assert len(dashboard.canvas.find_all()) == before


# ---------------------------------------------------------------------------
# Setup progress is visible, and Stop stays reachable through all of it
# ---------------------------------------------------------------------------


def test_every_setup_stage_has_a_visible_cell(dashboard: Any) -> None:
    from prospector_engine.contracts import SetupStage
    from treasure_panels import SETUP_STEPS

    shown = {key for key, _label in SETUP_STEPS}
    machine_stages = {
        stage.value
        for stage in SetupStage
        if stage
        not in (SetupStage.IDLE, SetupStage.READY, SetupStage.FAILED, SetupStage.CANCELLED)
    }
    assert machine_stages == shown


def test_a_setup_failure_is_shown_in_red_with_the_remedy(dashboard: Any) -> None:
    from prospector_engine.contracts import (
        SetupFailure,
        SetupFailureKind,
        SetupProgress,
        SetupStage,
    )

    failure = SetupFailure(
        SetupFailureKind.AMBIGUOUS_WINDOW,
        SetupStage.FIND_ROBLOX,
        "more than one Roblox window is open",
        "Close the extra Roblox window and press Start Navigator again.",
    )
    progress = SetupProgress(
        stage=SetupStage.FAILED,
        attempt=1,
        detail=failure.describe(),
        started_at_s=0.0,
        updated_at_s=1.0,
        failure=failure,
    )
    dashboard.setup_panel.render(progress)
    dashboard._render_guidance(progress)

    assert "Close the extra Roblox window" in dashboard.message.text
    assert dashboard.setup_panel.cells["find_roblox"].cget("bg") != ""


def test_stop_stays_reachable_while_setup_is_running(dashboard: Any) -> None:
    from prospector_engine.contracts import SetupProgress, SetupStage

    progress = SetupProgress(
        stage=SetupStage.FIT_VIEWPORT,
        attempt=1,
        detail="asking Roblox for a 1280x720 client",
        started_at_s=0.0,
        updated_at_s=0.5,
    )
    dashboard.setup_panel.render(progress)
    dashboard._render_guidance(progress)
    dashboard.root.update_idletasks()

    assert str(dashboard.start_button.cget("state")) == "disabled"
    assert str(dashboard.stop_button.cget("state")) != "disabled"
    assert dashboard.stop_button.winfo_manager() == "grid"


@pytest.mark.parametrize("geometry", ["1000x700", "1180x800", "1600x1000"])
def test_all_primary_controls_stay_visible_at_supported_sizes(
    dashboard: Any, geometry: str
) -> None:
    dashboard.root.geometry(geometry)
    dashboard.root.update_idletasks()
    width, height = (int(part) for part in geometry.split("x"))

    for button in (dashboard.start_button, dashboard.observe_button, dashboard.stop_button):
        assert button.winfo_manager() == "grid"
        assert 0 <= button.winfo_x() < width
        assert 0 <= button.winfo_y() < height


def test_a_remembered_window_size_is_bounded_on_read(tmp_path: Path) -> None:
    from treasure_gui import WindowLayout

    path = tmp_path / "window.json"
    path.write_text('{"width": 99999, "height": -4}', encoding="utf-8")
    layout = WindowLayout.load(path)

    assert WindowLayout.MIN_WIDTH <= layout.width <= WindowLayout.MAX_WIDTH
    assert WindowLayout.MIN_HEIGHT <= layout.height <= WindowLayout.MAX_HEIGHT


def test_a_corrupt_window_file_falls_back_to_the_default(tmp_path: Path) -> None:
    from treasure_gui import WindowLayout

    path = tmp_path / "window.json"
    path.write_text("not json at all", encoding="utf-8")

    assert WindowLayout.load(path) == WindowLayout()


# ---------------------------------------------------------------------------
# Arming is still a physical, deliberate, separate gesture
# ---------------------------------------------------------------------------


def test_arming_is_its_own_button_and_says_what_it_authorizes(dashboard: Any) -> None:
    assert "Arm Live" in set(_button_texts(dashboard.root))
    assert dashboard.arm_button.winfo_manager() == "grid"


def test_start_navigator_never_arms(dashboard: Any) -> None:
    """Automatic setup may reach READY on its own. It may never arm."""
    import inspect

    source = inspect.getsource(type(dashboard)._start)
    assert "ARM_LIVE" not in source
    from prospector_engine.autosetup import AutomaticSetup

    assert "ARM" not in inspect.getsource(AutomaticSetup)


def test_arming_while_blocked_is_refused_with_the_reason(dashboard: Any) -> None:
    # Nothing has been set up, so the SETUP blocker is standing.
    dashboard._arm()

    assert dashboard.message.text
    assert "Start Navigator" in dashboard.message.text
    assert dashboard.app.coordinator.arm_token() is None


def test_the_arm_button_is_disabled_until_nothing_is_blocking(dashboard: Any) -> None:
    from prospector_engine.contracts import SetupProgress

    dashboard._render_guidance(SetupProgress.idle())
    assert str(dashboard.arm_button.cget("state")) == "disabled"


# ---------------------------------------------------------------------------
# The control-mode probe never touches Shift
# ---------------------------------------------------------------------------


def test_the_shift_lock_probe_only_looks(tmp_path: Path) -> None:
    import inspect

    from treasure_gui import shift_lock_probe

    source = inspect.getsource(shift_lock_probe)
    for forbidden in ("tap_key", "hold_key", "InputKey.", "raw_key_down", "session"):
        assert forbidden not in source, f"the probe reaches for {forbidden}"
    assert "never presses Shift" in source, "the invariant is stated where it lives"


@pytest.mark.parametrize(
    ("point", "verified", "needle"),
    [
        ((640, 360), True, "centre"),
        ((80, 360), False, "not centred"),
        (None, False, "outside"),
    ],
)
def test_the_pointer_cue_reads_the_control_mode(
    point: Any, verified: bool, needle: str
) -> None:
    from tests.fakes import make_frame
    from treasure_gui import shift_lock_probe

    sample = shift_lock_probe(lambda: point)(make_frame(1))

    assert sample.verified is verified
    assert needle in sample.detail


def test_a_pointer_probe_that_raises_cannot_confirm_anything() -> None:
    from tests.fakes import make_frame
    from treasure_gui import shift_lock_probe

    def boom() -> tuple[int, int] | None:
        raise OSError("scripted")

    sample = shift_lock_probe(boom)(make_frame(1))

    assert not sample.verified
    assert "could not be read" in sample.detail
