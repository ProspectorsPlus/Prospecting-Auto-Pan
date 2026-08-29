"""The bounded forward pulse, and the six things it can conclude.

Driven through a fake port, so every branch - including the ones that need a
game that ignores keys - is reachable without hardware and without emitting a
single OS edge.
"""

from __future__ import annotations

from collections import deque

import pytest

from prospector_engine.acceptance import (
    AcceptanceConfig,
    AcceptanceOutcome,
    ForwardRequest,
    InputAcceptanceProbe,
    MotionSample,
)
from prospector_engine.contracts import MotionObservation, monotonic_s
from prospector_engine.lifecycle import LifecycleJournal, LifecycleStage


def _motion(speed: float, *, confidence: float = 0.8, valid: bool = True) -> MotionObservation:
    return MotionObservation(
        forward_speed_norm=speed,
        lateral_speed_norm=0.0,
        confidence=confidence,
        inlier_count=40,
        inlier_ratio=0.9,
        spatial_coverage=0.7,
        residual=1.0,
        yaw_contamination=0.0,
        valid=valid,
    )


class FakePort:
    """A game that may or may not act on the key. Emits nothing, ever."""

    def __init__(
        self,
        *,
        idle_speeds: list[float],
        moving_speeds: list[float],
        applied: bool = True,
        leases: tuple[str, ...] = ("w",),
        loopback: bool | None = True,
        posts: bool = True,
        journal: LifecycleJournal | None = None,
    ) -> None:
        self.idle = deque(idle_speeds)
        self.moving = deque(moving_speeds)
        self.applied = applied
        self.leases = leases
        self.loopback = loopback
        self.posts = posts
        self.journal = journal
        self.released: list[str] = []
        self.holding = False
        self._sequence = 0
        self._edge_at_s: float | None = None

    def next_motion(self, timeout_s: float) -> MotionSample | None:
        del timeout_s
        self._sequence += 1
        pool = self.moving if self.holding else self.idle
        if not pool:
            return None
        speed = pool.popleft()
        # Post-edge frames are stamped after the edge; idle frames before it.
        captured = monotonic_s()
        return MotionSample(self._sequence, captured, _motion(speed))

    def request_forward(self, hold_ms: int) -> ForwardRequest:
        del hold_ms
        edge_at = monotonic_s()
        self._edge_at_s = edge_at
        if self.posts and self.journal is not None:
            self.journal.note(LifecycleStage.OS_EDGE_POSTED, "w", target="w", posted=True)
        self.holding = self.applied and "w" in self.leases
        # The real authority notes this; the fake stands in for it exactly.
        if self.holding and self.journal is not None:
            self.journal.note(LifecycleStage.LEASE_HELD, "w", target="w")
        return ForwardRequest(
            applied=self.applied,
            leases_held=self.leases if self.applied else (),
            detail="applied" if self.applied else "refused: focus=False",
            edge_at_s=edge_at,
        )

    def forward_key_state(self) -> bool | None:
        return self.loopback

    def release_forward(self, reason: str) -> None:
        self.holding = False
        self.released.append(reason)


def _probe(
    port: FakePort, journal: LifecycleJournal, **overrides: object
) -> InputAcceptanceProbe:
    port.journal = journal
    defaults: dict[str, object] = {
        "idle_samples": 6,
        "min_idle_samples": 4,
        "min_post_edge_samples": 3,
        "idle_deadline_s": 2.0,
        "post_edge_deadline_s": 2.0,
        # Short by default: the tests about *classification* should not each
        # wait out a real pulse. The one test about the pulse's length asks
        # for a real one.
        "pulse_ms": 1,
    }
    config = AcceptanceConfig(**{**defaults, **overrides})  # type: ignore[arg-type]
    return InputAcceptanceProbe(port, journal, config=config)


STILL = [0.001, -0.002, 0.001, 0.000, -0.001, 0.002]
WALKING = [0.42, 0.44, 0.40, 0.43, 0.45]


def test_a_character_that_walks_is_confirmed() -> None:
    journal = LifecycleJournal()
    port = FakePort(idle_speeds=STILL, moving_speeds=WALKING)
    result = _probe(port, journal).run()
    assert result.outcome is AcceptanceOutcome.MOVED
    assert result.ok
    assert (
        result.moved_speed_norm is not None and result.moved_speed_norm > result.threshold_norm
    )
    assert journal.reached(LifecycleStage.GAME_MOTION_CONFIRMED)
    # Everything up to the release is present; the release stages belong to
    # the authority, which this fake does not stand in for.
    assert journal.first_missing() is LifecycleStage.W_RELEASE_POSTED


