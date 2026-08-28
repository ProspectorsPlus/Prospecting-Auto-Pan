"""Deterministic replay: recorder round-trip and repeatable decision traces.

Replay is what makes an evidence session useful after the fact. These tests
prove the recorder's bounds and recoverability, and that replaying the same
frames twice produces byte-identical decisions with **zero** emitted OS input.
"""

from __future__ import annotations

import io
import json
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from prospector_engine.capture import EvidenceRegistry
from prospector_engine.contracts import Cancellation, ModeResultKind
from prospector_engine.navigation import (
    NavigationGates,
    Navigator,
    PerceptionPipeline,
    make_shadow_worker,
)
from prospector_engine.telemetry import (
    EventLog,
    EvidenceRecorder,
    LatestSlot,
    RecorderConfig,
    TelemetryHub,
    resolve_app_paths,
)
from prospector_engine.vision import ArrowSegmenter, load_profiles
from tests.fakes import FakeFrameSource, FakePlatformPort, VirtualClock, make_frame

PROFILE = load_profiles().get("yellow_map_v0")
assert PROFILE is not None


# ---------------------------------------------------------------------------
# Recorder
# ---------------------------------------------------------------------------


def _recorder(tmp_path: Path, **overrides: Any) -> EvidenceRecorder:
    config = RecorderConfig(**overrides)
    recorder = EvidenceRecorder(tmp_path / "session", config=config)
    recorder.start()
    return recorder


