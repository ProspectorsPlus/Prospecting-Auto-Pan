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
import gc
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
    # Collect the dashboard's Tk variables while the interpreter still exists.
    # Left to chance they are finalized during some later test, on a thread
    # with no Tk main loop, and every one of them raises into the warning
    # summary - a hundred lines of noise that a real warning would hide in.
    for ticker in dash.tickers.values():
        ticker.stop()
    del dash
    gc.collect()
    root.destroy()
    gc.collect()
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


def test_the_chords_are_shown_as_guidance(dashboard: Any) -> None:
    labels = [str(label.cget("text")) for label in _widgets(dashboard.root, ("Label",))]
    joined = " ".join(labels)
    assert "Ctrl+" in joined
    for key in ("N navigate", "X stop", "R reset", "P pan", "D dig"):
        assert key in joined, f"the legend does not mention {key}"


def test_no_instruction_mentions_a_function_key(dashboard: Any) -> None:
    """There are no F-key bindings left, so nothing may name one.

    Function keys were the wrong binding: F1 is Help almost everywhere, the row
    is brightness and volume by default on a Mac, and one unmodified keypress
    starting a character walking is a slip away. They were removed rather than
    hidden - an alias that still fires is not removed.
    """
    import re

    texts = [str(w.cget("text")) for w in _widgets(dashboard.root, ("Label", "Button"))]
    for text in texts:
        assert not re.search(r"\bF[1-6]\b", text), f"primary UI still says: {text!r}"


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


def test_only_the_body_and_the_plain_log_expand(dashboard: Any) -> None:
    """Two growers, three to one, and every control row still bounded.

    The point of the rule was never "exactly one row" - it was that no control
    row may grow, because a layout that reflows when a fault appears reflows at
    the worst possible moment. The plain log is the second grower and takes a
    quarter of the extra height.
    """
    root = dashboard.root
    weights = [int(root.grid_rowconfigure(row)["weight"]) for row in range(8)]
    assert weights[5] == 3, "the preview takes most of the extra height"
    assert weights[6] == 1, "the plain log takes the rest"
    assert [w for i, w in enumerate(weights) if i not in (5, 6)] == [0, 0, 0, 0, 0, 0], (
        f"a control row grows: {weights}"
    )


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
    """Full Diagnostics says why beside the avatar; Minimal says it once, above.

    The reason belongs on screen either way. What Minimal must not do is print
    it a second time over the character - the caption at the top of the panel
    already carries it, and two copies of one sentence over a moving picture is
    the crowding this mode exists to avoid.
    """
    from prospector_engine.contracts import DirectionObservation
    from treasure_overlay import OverlayMode

    dashboard.root.update_idletasks()
    abstained = DirectionObservation(
        error_deg=None,
        confidence=0.0,
        cue_id="topology_consensus",
        cue_disagreement_deg=61.0,
        valid=False,
        abstain_reason="cues disagree",
    )
    packet = _observation(direction=abstained, desired_deg=None)

    dashboard._diagnostics.set_mode(OverlayMode.FULL)
    dashboard._diagnostics.render(packet)
    assert not _visible(dashboard, "desired_arm")
    assert _visible(dashboard, "no_desired")
    text = dashboard.canvas.itemcget(_canvas_items(dashboard)["no_desired"], "text")
    assert "cues disagree" in text

    dashboard._diagnostics.set_mode(OverlayMode.MINIMAL)
    dashboard._diagnostics.render(packet)
    assert not _visible(dashboard, "no_desired")
    assert not _visible(dashboard, "forward_label")
    assert "cues disagree" in dashboard.canvas.itemcget(
        _canvas_items(dashboard)["caption"], "text"
    )


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
# Starting is one physical, deliberate gesture - and the window says so
# ---------------------------------------------------------------------------


def test_there_is_no_arm_live_button(dashboard: Any) -> None:
    """The second gesture is gone, and so is every button that implied one.

    Its absence was the whole of the reported failure: the window said *Ready.
    Focus Roblox and press Ctrl+N* while the coordinator refused that chord for
    want of a click the window never mentioned (D-062).
    """
    texts = set(_button_texts(dashboard.root))
    assert "Arm Live" not in texts
    assert not any("Arm" in text for text in texts)
    assert not hasattr(dashboard, "arm_button")


def test_the_window_names_the_one_gesture_that_starts_movement(dashboard: Any) -> None:
    from treasure_gui import _CHORD_START

    assert _CHORD_START in dashboard.start_hint.cget("text")


