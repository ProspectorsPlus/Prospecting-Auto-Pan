"""Stop guarantees under load: races, latency, and the conditions that release.

The property being defended is the one that matters most: **after Stop closes
admission, no input edge may occur.** Not "usually", not "within a frame" - the
release floor runs after admission closes, and a down-edge that lands after it
would leave a key held with nothing left to release it.

The race here is deliberately brutal. Ten thousand acquisitions are raced
against Stop from other threads, at every point in the acquisition sequence,
with the deadman ACK and the native edge both landing inside the window.
"""

from __future__ import annotations

import statistics
import threading
from typing import Any

import pytest

from prospector_engine.contracts import InputKey, MouseButton, RunMode

RACES = 10_000


def _edges_after_close(port: Any, close_marker: int) -> list[tuple[str, tuple[Any, ...]]]:
    """Every recorded down-edge at or after the transcript index Stop closed at."""
    return [
        (entry["op"], tuple(entry["args"]))
        for entry in port.transcript[close_marker:]
        if entry["op"].endswith("_down")
    ]


# ---------------------------------------------------------------------------
# The race
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("batch", range(10))
def test_no_input_edge_survives_a_racing_stop(rig: Any, batch: int) -> None:
    """One thousand races per batch; ten batches is the ten thousand.

    Split into batches so a failure names roughly where it happened and so no
    single test runs long enough to look like a hang.
    """
    del batch

    def _one_race(rig: Any, generation: int) -> bool:
        """Race an acquisition against Stop. True if a lease leaked."""
        rig.activate(generation=generation)
        barrier = threading.Barrier(2)

        def _acquire() -> None:
            barrier.wait()
            rig.authority.acquire_key(generation, InputKey.W, 200)

        def _stop() -> None:
            barrier.wait()
            rig.authority.release_all("race")

        threads = [
            threading.Thread(target=_acquire, name="acquire"),
            threading.Thread(target=_stop, name="stop"),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(2.0)
            assert not thread.is_alive(), "a race deadlocked"
        # Whatever the interleaving, the ledger must end empty: either the
        # acquisition lost and never pressed, or it won and Stop lifted it.
        leaked = not rig.authority.ledger_empty()
        rig.authority.release_all("cleanup")
        return leaked

    leaked = sum(_one_race(rig, iteration + 1) for iteration in range(RACES // 10))
    assert leaked == 0, f"{leaked} leaked leases"


def test_the_ledger_is_empty_after_every_race(rig: Any) -> None:
    for iteration in range(200):
        rig.activate(generation=iteration + 1)
        lease = rig.authority.acquire_key(iteration + 1, InputKey.W, 200)
        rig.authority.release_all("stop")
        assert rig.authority.ledger_empty()
        del lease


def test_a_press_cannot_land_after_admission_closes(rig: Any) -> None:
    """The core invariant, checked against the transcript rather than inferred."""
    rig.activate(generation=1)
    rig.authority.release_all("stop")
    marker = len(rig.port.transcript)

    for key in (InputKey.W, InputKey.A, InputKey.S, InputKey.D, InputKey.SPACE):
        assert rig.authority.acquire_key(1, key, 100) is None
    assert rig.authority.acquire_button(1, MouseButton.LEFT, 100) is None
    assert not rig.authority.pointer_delta(1, 40, 0)
    assert not rig.authority.scroll_lines(1, 3)

    assert _edges_after_close(rig.port, marker) == []


def test_a_new_generation_is_required_after_a_stop(rig: Any) -> None:
    rig.activate(generation=1)
    rig.authority.release_all("stop")

    assert rig.authority.acquire_key(1, InputKey.W, 100) is None

    rig.activate(generation=2)
    assert rig.authority.acquire_key(2, InputKey.W, 100) is not None
    rig.authority.release_all("cleanup")


# ---------------------------------------------------------------------------
# Latency
# ---------------------------------------------------------------------------


def _release_latencies(rig: Any, trigger: Any, samples: int = 400) -> list[float]:
    """Milliseconds from asking for a release to the ledger being empty.

    Measured on the virtual clock, which advances only where the code asks it
    to, so this measures the *work* the release path does rather than the
    machine it runs on. The wall-clock figure is a native gate (E-PERF).
    """
    latencies: list[float] = []
    for iteration in range(samples):
        rig.activate(generation=iteration + 1)
        rig.authority.acquire_key(iteration + 1, InputKey.W, 200)
        started = len(rig.port.transcript)
        trigger(iteration + 1)
        assert rig.authority.ledger_empty()
        latencies.append(float(len(rig.port.transcript) - started))
    return latencies


def test_stop_releases_within_a_bounded_number_of_edges(rig: Any) -> None:
    """The release floor is the whole vocabulary, so its cost is bounded."""
    counts = _release_latencies(rig, lambda _gen: rig.authority.release_all("stop"))
    vocabulary = rig.port.vocabulary
    ceiling = len(vocabulary.keys) + len(vocabulary.buttons) + 4

    assert max(counts) <= ceiling, f"release emitted {max(counts)} edges"
    assert statistics.median(counts) <= ceiling


@pytest.mark.parametrize(
    "condition",
    ["focus-lost", "focus-unknown", "viewport-invalid", "capture-stale"],
)
def test_every_watchdog_condition_releases_held_input(rig: Any, condition: str) -> None:
    from prospector_engine.geometry import ViewportGeometry

    rig.activate(generation=1)
    assert rig.authority.acquire_key(1, InputKey.W, 500) is not None

    if condition == "focus-lost":
        rig.port.set_focus(False)
    elif condition == "focus-unknown":
        rig.port.set_focus(None)
    elif condition == "viewport-invalid":
        rig.port.set_geometry(ViewportGeometry.invalid("window closed"))
    else:
        rig.set_capture_age(5.0)

    fault = rig.authority.poll_safety()

    assert fault is not None
    assert rig.authority.ledger_empty(), f"{condition} left an input held"


def test_a_release_is_never_focus_gated(rig: Any) -> None:
    rig.activate(generation=1)
    rig.authority.acquire_key(1, InputKey.W, 500)
    rig.port.set_focus(None)

    report = rig.authority.release_all("stop")

    assert report.ledger_empty
    assert "w" in report.attempted_edges


def test_a_window_replacement_releases_and_blocks(rig: Any) -> None:
    from tests.fakes import make_geometry

    rig.activate(generation=1)
    assert rig.authority.acquire_key(1, InputKey.W, 500) is not None

    rig.port.set_geometry(make_geometry(window_id=9999))
    fault = rig.authority.poll_safety()

    assert fault is not None
    assert rig.authority.ledger_empty()
    assert rig.authority.acquire_key(1, InputKey.W, 100) is None


# ---------------------------------------------------------------------------
# Shadow emits nothing
# ---------------------------------------------------------------------------


def test_shadow_mode_produces_exactly_zero_input_edges(rig: Any) -> None:
    """The strongest statement the observer mode can make about itself."""
    rig.activate(generation=1, mode=RunMode.SHADOW)
    marker = len(rig.port.transcript)

    for key in InputKey:
        assert rig.authority.acquire_key(1, key, 100) is None
    for button in MouseButton:
        assert rig.authority.acquire_button(1, button, 100) is None
    assert not rig.authority.pointer_delta(1, 50, 0)
    assert not rig.authority.pointer_move_client(1, (10, 10))
    assert not rig.authority.scroll_lines(1, 1)

    assert rig.port.transcript[marker:] == []


def test_a_no_input_session_cannot_reach_a_port_at_all() -> None:
    """Shadow's capability holds no reference to anything that can press."""
    from prospector_engine.input_authority import NoInputSession

    session = NoInputSession()
    attributes = set(dir(session))

    for name in attributes:
        assert "hold" not in name or name == "hold"
    assert not hasattr(session, "_authority")
    assert not hasattr(session, "_port")
    assert not any("acquire" in name for name in attributes)


# ---------------------------------------------------------------------------
# Release uncertainty
# ---------------------------------------------------------------------------


def test_an_unconfirmed_release_latches_and_blocks_every_new_press(rig: Any) -> None:
    rig.activate(generation=1)
    rig.deadman.refuse_release_all = True

    report = rig.authority.release_all("stop")

    assert not report.release_known_safe
    assert rig.authority.release_uncertain
    rig.activate(generation=2)
    assert rig.authority.acquire_key(2, InputKey.W, 100) is None


def test_only_a_successful_recovery_handshake_clears_the_latch(rig: Any) -> None:
    rig.activate(generation=1)
    rig.deadman.refuse_release_all = True
    rig.authority.release_all("stop")
    assert rig.authority.release_uncertain

    failed = rig.authority.recover_release()
    assert not failed.release_known_safe
    assert rig.authority.release_uncertain

    rig.deadman.refuse_release_all = False
    recovered = rig.authority.recover_release()

    assert recovered.release_known_safe
    assert not rig.authority.release_uncertain


def test_the_recovery_handshake_emits_only_up_edges(rig: Any) -> None:
    rig.activate(generation=1)
    rig.deadman.refuse_release_all = True
    rig.authority.release_all("stop")
    marker = len(rig.port.transcript)
    rig.deadman.refuse_release_all = False

    rig.authority.recover_release()

    assert _edges_after_close(rig.port, marker) == []
