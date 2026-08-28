"""Regressions for the behaviour merged from the friend branch.

Nine commits arrived from ``origin/Treasure`` while this branch had moved the
composition root out of ``treasure_gui.py``. Git saw that conflict as "deleted
here, modified there" for the whole ``build_application`` block, which is the
shape of conflict where a hand resolution quietly drops somebody's work.

Each test below pins one of those changes to behaviour rather than to a line of
code, so a future refactor of the composition root cannot lose it silently the
way this merge nearly did.
"""

from __future__ import annotations

import os
import subprocess
import sys
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
from prospector_engine.geometry import ViewportState
from tests.fakes import (
    FakeCaptureSource,
    FakePlatformPort,
    VirtualClock,
    make_geometry,
)

ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# c55b39a - capture reacquisition permanently dying after one failed attempt
# ---------------------------------------------------------------------------


def test_a_failed_first_acquisition_keeps_being_retried() -> None:
    """``start()`` used to leave no source *and* nothing watching for one.

    ``_reacquire_reason`` read "source is None" as "deliberately stopped", so a
    first attempt that failed stranded the service forever.
    """
    port = FakePlatformPort(VirtualClock(), geometry=make_geometry())
    port.capture_source = FakeCaptureSource()
    guard = ViewportGuard(port)
    guard.connect()
    service = CaptureService(
        guard,
        EvidenceRegistry("test"),
        config=CaptureConfig(supervisor_interval_s=0.02),
        source_factory=port.create_capture_source,
    )
    # No source yet, but start() has been called: the supervisor must consider
    # this a retryable state rather than a deliberate stop.
    service._should_run = True
    service._source = None
    assert service._reacquire_reason() is not None, "a failed acquisition stopped retrying"

    service._should_run = False
    assert service._reacquire_reason() is None, "a deliberate stop kept retrying"


def test_the_supervisor_thread_survives_a_failed_start() -> None:
    port = FakePlatformPort(VirtualClock(), geometry=make_geometry())
    port.capture_source = FakeCaptureSource()
    guard = ViewportGuard(port)
    service = CaptureService(
        guard,
        EvidenceRegistry("test"),
        config=CaptureConfig(supervisor_interval_s=0.02),
        source_factory=port.create_capture_source,
    )
    # Never connected, so the geometry check inside start() fails.
    service.start()
    try:
        assert service._supervisor_thread is not None
        assert service._supervisor_thread.is_alive(), "nothing was left watching"
    finally:
        service.stop()


# ---------------------------------------------------------------------------
# 465faa2 - the supervisor loop notices and heals an UNPINNED viewport
# ---------------------------------------------------------------------------


def test_an_unpinned_viewport_is_a_reason_to_rebuild_the_source() -> None:
    """A running source only exists after a successful adopt.

    So UNPINNED here is one transient bad read having lost the adopted
    identity, and nothing else ever un-poisons it.
    """
    port = FakePlatformPort(VirtualClock(), geometry=make_geometry())
    source = FakeCaptureSource()
    port.capture_source = source
    guard = ViewportGuard(port)
    guard.connect()
    service = CaptureService(
        guard,
        EvidenceRegistry("test"),
        config=CaptureConfig(supervisor_interval_s=0.02),
        source_factory=port.create_capture_source,
    )
    assert service.start()
    try:
        service._guard = _StuckGuard(ViewportState.UNPINNED)  # type: ignore[assignment]
        reason = service._reacquire_reason()
        assert reason is not None and "pin" in reason.lower()
    finally:
        service._guard = guard  # type: ignore[assignment]
        service.stop()


class _StuckGuard:
    """A guard whose ``check()`` reports one fixed state."""

    def __init__(self, state: ViewportState) -> None:
        self._state = state

    def check(self) -> Any:
        class _Geometry:
            state = self._state
            detail = "injected"

        return _Geometry()


# ---------------------------------------------------------------------------
# 3224a91 - InputAuthority.on_safety_fault, which was never connected
# ---------------------------------------------------------------------------


def test_the_safety_fault_sink_is_wired_in_the_composition_root() -> None:
    """The one place that knows *why* the watchdog cancelled a run.

    It was computed and discarded. The merge had to re-apply this into the new
    engine-side composition root rather than the GUI module it arrived in.
    """
    import inspect

    from prospector_engine import application

    source = inspect.getsource(application.build_application)
    assert "on_safety_fault=on_safety_fault" in source, "the safety sink was dropped"
    assert "events=events" in source, "the coordinator lost the shared event log"