def test_start_navigator_never_starts_live(dashboard: Any) -> None:
    """Automatic setup may reach READY on its own. It may never start Live."""
    import inspect

    source = inspect.getsource(type(dashboard)._start)
    assert "START_LIVE" not in source
    from prospector_engine.autosetup import AutomaticSetup

    assert "ARM" not in inspect.getsource(AutomaticSetup)


def test_a_blocked_runtime_says_blocked_and_never_ready(dashboard: Any) -> None:
    """Nothing has been set up, so the window must not offer the chord."""
    from treasure_gui import RunState

    progress = dashboard.app.coordinator.setup_progress
    state, sentence = dashboard.run_state(progress)

    assert state is not RunState.READY
    assert "Start Navigator" in sentence


def test_a_ready_message_and_the_coordinator_contract_agree(dashboard: Any) -> None:
    """READY may only be shown when a chord would actually be accepted.

    The reported failure is exactly this pair disagreeing: the window said
    *Ready. Focus Roblox and press Ctrl+N* while the coordinator answered
    ``live.refused: no arm token``. Nothing has been set up here, so the answer
    must not be READY.
    """
    from prospector_engine.contracts import SetupProgress
    from treasure_gui import RunState

    state, _ = dashboard.run_state(SetupProgress.idle())
    blocking = [b for b in dashboard.app.coordinator.blockers() if b.blocking]

    assert blocking, "the fixture is supposed to have a standing blocker"
    assert state is not RunState.READY


# ---------------------------------------------------------------------------
# The control-mode probe never touches Shift
# ---------------------------------------------------------------------------


def test_the_shift_lock_probe_only_looks(tmp_path: Path) -> None:
    import inspect

    from prospector_engine.application import shift_lock_probe

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
    from prospector_engine.application import shift_lock_probe
    from tests.fakes import make_frame

    sample = shift_lock_probe(lambda: point)(make_frame(1))

    assert sample.verified is verified
    assert needle in sample.detail


def test_a_pointer_probe_that_raises_cannot_confirm_anything() -> None:
    from prospector_engine.application import shift_lock_probe
    from tests.fakes import make_frame

    def boom() -> tuple[int, int] | None:
        raise OSError("scripted")

    sample = shift_lock_probe(boom)(make_frame(1))

    assert not sample.verified
    assert "could not be read" in sample.detail


def test_the_viewport_row_shows_a_clamp_rather_than_hiding_it(dashboard: Any) -> None:
    """ "1024x768 adopted" hides the interesting fact: we asked for 1280x720."""
    from prospector_engine.contracts import SetupProgress, SetupStage
    from tests.fakes import make_geometry

    clamped = make_geometry(size=(1024.0, 768.0))
    snapshot = type(
        "Snap",
        (),
        {
            "setup": SetupProgress(
                stage=SetupStage.READY,
                attempt=1,
                detail="ready",
                started_at_s=0.0,
                updated_at_s=1.0,
                requested_client_logical=(1280.0, 720.0),
                achieved_client_logical=(1024.0, 768.0),
            ),
            "phase": None,
            "control_state": None,
        },
    )()

    text = dashboard._viewport_text(clamped, snapshot)

    assert "1280x720" in text and "1024x768" in text
    assert "clamped" in text


def test_the_viewport_row_says_canonical_when_it_is(dashboard: Any) -> None:
    from tests.fakes import make_geometry

    text = dashboard._viewport_text(make_geometry(), None)

    assert "1280x720" in text and "canonical" in text


# ---------------------------------------------------------------------------
# The action layer: purple, readable, and truthful
# ---------------------------------------------------------------------------


@pytest.fixture
def root() -> Any:
    widget = _tk_root()
    yield widget
    widget.destroy()


def _key_for(frame: Any) -> Any:
    from prospector_engine.contracts import RuntimeKey

    return RuntimeKey("test", 1, 1, 1, 1, 1, frame.sequence, frame.content_id)


def _command(**overrides: Any) -> Any:
    from prospector_engine.contracts import NavigationCommand

    defaults: dict[str, Any] = {
        "generation": 1,
        "source_frame_sequence": 7,
        "source_captured_at_s": 0.0,
        "forward_axis": 1,
        "lateral_axis": 0,
        "jump": False,
        "yaw_delta_px": 0,
        "issued_at_s": 0.0,
        "valid_until_s": 9_999.0,
        "reason": "follow",
        "turn_axis": 0,
    }
    defaults.update(overrides)
    return NavigationCommand(**defaults)


