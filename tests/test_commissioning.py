"""Keyed blockers, the guided commissioning steps, and typed fit completions.

Three defects from the field are pinned here. The blocker list repeated the
same gate under three wordings because a default controller was instantiated
just to ask it why it could not steer; "Roblox is not frontmost" was shown as
a permanent failure while the user was, necessarily, looking at the
dashboard; and the fit thread mutated coordinator state directly, so a stale
fit could invalidate a newer geometry.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from prospector_engine.capture import (
    CaptureConfig,
    CaptureService,
    EvidenceRegistry,
    ViewportGuard,
)
from prospector_engine.contracts import (
    BlockerScope,
    EvidenceStatus,
    FitCompletion,
    FitPhase,
    IntentType,
    LiveBlocker,
    ViewportFit,
)
from prospector_engine.coordinator import CoordinatorConfig, RuntimeCoordinator
from prospector_engine.input_authority import AuthorityConfig, HealthSources, InputAuthority
from prospector_engine.navigation import (
    COMMISSIONING_STEPS,
    NavigationGates,
    commissioning_blockers,
    commissioning_steps,
)
from tests.fakes import FakeDeadmanClient, FakePlatformPort, VirtualClock, make_geometry

# ---------------------------------------------------------------------------
# Blockers
# ---------------------------------------------------------------------------


def test_each_pending_gate_is_exactly_one_blocker_with_a_step_and_a_remedy() -> None:
    gates = NavigationGates(os_name="macos", profile_id="yellow_map_v1")
    blockers = commissioning_blockers(gates)
    codes = [blocker.code for blocker in blockers]
    assert len(codes) == len(set(codes)), "a gate appeared more than once"
    assert codes.count("E-YAW") == 1, "the yaw calibration is one gate, not three rows"
    yaw = next(blocker for blocker in blockers if blocker.code == "E-YAW")
    assert yaw.scope is BlockerScope.EVIDENCE
    assert yaw.status == "pending"
    assert yaw.remedy.startswith("Step 8:")
    assert "physical procedure" in yaw.detail
    assert yaw.evidence


def test_a_validated_gate_disappears_from_the_blockers() -> None:
    gates = NavigationGates(
        os_name="macos", profile_id="yellow_map_v1", e_view=EvidenceStatus.VALIDATED
    )
    assert "E-VIEW" not in [blocker.code for blocker in commissioning_blockers(gates)]


def test_the_steps_never_pass_an_evidence_step_from_runtime_state() -> None:
    gates = NavigationGates(os_name="macos", profile_id="yellow_map_v1")
    rows = commissioning_steps(
        gates, connected=True, viewport_canonical=True, viewport_usable=True
    )
    states = {step.number: state for step, state, _note in rows}
    assert states[1] == "done", "connecting is a runtime fact"
    assert states[2] == "pending", "a canonical viewport is not E-VIEW evidence"
    assert all(states[number] == "pending" for number in range(3, 11))
    assert states[11] == "blocked"
    note_for_two = next(note for step, _state, note in rows if step.number == 2)
    assert "canonical" in note_for_two and "PENDING" in note_for_two


def test_the_steps_distinguish_usable_from_canonical() -> None:
    gates = NavigationGates(os_name="macos", profile_id="yellow_map_v1")
    rows = commissioning_steps(
        gates, connected=True, viewport_canonical=False, viewport_usable=True
    )
    _step, state, note = next(row for row in rows if row[0].number == 2)
    assert state == "pending" and "usable for Shadow" in note


def test_every_step_names_the_control_that_performs_it() -> None:
    controls = {step.control for step in COMMISSIONING_STEPS}
    assert controls == {
        "Connect Roblox",
        "Fit & Verify Viewport",
        "Collect Calibration Evidence",
        "Calibrate Live Control",
        "Enable Live Control",
    }
    assert [step.number for step in COMMISSIONING_STEPS] == list(range(1, 12))


# ---------------------------------------------------------------------------
# Coordinator: live blockers and fit completions
# ---------------------------------------------------------------------------


def _coordinator(
    port: FakePlatformPort,
) -> tuple[RuntimeCoordinator, ViewportGuard, CaptureService]:
    deadman = FakeDeadmanClient()
    guard = ViewportGuard(
        port, config=CaptureConfig(fit_deadline_s=0.5, fit_readback_interval_s=0.01)
    )
    capture = CaptureService(
        guard, EvidenceRegistry("t"), source_factory=lambda: port.capture_source
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
    )
    coordinator = RuntimeCoordinator(
        authority=authority,
        guard=guard,
        capture=capture,
        registry=EvidenceRegistry("t"),
        workers={},
        config=CoordinatorConfig(),
    )
    return (coordinator, guard, capture)


def _await(condition: Any, timeout_s: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return False


def test_live_blockers_are_keyed_scoped_and_recomputed() -> None:
    port = FakePlatformPort(VirtualClock(), geometry=make_geometry(), focus=False)
    coordinator, _guard, _capture = _coordinator(port)
    coordinator.set_gate_blockers(
        commissioning_blockers(NavigationGates(os_name="test", profile_id="p"))
    )
    blockers = coordinator.blockers()
    codes = [blocker.code for blocker in blockers]
    assert len(codes) == len(set(codes))
    focus = next(blocker for blocker in blockers if blocker.code == "FOCUS")
    assert focus.scope is BlockerScope.RUNTIME
    assert focus.status == "expected", "not frontmost is an instruction, not a failure"
    assert "F1" in focus.remedy
    assert set(coordinator.live_blockers()) == {blocker.describe() for blocker in blockers}
    port.set_focus(True)
    assert "FOCUS" not in [blocker.code for blocker in coordinator.blockers()]


def test_a_gate_blocker_is_never_duplicated_by_a_runtime_one() -> None:
    port = FakePlatformPort(VirtualClock(), geometry=make_geometry())
    coordinator, _guard, _capture = _coordinator(port)
    duplicate = LiveBlocker("FOCUS", BlockerScope.EVIDENCE, "pending", "x", "y", "z")
    coordinator.set_gate_blockers((duplicate, duplicate))
    assert [b.code for b in coordinator.blockers()].count("FOCUS") == 1


def test_a_fit_completion_goes_through_the_coordinator_and_stale_ones_are_ignored() -> None:
    port = FakePlatformPort(VirtualClock(), geometry=make_geometry(size=(1600.0, 900.0)))
    port.settle_reads = 1
    coordinator, guard, _capture = _coordinator(port)
    coordinator.start()
    try:
        coordinator.submit(coordinator.next_intent(IntentType.FIT_VIEWPORT, "gui"))
        assert _await(lambda: guard.fit.phase is not FitPhase.IDLE and guard.fit.phase.terminal)
        assert _await(
            lambda: any(
                name == "viewport.fit" for _t, name, _d in coordinator.events.recent(50)
            )
        )
        assert guard.fit.phase is FitPhase.CANONICAL_VERIFIED
        assert coordinator.stale_fits == 0

        # A completion from a superseded generation must change nothing.
        stale = FitCompletion(
            generation=0,
            fit=ViewportFit(FitPhase.FAILED, 1, 0, 3, (1280.0, 720.0), None, None, None, "old"),
            revision_before=0,
        )
        coordinator._submit_fit_completion(stale)
        assert _await(lambda: coordinator.stale_fits == 1)
        assert guard.fit.phase is FitPhase.CANONICAL_VERIFIED
    finally:
        coordinator.shutdown()


def test_a_second_fit_request_while_one_is_active_is_refused_not_queued() -> None:
    port = FakePlatformPort(VirtualClock(), geometry=make_geometry(size=(1600.0, 900.0)))
    port.settle_reads = 200  # never settles inside the deadline
    coordinator, _guard, _capture = _coordinator(port)
    coordinator.start()
    try:
        coordinator.submit(coordinator.next_intent(IntentType.FIT_VIEWPORT, "gui"))
        assert _await(lambda: coordinator.fit_active, 1.0)
        coordinator.submit(coordinator.next_intent(IntentType.FIT_VIEWPORT, "gui"))
        assert _await(
            lambda: any(
                detail == "already fitting" for _t, _n, detail in coordinator.events.recent(50)
            ),
            2.0,
        )
        assert port.pin_calls <= 2, "the second request did not start another fit"
        # Stop stays responsive while the fit is in flight.
        coordinator.submit(coordinator.next_intent(IntentType.STOP, "gui"))
        assert _await(
            lambda: any(
                name == "intent.stop" for _t, name, _d in coordinator.events.recent(50)
            ),
            1.0,
        )
        assert _await(lambda: not coordinator.fit_active, 3.0)
    finally:
        coordinator.shutdown()


def test_a_clamped_fit_is_adopted_not_failed() -> None:
    port = FakePlatformPort(VirtualClock(), geometry=make_geometry(size=(1600.0, 900.0)))
    port.min_client_logical = (1400.0, 800.0)
    coordinator, guard, _capture = _coordinator(port)
    coordinator.start()
    try:
        coordinator.submit(coordinator.next_intent(IntentType.FIT_VIEWPORT, "gui"))
        assert _await(lambda: guard.fit.phase is not FitPhase.IDLE and guard.fit.phase.terminal)
        assert guard.fit.phase is FitPhase.ACHIEVED_CLAMPED
        assert guard.geometry.valid and not guard.geometry.is_canonical
    finally:
        coordinator.shutdown()


@pytest.mark.parametrize("scale", [1.0, 1.25, 1.5, 2.0])
def test_backing_scale_contracts_hold_at_every_common_dpi(scale: float) -> None:
    """Windows 100/125/150 % and macOS Retina share one contract: logical
    points map to backing pixels by the scale, and the canonical raster is
    independent of both."""
    geometry = make_geometry(size=(1280.0, 720.0), backing_scale=scale)
    assert geometry.client_backing_px == (round(1280 * scale), round(720 * scale))
    assert geometry.canonical_px == (1280, 720)
    transform = geometry.canonical_from_client_logical
    assert transform.is_uniform
