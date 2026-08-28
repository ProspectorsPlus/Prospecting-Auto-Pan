"""Input authority, lease/deadman ordering, release floor, and capture.

These are the plan's section 16.2 safety invariants. Every one of them is a
property the application must hold no matter how the threads interleave, so
they are written as deterministic tests with an explicit virtual clock rather
than as timing-dependent sleeps.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from prospector_engine.capture import (
    CaptureConfig,
    CaptureService,
    EvidenceRegistry,
    ViewportGuard,
)
from prospector_engine.contracts import (
    EVIDENCE_MINT_KEY,
    EvidenceToken,
    InputKey,
    MouseButton,
    NavigationApplyStatus,
    NavigationCommand,
    SafetyFaultKind,
    freeze_array,
)
from prospector_engine.input_authority import InputAuthority
from tests.fakes import FakeCaptureBackend, VirtualClock, make_frame, make_rect

ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Lease lifetime
# ---------------------------------------------------------------------------


def test_no_lease_survives_its_deadline(rig: Any) -> None:
    rig.activate()
    lease = rig.session().hold_key(InputKey.W, 200)
    assert lease is not None
    assert rig.authority.held_targets() == ("w",)

    rig.clock.advance(0.25)
    fault = rig.authority.poll_safety()

    assert fault is not None and fault.kind is SafetyFaultKind.LEASE_EXPIRED
    assert rig.authority.ledger_empty()
    assert ("key_up", (rig.port.key_code(InputKey.W),)) in rig.port.ops()


def test_deadman_ack_happens_before_the_down_edge(rig: Any) -> None:
    rig.activate()
    rig.session().hold_key(InputKey.W, 500)

    assert rig.journal.index("deadman:register:w") < rig.journal.index("port:key_down")


def test_a_refused_deadman_registration_prevents_any_edge(rig: Any) -> None:
    rig.activate()
    rig.deadman.refuse_register = True

    assert rig.session().hold_key(InputKey.W, 500) is None
    assert "port:key_down" not in rig.journal
    assert rig.authority.ledger_empty()


def test_renewal_cannot_walk_expiry_past_the_rolling_horizon(rig: Any) -> None:
    rig.activate()
    session = rig.session()
    lease = session.hold_key(InputKey.W, 2000)
    assert lease is not None
    horizon_s = rig.authority.config.max_rolling_lease_horizon_ms / 1000.0

    for _ in range(20):
        rig.clock.advance(0.01)
        assert session.renew(lease, 5000) is True
        held = rig.authority.held_targets()
        assert held == ("w",)
        # Expiry is always "now + capped horizon", never additive.
        remaining = _expiry_of(rig.authority, lease.lease_id) - rig.clock.now()
        assert remaining <= horizon_s + 1e-6


def _expiry_of(authority: InputAuthority, lease_id: int) -> float:
    entry = authority._leases[lease_id]
    return entry.handle.expires_at_s


# ---------------------------------------------------------------------------
# Release floor
# ---------------------------------------------------------------------------


def test_release_all_is_idempotent_under_concurrent_calls(rig: Any) -> None:
    rig.activate()
    session = rig.session()
    session.hold_key(InputKey.W, 1000)
    session.hold_button(MouseButton.LEFT, 1000)

    reports = []
    barrier = threading.Barrier(4)

    def call() -> None:
        barrier.wait()
        reports.append(rig.authority.release_all("concurrent"))

    threads = [threading.Thread(target=call) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(2.0)

    assert len(reports) == 4
    assert all(report.ledger_empty for report in reports)
    assert rig.authority.ledger_empty()
    assert all(report.release_known_safe for report in reports)


def test_a_partial_release_failure_still_covers_the_whole_vocabulary(rig: Any) -> None:
    rig.activate()
    rig.session().hold_key(InputKey.W, 1000)
    rig.port.fail("key_up")  # every key_up now raises

    report = rig.authority.release_all("injected failure")

    # The floor was still attempted for everything, the deadman was still told,
    # and the uncertainty is latched rather than swallowed.
    assert {"mouse:left", "mouse:right", "mouse:middle"} <= set(report.attempted_edges)
    assert report.failures
    assert report.deadman_acknowledged
    assert not report.release_known_safe
    assert rig.authority.release_uncertain


def test_release_uncertainty_blocks_every_new_press_until_recovery(rig: Any) -> None:
    rig.activate()
    rig.port.fail("key_up")
    rig.authority.release_all("injected failure")
    rig.activate(generation=2)

    assert rig.session(2).hold_key(InputKey.W, 500) is None

    rig.port.fail_ops.clear()
    report = rig.authority.recover_release()

    assert report.release_known_safe
    assert not rig.authority.release_uncertain
    rig.activate(generation=3)
    assert rig.session(3).hold_key(InputKey.W, 500) is not None


def test_a_missing_deadman_ack_latches_uncertainty(rig: Any) -> None:
    rig.activate()
    rig.deadman.refuse_release_all = True

    report = rig.authority.release_all("stop")

    assert not report.deadman_acknowledged
    assert not report.release_known_safe
    assert rig.authority.release_uncertain


# ---------------------------------------------------------------------------
# Generations and the edge barrier
# ---------------------------------------------------------------------------


def test_a_stale_generation_can_neither_press_nor_renew(rig: Any) -> None:
    rig.activate(generation=1)
    stale_session = rig.session(1)
    lease = stale_session.hold_key(InputKey.W, 1000)
    assert lease is not None

    rig.authority.release_all("stop")
    rig.activate(generation=2)
    rig.port.transcript.clear()

    assert stale_session.hold_key(InputKey.A, 1000) is None
    assert stale_session.renew(lease, 200) is False
    assert stale_session.pointer_delta(10, 0) is False
    assert stale_session.scroll_lines(-1) is False
    assert rig.port.ops() == []


def test_stop_racing_an_acquisition_leaves_no_edge_after_the_release(rig: Any) -> None:
    """The shared edge barrier orders every down against Stop (plan 16.2).

    The port is instrumented to call ``release_all`` from *inside* the down
    edge, which is the worst possible interleaving: if the ordering were wrong
    a key would remain held after a completed release.
    """
    rig.activate()
    original_key_down = rig.port.raw_key_down
    fired: list[str] = []

    def racing_key_down(code: int) -> None:
        original_key_down(code)
        if not fired:
            fired.append("stop")
            threading.Thread(target=rig.authority.release_all, args=("racing stop",)).start()
            time.sleep(0.05)  # let the racing thread reach the barrier

    rig.port.raw_key_down = racing_key_down  # type: ignore[method-assign]
    rig.session().hold_key(InputKey.W, 1000)

    for _ in range(50):
        if rig.authority.ledger_empty():
            break
        time.sleep(0.02)
    assert rig.authority.ledger_empty()
    ops = rig.port.ops()
    last_key_up = max(i for i, (op, _) in enumerate(ops) if op == "key_up")
    assert not any(op == "key_down" for op, _ in ops[last_key_up:])


def test_pointer_and_scroll_edges_are_refused_after_admission_closes(rig: Any) -> None:
    rig.activate()
    session = rig.session()
    assert session.pointer_delta(5, 0) is True
    rig.authority.release_all("stop")
    rig.port.transcript.clear()

    assert session.pointer_delta(5, 0) is False
    assert session.scroll_lines(-3) is False
    assert session.pointer_move_client((10, 10)) is False
    assert rig.port.ops() == []


def test_pointer_moves_outside_the_client_rect_are_refused(rig: Any) -> None:
    rig.activate()
    assert rig.session().pointer_move_client((5000, 5000)) is False
    assert rig.port.ops() == []


# ---------------------------------------------------------------------------
# Focus policy
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("focus", [False, None])
def test_non_positive_focus_prevents_new_presses(rig: Any, focus: bool | None) -> None:
    rig.activate()
    rig.port.set_focus(focus)
    assert rig.session().hold_key(InputKey.W, 500) is None


@pytest.mark.parametrize("focus", [False, None])
def test_non_positive_focus_releases_held_input(rig: Any, focus: bool | None) -> None:
    rig.activate()
    rig.session().hold_key(InputKey.W, 5000)
    rig.port.set_focus(focus)

    fault = rig.authority.poll_safety()

    assert fault is not None
    assert fault.kind in (SafetyFaultKind.FOCUS_LOST, SafetyFaultKind.FOCUS_UNKNOWN)
    assert rig.authority.ledger_empty()


def test_release_is_never_focus_gated(rig: Any) -> None:
    rig.activate()
    lease = rig.session().hold_key(InputKey.W, 5000)
    assert lease is not None
    rig.port.set_focus(False)
    rig.port.transcript.clear()

    rig.session().release(lease)

    assert ("key_up", (rig.port.key_code(InputKey.W),)) in rig.port.ops()
    assert rig.authority.ledger_empty()


def test_stale_capture_releases_input_and_blocks_new_presses(rig: Any) -> None:
    rig.activate()
    rig.session().hold_key(InputKey.W, 5000)
    rig.set_capture_age(5.0)

    fault = rig.authority.poll_safety()

    assert fault is not None and fault.kind is SafetyFaultKind.CAPTURE_STALE
    assert rig.authority.ledger_empty()
    assert rig.session().hold_key(InputKey.W, 500) is None


def test_outside_an_input_generation_unhealthy_conditions_do_not_fault(rig: Any) -> None:
    """Idle/Shadow must not manufacture a releasing fault (plan 4.5)."""
    rig.port.set_focus(None)
    rig.set_capture_age(None)
    assert rig.authority.poll_safety() is None


# ---------------------------------------------------------------------------
# Evidence tokens
# ---------------------------------------------------------------------------


def _command(
    generation: int, sequence: int, captured_at_s: float, now_s: float
) -> NavigationCommand:
    return NavigationCommand(
        generation=generation,
        source_frame_sequence=sequence,
        source_captured_at_s=captured_at_s,
        forward_axis=1,
        lateral_axis=0,
        jump=False,
        yaw_delta_px=0,
        issued_at_s=now_s,
        valid_until_s=captured_at_s + 0.09,
        reason="test",
    )


def test_a_forged_token_cannot_be_constructed() -> None:
    with pytest.raises(PermissionError):
        EvidenceToken("run", 1, 1, 0.0, 1.0, (0, 0, 0, 0, "d"), object())  # type: ignore[arg-type]


def test_a_token_the_authority_never_issued_is_rejected(rig: Any) -> None:
    rig.activate(generation=1)
    rig.authority.activate_generation(
        1, emits_input=True, requires_capture=True, pinned_rect=rig.port.find_client_rect()
    )
    unregistered = EvidenceToken(
        run_id=rig.authority.run_id,
        generation=1,
        frame_sequence=7,
        captured_at_s=rig.clock.now(),
        duration_ms=5.0,
        viewport_identity=make_rect().identity(),
        _mint_key=EVIDENCE_MINT_KEY,
    )
    command = _command(1, 7, rig.clock.now(), rig.clock.now())

    result = rig.authority.navigation_session(1).apply_navigation_command(command, unregistered)

    assert result.status is NavigationApplyStatus.REJECTED_EVIDENCE
    assert rig.port.ops() == []


def test_replaying_the_same_frame_cannot_extend_a_command_lease(rig: Any) -> None:
    rig.activate(generation=1)
    rig.authority.activate_generation(
        1, emits_input=True, requires_capture=True, pinned_rect=rig.port.find_client_rect()
    )
    registry = EvidenceRegistry(rig.authority.run_id, on_token=rig.authority.register_evidence)
    registry.set_generation(1)
    frame = make_frame(7, captured_at_s=rig.clock.now())
    envelope = registry.envelope_for(frame)
    session = rig.authority.navigation_session(1)
    command = _command(1, 7, frame.captured_at_s, rig.clock.now())

    first = session.apply_navigation_command(command, envelope.evidence_token)
    second = session.apply_navigation_command(command, envelope.evidence_token)

    assert first.applied
    assert second.status is NavigationApplyStatus.REJECTED_EVIDENCE
    assert "strictly newer" in second.detail


def test_an_over_age_frame_cannot_authorize_a_command(rig: Any) -> None:
    rig.activate(generation=1)
    rig.authority.activate_generation(
        1, emits_input=True, requires_capture=True, pinned_rect=rig.port.find_client_rect()
    )
    registry = EvidenceRegistry(rig.authority.run_id, on_token=rig.authority.register_evidence)
    registry.set_generation(1)
    frame = make_frame(1, captured_at_s=rig.clock.now())
    envelope = registry.envelope_for(frame)
    rig.clock.advance(0.5)  # far beyond max_evidence_age_ms
    # A worker could still build a well-formed command; the authority must
    # reject it on the frame's age, independently of what the worker claims.
    command = NavigationCommand(
        generation=1,
        source_frame_sequence=1,
        source_captured_at_s=frame.captured_at_s,
        forward_axis=1,
        lateral_axis=0,
        jump=False,
        yaw_delta_px=0,
        issued_at_s=rig.clock.now(),
        valid_until_s=rig.clock.now() + 0.01,
        reason="stale-source",
    )

    result = rig.authority.navigation_session(1).apply_navigation_command(
        command, envelope.evidence_token
    )

    assert result.status is NavigationApplyStatus.REJECTED_EVIDENCE
    assert rig.port.ops() == []


def test_an_over_budget_capture_duration_cannot_authorize_a_command(rig: Any) -> None:
    rig.activate(generation=1)
    rig.authority.activate_generation(
        1, emits_input=True, requires_capture=True, pinned_rect=rig.port.find_client_rect()
    )
    registry = EvidenceRegistry(rig.authority.run_id, on_token=rig.authority.register_evidence)
    registry.set_generation(1)
    frame = make_frame(1, captured_at_s=rig.clock.now(), duration_ms=500.0)
    envelope = registry.envelope_for(frame)
    command = _command(1, 1, frame.captured_at_s, rig.clock.now())

    result = rig.authority.navigation_session(1).apply_navigation_command(
        command, envelope.evidence_token
    )

    assert result.status is NavigationApplyStatus.REJECTED_EVIDENCE
    assert "duration" in result.detail


def test_navigation_translates_axes_into_leases_and_releases_the_rest(rig: Any) -> None:
    rig.activate(generation=1)
    rig.authority.activate_generation(
        1, emits_input=True, requires_capture=True, pinned_rect=rig.port.find_client_rect()
    )
    registry = EvidenceRegistry(rig.authority.run_id, on_token=rig.authority.register_evidence)
    registry.set_generation(1)
    session = rig.authority.navigation_session(1)

    first_frame = make_frame(1, captured_at_s=rig.clock.now())
    first = registry.envelope_for(first_frame)
    forward_left = NavigationCommand(
        1,
        1,
        first_frame.captured_at_s,
        1,
        -1,
        False,
        0,
        rig.clock.now(),
        first_frame.captured_at_s + 0.09,
        "align",
    )
    assert session.apply_navigation_command(forward_left, first.evidence_token).applied
    assert set(rig.authority.held_targets()) == {"w", "a"}

    second_frame = make_frame(2, captured_at_s=rig.clock.now())
    second = registry.envelope_for(second_frame)
    forward_only = NavigationCommand(
        1,
        2,
        second_frame.captured_at_s,
        1,
        0,
        False,
        0,
        rig.clock.now(),
        second_frame.captured_at_s + 0.09,
        "follow",
    )
    assert session.apply_navigation_command(forward_only, second.evidence_token).applied
    assert rig.authority.held_targets() == ("w",)


# ---------------------------------------------------------------------------
# Frames are genuinely immutable
# ---------------------------------------------------------------------------


def test_captured_frames_are_read_only() -> None:
    frame = make_frame(1)
    with pytest.raises(ValueError):
        frame.bgr[0, 0, 0] = 255


def test_a_writeable_array_is_rejected_by_the_contract() -> None:
    from prospector_engine.contracts import CapturedFrame

    array = np.zeros((4, 4, 3), dtype=np.uint8)
    with pytest.raises(ValueError, match="non-writeable"):
        CapturedFrame(1, 0.0, 0.0, 1.0, make_rect(), array)


def test_freeze_array_copies_when_the_buffer_is_not_ours() -> None:
    base = np.zeros((4, 4, 3), dtype=np.uint8)
    base.flags.writeable = False
    view = base[1:3]
    frozen = freeze_array(view)
    assert not frozen.flags.writeable


# ---------------------------------------------------------------------------
# Capture service
# ---------------------------------------------------------------------------


class _StubPort:
    """Just enough of a port for the capture tests, with a settable rect."""

    def __init__(self) -> None:
        self.rect = make_rect(size_px=(64, 48))
        self.pins = 0

    def find_client_rect(self) -> Any:
        return self.rect

    def pin_client_rect(self, size_px: tuple[int, int]) -> Any:
        from prospector_engine.contracts import PinResult

        self.pins += 1
        return PinResult(True, "pinned", self.rect)


def _capture_rig(backend: FakeCaptureBackend) -> tuple[CaptureService, _StubPort]:
    port = _StubPort()
    guard = ViewportGuard(port, requested_size_px=(64, 48))  # type: ignore[arg-type]
    guard.adopt_current()
    service = CaptureService(
        guard,
        EvidenceRegistry("capture-test"),
        config=CaptureConfig(target_interval_ms=1),
        backend_factory=lambda: backend,
    )
    return service, port


def test_capture_publishes_one_coherent_stamped_frame() -> None:
    backend = FakeCaptureBackend()
    service, _port = _capture_rig(backend)

    envelope = service.capture_once()

    assert envelope is not None
    assert envelope.frame.sequence == 1
    assert envelope.frame.completed_at_s >= envelope.frame.captured_at_s
    assert envelope.evidence_token.frame_sequence == 1
    assert not envelope.frame.bgr.flags.writeable


def test_repeated_identical_grabs_are_flagged_duplicate() -> None:
    backend = FakeCaptureBackend()
    service, _port = _capture_rig(backend)

    first = service.capture_once()
    second = service.capture_once()

    assert first is not None and second is not None
    assert first.frame.duplicate is False
    assert second.frame.duplicate is True
    assert service.duplicate_run() == 1


def test_a_failing_backend_is_recreated_and_reported() -> None:
    backend = FakeCaptureBackend(fail_times=1)
    service, _port = _capture_rig(backend)

    assert service.capture_once() is None
    assert service.last_error() is not None and "backend" in service.last_error()
    assert backend.closed == 1
    assert service.capture_once() is not None


def test_an_invalid_viewport_stops_capture_from_publishing() -> None:
    backend = FakeCaptureBackend()
    service, port = _capture_rig(backend)
    port.rect = make_rect(size_px=(999, 999))  # identity no longer matches the pin

    assert service.capture_once() is None
    assert "viewport" in (service.last_error() or "")


def test_frame_age_is_measured_from_the_start_of_acquisition() -> None:
    clock = VirtualClock()
    frame = make_frame(1, captured_at_s=clock.now(), duration_ms=30.0)
    clock.advance(0.02)
    # Completion was later than the start, but age must not shrink because of it.
    assert frame.age_s(clock.now()) == pytest.approx(0.02)


# ---------------------------------------------------------------------------
# The real deadman helper, end to end, with a file sink
# ---------------------------------------------------------------------------


class DeadmanProcess:
    """Spawns the actual helper with ``TREASURE_DEADMAN_SINK`` so no OS input flows."""

    def __init__(self, sink: Path, poll_ms: int = 10) -> None:
        environment = dict(os.environ)
        environment["TREASURE_DEADMAN_TOKEN"] = "test-token"
        environment["TREASURE_DEADMAN_SINK"] = str(sink)
        environment["TREASURE_DEADMAN_POLL_MS"] = str(poll_ms)
        self.sink = sink
        self.process = subprocess.Popen(
            [sys.executable, str(ROOT / "treasure.py"), "--deadman"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            env=environment,
            text=True,
            bufsize=1,
            cwd=str(ROOT),
        )

    def request(self, payload: dict[str, Any]) -> dict[str, Any]:
        assert self.process.stdin is not None and self.process.stdout is not None
        message = dict(payload)
        message.setdefault("token", "test-token")
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()
        return json.loads(self.process.stdout.readline())  # type: ignore[no-any-return]

    def released(self) -> list[str]:
        if not self.sink.exists():
            return []
        return [line.split()[-1] for line in self.sink.read_text().splitlines() if line.strip()]

    def close(self) -> None:
        if self.process.poll() is None:
            if self.process.stdin is not None:
                self.process.stdin.close()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()


@pytest.fixture
def helper(tmp_path: Path) -> Any:
    process = DeadmanProcess(tmp_path / "released.log")
    yield process
    process.close()


@pytest.mark.slow
def test_the_real_helper_authenticates_and_answers(helper: DeadmanProcess) -> None:
    assert helper.request({"op": "hello"})["ok"] is True
    assert helper.request({"op": "ping"})["ok"] is True


@pytest.mark.slow
def test_the_real_helper_refuses_an_unauthenticated_request(helper: DeadmanProcess) -> None:
    helper.request({"op": "hello"})
    reply = helper.request({"op": "release_all", "token": "wrong"})
    assert reply["ok"] is False and reply["error"] == "bad-token"
    assert helper.released() == []


@pytest.mark.slow
def test_the_real_helper_releases_a_lease_that_expires(helper: DeadmanProcess) -> None:
    helper.request({"op": "hello"})
    helper.request(
        {"op": "register", "gen": 1, "lease_id": 1, "target": "w", "expires_in_ms": 50}
    )

    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and "w" not in helper.released():
        time.sleep(0.02)

    assert "w" in helper.released()


@pytest.mark.slow
def test_the_real_helper_refuses_a_stale_generation(helper: DeadmanProcess) -> None:
    helper.request({"op": "hello"})
    helper.request({"op": "release_all"})  # advances the helper's generation
    reply = helper.request(
        {"op": "register", "gen": 0, "lease_id": 9, "target": "w", "expires_in_ms": 5000}
    )
    assert reply["ok"] is False and reply["error"] == "stale-generation"


@pytest.mark.slow
def test_the_real_helper_releases_everything_on_stdin_eof(tmp_path: Path) -> None:
    process = DeadmanProcess(tmp_path / "eof.log")
    process.request({"op": "hello"})
    assert process.process.stdin is not None
    process.process.stdin.close()
    process.process.wait(timeout=5)

    released = set(process.released())
    assert {key.value for key in InputKey} <= released
    assert {"mouse:left", "mouse:right", "mouse:middle"} <= released


@pytest.mark.slow
def test_the_real_helper_has_no_press_capability(helper: DeadmanProcess) -> None:
    """A down-edge is not merely unused - the protocol has no such operation."""
    helper.request({"op": "hello"})
    for operation in ("press", "key_down", "hold", "button_down"):
        reply = helper.request({"op": operation, "target": "w"})
        assert reply["ok"] is False and reply["error"] == "unknown-op"
    assert helper.released() == []