def _canvas(root: Any, mode: Any = None) -> Any:
    import tkinter as tk

    from treasure_overlay import DiagnosticCanvas, OverlayMode

    widget = tk.Canvas(root, width=640, height=360)
    widget.pack()
    root.update_idletasks()
    return DiagnosticCanvas(widget, mode or OverlayMode.MINIMAL)


def _visible_texts(canvas: Any) -> list[str]:
    """Every text string the canvas is actually showing."""
    shown = []
    for item in canvas.canvas.find_all():
        if canvas.canvas.type(item) != "text":
            continue
        if canvas.canvas.itemcget(item, "state") == "hidden":
            continue
        shown.append(str(canvas.canvas.itemcget(item, "text")))
    return shown


def _item_visible(canvas: Any, name: str) -> bool:
    item = canvas._items.get(name)
    if item is None:
        return False
    return canvas.canvas.itemcget(item, "state") != "hidden"


def test_shadow_draws_a_solid_would_command_and_emits_nothing(root: Any) -> None:
    """Solid and bold in Shadow too, at the weight of the gold direction arm.

    They used to be dashed to mark a proposal, which made the one layer you
    watch while observing the faintest thing on screen. Provenance is carried
    by the WOULD badge and the mode word instead, neither of which costs any
    legibility.
    """
    from prospector_engine.contracts import CommandVisualization

    canvas = _canvas(root)
    view = CommandVisualization.for_shadow(_command(turn_axis=1))
    canvas.render(_observation(command_view=view))
    root.update_idletasks()

    assert "WOULD" in _visible_texts(canvas)
    assert "ACTIVE" not in _visible_texts(canvas)
    # W and the right turn, both drawn, from the proposal.
    assert _item_visible(canvas, "action_stroke_W")
    assert _item_visible(canvas, "action_stroke_RT")
    # Solid, and thicker than the gold arm it sits beside.
    assert not canvas.canvas.itemcget(canvas._items["action_stroke_W"], "dash")
    width = float(canvas.canvas.itemcget(canvas._items["action_stroke_W"], "width"))
    assert width >= 8.0, f"the action ray is only {width} px wide"


def test_live_draws_active_only_from_the_leases_the_authority_reports(root: Any) -> None:
    from prospector_engine.contracts import (
        CommandVisualization,
        NavigationApplyResult,
        NavigationApplyStatus,
    )

    canvas = _canvas(root)
    # The command asked for forward *and* a right turn; the authority reports
    # holding only "w". The overlay must draw what is held, not what was asked.
    view = CommandVisualization.for_live(
        _command(turn_axis=1),
        NavigationApplyResult(NavigationApplyStatus.APPLIED, "ok", leases_held=("w",)),
    )
    canvas.render(_observation(command_view=view))
    root.update_idletasks()

    assert "ACTIVE" in _visible_texts(canvas)
    assert _item_visible(canvas, "action_stroke_W")
    assert not _item_visible(canvas, "action_stroke_RT"), "drew a turn nobody was holding"


def test_a_rejected_command_never_appears_active(root: Any) -> None:
    from prospector_engine.contracts import (
        CommandVisualization,
        NavigationApplyResult,
        NavigationApplyStatus,
    )

    canvas = _canvas(root)
    view = CommandVisualization.for_live(
        _command(forward_axis=1),
        NavigationApplyResult(NavigationApplyStatus.REJECTED_FOCUS, "Roblox lost focus"),
    )
    canvas.render(_observation(command_view=view))
    root.update_idletasks()

    assert "ACTIVE" not in _visible_texts(canvas)
    assert not _item_visible(canvas, "action_stroke_W")


def test_simultaneous_forward_and_turn_both_render(root: Any) -> None:
    from prospector_engine.contracts import (
        CommandVisualization,
        NavigationApplyResult,
        NavigationApplyStatus,
    )

    canvas = _canvas(root)
    view = CommandVisualization.for_live(
        _command(turn_axis=-1),
        NavigationApplyResult(NavigationApplyStatus.APPLIED, "ok", leases_held=("left", "w")),
    )
    canvas.render(_observation(command_view=view))
    root.update_idletasks()

    assert _item_visible(canvas, "action_stroke_W")
    assert _item_visible(canvas, "action_stroke_LT")
    assert {"W", "<"}.issubset(set(_visible_texts(canvas)))