def test_flow_noise_is_not_movement() -> None:
    """``abs(speed) > 0`` was the old test, and it called noise walking."""
    journal = LifecycleJournal()
    port = FakePort(idle_speeds=STILL, moving_speeds=[0.002, -0.001, 0.003, 0.001, -0.002])
    result = _probe(port, journal).run()
    assert result.outcome is AcceptanceOutcome.NO_MOTION
    assert journal.reached(LifecycleStage.GAME_MOTION_NOT_CONFIRMED)
    assert "not acting on the key" in result.outcome.remedy


def test_a_noisy_idle_raises_the_bar_rather_than_being_ignored() -> None:
    """The threshold is measured here, this session, not written down."""
    journal = LifecycleJournal()
    noisy = [0.10, -0.12, 0.11, 0.09, -0.10, 0.13]
    port = FakePort(idle_speeds=noisy, moving_speeds=[0.14, 0.13, 0.15, 0.12, 0.14])
    result = _probe(port, journal).run()
    assert result.threshold_norm > 0.4
    assert result.outcome is AcceptanceOutcome.NO_MOTION


def test_no_post_is_distinguished_from_no_motion() -> None:
    journal = LifecycleJournal()
    port = FakePort(idle_speeds=STILL, moving_speeds=WALKING, posts=False)
    result = _probe(port, journal).run()
    assert result.outcome is AcceptanceOutcome.NO_POST
    assert result.first_missing is LifecycleStage.OS_EDGE_POSTED
    assert "Accessibility" in result.outcome.remedy


def test_a_false_loopback_does_not_preempt_the_motion_evidence() -> None:
    """The regression that cost a whole night of runs.

    In ``stop-epoch4-1914449166.jsonl`` the window server answered "W is not
    down" microseconds after the post; ``W`` was then physically held for
    322.7 ms, six frames were captured after the edge, and the run was failed
    on the loopback without any of those six frames being looked at. The
    reading is kept - it is genuinely useful when it is true - and it may no
    longer decide anything.
    """
    journal = LifecycleJournal()
    port = FakePort(idle_speeds=STILL, moving_speeds=WALKING, loopback=False)
    result = _probe(port, journal).run()

    assert result.outcome is AcceptanceOutcome.MOVED
    assert result.loopback is False, "the reading is still reported"
    assert journal.reached(LifecycleStage.OS_EDGE_LOOPBACK_MISSING)


def test_a_false_loopback_with_no_motion_is_reported_as_no_motion() -> None:
    """Posted-but-not-received, not "the OS did not register the key".

    Both facts are in the detail; only one of them is the verdict, because
    only one of them is about Roblox.
    """
    journal = LifecycleJournal()
    port = FakePort(idle_speeds=STILL, moving_speeds=STILL, loopback=False)
    result = _probe(port, journal).run()

    assert result.outcome is AcceptanceOutcome.NO_MOTION
    assert "did not report the key as down" in result.detail
    assert "Roblox is not acting on the key" in result.outcome.remedy


def test_an_unknown_loopback_does_not_fail_the_probe() -> None:
    """A probe that could not run is not evidence that a permission is missing."""
    journal = LifecycleJournal()
    port = FakePort(idle_speeds=STILL, moving_speeds=WALKING, loopback=None)
    result = _probe(port, journal).run()
    assert result.outcome is AcceptanceOutcome.MOVED


def test_a_refused_lease_is_distinguished_from_a_game_that_ignores_the_key() -> None:
    journal = LifecycleJournal()
    port = FakePort(idle_speeds=STILL, moving_speeds=WALKING, applied=False)
    result = _probe(port, journal).run()
    assert result.outcome is AcceptanceOutcome.NO_LEASE
    assert "focus=False" in result.detail


def test_too_few_post_edge_frames_is_not_reported_as_no_motion() -> None:
    journal = LifecycleJournal()
    port = FakePort(idle_speeds=STILL, moving_speeds=[0.42])
    result = _probe(port, journal).run()
    assert result.outcome is AcceptanceOutcome.INSUFFICIENT_EVIDENCE


