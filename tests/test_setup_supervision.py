"""Setup that repairs itself: rolling evidence, checkpoints, bounded retries.

The behaviour under test is the one the real logs argued for. In
``safe-stop-da4088f8.log`` automatic setup fails at 12:45:10 with "the
direction to the arrow never held still long enough to trust" and then reaches
"Setup finished" at 12:45:19 - nine seconds later, on a manual retry, with
nothing about the window, the character or the room having changed. Seven more
traces show the same shape for profile selection. A stage whose verdict flips
on a second look with no new information was not measuring what it claimed to.

So these tests inject exactly that: conditions that are wrong at first and
right shortly afterwards. Under every one of them setup must reach READY with
no second user action. Under a genuinely hard fault it must never report READY,
never retry, and never hold input.

The port is a fake, so no window, no capture, no input and no game is involved.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

import pytest

from prospector_engine.autosetup import (
    AutomaticSetup,
    PerceptionSample,
    ProfileVote,
    SetupConfig,
)
from prospector_engine.contracts import (
    SetupDisposition,
    SetupFailureKind,
    SetupStage,
    disposition_of,
)
from tests.test_autosetup import FAST, Clock, FakePort

#: The supervised config used throughout: the same fast stage budgets, plus
#: retry bounds small enough that an exhausted run finishes inside a test.
SUPERVISED = replace(
    FAST,
    max_recoverable_attempts=4,
    max_environmental_attempts=4,
    max_total_attempts=8,
    supervision_deadline_s=60.0,
    retry_backoff_base_s=0.01,
    retry_backoff_max_s=0.05,
)


@dataclass
class ScriptedPort(FakePort):
    """A Roblox that is wrong for a while and then is not.

    Every field on :class:`FakePort` stays what it was; this only adds the
    ability to *change* one after a given number of observations, which is what
    "transient" means and what the one-shot machine had no way to survive.
    """

    #: Perception samples to abstain on before the arrow becomes readable.
    abstain_samples: int = 0
    #: Perception samples to report wild headings on before they settle.
    jitter_samples: int = 0
    wild_jitter_deg: float = 90.0
    #: Profile votes to report as a dead heat before the winner pulls ahead.
    ambiguous_votes: int = 0
    #: Capture samples to report an error on before the pipeline settles.
    stalled_samples: int = 0
    #: Window identities to hand out in order; the last one repeats.
    identities: tuple[tuple[object, ...], ...] = ()

    samples_seen: int = 0
    votes_seen: int = 0
    captures_seen: int = 0
    windows_seen: int = 0

    def locate_window(self) -> Any:
        from prospector_engine.autosetup import WindowProbe

        if not self.identities:
            return super().locate_window()
        index = min(self.windows_seen, len(self.identities) - 1)
        self.windows_seen += 1
        return WindowProbe(True, "Roblox found", identity=self.identities[index])

    def capture_sample(self) -> Any:
        self.captures_seen += 1
        if self.captures_seen <= self.stalled_samples:
            self.capture_error = "capture is settling"
        else:
            self.capture_error = None
        return super().capture_sample()

    def profile_vote(self) -> ProfileVote:
        self.votes_seen += 1
        self.sequence += 1
        if self.votes_seen <= self.ambiguous_votes:
            # A dead heat: neither profile can be chosen from this frame.
            return ProfileVote(self.sequence, {"green_arrow_v1": 0.5, "yellow_map_v1": 0.5})
        return ProfileVote(self.sequence, {self.winner: 0.9, "yellow_map_v1": 0.1})

    def perception_sample(self) -> PerceptionSample:
        self.samples_seen += 1
        self.sequence += 1
        if self.samples_seen <= self.abstain_samples:
            return PerceptionSample(
                frame_sequence=self.sequence,
                arrow_valid=False,
                direction_valid=False,
                error_deg=None,
                confidence=0.0,
                processed_fps=self.processed_fps,
            )
        jittering = self.samples_seen <= self.abstain_samples + self.jitter_samples
        spread = self.wild_jitter_deg if jittering else self.heading_jitter
        drift = spread * (1 if self.sequence % 2 else -1)
        return PerceptionSample(
            frame_sequence=self.sequence,
            arrow_valid=True,
            direction_valid=True,
            error_deg=self.heading_deg + drift,
            confidence=0.9,
            processed_fps=self.processed_fps,
        )


def _supervised(port: FakePort, config: SetupConfig | None = None) -> AutomaticSetup:
    clock = Clock()
    published: list[Any] = []
    machine = AutomaticSetup(
        port,
        config=config or SUPERVISED,
        publish=published.append,
        now=clock.now,
        sleep=clock.sleep,
        candidates=("green_arrow_v1", "yellow_map_v1"),
    )
    machine.published = published  # type: ignore[attr-defined]
    return machine


# ---------------------------------------------------------------------------
# Rolling evidence: one bad frame is one bad frame
# ---------------------------------------------------------------------------


def test_an_abstention_costs_one_frame_rather_than_the_whole_reference() -> None:
    """The defect the real traces show, reduced to its smallest form.

    Sprinkling abstentions through the reference stage used to clear the
    accumulated readings every time, so the stage could only pass if the
    detector happened to produce an unbroken run. With rolling evidence the
    same stream reaches a verdict.
    """
    port = ScriptedPort(abstain_samples=2)
    machine = _supervised(port)

    progress = machine.run_observation()

    assert progress.stage is SetupStage.READY, progress.detail
    assert machine.reference is not None
    assert machine.reference.stable
    assert progress.pass_index == 1, "it should not have needed a retry at all"


def test_a_steady_heading_read_from_mostly_unreadable_frames_is_refused() -> None:
    """Rolling must not mean lax. The hit rate is the condition that says so.

    Six readings gathered across a window that was mostly unreadable is not a
    steady reference - it is a detector that cannot see - and accepting it
    would make the new rule strictly worse than the one it replaces.
    """
    config = replace(SUPERVISED, reference_min_hit_rate=0.9, max_recoverable_attempts=1)
    port = ScriptedPort(abstain_samples=0)
    # Alternate usable and unusable samples: the headings are perfectly steady
    # and only half the frames carry one.
    original = port.perception_sample

    def alternating() -> PerceptionSample:
        sample = original()
        if port.samples_seen % 2 == 0:
            return replace(sample, arrow_valid=False, direction_valid=False, error_deg=None)
        return sample

    port.perception_sample = alternating  # type: ignore[method-assign]
    machine = _supervised(port, config)

    progress = machine.run_observation()

    assert progress.stage is SetupStage.FAILED
    assert progress.failure is not None
    assert progress.failure.kind is SetupFailureKind.REFERENCE_UNSTABLE


def test_an_unlucky_first_qualification_batch_does_not_fail_the_attempt() -> None:
    """The opening frames are the worst ones, and they used to be the verdict.

    The character is still settling, the detector is mid-acquisition, a cloud
    is crossing. Judging the first ``qualify_frames`` and stopping turned that
    into a failed setup; judging a rolling window lets the very next second of
    frames answer.
    """
    port = ScriptedPort(abstain_samples=6)
    machine = _supervised(port)

    progress = machine.run_observation()

    assert progress.stage is SetupStage.READY, progress.detail
    assert machine.qualification is not None
    assert machine.qualification.shortfalls(SUPERVISED) == ()


def test_reference_jitter_that_settles_reaches_ready_without_another_click() -> None:
    port = ScriptedPort(jitter_samples=8, wild_jitter_deg=80.0)
    machine = _supervised(port)

    progress = machine.run_observation()

    assert progress.stage is SetupStage.READY, progress.detail


# ---------------------------------------------------------------------------
# Retrying, and resuming rather than restarting
# ---------------------------------------------------------------------------


def test_initial_profile_ambiguity_clears_and_setup_reaches_ready() -> None:
    """A map that cannot be told apart at first is waited out, not given up on."""
    port = ScriptedPort(ambiguous_votes=150)
    machine = _supervised(port)

    progress = machine.run_observation()

    assert progress.stage is SetupStage.READY, progress.detail
    assert progress.pass_index > 1, "this should have taken a retry"
    assert port.locked == ["green_arrow_v1"]


def test_a_transient_capture_stall_resumes_from_capture_not_from_the_top() -> None:
    """Still-valid upstream work survives a recoverable failure.

    The window was found and the client was sized; a capture stall says nothing
    about either. Re-running the fit would be both wasted work and a second
    unnecessary resize of somebody's game window.
    """
    port = ScriptedPort(stalled_samples=400)
    machine = _supervised(port, replace(SUPERVISED, max_recoverable_attempts=3))

    progress = machine.run_observation()

    assert progress.stage is SetupStage.FAILED
    assert progress.failure is not None
    assert progress.failure.kind is SetupFailureKind.CAPTURE_STALE
    # One fit, however many capture attempts there were.
    assert port.fits == 1, f"the viewport was re-fitted {port.fits} times"
    resumed = [entry.resumed_from for entry in progress.history[1:]]
    assert resumed and all(stage is SetupStage.RESTART_CAPTURE for stage in resumed), resumed


def test_a_changed_window_identity_restarts_from_the_top() -> None:
    """Dependency-aware invalidation, in the one case that invalidates everything.

    The fit, the capture binding, the locked profile and the reference were all
    measured against a window that is no longer the window in front of us.
    """
    port = ScriptedPort(stalled_samples=400, identities=((1,), (2,), (2,)))
    machine = _supervised(port, replace(SUPERVISED, max_recoverable_attempts=3))

    progress = machine.run_observation()

    assert progress.stage is SetupStage.FAILED
    resumed = [entry.resumed_from for entry in progress.history[1:]]
    assert SetupStage.FIND_ROBLOX in resumed, resumed
    assert port.fits > 1, "a new window must be re-fitted"


# ---------------------------------------------------------------------------
# What is never retried
# ---------------------------------------------------------------------------


def test_the_hard_set_is_exactly_what_it_says_it_is() -> None:
    """A guard on the table itself, because widening it silently is the risk.

    Retry machinery that can quietly reclassify "permission denied" as
    "recoverable" would turn a safety boundary into a spin, and the change
    would look like a one-line edit to a dictionary.
    """
    hard = {kind for kind in SetupFailureKind if disposition_of(kind) is SetupDisposition.HARD}
    assert hard == {
        SetupFailureKind.PERMISSION,
        SetupFailureKind.AMBIGUOUS_WINDOW,
        SetupFailureKind.CANCELLED,
    }


def test_a_permission_failure_is_reported_at_once_and_never_retried() -> None:
    from prospector_engine.autosetup import WindowProbe

    port = ScriptedPort()
    port.window = WindowProbe(False, "not trusted", permission_denied=True)
    machine = _supervised(port)

    progress = machine.run_observation()

    assert progress.stage is SetupStage.FAILED
    assert progress.failure is not None
    assert progress.failure.kind is SetupFailureKind.PERMISSION
    assert progress.failure.disposition is SetupDisposition.HARD
    assert len(progress.history) == 1, "a hard failure must not be retried"
    assert not progress.ok


def test_a_cancellation_is_not_a_failure_and_is_never_retried() -> None:
    """Stop is a person's decision. Retrying it is the worst possible reading."""
    port = ScriptedPort()
    calls = {"n": 0}

    def cancelled() -> bool:
        calls["n"] += 1
        return calls["n"] > 3

    machine = AutomaticSetup(
        port,
        config=SUPERVISED,
        cancelled=cancelled,
        now=Clock().now,
        sleep=lambda _s: None,
        candidates=("green_arrow_v1", "yellow_map_v1"),
    )

    progress = machine.run_observation()

    assert progress.stage is SetupStage.CANCELLED
    assert not progress.ok