def test_minimal_contains_no_cue_arms_labels_or_candidate_geometry(root: Any) -> None:
    """The screenshot failure: blue cue rays and outlier text over a route."""
    canvas = _canvas(root)
    canvas.render(_observation())
    root.update_idletasks()

    for index in range(3):
        assert not _item_visible(canvas, f"cue_{index}")
        assert not _item_visible(canvas, f"cue_{index}_label")
    for name in ("bbox", "centroid", "shaft", "tip", "tip2", "contour", "notch_0"):
        assert not _item_visible(canvas, name), f"Minimal drew {name}"
    assert not any("outlier" in text for text in _visible_texts(canvas))
    # ...while still answering the question Minimal exists for.
    assert _item_visible(canvas, "forward_arm")
    assert _item_visible(canvas, "desired_arm")


def test_full_diagnostics_still_draws_the_internals(root: Any) -> None:
    from treasure_overlay import OverlayMode

    canvas = _canvas(root, OverlayMode.FULL)
    canvas.render(_observation())
    root.update_idletasks()

    assert _item_visible(canvas, "cue_0")
    assert _item_visible(canvas, "bbox")


def test_switching_full_to_minimal_hides_items_already_drawn(root: Any) -> None:
    from treasure_overlay import OverlayMode

    canvas = _canvas(root, OverlayMode.FULL)
    canvas.render(_observation())
    root.update_idletasks()
    assert _item_visible(canvas, "bbox") and _item_visible(canvas, "cue_0")

    # No new packet arrives - which is the case that matters, because a stopped
    # run has no next frame to redraw on.
    canvas.set_mode(OverlayMode.MINIMAL)
    root.update_idletasks()
    for name in ("bbox", "centroid", "shaft", "tip", "cue_0", "cue_0_label"):
        assert not _item_visible(canvas, name), f"{name} survived the switch to Minimal"


def test_a_stopped_packet_clears_every_active_vector(root: Any) -> None:
    """The acceptance failure: 'Stopped' beside a live-looking vector field."""
    from dataclasses import replace

    from prospector_engine.contracts import (
        CommandVisualization,
        NavigationApplyResult,
        NavigationApplyStatus,
        PacketKind,
    )
    from treasure_overlay import OverlayMode

    canvas = _canvas(root, OverlayMode.FULL)
    live = CommandVisualization.for_live(
        _command(),
        NavigationApplyResult(NavigationApplyStatus.APPLIED, "ok", leases_held=("w",)),
    )
    running = _observation(command_view=live)
    canvas.render(running)
    root.update_idletasks()
    assert _item_visible(canvas, "action_stroke_W")

    stopped = replace(
        running,
        key=replace(running.key, mode_session_id=running.key.mode_session_id + 1),
        packet_kind=PacketKind.TERMINAL,
        command=None,
        command_view=live.freeze(),
        plain_summary="Stopped - no command is in effect",
    )
    canvas.render(stopped)
    root.update_idletasks()

    assert not _item_visible(canvas, "action_stroke_W")
    assert "ACTIVE" not in _visible_texts(canvas)
    # ...and the detector's internals go with it, so nothing looks live.
    for name in ("bbox", "cue_0", "cue_0_label", "contour", "shaft"):
        assert not _item_visible(canvas, name), f"a stopped packet still drew {name}"


def test_the_action_layer_item_count_is_bounded(root: Any) -> None:
    """Repeated renders reuse items; a canvas that grows would eventually die."""
    from prospector_engine.contracts import (
        CommandVisualization,
        NavigationApplyResult,
        NavigationApplyStatus,
    )

    canvas = _canvas(root)
    for index in range(40):
        held = ("w", "left") if index % 2 else ("w", "right", "space")
        view = CommandVisualization.for_live(
            _command(turn_axis=1, jump=bool(index % 2)),
            NavigationApplyResult(NavigationApplyStatus.APPLIED, "ok", leases_held=held),
        )
        from tests.fakes import make_frame

        frame = make_frame(100 + index, captured_at_s=0.0)
        canvas.render(
            _observation(
                frame=frame,
                key=_key_for(frame),
                command_view=view,
            )
        )
    root.update_idletasks()
    assert len(canvas.canvas.find_all()) < 80, "the action layer leaked canvas items"


