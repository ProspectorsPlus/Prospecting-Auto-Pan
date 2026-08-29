"""Does Roblox actually receive our input? One bounded pulse, answered causally.

This runs under the physical arm, before any steering is characterized, and it
is the first thing in the whole system that emits input. It exists because
every stage downstream of it was being asked to prove something it cannot: turn
characterization spent thirty seconds failing to measure a camera that was
never going to move, and reported a timeout, when the answer needed was "the
game is not receiving anything".

**What it does.** With the character stationary it measures the idle motion
noise floor, posts one forward pulse of about a sixth of a second, watches only
frames captured *after* the down edge, releases, and then says which of six
things happened:

=========================  ================================================
``NO_POST``                the post call did not return cleanly
``NO_LOOPBACK``            it returned, and the OS does not believe W is down
``NO_LEASE``               the authority refused - focus, evidence, viewport
``INSUFFICIENT_EVIDENCE``  too few usable frames after the edge to judge
``NO_MOTION``              W was genuinely held and the world did not move
``MOVED``                  the world moved, causally, after the edge
=========================  ================================================

**Three rules it exists to enforce**, each of which was previously violated:

* *The frame that produced a command can never confirm it.* Motion was being
  read off the same pre-command frame the decision was made from, so it
  described the world **before** the edge. Only frames whose
  ``captured_at_s`` is after the recorded down edge are looked at here.
* *``abs(speed) > 0`` is not movement.* Optical flow is never exactly zero.
  The threshold is a multiple of the idle noise measured in this session, on
  this machine, moments earlier - not a constant, and not zero.
* *Release comes before any verdict.* The pulse is released in a ``finally``
  on every path: deadline, cancellation, exception, lost focus and Stop.

Nothing here decides to navigate. It reports, and the caller stops.
"""

from __future__ import annotations

import statistics
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol, runtime_checkable

from prospector_engine.contracts import (
    EvidenceStatus,
    MotionObservation,
    Provenance,
    monotonic_s,
)
from prospector_engine.lifecycle import LifecycleJournal, LifecycleStage

__all__ = [
    "AcceptanceConfig",
    "AcceptanceOutcome",
    "AcceptancePort",
    "AcceptanceResult",
    "ForwardMotionWitness",
    "ForwardRequest",
    "InputAcceptanceProbe",
    "MotionSample",
]


class AcceptanceOutcome(Enum):
    """The six distinguishable answers. Never merged into "it failed"."""

    NOT_RUN = "not_run"
    NO_POST = "no_post"
    NO_LOOPBACK = "no_loopback"
    NO_LEASE = "no_lease"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    NO_MOTION = "no_motion"
    MOVED = "moved"

    @property
    def ok(self) -> bool:
        return self is AcceptanceOutcome.MOVED

    @property
    def remedy(self) -> str:
        """One sentence. A blocked user needs the next action, not a checklist."""
        return {
            AcceptanceOutcome.NOT_RUN: "Press Start Navigator.",
            AcceptanceOutcome.NO_POST: (
                "This process cannot post keyboard events. Grant Accessibility to "
                "whichever application launched it, then restart it."
            ),
            AcceptanceOutcome.NO_LOOPBACK: (
                "The key edge went out and the OS did not register it. Restart the "
                "application; if it persists, Accessibility is granted to a "
                "different copy than the one running."
            ),
            AcceptanceOutcome.NO_LEASE: (
                "The input authority refused the command. Focus Roblox and try again."
            ),
            AcceptanceOutcome.INSUFFICIENT_EVIDENCE: (
                "Too few usable frames to judge. Stand somewhere with visible ground "
                "texture and try again."
            ),
            AcceptanceOutcome.NO_MOTION: (
                "Roblox is not acting on the key. Click into the game window once so "
                "the character has keyboard focus, make sure you are not in a menu or "
                "against a wall, then try again."
            ),
            AcceptanceOutcome.MOVED: "",
        }[self]


