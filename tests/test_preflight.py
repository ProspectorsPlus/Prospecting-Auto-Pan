"""The input preflight, and the cadence self-lock it exists beside.

The failure these guard against is one sentence: *"it says APPLIED and the
character does not move."* Two very different causes produced it, and neither
was visible.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from prospector_engine.preflight import (
    CapabilityId,
    CapabilityKind,
    CapabilityState,
    CommandStage,
    PreflightInputs,
    run_preflight,
)


def _inputs(**overrides: Any) -> PreflightInputs:
    defaults: dict[str, Any] = {
        "os_name": "darwin",
        "launcher": "Terminal",
        "event_post": True,
        "input_listen": True,
        "screen_capture": True,
        "hotkey_listener_running": True,
        "roblox_focused": True,
        "arm_token_present": True,
        "processed_fps": 60.0,
        "min_processed_fps": 30.0,
        "release_uncertain": False,
        "ledger_empty": True,
    }
    defaults.update(overrides)
    return PreflightInputs(**defaults)


# ---------------------------------------------------------------------------
# Permissions
# ---------------------------------------------------------------------------


def test_everything_granted_is_ready_to_arm() -> None:
    report = run_preflight(_inputs())
    assert report.ok and report.can_start_live
    assert report.summary == "Ready to arm."


@pytest.mark.parametrize(
    ("field", "capability"),
    [
        ("event_post", CapabilityId.EVENT_POST),
        ("input_listen", CapabilityId.INPUT_LISTEN),
        ("screen_capture", CapabilityId.SCREEN_CAPTURE),
    ],
)
def test_a_denied_permission_names_its_settings_pane_and_the_launcher(
    field: str, capability: CapabilityId
) -> None:
    report = run_preflight(_inputs(**{field: False}))
    entry = report.get(capability)
    assert entry is not None
    assert entry.state is CapabilityState.DENIED
    assert not report.ok
    assert "Privacy & Security" in entry.settings_pane
    # The permission belongs to whatever launched the process, and saying so is
    # the difference between an instruction and a shrug.
    assert "Terminal" in entry.detail or "Terminal" in entry.remedy


def test_a_probe_that_could_not_run_is_unknown_and_never_a_fault() -> None:
    """Claiming a permission is missing because a call failed sends someone
    to a settings pane that was never the problem."""
    report = run_preflight(_inputs(event_post=None))
    entry = report.get(CapabilityId.EVENT_POST)
    assert entry is not None and entry.state is CapabilityState.UNKNOWN
    assert not entry.is_fault
    assert report.ok


def test_permissions_granted_means_the_failure_is_not_a_permission() -> None:
    """The measured state of the development machine, asserted as a case.

    AXIsProcessTrusted, CGPreflightPostEventAccess and
    CGPreflightListenEventAccess all read True here, so a Live failure on this
    machine must be explained by something else - and the preflight has to say
    so rather than leaving permissions as a standing suspect.
    """
    report = run_preflight(_inputs(processed_fps=15.0))
    assert report.ok, "no permission fault"
    assert not report.can_start_live
    cadence = report.get(CapabilityId.CADENCE)
    assert cadence is not None and cadence.state is CapabilityState.DENIED
    assert "15 processed fps" in cadence.detail


# ---------------------------------------------------------------------------
# Faults versus preconditions
# ---------------------------------------------------------------------------


def test_not_being_armed_is_a_precondition_not_a_fault() -> None:
    report = run_preflight(_inputs(arm_token_present=False, roblox_focused=False))
    assert report.ok, "an unmet precondition was reported as something broken"
    assert not report.can_start_live
    for identifier in (CapabilityId.ARM_TOKEN, CapabilityId.ROBLOX_FOCUS):
        entry = report.get(identifier)
        assert entry is not None and entry.kind is CapabilityKind.PRECONDITION


def test_a_dead_listener_is_a_fault_and_says_which_kind() -> None:
    with_permission = run_preflight(_inputs(hotkey_listener_running=False))
    entry = with_permission.get(CapabilityId.HOTKEY_LISTENER)
    assert entry is not None and entry.is_fault
    # Input Monitoring is granted, so it must not be blamed.
    assert "Input Monitoring" not in entry.remedy
    assert entry.settings_pane == ""

    without = run_preflight(_inputs(hotkey_listener_running=False, input_listen=False))
    entry = without.get(CapabilityId.HOTKEY_LISTENER)
    assert entry is not None and "Input Monitoring" in entry.remedy


def test_stuck_input_is_a_fault_and_says_to_press_stop() -> None:
    report = run_preflight(_inputs(release_uncertain=True))
    entry = report.get(CapabilityId.RELEASE_HEALTH)
    assert entry is not None and entry.is_fault
    assert "Stop" in entry.remedy


def test_the_summary_reports_a_fault_ahead_of_a_precondition() -> None:
    report = run_preflight(_inputs(event_post=False, arm_token_present=False))
    assert "Send keys and mouse to Roblox" in report.summary


# ---------------------------------------------------------------------------
# Applied is not moved
# ---------------------------------------------------------------------------


def test_only_observed_motion_counts_as_success() -> None:
    """CGEventPost returning is evidence that the call returned."""
    assert not CommandStage.REQUESTED.is_success
    assert not CommandStage.OS_EDGE_POSTED.is_success
    assert not CommandStage.AUTHORITY_APPLIED.is_success
    assert not CommandStage.REJECTED.is_success
    assert CommandStage.GAME_MOTION_CONFIRMED.is_success


# ---------------------------------------------------------------------------
# The cadence self-lock
# ---------------------------------------------------------------------------


def _service(**kwargs: Any) -> Any:
    from prospector_engine.capture import (
        CaptureConfig,
        CaptureService,
        EvidenceRegistry,
        ViewportGuard,
    )
    from tests.fakes import FakeCaptureSource, FakePlatformPort, VirtualClock, make_geometry

    port = FakePlatformPort(VirtualClock(), geometry=make_geometry())
    source = FakeCaptureSource()
    port.capture_source = source
    guard = ViewportGuard(port)
    guard.connect()
    service = CaptureService(
        guard,
        EvidenceRegistry("test"),
        config=CaptureConfig(supervisor_interval_s=0.05, **kwargs),
        source_factory=port.create_capture_source,
    )
    return service


def test_reading_frames_is_not_consuming_them() -> None:
    """Automatic setup and the live prologue both read frames.

    Latching "someone is consuming" on their behalf is what let the governor
    see a processed rate of zero against a consumer that no longer existed,
    and answer it by walking the cadence down to 15 Hz - below the 30 the
    steering controller requires. Live then refused to start, for a reason
    that was really the pipeline talking to itself.
    """
    service = _service()
    assert service.start()
    try:
        assert service.wait_for_new(0, 1.0) is not None
        assert not service.measuring, "a bare read registered as throughput"
        time.sleep(0.2)
        assert service.metrics().superseded_frames.session_total == 0
    finally:
        service.stop()


def test_a_measured_scope_is_entered_and_left() -> None:
    service = _service()
    assert service.start()
    try:
        assert not service.measuring
        with service.consuming("live"):
            assert service.measuring
        assert not service.measuring, "the scope outlived its phase"
    finally:
        service.stop()


def test_an_unmeasured_scope_consumes_without_being_judged() -> None:
    """Setup and the prologue poll on their own schedule.

    Their cadence is a property of the probe, not of the pipeline, so counting
    it as throughput would judge the pipeline on how slowly a probe chose to
    run.
    """
    service = _service()
    assert service.start()
    try:
        with service.consuming("setup", measured=False):
            # Consuming - supersedes are accounted honestly - but not measured.
            assert not service.measuring
    finally:
        service.stop()


def test_the_governor_never_rests_below_the_live_floor() -> None:
    """A stable-but-ineligible tier is the worst outcome available."""
    from prospector_engine.capture import CadenceGovernor, CaptureConfig
    from prospector_engine.contracts import PerformanceTier

    governor = CadenceGovernor(
        CaptureConfig(
            start_tier=PerformanceTier.DEGRADED,
            upshift_after_s=0.1,
            ineligible_retry_s=0.2,
        )
    )
    assert not PerformanceTier.DEGRADED.acceptable, "the 15 Hz tier is below the floor"

    now = 0.0
    tier = governor.tier
    # Healthy, but never saturating: the old saturation gate would hold this
    # tier forever and Live would refuse to steer for as long as it did.
    for _ in range(40):
        now += 0.1
        tier = governor.update(unique_fps=9.0, frame_age_ms=20.0, now_s=now, processed_fps=9.0)
        if tier is not PerformanceTier.DEGRADED:
            break
    assert tier is not PerformanceTier.DEGRADED, "the governor rested on an ineligible tier"
