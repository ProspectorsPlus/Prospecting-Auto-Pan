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
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from prospector_engine.capture import (
    CaptureConfig,
    CaptureService,
    EvidenceRegistry,
    FrameBufferPool,
    LatestFrameSlot,
    ViewportGuard,
)
from prospector_engine.contracts import (
    EVIDENCE_MINT_KEY,
    EvidenceToken,
    InputKey,
    MouseButton,
    NavigationApplyStatus,
    NavigationCommand,
    PerformanceTier,
    SafetyFaultKind,
    freeze_array,
)
from prospector_engine.geometry import ViewportGeometry, ViewportState
from prospector_engine.input_authority import InputAuthority
from tests.fakes import (
    FakeCaptureSource,
    FakePlatformPort,
    VirtualClock,
    make_frame,
    make_geometry,
)

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
        1, emits_input=True, requires_capture=True, pinned_rect=rig.port.window_geometry()
    )
    unregistered = EvidenceToken(
        run_id=rig.authority.run_id,
        generation=1,
        frame_sequence=7,
        captured_at_s=rig.clock.now(),
        duration_ms=5.0,
        viewport_identity=make_geometry().identity(),
        _mint_key=EVIDENCE_MINT_KEY,
    )
    command = _command(1, 7, rig.clock.now(), rig.clock.now())

    result = rig.authority.navigation_session(1).apply_navigation_command(command, unregistered)

    assert result.status is NavigationApplyStatus.REJECTED_EVIDENCE
    assert rig.port.ops() == []


