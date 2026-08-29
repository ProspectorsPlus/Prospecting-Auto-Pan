"""DegreeMonitor tests (D-089).

``DegreeMonitor`` exists because a single cold call to the *shared*
perception pipeline could not reliably reacquire a confident heading once
that pipeline's tracker had gone cold - the first Ctrl+4 press after leaving
Shadow worked off Shadow's own just-stopped, still-warm tracker state, and
the second, minutes later, did not. These tests exercise it with fakes:
a duck-typed pipeline standing in for ``PerceptionPipeline`` (only
``.reference`` and ``.observe(frame, *, map_id, approach_valid)`` matter to
the monitor), so nothing here touches real detection or real threading
except the two tests that specifically exercise the background thread.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import pytest

from prospector_engine.navigation import DegreeMonitor


@dataclass
class _Direction:
    valid: bool
    error_deg: float | None


@dataclass
class _Inputs:
    direction: _Direction


class _FakePipeline:
    """Stands in for PerceptionPipeline: only ``.reference``/``.observe()`` matter."""

    def __init__(self) -> None:
        self.reference: Any = "initial-reference"
        self.calls: list[tuple[Any, str, bool]] = []
        self.script: list[_Inputs] = []
        self.raise_on_observe = False

    def observe(self, frame: Any, *, map_id: str, approach_valid: bool) -> _Inputs:
        self.calls.append((frame, map_id, approach_valid))
        if self.raise_on_observe:
            raise RuntimeError("bad frame")
        index = min(len(self.calls) - 1, len(self.script) - 1)
        return self.script[index]


@dataclass
class _FakeEnvelope:
    frame: Any = "a-captured-frame"


_UNSET = object()


class _FakeFrames:
    def __init__(self, envelope: Any = _UNSET) -> None:
        self.envelope: Any = _FakeEnvelope() if envelope is _UNSET else envelope

    def latest(self) -> Any:
        return self.envelope


def _monitor(
    primary: _FakePipeline, mirror: _FakePipeline, frames: _FakeFrames
) -> DegreeMonitor:
    return DegreeMonitor(frames=frames, primary=primary, mirror=mirror, poll_interval_s=0.01)


def test_current_starts_at_zero_before_anything_is_armed() -> None:
    monitor = _monitor(_FakePipeline(), _FakePipeline(), _FakeFrames())
    assert monitor.current() == 0.0


def test_poll_once_stores_a_valid_reading() -> None:
    primary, mirror = _FakePipeline(), _FakePipeline()
    mirror.script = [_Inputs(_Direction(True, 42.5))]
    monitor = _monitor(primary, mirror, _FakeFrames())

    monitor._poll_once()

    assert monitor.current() == pytest.approx(42.5)


def test_poll_once_copies_the_primarys_reference_onto_the_mirror() -> None:
    primary, mirror = _FakePipeline(), _FakePipeline()
    primary.reference = "distinctive-reference"
    mirror.script = [_Inputs(_Direction(True, 1.0))]
    monitor = _monitor(primary, mirror, _FakeFrames())

    monitor._poll_once()

    assert mirror.reference == "distinctive-reference"


def test_poll_once_leaves_the_stored_angle_alone_on_an_invalid_reading() -> None:
    primary, mirror = _FakePipeline(), _FakePipeline()
    mirror.script = [_Inputs(_Direction(True, 7.0))]
    monitor = _monitor(primary, mirror, _FakeFrames())
    monitor._poll_once()
    assert monitor.current() == pytest.approx(7.0)

    mirror.script = [_Inputs(_Direction(False, None))]
    monitor._poll_once()

    assert monitor.current() == pytest.approx(7.0), (
        "a lost arrow must not erase the last reading"
    )


def test_poll_once_does_nothing_when_no_frame_is_available() -> None:
    primary, mirror = _FakePipeline(), _FakePipeline()
    monitor = _monitor(primary, mirror, _FakeFrames(envelope=None))

    monitor._poll_once()

    assert mirror.calls == []
    assert monitor.current() == 0.0


def test_poll_once_survives_a_pipeline_exception() -> None:
    primary, mirror = _FakePipeline(), _FakePipeline()
    mirror.raise_on_observe = True
    monitor = _monitor(primary, mirror, _FakeFrames())

    monitor._poll_once()  # must not raise

    assert monitor.current() == 0.0


def test_start_or_reset_arms_the_background_poll() -> None:
    primary, mirror = _FakePipeline(), _FakePipeline()
    mirror.script = [_Inputs(_Direction(True, 99.0))]
    monitor = _monitor(primary, mirror, _FakeFrames())

    monitor.start_or_reset()
    deadline = time.monotonic() + 2.0
    while monitor.current() == 0.0 and time.monotonic() < deadline:
        time.sleep(0.01)

    assert monitor.current() == pytest.approx(99.0)
    monitor.stop()


def test_start_or_reset_does_not_spawn_a_second_thread_when_already_running() -> None:
    primary, mirror = _FakePipeline(), _FakePipeline()
    mirror.script = [_Inputs(_Direction(True, 12.0))]
    monitor = _monitor(primary, mirror, _FakeFrames())
    monitor.start_or_reset()
    deadline = time.monotonic() + 2.0
    while monitor.current() == 0.0 and time.monotonic() < deadline:
        time.sleep(0.01)
    first_thread = monitor._thread
    assert first_thread is not None and first_thread.is_alive()

    monitor.start_or_reset()

    assert monitor._thread is first_thread, (
        "a second press must reset the value, not the thread"
    )
    monitor.stop()


def test_stop_ends_the_background_thread() -> None:
    primary, mirror = _FakePipeline(), _FakePipeline()
    mirror.script = [_Inputs(_Direction(True, 5.0))]
    monitor = _monitor(primary, mirror, _FakeFrames())
    monitor.start_or_reset()
    thread = monitor._thread
    assert thread is not None

    monitor.stop()
    thread.join(timeout=2.0)

    assert not thread.is_alive()