def test_a_safety_fault_reaches_the_event_log(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv("TREASURE_DATA_DIR", str(tmp_path / "data"))
    from prospector_engine import application as composition
    from prospector_engine.contracts import SafetyFault, SafetyFaultKind
    from tests.fakes import FakeDeadmanClient

    port = FakePlatformPort(VirtualClock(), geometry=make_geometry())
    port.capture_source = FakeCaptureSource()
    monkeypatch.setattr(composition, "create_platform_port", lambda: port)
    monkeypatch.setattr(composition, "DeadmanClient", lambda **_k: FakeDeadmanClient())

    app = composition.build_application()
    try:
        app.authority._on_safety_fault(
            SafetyFault(
                kind=SafetyFaultKind.FOCUS_LOST,
                generation=3,
                observed_at_s=0.0,
                evidence=("focus=False",),
            )
        )
        recent = app.coordinator.events.recent()
        assert any("safety.fault" in str(row) for row in recent), (
            "the watchdog's reason was discarded again"
        )
    finally:
        app.shutdown()


# ---------------------------------------------------------------------------
# 5076b3f - worker completion reasons
# ---------------------------------------------------------------------------


def test_a_worker_completion_carries_its_reason() -> None:
    import inspect

    from prospector_engine import coordinator

    source = inspect.getsource(coordinator.RuntimeCoordinator)
    assert 'f" - {detail}" if detail else ""' in source, "the completion reason was dropped"


# ---------------------------------------------------------------------------
# af3a039 - TREASURE_VERBOSE mirrors every event to stderr
# ---------------------------------------------------------------------------


def test_treasure_verbose_mirrors_events_to_stderr() -> None:
    environment = dict(os.environ, TREASURE_VERBOSE="1")
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from prospector_engine.telemetry import EventLog\n"
            "EventLog().add('probe.event', 'a detail')\n",
        ],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        env=environment,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "probe.event: a detail" in result.stderr

    quiet = subprocess.run(
        [
            sys.executable,
            "-c",
            "from prospector_engine.telemetry import EventLog\n"
            "EventLog().add('probe.event', 'a detail')\n",
        ],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        env=dict(os.environ, TREASURE_VERBOSE=""),
        timeout=60,
        check=False,
    )
    assert "probe.event" not in quiet.stderr, "verbose mirroring is on by default"


# ---------------------------------------------------------------------------
# 8b91b5f - egg-info untracked, and savePatel.md preserved
# ---------------------------------------------------------------------------


def test_the_friends_notes_and_gitignore_rules_survived_the_merge() -> None:
    assert (ROOT / "savePatel.md").exists(), "savePatel.md was lost in the merge"
    ignore = (ROOT / ".gitignore").read_text()
    assert "egg-info" in ignore, "the egg-info ignore rule was lost"


@pytest.mark.parametrize("name", ["PKG-INFO", "SOURCES.txt", "top_level.txt"])
def test_the_build_metadata_stays_untracked(name: str) -> None:
    tracked = subprocess.run(
        ["git", "ls-files", f"prospector_treasure.egg-info/{name}"],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        check=False,
    )
    assert tracked.stdout.strip() == "", f"{name} is tracked again"


# ---------------------------------------------------------------------------
# Shadow, through the real builder, emits nothing
# ---------------------------------------------------------------------------


def test_shadow_proposes_commands_and_emits_no_os_input(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """WOULD is a proposal from a session with no route to a platform port."""
    monkeypatch.setenv("TREASURE_DATA_DIR", str(tmp_path / "data"))
    from prospector_engine import application as composition
    from prospector_engine.contracts import IntentType, RunMode
    from tests.arrow_fixtures import render_scene
    from tests.fakes import FakeDeadmanClient

    port = FakePlatformPort(VirtualClock(), geometry=make_geometry())
    port.capture_source = FakeCaptureSource(
        frames=[
            render_scene(heading_deg=float(a), terrain="grass", scale_px=100.0, seed=a).bgr
            for a in (0, 20, 40, 60, 80)
        ]
    )
    monkeypatch.setattr(composition, "create_platform_port", lambda: port)
    monkeypatch.setattr(composition, "DeadmanClient", lambda **_k: FakeDeadmanClient())

    app = composition.build_application()
    try:
        assert app.capture.start()
        app.coordinator.start()
        app.coordinator.submit(app.coordinator.next_intent(IntentType.START_SHADOW, "test"))
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            if app.coordinator.mode is RunMode.SHADOW:
                break
            time.sleep(0.02)
        time.sleep(0.6)

        pressed = [row for row in port.journal if "key_down" in row or "button" in row]
        assert pressed == [], f"Shadow emitted input: {pressed}"
        assert app.authority.held_targets() == ()
    finally:
        app.coordinator.shutdown(2.0)
        app.capture.stop(2.0)
        app.shutdown()