def test_replaying_the_same_frame_cannot_extend_a_command_lease(rig: Any) -> None:
    rig.activate(generation=1)
    rig.authority.activate_generation(
        1, emits_input=True, requires_capture=True, pinned_rect=rig.port.window_geometry()
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
        1, emits_input=True, requires_capture=True, pinned_rect=rig.port.window_geometry()
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
        1, emits_input=True, requires_capture=True, pinned_rect=rig.port.window_geometry()
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
        1, emits_input=True, requires_capture=True, pinned_rect=rig.port.window_geometry()
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
        CapturedFrame(1, 0.0, 0.0, 1.0, make_geometry(), array)


def test_freeze_array_copies_when_the_buffer_is_not_ours() -> None:
    base = np.zeros((4, 4, 3), dtype=np.uint8)
    base.flags.writeable = False
    view = base[1:3]
    frozen = freeze_array(view)
    assert not frozen.flags.writeable


# ---------------------------------------------------------------------------
# Capture service
# ---------------------------------------------------------------------------


def _capture_rig(
    source: FakeCaptureSource, *, size: tuple[float, float] = (64.0, 48.0)
) -> tuple[CaptureService, FakePlatformPort, ViewportGuard]:
    clock = VirtualClock()
    port = FakePlatformPort(
        clock,
        geometry=make_geometry(size=size, canonical_px=(round(size[0]), round(size[1]))),
    )
    guard = ViewportGuard(port, requested_client_logical=size)
    guard.adopt_current()
    service = CaptureService(
        guard,
        EvidenceRegistry("capture-test"),
        config=CaptureConfig(start_tier=PerformanceTier.MINIMUM, max_frame_age_ms=100000),
        source_factory=lambda: source,
    )
    return service, port, guard


def _await_frames(service: CaptureService, count: int, timeout_s: float = 3.0) -> int:
    seen, last = 0, 0
    deadline = time.monotonic() + timeout_s
    while seen < count and time.monotonic() < deadline:
        envelope = service.wait_for_new(last, 0.2)
        if envelope is None:
            continue
        last = envelope.frame.sequence
        seen += 1
    return seen


def test_capture_publishes_one_coherent_stamped_frame() -> None:
    service, _port, _guard = _capture_rig(FakeCaptureSource())
    assert service.start()
    try:
        assert _await_frames(service, 1) == 1
        envelope = service.latest()
        assert envelope is not None
        frame = envelope.frame
        assert frame.sequence >= 1
        assert frame.completed_at_s >= frame.captured_at_s
        assert envelope.evidence_token.frame_sequence == frame.sequence
        assert not frame.bgr.flags.writeable
        assert frame.geometry.state is ViewportState.CANONICAL_VERIFIED
        assert frame.backend == "fake-source"
    finally:
        service.stop()


def test_the_slot_is_event_driven_rather_than_polled() -> None:
    """A consumer wakes on publication instead of sampling on a timer."""
    slot = LatestFrameSlot()
    service, _port, _guard = _capture_rig(FakeCaptureSource())
    del slot

    assert service.start()
    try:
        started = time.monotonic()
        envelope = service.wait_for_new(0, 2.0)
        elapsed = time.monotonic() - started
        assert envelope is not None
        # The MINIMUM tier is 30 Hz, so a polled implementation could not
        # return in materially less than a frame interval on the first call.
        assert elapsed < 1.0
    finally:
        service.stop()


def test_wait_for_new_returns_none_when_nothing_newer_arrives() -> None:
    slot = LatestFrameSlot()
    assert slot.wait_for_new(0, 0.05) is None


def test_source_reported_duplicates_never_reach_a_consumer() -> None:
    """A redelivered surface must not inflate the unique frame rate."""
    source = FakeCaptureSource(content_ids=True)
    service, _port, _guard = _capture_rig(source)
    assert service.start()
    try:
        _await_frames(service, 2)
        before = service.metrics().duplicate_frames
        # Freeze the content id: every later poll is the same surface.
        source._content = 7
        source._content_ids = True
        original_poll = source.poll

        def repeating() -> Any:
            frame = original_poll()
            if frame is None:
                return None
            source._content = 7
            return replace(frame, content_id=7)

        source.poll = repeating  # type: ignore[method-assign]
        time.sleep(0.3)
        after = service.metrics()
        assert after.duplicate_frames > before
    finally:
        service.stop()


def test_a_failing_source_is_reported_and_recovers() -> None:
    source = FakeCaptureSource(fail_times=2)
    service, _port, _guard = _capture_rig(source)
    assert service.start()
    try:
        assert _await_frames(service, 1) == 1
        assert source.polls >= 3  # it failed twice before succeeding
    finally:
        service.stop()


def test_capture_refuses_to_start_without_a_usable_viewport() -> None:
    clock = VirtualClock()
    port = FakePlatformPort(clock, geometry=ViewportGeometry.invalid("no window"))
    guard = ViewportGuard(port)
    service = CaptureService(
        guard, EvidenceRegistry("t"), source_factory=lambda: FakeCaptureSource()
    )

    assert service.start() is False
    assert "invalid" in (service.last_error() or "")


def test_a_delivered_size_mismatch_becomes_a_capture_mismatch() -> None:
    """A resize landing between geometry and delivery must fail closed."""
    source = FakeCaptureSource()
    service, _port, guard = _capture_rig(source)
    assert service.start()
    try:
        _await_frames(service, 1)
        smaller = make_geometry(size=(32.0, 24.0), canonical_px=(32, 24))
        source._geometry = smaller
        time.sleep(0.2)
        assert guard.geometry.state is ViewportState.CAPTURE_MISMATCH
    finally:
        service.stop()


def test_the_buffer_pool_is_bounded_and_returns_buffers() -> None:
    """Memory must stay flat as the frame rate rises."""
    pool = FrameBufferPool(capacity=3)
    first = pool.acquire(8, 8)
    second = pool.acquire(8, 8)
    third = pool.acquire(8, 8)
    assert first is not None and second is not None and third is not None
    assert pool.acquire(8, 8) is None
    assert pool.exhausted == 1

    pool.release(first)
    reused = pool.acquire(8, 8)
    assert reused is first


def test_pooled_buffers_return_when_their_frame_is_released() -> None:
    source = FakeCaptureSource()
    service, _port, _guard = _capture_rig(source)
    assert service.start()
    try:
        _await_frames(service, 20)
        # Far more frames than the pool holds, yet nothing was exhausted.
        assert service.metrics().duplicate_frames >= 0
        assert service._pool.exhausted == 0
        assert service._pool.live <= service._pool.capacity
    finally:
        service.stop()


def test_frame_age_is_measured_from_the_start_of_acquisition() -> None:
    clock = VirtualClock()
    frame = make_frame(1, captured_at_s=clock.now(), duration_ms=30.0)
    clock.advance(0.02)
    assert frame.age_s(clock.now()) == pytest.approx(0.02)


def test_metrics_count_unique_frames_not_loop_iterations() -> None:
    source = FakeCaptureSource()
    service, _port, _guard = _capture_rig(source)
    assert service.start()
    try:
        _await_frames(service, 5)
        metrics = service.metrics()
        assert metrics.backend == "fake-source"
        assert metrics.slot_depth <= 1
        assert metrics.tier is PerformanceTier.MINIMUM
    finally:
        service.stop()


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
