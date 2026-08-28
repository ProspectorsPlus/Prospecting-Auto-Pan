"""One test that drives the whole path, because the seams are where bugs live.

Capture publishes a rendered arrow, the coordinator starts a Shadow worker, the
worker runs the real detector and the real navigator, and the dashboard renders
the resulting packet. Every unit is covered elsewhere; this is here to catch
the wiring between them - a key that never reaches the overlay, a profile swap
that never reaches the pipeline, a packet the preview silently refuses.

No OS input is emitted at any point: Shadow holds a `NoInputSession`, and the
platform port is a fake that records every edge it is asked for.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from prospector_engine.capture import (
    CaptureConfig,
    CaptureService,
    EvidenceRegistry,
    ViewportGuard,
)
from prospector_engine.contracts import IntentType, PerformanceTier, RunMode
from prospector_engine.coordinator import CoordinatorConfig, RuntimeCoordinator
from prospector_engine.input_authority import AuthorityConfig, HealthSources, InputAuthority
from prospector_engine.navigation import (
    NavigationCapabilities,
    PerceptionPipeline,
    make_shadow_worker,
)
from prospector_engine.vision import ArrowSegmenter, ProfileAuthority, load_profiles
from tests.arrow_fixtures import render_scene
from tests.fakes import (
    FakeCaptureSource,
    FakeDeadmanClient,
    FakePlatformPort,
    VirtualClock,
    make_geometry,
)


@pytest.fixture
def wired(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("TREASURE_DATA_DIR", str(tmp_path / "data"))
    from prospector_engine.telemetry import resolve_app_paths

    port = FakePlatformPort(VirtualClock(), geometry=make_geometry())
    guard = ViewportGuard(port)
    guard.connect()
    deadman = FakeDeadmanClient()

    frames = [
        render_scene(heading_deg=float(angle), terrain="grass", scale_px=100.0, seed=angle).bgr
        for angle in (30, 45, 60, 75, 90)
    ]
    source = FakeCaptureSource(frames=frames)
    registry = EvidenceRegistry("e2e")
    capture = CaptureService(
        guard,
        registry,
        config=CaptureConfig(start_tier=PerformanceTier.STANDARD, max_frame_age_ms=100_000),
        source_factory=lambda: source,
    )
    authority = InputAuthority(
        port,
        deadman=deadman,
        health=HealthSources(
            focus=port.focus_state,
            client_rect=lambda: guard.geometry if guard.geometry.valid else None,
            capture_age_s=capture.latest_age_s,
        ),
        config=AuthorityConfig(),
        run_id="e2e-run",
    )
    library = load_profiles()
    profiles = ProfileAuthority(library, "green_arrow_v1")
    pipeline = PerceptionPipeline(segmenter=ArrowSegmenter(profiles.active), profiles=profiles)
    capabilities = NavigationCapabilities.observing(
        os_name="test", profile_id=profiles.active_id
    )
    coordinator = RuntimeCoordinator(
        authority=authority,
        guard=guard,
        capture=capture,
        registry=registry,
        workers={
            IntentType.START_SHADOW: make_shadow_worker(lambda: pipeline, lambda: capabilities)
        },
        config=CoordinatorConfig(),
        paths=resolve_app_paths().ensure(),
        pipeline_provider=lambda: pipeline,
        profiles=profiles,
    )
    assert capture.start()
    coordinator.start()
    yield {
        "coordinator": coordinator,
        "capture": capture,
        "authority": authority,
        "guard": guard,
        "port": port,
        "profiles": profiles,
        "pipeline": pipeline,
    }
    coordinator.shutdown(2.0)
    capture.stop(2.0)


def _await(predicate: Any, timeout_s: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_shadow_runs_the_real_detector_and_publishes_keyed_packets(wired: Any) -> None:
    coordinator = wired["coordinator"]
    coordinator.submit(coordinator.next_intent(IntentType.START_SHADOW, "gui"))

    assert _await(lambda: coordinator.mode is RunMode.SHADOW), "Shadow never started"
    assert _await(lambda: coordinator.observations.peek() is not None), "no packet published"

    packet = coordinator.observations.peek()
    assert packet is not None
    assert packet.key.run_id == "e2e-run"
    assert packet.key.frame_sequence > 0
    assert packet.profile_id == "green_arrow_v1"
    assert packet.plain_summary, "every packet carries a sentence a person can read"


def test_the_detector_finds_the_arrow_end_to_end(wired: Any) -> None:
    coordinator = wired["coordinator"]
    coordinator.submit(coordinator.next_intent(IntentType.START_SHADOW, "gui"))
    assert _await(lambda: coordinator.mode is RunMode.SHADOW)

    def _valid() -> bool:
        packet = coordinator.observations.peek()
        return packet is not None and packet.arrow.valid

    assert _await(_valid, 6.0), "the arrow was never detected through the full path"
    packet = coordinator.observations.peek()
    assert packet is not None
    assert packet.arrow.score_terms, "the score breakdown reached the packet"
    assert packet.cues, "the direction cues reached the packet"


def test_shadow_never_presses_anything(wired: Any) -> None:
    """Up-edges are expected - a mode transition runs the release floor.

    Down-edges are the ones that matter, and Shadow must produce none.
    """
    coordinator, port = wired["coordinator"], wired["port"]
    coordinator.submit(coordinator.next_intent(IntentType.START_SHADOW, "gui"))
    assert _await(lambda: coordinator.observations.peek() is not None, 6.0)
    time.sleep(0.3)

    presses = [entry["op"] for entry in port.transcript if entry["op"].endswith("_down")]
    assert presses == [], f"Shadow pressed {presses}"
    moves = [
        entry["op"]
        for entry in port.transcript
        if entry["op"] in ("drag_delta", "move_abs_px", "scroll")
    ]
    assert moves == [], f"Shadow moved the pointer: {moves}"


def test_stop_publishes_a_terminal_packet_with_no_command(wired: Any) -> None:
    coordinator = wired["coordinator"]
    coordinator.submit(coordinator.next_intent(IntentType.START_SHADOW, "gui"))
    assert _await(lambda: coordinator.observations.peek() is not None, 6.0)

    coordinator.submit(coordinator.next_intent(IntentType.STOP, "gui"))
    assert _await(lambda: coordinator.mode is RunMode.IDLE, 3.0)

    from prospector_engine.contracts import PacketKind

    packet = coordinator.observations.peek()
    assert packet is not None
    assert packet.packet_kind is PacketKind.TERMINAL
    assert packet.command is None
    snapshot = coordinator.snapshot()
    assert snapshot is not None and snapshot.command is None


def test_a_profile_swap_reaches_the_running_worker_and_bumps_the_key(wired: Any) -> None:
    coordinator, profiles = wired["coordinator"], wired["profiles"]
    coordinator.submit(coordinator.next_intent(IntentType.START_SHADOW, "gui"))
    assert _await(lambda: coordinator.observations.peek() is not None, 6.0)
    before = coordinator.observations.peek()
    assert before is not None

    assert profiles.request("yellow_map_v0")
    coordinator.submit(coordinator.next_intent(IntentType.SELECT_PROFILE, "gui"))

    def _swapped() -> bool:
        packet = coordinator.observations.peek()
        return packet is not None and packet.profile_id == "yellow_map_v0"

    assert _await(_swapped, 6.0), "the swap never reached the running worker"
    after = coordinator.observations.peek()
    assert after is not None
    assert after.key.profile_revision > before.key.profile_revision
    assert after.key.supersedes(before.key)


def test_navigation_is_blocked_before_setup_and_says_what_to_press(wired: Any) -> None:
    """Before automatic setup has run, the blocker names the button, not a gate."""
    coordinator = wired["coordinator"]
    blockers = coordinator.blockers()

    assert blockers, "navigation must never be startable before setup"
    setup = next(b for b in blockers if b.code == "SETUP")
    assert "Start Navigator" in setup.remedy
    joined = " ".join(coordinator.live_blockers())
    assert "E-YAW" not in joined, "a frozen experiment id is not an instruction"
    assert "E-STEER" not in joined
    for line in coordinator.live_blockers():
        assert line[0].isupper(), line


def test_a_failed_setup_becomes_the_one_blocker_a_user_can_act_on(wired: Any) -> None:
    from prospector_engine.contracts import (
        SetupFailure,
        SetupFailureKind,
        SetupProgress,
        SetupStage,
    )

    coordinator = wired["coordinator"]
    failure = SetupFailure(
        SetupFailureKind.NO_WINDOW,
        SetupStage.FIND_ROBLOX,
        "no Roblox window was found",
        "Open Roblox in windowed mode and press Start Navigator again.",
    )
    coordinator._publish_setup(
        SetupProgress(
            stage=SetupStage.FAILED,
            attempt=1,
            detail=failure.describe(),
            started_at_s=0.0,
            updated_at_s=0.0,
            failure=failure,
        )
    )

    codes = {blocker.code for blocker in coordinator.blockers()}
    assert "SETUP" in codes
    setup = next(b for b in coordinator.blockers() if b.code == "SETUP")
    assert setup.remedy == failure.remedy


def test_a_geometry_change_flushes_state_and_bumps_the_revision(wired: Any) -> None:
    coordinator, guard, port = wired["coordinator"], wired["guard"], wired["port"]
    coordinator.submit(coordinator.next_intent(IntentType.START_SHADOW, "gui"))
    assert _await(lambda: coordinator.observations.peek() is not None, 6.0)
    before = guard.revision

    port.set_geometry(make_geometry(size=(1024.0, 768.0)))
    coordinator.submit(coordinator.next_intent(IntentType.CONNECT_WINDOW, "gui"))

    assert _await(lambda: guard.revision > before, 3.0), "the revision never advanced"


def test_the_dashboard_renders_a_real_packet_without_crashing(
    wired: Any, tmp_path: Path
) -> None:
    """The last seam: a packet the engine produced, drawn by the real canvas."""
    import tkinter as tk

    from treasure_overlay import DiagnosticCanvas, OverlayMode

    coordinator = wired["coordinator"]
    coordinator.submit(coordinator.next_intent(IntentType.START_SHADOW, "gui"))
    assert _await(lambda: coordinator.observations.peek() is not None, 6.0)
    packet = coordinator.observations.peek()
    assert packet is not None

    try:
        root = tk.Tk()
    except tk.TclError as exc:  # pragma: no cover - headless environment
        pytest.skip(f"no Tk display available: {exc}")
    root.withdraw()
    try:
        canvas = tk.Canvas(root, width=640, height=360)
        canvas.pack()
        root.update_idletasks()
        diagnostics = DiagnosticCanvas(canvas, OverlayMode.FULL)

        assert diagnostics.render(packet), "the real packet was refused"
        assert not diagnostics.render(packet), "the same packet was drawn twice"
    finally:
        root.destroy()