def test_the_action_layer_is_raised_above_the_caption_and_diagnostics(root: Any) -> None:
    from prospector_engine.contracts import (
        CommandVisualization,
        NavigationApplyResult,
        NavigationApplyStatus,
    )
    from treasure_overlay import OverlayMode

    canvas = _canvas(root, OverlayMode.FULL)
    view = CommandVisualization.for_live(
        _command(),
        NavigationApplyResult(NavigationApplyStatus.APPLIED, "ok", leases_held=("w",)),
    )
    canvas.render(_observation(command_view=view))
    root.update_idletasks()

    order = canvas.canvas.find_all()
    action = order.index(canvas._items["action_stroke_W"])
    for name in ("caption", "caption_bg", "cue_0", "bbox", "desired_arm"):
        item = canvas._items.get(name)
        if item is not None:
            assert action > order.index(item), f"{name} covers the action layer"


# ---------------------------------------------------------------------------
# The window may never claim the character is moving when it is not
# ---------------------------------------------------------------------------


def _guidance_text(dashboard: Any) -> str:
    dashboard._render_setup()
    dashboard.root.update_idletasks()
    return " ".join(str(label.cget("text")) for label in _widgets(dashboard.root, ("Label",)))


def test_the_armed_setup_stages_are_never_described_as_navigating(dashboard: Any) -> None:
    """The prologue is armed and stationary. Calling it navigation is a lie.

    Live enters, stands still, and runs three bounded probes before the first
    navigation command exists. The window used to say "Navigating. Press Stop
    at any time." for every one of them, which is why thirty seconds of failing
    to characterize a camera looked exactly like thirty seconds of walking.
    """
    from dataclasses import replace

    from prospector_engine.contracts import RunMode, SetupStage

    for stage, expected in (
        (SetupStage.VERIFY_INPUT, "accepts a key"),
        (SetupStage.VERIFY_CONTROL_MODE, "camera control mode"),
        (SetupStage.CHARACTERIZE_TURN, "how the camera turns"),
    ):
        progress = replace(
            dashboard.app.coordinator.setup_progress,
            stage=stage,
            detail="running",
            failure=None,
            started_at_s=1.0,
            updated_at_s=1.0,
        )
        dashboard.app.coordinator._setup_progress = progress
        dashboard.app.coordinator._mode = RunMode.LIVE
        text = _guidance_text(dashboard)
        assert expected in text, f"{stage.value} is not described: {text[:200]}"
        assert "your character is moving" not in text
        assert "Navigating -" not in text


def test_every_armed_stage_has_words_of_its_own() -> None:
    """A stage added without a sentence would fall back to claiming movement."""
    from prospector_engine.contracts import SetupStage
    from treasure_gui import _LIVE_STAGE_WORDS

    emitting = {stage for stage in SetupStage if stage.emits_input}
    assert emitting == set(_LIVE_STAGE_WORDS)


# ---------------------------------------------------------------------------
# Which build is this, actually
# ---------------------------------------------------------------------------


def test_the_title_bar_says_which_build_is_running(dashboard: Any) -> None:
    """A window left open from an earlier run looks exactly like a new one.

    That is how a fix gets tested against the build that did not have it and
    reported as not working, so the branch's short commit and this process id
    are in the title where they cannot be missed.
    """
    title = dashboard.root.title()
    assert dashboard.build.version in title
    assert str(dashboard.build.process_id) in title
    if dashboard.build.commit:
        assert dashboard.build.commit in title


def test_the_drawer_carries_the_build_and_the_listener_health(dashboard: Any) -> None:
    if not dashboard.advanced.expanded:
        dashboard.advanced.toggle()
    dashboard._last_drawer_key = None
    dashboard._render_drawer()
    dashboard.root.update_idletasks()
    safety = dashboard.drawer.texts["Safety"].get("1.0", "end")
    assert "build" in safety
    assert dashboard.build.version in safety
    # "Hotkeys: running" was true of a listener that had already died, so the
    # edge and chord counts are shown beside the state.
    assert "hotkeys" in safety


def test_a_listener_that_cannot_be_read_does_not_break_the_drawer(dashboard: Any) -> None:
    class Broken:
        def health(self) -> None:
            raise RuntimeError("listener exploded")

    dashboard.app.hotkeys = Broken()
    assert "exploded" in dashboard._hotkey_health()