@dataclass(frozen=True)
class MotionSample:
    """One frame's motion reading, with the moment it was captured.

    The timestamp is the whole point: it is what makes a frame *after* the
    edge distinguishable from the frame that produced the command.
    """

    frame_sequence: int
    captured_at_s: float
    motion: MotionObservation | None

    @property
    def usable(self) -> bool:
        return (
            self.motion is not None
            and self.motion.valid
            and self.motion.forward_speed_norm is not None
        )

    @property
    def speed_norm(self) -> float:
        motion = self.motion
        if motion is None or motion.forward_speed_norm is None:
            return 0.0
        return float(motion.forward_speed_norm)


@dataclass(frozen=True)
class ForwardRequest:
    """What the authority did with one forward request. Its answer, not ours."""

    applied: bool
    leases_held: tuple[str, ...]
    detail: str
    #: The monotonic instant the down edge was requested. Every frame compared
    #: against this must have been captured strictly later.
    edge_at_s: float

    @property
    def holds_forward(self) -> bool:
        return self.applied and "w" in self.leases_held


@runtime_checkable
class AcceptancePort(Protocol):
    """Everything the probe needs, injected so it is testable without hardware."""

    def next_motion(self, timeout_s: float) -> MotionSample | None:
        """The next frame's motion reading, or ``None`` if none arrived in time."""
        ...

    def request_forward(self, hold_ms: int) -> ForwardRequest:
        """Ask the authority for one bounded forward lease."""
        ...

    def forward_key_state(self) -> bool | None:
        """Whether the OS believes the forward key is down. ``None`` if unknown."""
        ...

    def release_forward(self, reason: str) -> None:
        """Unconditional. Called on every path, including the failures."""
        ...


@dataclass(frozen=True)
class AcceptanceConfig:
    """Bounds and thresholds. Every retry has a cap *and* a monotonic deadline."""

    #: The pulse itself, held by renewal rather than tapped. Long enough for
    #: Roblox to start the walk animation and for several frames to land on
    #: real motion, short enough that a character which does start walking has
    #: gone a few studs.
    #:
    #: The mission specified 120-200 ms. This is longer, on the owner's own
    #: report of what a short press does: *"it taps it so fast and for so short
    #: it either doesn't register or moves like 10 atoms forwards"*. That is
    #: the observation the old ceiling made inevitable - no command could hold
    #: a key past its evidence budget - and the renewal chain is what makes a
    #: press this long expressible at all.
    pulse_ms: int = 320
    #: Idle noise floor: how many usable readings, and how long to wait.
    idle_samples: int = 8
    idle_deadline_s: float = 3.0
    min_idle_samples: int = 4
    #: Post-edge evidence: how many usable readings, and how long to wait.
    post_edge_deadline_s: float = 2.0
    min_post_edge_samples: int = 3
    #: Movement must exceed this multiple of the measured idle noise...
    noise_multiple: float = 4.0
    #: ...and this absolute floor, so a perfectly still idle window cannot make
    #: any flicker count as walking.
    min_speed_norm: float = 0.02
    #: ...and this fraction of the post-edge readings must agree on the sign,
    #: so one frame of noise is not a direction.
    min_sign_agreement: float = 0.66
    #: ...at this mean estimator confidence or better.
    min_confidence: float = 0.15
    frame_timeout_s: float = 0.30
    provenance: Provenance = field(
        default_factory=lambda: Provenance(
            status=EvidenceStatus.PROVISIONAL,
            source="acceptance.AcceptanceConfig",
            note=(
                "chosen starting values, not measurements. The discriminating "
                "number is the idle noise floor, which is measured per session; "
                "min_speed_norm only stops a perfectly still window from making "
                "flicker count as walking."
            ),
        )
    )


@dataclass(frozen=True)
class AcceptanceResult:
    """What happened, with the numbers that justify it."""

    outcome: AcceptanceOutcome
    detail: str
    #: The earliest stage of the forward path that never happened, or ``None``.
    first_missing: LifecycleStage | None = None
    idle_noise_norm: float = 0.0
    idle_samples: int = 0
    threshold_norm: float = 0.0
    moved_speed_norm: float | None = None
    post_edge_samples: int = 0
    sign_agreement: float = 0.0
    mean_confidence: float = 0.0
    loopback: bool | None = None
    leases_held: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.outcome.ok

    def describe(self) -> str:
        if self.outcome is AcceptanceOutcome.MOVED:
            return (
                f"W accepted; movement observed ({self.moved_speed_norm:.3f} vs "
                f"{self.threshold_norm:.3f} threshold over {self.post_edge_samples} "
                "frames captured after the edge)"
            )
        stage = self.first_missing.label if self.first_missing else "unknown stage"
        return f"blocked at {stage}: {self.detail}"

    def summary_line(self) -> str:
        """The one sentence the dashboard shows."""
        return self.describe() if self.ok else f"{self.describe()} {self.outcome.remedy}"