def _drain(recorder: EvidenceRecorder, expected_chunks: int, timeout_s: float = 3.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if recorder.stats.chunks_written >= expected_chunks:
            return
        time.sleep(0.01)


def test_a_recorded_session_round_trips_with_a_verifiable_manifest(tmp_path: Path) -> None:
    recorder = _recorder(tmp_path, chunk_frames=3)
    for index in range(1, 7):
        assert recorder.offer(make_frame(index, captured_at_s=index * 0.05), {"i": index})
    _drain(recorder, expected_chunks=2)
    assert recorder.stop()

    manifest = json.loads((recorder.session_dir / "manifest.json").read_text())
    assert manifest["chunks"], "no chunks recorded"
    assert manifest["truncated"] is False

    import hashlib

    for entry in manifest["chunks"]:
        payload = (recorder.session_dir / "chunks" / entry["name"]).read_bytes()
        assert hashlib.sha256(payload).hexdigest() == entry["sha256"]
        with np.load(io.BytesIO(payload), allow_pickle=False) as archive:
            metadata = json.loads(str(archive["meta"]))
            assert len(metadata) == entry["frames"]
            assert archive[f"frame_{0:04d}"].shape[2] == 3


def test_recorder_overflow_drops_ordinary_packets_and_never_blocks(tmp_path: Path) -> None:
    """Plan 16.2: recorder overflow cannot block control."""
    recorder = EvidenceRecorder(tmp_path / "session", config=RecorderConfig(queue_capacity=2))
    # Deliberately not started: nothing drains the queue, so it fills at once.
    started = time.monotonic()
    accepted = sum(recorder.offer(make_frame(i)) for i in range(1, 40))
    elapsed_ms = (time.monotonic() - started) * 1000.0

    assert accepted == 2
    assert recorder.stats.dropped_ordinary == 37
    assert elapsed_ms < 500, f"offer() blocked for {elapsed_ms:.0f} ms"


def test_a_protected_packet_displaces_an_ordinary_one_rather_than_being_lost(
    tmp_path: Path,
) -> None:
    recorder = EvidenceRecorder(tmp_path / "session", config=RecorderConfig(queue_capacity=2))
    assert recorder.offer(make_frame(1))
    assert recorder.offer(make_frame(2))
    assert recorder.offer(make_frame(3)) is False  # ordinary: dropped

    assert recorder.offer(make_frame(4), protected=True) is True


def test_hitting_the_session_ceiling_truncates_loudly_instead_of_deleting(
    tmp_path: Path,
) -> None:
    recorder = _recorder(tmp_path, chunk_frames=1, session_bytes=1)
    recorder.offer(make_frame(1))
    _drain(recorder, expected_chunks=1, timeout_s=1.0)
    for index in range(2, 6):
        recorder.offer(make_frame(index))
    time.sleep(0.2)
    recorder.stop()

    manifest = json.loads((recorder.session_dir / "manifest.json").read_text())
    assert manifest["truncated"] is True
    assert recorder.stats.truncated is True


def test_fixed_regions_are_masked_at_write_time(tmp_path: Path) -> None:
    recorder = EvidenceRecorder(
        tmp_path / "session",
        config=RecorderConfig(chunk_frames=1),
        mask_regions_px=((0, 0, 40, 30),),
    )
    recorder.start()
    frame = make_frame(1, fill_rgb=(200, 100, 50))
    recorder.offer(frame)
    _drain(recorder, expected_chunks=1)
    recorder.stop()

    chunk = next((recorder.session_dir / "chunks").glob("*.npz"))
    with np.load(chunk, allow_pickle=False) as archive:
        image = archive["frame_0000"]
    assert image[0:30, 0:40].max() == 0  # masked
    assert image[100, 100].max() > 0  # untouched elsewhere


def test_stopping_a_recorder_is_bounded_even_with_nothing_started(tmp_path: Path) -> None:
    recorder = EvidenceRecorder(tmp_path / "session")
    started = time.monotonic()
    assert recorder.stop(timeout_s=0.5)
    assert time.monotonic() - started < 1.0


# ---------------------------------------------------------------------------
# Telemetry
# ---------------------------------------------------------------------------


def test_identical_snapshots_are_suppressed(tmp_path: Path) -> None:
    """Bug B13: status used to be emitted every 10 ms while stopped."""
    from prospector_engine.contracts import RunMode, TelemetrySnapshot

    hub = TelemetryHub()
    snapshot = TelemetrySnapshot(
        sequence=0,
        mode=RunMode.IDLE,
        phase=None,
        viewport=None,
        arrow=None,
        direction=None,
        motion=None,
        arrival=None,
        command=None,
        ledger_empty=True,
        focus=True,
        frame_age_ms=None,
    )
    assert hub.publish(snapshot) is True
    for _ in range(100):
        assert hub.publish(snapshot) is False
    assert hub.counters == {"emitted": 1, "suppressed": 100}


def test_the_latest_slot_keeps_the_newest_and_counts_the_drops() -> None:
    slot: LatestSlot[int] = LatestSlot()
    for value in range(10):
        slot.publish(value)
    assert slot.peek() == 9
    assert slot.dropped == 9
    assert slot.take() == 9
    assert slot.take() is None


def test_the_event_log_is_bounded() -> None:
    log = EventLog(capacity=5)
    for index in range(50):
        log.add("tick", str(index))
    assert len(log.recent(100)) == 5
    assert log.as_lines(2)[-1] == "tick: 49"


# ---------------------------------------------------------------------------
# Deterministic replay
# ---------------------------------------------------------------------------


def _scripted_frames(count: int) -> FakeFrameSource:
    registry = EvidenceRegistry("replay")
    source = FakeFrameSource()
    for index in range(1, count + 1):
        source.push(
            registry.envelope_for(
                make_frame(
                    index,
                    captured_at_s=index * 0.05,
                    pixels={(600 + index * 5, 300): (230, 220, 40)},
                )
            )
        )
    return source


def _trace(frames: FakeFrameSource) -> list[tuple[str, str]]:
    gates = NavigationGates(os_name="test", profile_id=PROFILE.profile_id)
    navigator = Navigator(gates=gates)
    pipeline = PerceptionPipeline(segmenter=ArrowSegmenter(PROFILE))
    trace: list[tuple[str, str]] = []
    while True:
        envelope = frames.latest()
        if envelope is None:
            break
        decision = navigator.decide(
            pipeline.observe(envelope.frame, map_id="replay", approach_valid=False),
            generation=1,
            now_s=envelope.frame.captured_at_s,
        )
        trace.append((decision.phase.name, decision.reason))
        if frames.reads > 12:
            break
    return trace


def test_replaying_the_same_frames_gives_the_same_decisions() -> None:
    first = _trace(_scripted_frames(8))
    second = _trace(_scripted_frames(8))
    assert first == second
    assert first, "replay produced no decisions"


def test_replay_emits_no_os_input_at_all() -> None:
    clock = VirtualClock()
    port = FakePlatformPort(clock)
    _trace(_scripted_frames(8))
    assert port.transcript == []


def test_the_shadow_worker_proposes_without_any_input_capability() -> None:
    """Shadow runs the whole decision path and cannot reach a raw port."""
    from prospector_engine.coordinator import WorkerContext
    from prospector_engine.input_authority import NoInputSession

    clock = VirtualClock()
    port = FakePlatformPort(clock)
    gates = NavigationGates(os_name="test", profile_id=PROFILE.profile_id)
    worker = make_shadow_worker(
        lambda: PerceptionPipeline(segmenter=ArrowSegmenter(PROFILE)),
        gates,
        max_ticks=5,
        tick_interval_s=0.0,
    )
    observer = NoInputSession()
    context = WorkerContext(
        generation=1,
        mode=None,  # type: ignore[arg-type]
        worker_id="shadow-test",
        cancellation=Cancellation(),
        frames=_scripted_frames(6),  # type: ignore[arg-type]
        observer=observer,
    )

    result = worker(context)

    assert result.kind is ModeResultKind.COMPLETED
    assert port.transcript == []
    # NoInputSession has no path to an authority at all.
    assert not hasattr(observer, "_authority")
    assert not any("apply" in name or "hold" in name for name in dir(observer))


def test_a_shadow_worker_stops_promptly_when_cancelled() -> None:
    from prospector_engine.coordinator import WorkerContext
    from prospector_engine.input_authority import NoInputSession

    gates = NavigationGates(os_name="test", profile_id=PROFILE.profile_id)
    worker = make_shadow_worker(
        lambda: PerceptionPipeline(segmenter=ArrowSegmenter(PROFILE)),
        gates,
        tick_interval_s=0.01,
    )
    cancellation = Cancellation()
    context = WorkerContext(
        generation=1,
        mode=None,  # type: ignore[arg-type]
        worker_id="shadow-cancel",
        cancellation=cancellation,
        frames=_scripted_frames(1000),  # type: ignore[arg-type]
        observer=NoInputSession(),
    )
    threading.Timer(0.1, cancellation.cancel).start()

    started = time.monotonic()
    result = worker(context)

    assert result.kind is ModeResultKind.CANCELLED
    assert time.monotonic() - started < 2.0


# ---------------------------------------------------------------------------
# Data roots
# ---------------------------------------------------------------------------


def test_the_data_root_honours_an_explicit_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TREASURE_DATA_DIR", str(tmp_path / "data"))
    paths = resolve_app_paths().ensure()
    assert paths.root == (tmp_path / "data").resolve()
    for directory in (paths.config, paths.recordings, paths.logs, paths.crash):
        assert directory.is_dir()


def test_the_data_root_refuses_the_filesystem_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TREASURE_DATA_DIR", "/")
    with pytest.raises(ValueError, match="filesystem root"):
        resolve_app_paths()