def test_a_recoverable_failure_that_never_clears_stops_bounded_and_says_so() -> None:
    port = ScriptedPort(stalled_samples=100_000)
    machine = _supervised(port)

    progress = machine.run_observation()

    assert progress.stage is SetupStage.FAILED
    assert progress.failure is not None
    assert "gave up after" in progress.failure.detail
    assert len(progress.history) <= SUPERVISED.max_total_attempts
    assert not progress.ok


def test_the_attempt_history_is_bounded() -> None:
    """A supervised run that waits all night may not accumulate one entry a minute."""
    config = replace(SUPERVISED, max_total_attempts=40, max_recoverable_attempts=40)
    port = ScriptedPort(stalled_samples=1_000_000)
    machine = _supervised(port, config)

    progress = machine.run_observation()

    assert len(progress.history) <= AutomaticSetup.MAX_HISTORY
    assert progress.history[-1].index > AutomaticSetup.MAX_HISTORY


def test_nothing_is_held_while_the_supervisor_waits() -> None:
    """The whole observation half emits no input, retries included.

    ``release_all_input`` is the only input-shaped call the port exposes, and
    the only stage that makes it is the fit - deliberately, before geometry is
    touched. A backoff must add none of its own and must certainly never leave
    something pressed while it sleeps.
    """
    port = ScriptedPort(stalled_samples=400)
    machine = _supervised(port, replace(SUPERVISED, max_recoverable_attempts=3))

    machine.run_observation()

    assert all("fitting the viewport" in reason for reason in port.released), port.released