def test_too_few_idle_frames_never_reaches_the_pulse() -> None:
    """No baseline means no threshold, and no key is pressed to find out."""
    journal = LifecycleJournal()
    port = FakePort(idle_speeds=[0.001, 0.002], moving_speeds=WALKING)
    result = _probe(port, journal).run()
    assert result.outcome is AcceptanceOutcome.INSUFFICIENT_EVIDENCE
    assert not journal.reached(LifecycleStage.W_REQUESTED)
    assert port.released == []  # nothing was ever held


@pytest.mark.parametrize(
    "kwargs",
    [
        {"applied": False},
        {"loopback": False},
        {"posts": False},
        {"moving_speeds": []},
        {"moving_speeds": [0.001, 0.001, 0.002, 0.001]},
    ],
)
def test_forward_is_released_on_every_path(kwargs: dict[str, object]) -> None:
    """Release is not tidy-up. A verdict returned while holding W is the worst
    thing this module could do, so it is asserted on every failure shape."""
    journal = LifecycleJournal()
    defaults: dict[str, object] = {"idle_speeds": list(STILL), "moving_speeds": list(WALKING)}
    port = FakePort(**{**defaults, **kwargs})  # type: ignore[arg-type]
    _probe(port, journal).run()
    assert port.released, "the probe returned without releasing"
    assert port.holding is False


def test_a_port_that_raises_is_released_and_reported() -> None:
    journal = LifecycleJournal()
    port = FakePort(idle_speeds=STILL, moving_speeds=WALKING)

    def boom(_hold_ms: int) -> ForwardRequest:
        raise RuntimeError("the port exploded")

    port.request_forward = boom  # type: ignore[assignment]
    result = _probe(port, journal).run()
    assert result.outcome is AcceptanceOutcome.NOT_RUN
    assert "exploded" in result.detail
    assert port.released


def test_frames_captured_before_the_edge_are_never_used() -> None:
    """The frame that produced a command can never confirm it."""
    journal = LifecycleJournal()

    class StaleFramePort(FakePort):
        def next_motion(self, timeout_s: float) -> MotionSample | None:
            sample = super().next_motion(timeout_s)
            if sample is None or not self.holding:
                return sample
            # A frame captured a second *before* the edge, showing fast motion.
            return MotionSample(sample.frame_sequence, sample.captured_at_s - 1.0, _motion(9.9))

    port = StaleFramePort(idle_speeds=STILL, moving_speeds=WALKING)
    result = _probe(port, journal).run()
    assert result.outcome is AcceptanceOutcome.INSUFFICIENT_EVIDENCE
    assert not journal.reached(LifecycleStage.POST_EDGE_FRAME_OBSERVED)
    assert not journal.reached(LifecycleStage.GAME_MOTION_CONFIRMED)


def test_cancellation_stops_collecting_and_still_releases() -> None:
    journal = LifecycleJournal()
    port = FakePort(idle_speeds=STILL, moving_speeds=WALKING)
    probe = InputAcceptanceProbe(
        port,
        journal,
        config=AcceptanceConfig(idle_samples=6, min_idle_samples=4),
        cancelled=lambda: True,
    )
    result = probe.run()
    assert result.outcome is AcceptanceOutcome.INSUFFICIENT_EVIDENCE
    assert port.holding is False


def test_the_key_is_held_for_the_whole_pulse_not_until_the_frames_arrive() -> None:
    """A press as short as the pipeline is fast is the tap this exists to stop.

    Collecting until the sample count is satisfied made the hold's length a
    property of how quickly frames happened to arrive - a few milliseconds on
    a fast machine. The pulse duration is the bound that matters, because it
    is the one the game has to act on.
    """
    journal = LifecycleJournal()
    port = FakePort(idle_speeds=list(STILL) * 4, moving_speeds=list(WALKING) * 20)
    probe = _probe(port, journal, pulse_ms=250, post_edge_deadline_s=3.0)
    started = monotonic_s()
    result = probe.run()
    elapsed_s = monotonic_s() - started
    assert result.outcome is AcceptanceOutcome.MOVED
    assert elapsed_s >= 0.25, f"the pulse lasted {elapsed_s * 1000:.0f} ms"
    # ...and it did not simply run to the deadline either.
    assert elapsed_s < 3.0
    assert port.released, "forward was still held"