def _noise_floor(samples: Sequence[MotionSample]) -> float:
    """The idle noise level: the largest of the smaller readings.

    A high quantile rather than a mean, because the thing being guarded against
    is exactly the occasional large flicker that a mean hides behind the still
    frames either side of it.
    """
    speeds = sorted(abs(sample.speed_norm) for sample in samples if sample.usable)
    if not speeds:
        return 0.0
    if len(speeds) < 4:
        return speeds[-1]
    index = min(len(speeds) - 1, round(0.9 * (len(speeds) - 1)))
    return speeds[index]


class InputAcceptanceProbe:
    """One bounded forward pulse, and a causal answer about what it did.

    Constructed per attempt. It holds no state between runs, emits exactly one
    forward request, and releases it on every exit path.
    """

    def __init__(
        self,
        port: AcceptancePort,
        journal: LifecycleJournal,
        *,
        config: AcceptanceConfig | None = None,
        cancelled: Callable[[], bool] | None = None,
        on_progress: Callable[[str], None] | None = None,
    ) -> None:
        self._port = port
        self._journal = journal
        self._config = config or AcceptanceConfig()
        self._cancelled = cancelled or (lambda: False)
        self._on_progress = on_progress or (lambda _message: None)

    def run(self) -> AcceptanceResult:
        """Measure, pulse, watch, release, classify. Never raises past here."""
        config = self._config
        started_s = monotonic_s()
        self._on_progress("measuring how still the picture is")
        idle = self._collect(config.idle_samples, config.idle_deadline_s)
        if len(idle) < config.min_idle_samples:
            return AcceptanceResult(
                AcceptanceOutcome.INSUFFICIENT_EVIDENCE,
                f"only {len(idle)} usable motion readings before the pulse; "
                f"{config.min_idle_samples} are needed to know what still looks like",
                idle_samples=len(idle),
            )
        noise = _noise_floor(idle)
        threshold = max(config.noise_multiple * noise, config.min_speed_norm)

        request = ForwardRequest(False, (), "not requested", monotonic_s())
        loopback: bool | None = None
        post: list[MotionSample] = []
        try:
            self._on_progress("testing whether Roblox accepts a key")
            self._journal.note(
                LifecycleStage.W_REQUESTED,
                f"{config.pulse_ms} ms forward pulse",
                pulse_ms=config.pulse_ms,
                idle_noise_norm=round(noise, 5),
                threshold_norm=round(threshold, 5),
            )
            request = self._port.request_forward(config.pulse_ms)
            loopback = self._port.forward_key_state()
            self._journal.note(
                LifecycleStage.OS_EDGE_LOOPBACK_OBSERVED
                if loopback
                else LifecycleStage.OS_EDGE_LOOPBACK_MISSING,
                f"the OS reports forward {'down' if loopback else 'not down'}",
                loopback=loopback,
            )
            if request.holds_forward:
                # Watch for the *whole* pulse, not until the sample count is
                # satisfied. Releasing the moment three frames have arrived
                # makes the press as short as the pipeline is fast, which is
                # exactly the tap this was written to stop being.
                post = self._collect(
                    config.min_post_edge_samples,
                    config.post_edge_deadline_s,
                    after_s=request.edge_at_s,
                    min_duration_s=config.pulse_ms / 1000.0,
                )
        except Exception as exc:  # a probe must never take the worker down
            return AcceptanceResult(
                AcceptanceOutcome.NOT_RUN,
                f"the acceptance probe raised: {exc!r}",
                first_missing=self._journal.first_missing(since_s=started_s),
                idle_noise_norm=noise,
                idle_samples=len(idle),
                threshold_norm=threshold,
            )
        finally:
            # Unconditional, on every path. This is the release floor, not a
            # tidy-up: a probe that returned a verdict while still holding W
            # would be the worst possible thing in this file.
            self._port.release_forward("acceptance-probe-complete")

        return self._classify(
            started_s=started_s,
            request=request,
            loopback=loopback,
            idle=idle,
            post=post,
            noise=noise,
            threshold=threshold,
        )

    # -- collection --------------------------------------------------------
    def _collect(
        self,
        wanted: int,
        deadline_s: float,
        *,
        after_s: float | None = None,
        min_duration_s: float = 0.0,
    ) -> list[MotionSample]:
        """Usable motion readings, bounded by a count *and* a monotonic deadline.

        ``after_s`` is the causality gate: a frame captured before the edge
        describes the world before the edge, and is discarded however good it
        looks.

        ``min_duration_s`` keeps collecting after the count is satisfied. It is
        what holds the key down for the pulse it was asked for instead of for
        however long three frames happen to take.
        """
        started = monotonic_s()
        deadline = started + deadline_s
        collected: list[MotionSample] = []
        first_post_noted = False
        while monotonic_s() < deadline:
            if len(collected) >= wanted and monotonic_s() - started >= min_duration_s:
                break
            if self._cancelled():
                break
            sample = self._port.next_motion(self._config.frame_timeout_s)
            if sample is None:
                continue
            if after_s is not None:
                if sample.captured_at_s <= after_s:
                    continue
                if not first_post_noted:
                    first_post_noted = True
                    self._journal.note(
                        LifecycleStage.POST_EDGE_FRAME_OBSERVED,
                        f"frame {sample.frame_sequence} captured "
                        f"{(sample.captured_at_s - after_s) * 1000.0:.0f} ms after the edge",
                        frame_sequence=sample.frame_sequence,
                        lag_ms=round((sample.captured_at_s - after_s) * 1000.0, 2),
                    )
            if sample.usable:
                collected.append(sample)
        return collected

    # -- classification ----------------------------------------------------
    def _classify(
        self,
        *,
        started_s: float,
        request: ForwardRequest,
        loopback: bool | None,
        idle: Sequence[MotionSample],
        post: Sequence[MotionSample],
        noise: float,
        threshold: float,
    ) -> AcceptanceResult:
        def result(
            outcome: AcceptanceOutcome, detail: str, **extra: object
        ) -> AcceptanceResult:
            return AcceptanceResult(
                outcome,
                detail,
                first_missing=self._journal.first_missing(since_s=started_s),
                idle_noise_norm=round(noise, 5),
                idle_samples=len(idle),
                threshold_norm=round(threshold, 5),
                post_edge_samples=len(post),
                loopback=loopback,
                leases_held=request.leases_held,
                **extra,  # type: ignore[arg-type]
            )

        posted = self._journal.reached(LifecycleStage.OS_EDGE_POSTED, since_s=started_s)
        if not posted:
            return result(
                AcceptanceOutcome.NO_POST,
                f"no key edge reached the OS ({request.detail or 'no detail'})",
            )
        if loopback is False:
            return result(
                AcceptanceOutcome.NO_LOOPBACK,
                "the edge was posted and the OS does not report the key as down",
            )
        if not request.holds_forward:
            return result(
                AcceptanceOutcome.NO_LEASE,
                f"the input authority did not hold forward: {request.detail}",
            )
        if len(post) < self._config.min_post_edge_samples:
            return result(
                AcceptanceOutcome.INSUFFICIENT_EVIDENCE,
                f"only {len(post)} usable frames captured after the edge; "
                f"{self._config.min_post_edge_samples} are needed",
            )

        speeds = [sample.speed_norm for sample in post]
        median = statistics.median(speeds)
        forward = sum(1 for speed in speeds if speed > 0.0)
        agreement = max(forward, len(speeds) - forward) / len(speeds)
        confidence = statistics.fmean(
            sample.motion.confidence for sample in post if sample.motion is not None
        )
        moved = (
            abs(median) > threshold
            and agreement >= self._config.min_sign_agreement
            and confidence >= self._config.min_confidence
        )
        stage = (
            LifecycleStage.GAME_MOTION_CONFIRMED
            if moved
            else LifecycleStage.GAME_MOTION_NOT_CONFIRMED
        )
        self._journal.note(
            stage,
            f"median {median:+.3f} vs threshold {threshold:.3f}",
            median_speed_norm=round(median, 5),
            threshold_norm=round(threshold, 5),
            idle_noise_norm=round(noise, 5),
            sign_agreement=round(agreement, 3),
            mean_confidence=round(confidence, 3),
            frames=len(post),
        )
        detail = (
            f"median {median:+.3f}, threshold {threshold:.3f}, "
            f"agreement {agreement:.0%}, confidence {confidence:.2f}, "
            f"{len(post)} post-edge frames"
        )
        return result(
            AcceptanceOutcome.MOVED if moved else AcceptanceOutcome.NO_MOTION,
            detail,
            moved_speed_norm=round(median, 5),
            sign_agreement=round(agreement, 3),
            mean_confidence=round(confidence, 3),
        )


