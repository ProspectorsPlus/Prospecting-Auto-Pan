"""The real application, built by the real builder, driven to READY.

This is the test the previous structure could not have: it constructs the
application through ``build_application`` - the same function ``main()`` calls -
and presses Start Navigator. Nothing is injected. There is no ALL_PASSED gate
set, no fabricated calibration and no hand-installed capability. If automatic
setup cannot reach READY on its own, this fails.

What *is* faked is the machine: a platform port that reports a window and hands
back rendered frames instead of a screenshot. That is the boundary the mission
draws - everything above the port is the real thing.

The rendered frames are *wiring* stress, exactly as plan 7.2 permits: nothing
here judges the detector, and no evidence gate is passed on their output. What
is being judged is whether the nine stages, the coordinator, the capture
service and the profile authority actually connect.

No OS input is emitted: the port records every edge it is asked for, and the
observation phase must ask for none.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from prospector_engine.contracts import IntentType, RunMode, SetupStage
from tests.arrow_fixtures import render_scene
from tests.fakes import (
    FakeCaptureSource,
    FakeDeadmanClient,
    FakePlatformPort,
    VirtualClock,
    make_geometry,
)


def _frames(terrain: str = "grass") -> list[Any]:
    """A short loop of rendered arrows, enough for every setup stage."""
    return [
        render_scene(heading_deg=float(angle), terrain=terrain, scale_px=100.0, seed=angle).bgr
        for angle in (0, 15, 30, 45, 60, 75, 90, 105)
    ]


@pytest.fixture
def application(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("TREASURE_DATA_DIR", str(tmp_path / "data"))
    import treasure_gui

    port = FakePlatformPort(VirtualClock(), geometry=make_geometry())
    port.capture_source = FakeCaptureSource(frames=_frames())
    monkeypatch.setattr(treasure_gui, "create_platform_port", lambda: port)
    monkeypatch.setattr(treasure_gui, "DeadmanClient", lambda **_kwargs: FakeDeadmanClient())

    app = treasure_gui.build_application()
    app.port_under_test = port  # type: ignore[attr-defined]
    assert app.capture.start()
    app.coordinator.start()
    yield app
    app.coordinator.shutdown(2.0)
    app.capture.stop(2.0)


def _await(predicate: Any, timeout_s: float = 12.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _start(app: Any) -> None:
    coordinator = app.coordinator
    coordinator.submit(coordinator.next_intent(IntentType.START_NAVIGATOR, "gui"))


# ---------------------------------------------------------------------------
# The whole point
# ---------------------------------------------------------------------------


def test_start_navigator_reaches_ready_with_no_manual_step(application: Any) -> None:
    _start(application)

    assert _await(lambda: application.coordinator.setup_progress.stage is SetupStage.READY), (
        f"setup stopped at {application.coordinator.setup_progress}"
    )
    progress = application.coordinator.setup_progress
    assert progress.failure is None
    assert progress.profile_id, "a profile was locked"


def test_reaching_ready_needs_no_fabricated_gate_or_calibration(application: Any) -> None:
    """The regression this whole pass exists to prevent."""
    _start(application)
    assert _await(lambda: application.coordinator.setup_progress.stage is SetupStage.READY)

    capabilities = application.capabilities
    assert capabilities.reference_ok, "the reference check actually ran"
    # Steering still needs the two live stages, which run under the physical
    # arm inside the live worker. Reaching READY does not grant them.
    assert not capabilities.control_mode_ok
    assert capabilities.turn_response is None
    assert not capabilities.steering_enabled


def test_ready_starts_observing_by_itself(application: Any) -> None:
    _start(application)
    assert _await(lambda: application.coordinator.mode is RunMode.SHADOW), (
        "reaching READY must start observing without a second button"
    )
    assert _await(lambda: application.coordinator.observations.peek() is not None)


def test_automatic_setup_emits_no_input_at_all(application: Any) -> None:
    port = application.port_under_test
    _start(application)
    assert _await(lambda: application.coordinator.setup_progress.stage is SetupStage.READY)
    time.sleep(0.2)

    presses = [entry["op"] for entry in port.transcript if entry["op"].endswith("_down")]
    moves = [
        entry["op"]
        for entry in port.transcript
        if entry["op"] in ("drag_delta", "move_abs_px", "scroll")
    ]
    assert presses == [], f"setup pressed {presses}"
    assert moves == [], f"setup moved the pointer: {moves}"


def test_setup_sizes_the_window_and_rebinds_capture_once(application: Any) -> None:
    port = application.port_under_test
    _start(application)
    assert _await(lambda: application.coordinator.setup_progress.stage is SetupStage.READY)

    assert port.pin_calls >= 1, "the viewport was never fitted"
    assert port.pin_calls <= 2, f"{port.pin_calls} fit attempts - the cap is two"


def test_the_setup_blocker_disappears_once_ready(application: Any) -> None:
    codes = {b.code for b in application.coordinator.blockers()}
    assert "SETUP" in codes

    _start(application)
    assert _await(lambda: application.coordinator.setup_progress.stage is SetupStage.READY)

    assert "SETUP" not in {b.code for b in application.coordinator.blockers()}


# ---------------------------------------------------------------------------
# Bounded failure, through the real builder
# ---------------------------------------------------------------------------


def test_no_window_stops_setup_with_the_action_to_take(application: Any) -> None:
    from prospector_engine.geometry import ViewportGeometry

    application.port_under_test.set_geometry(
        ViewportGeometry.invalid("Roblox window not found")
    )
    _start(application)

    assert _await(lambda: application.coordinator.setup_progress.stage is SetupStage.FAILED)
    failure = application.coordinator.setup_progress.failure
    assert failure is not None
    assert "windowed" in failure.remedy


def test_a_refused_resize_stops_setup_rather_than_carrying_on(application: Any) -> None:
    application.port_under_test.pin_should_fail = True
    _start(application)

    assert _await(lambda: application.coordinator.setup_progress.stage is SetupStage.FAILED)
    failure = application.coordinator.setup_progress.failure
    assert failure is not None
    assert failure.stage is SetupStage.FIT_VIEWPORT
    assert failure.remedy


def test_stop_cancels_a_running_setup(application: Any) -> None:
    coordinator = application.coordinator
    from prospector_engine.geometry import ViewportGeometry

    application.port_under_test.set_geometry(ViewportGeometry.invalid("still looking"))
    _start(application)
    assert _await(lambda: coordinator.setup_progress.running, 3.0)

    coordinator.submit(coordinator.next_intent(IntentType.STOP, "gui"))

    assert _await(
        lambda: coordinator.setup_progress.stage in (SetupStage.CANCELLED, SetupStage.FAILED),
        5.0,
    )
    assert _await(lambda: not coordinator.setup_active, 5.0)


def test_pressing_start_twice_does_not_run_two_setups(application: Any) -> None:
    coordinator = application.coordinator
    _start(application)
    _start(application)
    _start(application)
    assert _await(lambda: coordinator.setup_progress.stage is SetupStage.READY)

    # The fit cap is two attempts per run; three concurrent runs would blow it.
    assert application.port_under_test.pin_calls <= 2


def test_retry_setup_runs_the_same_machine_again(application: Any) -> None:
    coordinator = application.coordinator
    application.port_under_test.pin_should_fail = True
    _start(application)
    assert _await(lambda: coordinator.setup_progress.stage is SetupStage.FAILED)

    application.port_under_test.pin_should_fail = False
    coordinator.submit(coordinator.next_intent(IntentType.RETRY_SETUP, "gui"))

    assert _await(lambda: coordinator.setup_progress.stage is SetupStage.READY)
