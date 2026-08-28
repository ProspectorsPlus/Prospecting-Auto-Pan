"""Bounded soaks: does anything grow that should not?

A leak is invisible in a unit test and obvious after ten minutes, so these run
the real pipeline against a synthetic source and watch the things that grow:
threads, file descriptors, buffers, queues, the event log, and resident memory.

The default duration is short so the suite stays fast. The full ten-minute
soak the mission asks for is a separate, deliberate run:

    .venv/bin/python treasure.py --soak 10

Every one of these has a hard deadline and joins its threads, because a soak
test that hangs is worse than no soak test.
"""

from __future__ import annotations

import gc
import os
import threading
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
from prospector_engine.contracts import PerformanceTier
from prospector_engine.telemetry import EventLog
from tests.fakes import FakeCaptureSource, FakePlatformPort, VirtualClock, make_geometry

#: Short by default. The mission's ten-minute run is the CLI, not the suite.
SOAK_SECONDS = float(os.environ.get("TREASURE_SOAK_SECONDS", "6"))


def _descriptor_count() -> int:
    """Open file descriptors for this process, or zero where unavailable."""
    try:
        return len(os.listdir(f"/dev/fd/{''}".rstrip("/")))
    except OSError:  # pragma: no cover - not all platforms expose this
        return 0


def _rss_mb() -> float:
    from prospector_engine.capture import _ProcessUsage

    return _ProcessUsage().sample().rss_current_mb


def _service(**config: Any) -> tuple[CaptureService, FakeCaptureSource]:
    clock = VirtualClock()
    port = FakePlatformPort(
        clock, geometry=make_geometry(size=(320.0, 180.0), canonical_px=(320, 180))
    )
    guard = ViewportGuard(port, requested_client_logical=(320.0, 180.0))
    guard.connect()
    source = FakeCaptureSource()
    service = CaptureService(
        guard,
        EvidenceRegistry("soak"),
        config=CaptureConfig(
            start_tier=PerformanceTier.STANDARD, max_frame_age_ms=100000, **config
        ),
        source_factory=lambda: source,
    )
    return (service, source)


@pytest.mark.slow
def test_a_bounded_soak_leaks_no_threads_descriptors_or_buffers() -> None:
    threads_before = threading.active_count()
    descriptors_before = _descriptor_count()
    service, _source = _service()
    assert service.start()
    try:
        deadline = time.monotonic() + SOAK_SECONDS
        last = 0
        consumed = 0
        while time.monotonic() < deadline:
            envelope = service.wait_for_new(last, 0.25)
            if envelope is None:
                continue
            last = envelope.frame.sequence
            consumed += 1
            service.note_perception_ms(0.4)
            service.note_decision_ms(0.05)
        assert consumed > 20, f"only {consumed} frames in {SOAK_SECONDS}s"
        # The pool is bounded by construction; nothing may exceed it.
        assert service._pool.live <= service._pool.capacity
        assert service.metrics().slot_depth <= 1
    finally:
        assert service.stop(2.0), "capture did not shut down cleanly"

    gc.collect()
    assert threading.active_count() <= threads_before + 1
    if descriptors_before:
        assert _descriptor_count() <= descriptors_before + 4


@pytest.mark.slow
def test_resident_memory_does_not_climb_after_warmup() -> None:
    """Provisional target: under 1 MB per minute of slope after warmup."""
    service, _source = _service()
    assert service.start()
    try:
        warmup = time.monotonic() + 1.0
        last = 0
        while time.monotonic() < warmup:
            envelope = service.wait_for_new(last, 0.25)
            if envelope is not None:
                last = envelope.frame.sequence
                service.note_perception_ms(0.4)
        gc.collect()
        started_at = time.monotonic()
        baseline = _rss_mb()

        deadline = started_at + SOAK_SECONDS
        while time.monotonic() < deadline:
            envelope = service.wait_for_new(last, 0.25)
            if envelope is not None:
                last = envelope.frame.sequence
                service.note_perception_ms(0.4)
        gc.collect()
        elapsed_minutes = (time.monotonic() - started_at) / 60.0
        growth = _rss_mb() - baseline
    finally:
        service.stop(2.0)

    if baseline <= 0.0:  # pragma: no cover - platform without current RSS
        pytest.skip("current RSS is not available on this platform")
    slope = growth / max(elapsed_minutes, 1e-6)
    assert slope < 1.0, f"RSS grew {growth:.1f} MB in {elapsed_minutes:.2f} min"


@pytest.mark.slow
def test_repeated_source_replacement_does_not_accumulate_anything() -> None:
    """Reacquisition is the path most likely to strand a thread or a buffer."""
    threads_before = threading.active_count()
    service, _source = _service(supervisor_interval_s=0.02)
    assert service.start()
    try:
        for _ in range(25):
            service.restart_source("soak")
            time.sleep(0.01)
        assert service.metrics().reacquisitions >= 25
        assert service._pool.live <= service._pool.capacity
    finally:
        assert service.stop(2.0)

    gc.collect()
    assert threading.active_count() <= threads_before + 1


def test_the_event_log_never_grows_without_bound() -> None:
    log = EventLog(capacity=64)
    for index in range(20_000):
        log.add("worker.status", f"message {index % 7}")

    assert len(log.events(10_000)) <= 64
    assert len(log.verbatim(10_000)) <= 64 * 4


def test_a_repeated_event_costs_one_line_however_often_it_repeats() -> None:
    log = EventLog()
    for _ in range(5_000):
        log.add("worker.status", "ALIGN: steering disabled")

    lines = log.as_lines(50)
    assert len(lines) == 1
    assert "x5000" in lines[0]


def test_the_latency_windows_stay_bounded() -> None:
    from prospector_engine.capture import LatencyTracker

    tracker = LatencyTracker("soak", window=32)
    for value in range(50_000):
        tracker.record_ms(float(value % 17))

    assert tracker.summary().samples == 32


def test_the_diagnostic_recorder_finalises_and_stays_within_its_quota(
    tmp_path: Path,
) -> None:
    """A bounded recorder must stop accepting rather than fill the disk."""
    import numpy as np

    from prospector_engine.contracts import CapturedFrame, freeze_array
    from prospector_engine.telemetry import EvidenceRecorder, RecorderConfig

    recorder = EvidenceRecorder(
        tmp_path / "session",
        config=RecorderConfig(chunk_frames=4, session_bytes=40_000, queue_capacity=8),
    )
    recorder.start()
    geometry = make_geometry(size=(64.0, 48.0), canonical_px=(64, 48))
    try:
        for sequence in range(400):
            image = np.full((48, 64, 3), sequence % 251, dtype=np.uint8)
            recorder.offer(
                CapturedFrame(
                    sequence=sequence,
                    captured_at_s=sequence * 0.01,
                    completed_at_s=sequence * 0.01 + 0.001,
                    duration_ms=1.0,
                    geometry=geometry,
                    bgr=freeze_array(image),
                )
            )
    finally:
        assert recorder.stop(3.0), "the recorder did not finalise"

    stats = recorder.stats
    manifest = tmp_path / "session" / "manifest.json"
    assert manifest.exists()
    assert stats.truncated or stats.bytes_written <= 40_000
    assert stats.bytes_written < 4_000_000, "the quota was not enforced"