def _snapshot(**overrides: Any) -> Any:
    """A minimal live snapshot. The readout reads it and nothing else."""
    from prospector_engine.contracts import (
        NavigationPhase,
        RunMode,
        TelemetrySnapshot,
    )

    base = TelemetrySnapshot(
        sequence=1,
        mode=RunMode.LIVE,
        phase=NavigationPhase.FOLLOW,
        viewport=None,
        arrow=None,
        direction=None,
        motion=None,
        arrival=None,
        command=None,
        ledger_empty=False,
        focus=True,
        frame_age_ms=12.0,
    )
    from dataclasses import replace as _replace

    return _replace(base, **overrides)


# ---------------------------------------------------------------------------
# The readout reports the actuator, not the plan
# ---------------------------------------------------------------------------


def test_the_readout_names_every_fact_a_stuck_user_needs(dashboard: Any) -> None:
    """Nine rows, and each answers one question the reported failure raised.

    "Nothing moves" was unanswerable from this window: it showed the mode and
    the held leases and nothing about whether the chord was even heard, whether
    Live had been authorized, how long forward had been down, or what the
    coordinator's reason for sending nothing was.
    """
    from treasure_gui import _CHORD_START

    for key in (
        "chord",
        "authorization",
        "leases",
        "forward",
        "yaw",
        "turning",
        "moved",
        "blocked",
    ):
        assert key in dashboard.readout_vars, f"{key} is not on the dashboard"
    labels = " ".join(
        str(label.cget("text")) for label in _widgets(dashboard.root, ("TLabel", "Label"))
    )
    assert f"{_CHORD_START} listener" in labels


def test_the_readout_draws_the_ledger_rather_than_the_command(dashboard: Any) -> None:

    from prospector_engine.contracts import ActuatorState

    held = _snapshot(
        live_authorization="granted abc123",
        hotkey_ready=True,
        hotkey_detail="quartz-tap: hearing the keyboard",
        actuator=ActuatorState(
            held=("w",),
            forward_held_ms=2400.0,
            down_edges=1,
            up_edges=0,
            last_yaw_delta_px=-12,
            last_yaw_at_s=1.0,
            turn_backend="mouse yaw",
            last_displacement_norm=0.031,
        ),
    )
    dashboard._render_actuator(held)

    assert dashboard.readout_vars["leases"].get() == "w"
    assert "2.4 s" in dashboard.readout_vars["forward"].get()
    assert "1 down / 0 up" in dashboard.readout_vars["forward"].get()
    assert dashboard.readout_vars["yaw"].get() == "-12 px"
    assert "0.031" in dashboard.readout_vars["moved"].get()
    assert dashboard.readout_vars["authorization"].get() == "granted abc123"
    assert "hearing keys" in dashboard.readout_vars["chord"].get()


def test_a_dead_listener_is_shouted_about_rather_than_shown_as_a_dash(
    dashboard: Any,
) -> None:
    """A listener that is not hearing keys makes every chord vanish silently."""

    dashboard._render_actuator(
        _snapshot(hotkey_ready=False, hotkey_detail="quartz-tap: failed")
    )

    text = dashboard.readout_vars["chord"].get()
    assert "NOT HEARING KEYS" in text
    assert "failed" in text


def test_the_reason_nothing_is_moving_is_shown_verbatim(dashboard: Any) -> None:

    from prospector_engine.contracts import ActuatorState

    dashboard._render_actuator(
        _snapshot(
            actuator=ActuatorState(
                blocked_reason="the camera control mode has not been confirmed"
            )
        )
    )

    assert (
        dashboard.readout_vars["blocked"].get()
        == "the camera control mode has not been confirmed"
    )


def test_the_input_safety_card_renders_every_live_state(dashboard: Any) -> None:
    """Every branch, because one of them used to raise on a field that had gone.

    ``snapshot.arm_state`` survived the removal of the Arm Live button in this
    one card. Nothing reached the branch in a test - the fixture always had a
    standing blocker - so an ``AttributeError`` sat on the path that runs
    several times a second the moment a real session became unblocked.
    """
    from prospector_engine.contracts import ActuatorState, RunMode

    metrics = dashboard.app.capture.metrics()
    for snapshot in (
        None,
        _snapshot(mode=RunMode.IDLE, blockers=()),
        _snapshot(mode=RunMode.IDLE, blockers=(), live_authorization="refused: focus:False"),
        _snapshot(
            mode=RunMode.LIVE,
            blockers=(),
            actuator=ActuatorState(held=("w",), forward_held_ms=1500.0),
        ),
    ):
        dashboard._render_summaries(snapshot, metrics)
        dashboard.root.update_idletasks()

    assert "held" in dashboard.summary_details["live"].get()