# ---------------------------------------------------------------------------
# What the packet says while it is repairing
# ---------------------------------------------------------------------------


def test_a_scheduled_retry_is_visible_and_names_where_it_will_resume() -> None:
    port = ScriptedPort(stalled_samples=400)
    machine = _supervised(port, replace(SUPERVISED, max_recoverable_attempts=3))

    machine.run_observation()

    repairing = [p for p in machine.published if p.repairing]  # type: ignore[attr-defined]
    assert repairing, "no packet ever reported a scheduled retry"
    packet = repairing[0]
    assert packet.resume_from is SetupStage.RESTART_CAPTURE
    assert packet.failure is not None
    assert not packet.ok
    assert "trying again" in packet.detail


def test_a_supervised_run_never_reports_ready_before_it_is() -> None:
    """The one invariant that matters most: no false READY, on any path."""
    for port in (
        ScriptedPort(stalled_samples=400),
        ScriptedPort(ambiguous_votes=1_000_000),
        ScriptedPort(abstain_samples=1_000_000),
    ):
        machine = _supervised(port, replace(SUPERVISED, max_recoverable_attempts=2))
        published: list[Any] = machine.published  # type: ignore[attr-defined]

        progress = machine.run_observation()

        assert progress.stage is SetupStage.FAILED, progress.detail
        assert not any(packet.ok for packet in published)


@pytest.mark.parametrize(
    "kind",
    [
        SetupFailureKind.NO_WINDOW,
        SetupFailureKind.FULLSCREEN,
        SetupFailureKind.PROFILE_AMBIGUOUS,
    ],
)
def test_environmental_conditions_are_waited_for_rather_than_given_up_on(kind) -> None:
    """These become false when a person does something ordinary."""
    assert disposition_of(kind) is SetupDisposition.ENVIRONMENTAL