class ForwardMotionWitness:
    """Is the world moving *because* forward is held, judged causally.

    The same three rules as the probe, applied continuously during navigation
    rather than once during setup. It answers the question the overlay asks on
    every frame - "it says ACTIVE, is anything happening?" - and it answers it
    honestly, which means abstaining far more often than the thing it replaced.

    What it replaced was ``abs(forward_speed_norm) > 0.0`` read off the frame
    that produced the command. Two faults in one line: optical flow is never
    exactly zero, so noise read as walking; and the frame predates the edge, so
    it described the world *before* the key went down.

    ``None`` is a real answer here and is not the same as ``False``. Holding W
    against a wall and having no motion estimate are different facts, and only
    one of them is worth showing a person.
    """

    #: Readings kept for the idle noise floor, and for the held window. Both
    #: bounded: a deque with a maxlen, like every other ring in this codebase.
    IDLE_WINDOW = 64
    HELD_WINDOW = 12

    def __init__(
        self,
        *,
        config: AcceptanceConfig | None = None,
        seed_noise_norm: float | None = None,
    ) -> None:
        self._config = config or AcceptanceConfig()
        self._idle: deque[float] = deque(maxlen=self.IDLE_WINDOW)
        self._held: deque[float] = deque(maxlen=self.HELD_WINDOW)
        self._holding = False
        self._hold_started_at_s: float | None = None
        # The prologue measured this moments ago with the character stationary;
        # starting from it means the first held frames are judged against a real
        # floor instead of against nothing.
        self._seed = seed_noise_norm

    @property
    def threshold_norm(self) -> float:
        """What a reading must beat to count as movement, measured this session."""
        speeds = sorted(self._idle)
        if speeds:
            index = min(len(speeds) - 1, round(0.9 * (len(speeds) - 1)))
            noise = speeds[index]
        else:
            noise = self._seed or 0.0
        return max(self._config.noise_multiple * noise, self._config.min_speed_norm)

    def note_command(self, *, forward_held: bool, at_s: float) -> None:
        """Adopt what the *authority* reports holding, never what was requested.

        The hold's start is recorded on its rising edge only. Restamping it on
        every renewal would make every frame arrive "before the edge" and the
        witness would abstain forever.
        """
        if forward_held and not self._holding:
            self._hold_started_at_s = at_s
            self._held.clear()
        elif not forward_held:
            self._hold_started_at_s = None
            self._held.clear()
        self._holding = forward_held

    def observe(self, sample: MotionSample) -> bool | None:
        """``True`` moved, ``False`` did not, ``None`` cannot say yet."""
        if not self._holding or self._hold_started_at_s is None:
            if sample.usable:
                self._idle.append(abs(sample.speed_norm))
            return None
        if sample.captured_at_s <= self._hold_started_at_s:
            return None  # captured before the edge: it describes the old world
        if not sample.usable:
            return None
        self._held.append(sample.speed_norm)
        if len(self._held) < self._config.min_post_edge_samples:
            return None
        speeds = list(self._held)
        median = statistics.median(speeds)
        forward = sum(1 for speed in speeds if speed > 0.0)
        agreement = max(forward, len(speeds) - forward) / len(speeds)
        return (
            abs(median) > self.threshold_norm and agreement >= self._config.min_sign_agreement
        )
