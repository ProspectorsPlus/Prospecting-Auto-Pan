"""One authorization, one mission: what survives an episode and what ends it.

Before the supervisor, every bounded thing the navigator could exhaust ended
the *run*. An arrow lost behind two seconds of foliage past its search budget,
or a recovery ladder that ran out of rungs against a rock, returned ABANDONED
from the worker; the coordinator safe-stopped to IDLE; and continuing cost the
user another physical Ctrl+N. For an all-night unattended macro that is the
same as failing, because nobody is there to press it.

What the supervisor changes is narrow and it is deliberately *not* a weakening
of anything: ABANDONED is a statement about one episode, not about whether the
treasure is still reachable, so it buys a bounded pause with nothing held and a
reacquisition inside the same mode session, the same coordinator generation and
the same physical authorization. No chord is minted, simulated or replayed -
there is no code path here that could.

Everything else still ends the mission. ARRIVED is terminal and happens once.
CANCELLED is a person pressing Stop. FAILED is the worker saying it cannot run.
And the repairs themselves are bounded four ways, because "keep trying" without
bounds is how an unattended macro spends a night walking into a wall.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from typing import Any

import pytest

from prospector_engine.contracts import (
    ArrowObservation,
    DirectionObservation,
    EvidenceProvenance,
    InputKey,
    ModeResultKind,
    NavigationPhase,
)
from prospector_engine.navigation import (
    MissionLimits,
    MissionSupervisor,
    Navigator,
    make_live_worker,
)
from prospector_engine.steering import ArrowFollowerController, SteeringLimits
from tests.test_live_feedback import CAPABLE, Rig, ScriptedPipeline

# ---------------------------------------------------------------------------
# The decision itself
# ---------------------------------------------------------------------------


def test_arrival_is_never_repaired() -> None:
    """The one terminal the whole mission exists to reach. It happens once."""
    supervisor = MissionSupervisor()

    assert supervisor.judge(ModeResultKind.ARRIVED, "arrival confirmed", now_s=0.0) is None
    assert supervisor.repairs == 0


@pytest.mark.parametrize(
    "kind",
    [
        ModeResultKind.CANCELLED,
        ModeResultKind.FAILED,
        ModeResultKind.COMPLETED,
        ModeResultKind.SESSION_COMPLETE,
    ],
)
def test_only_an_abandoned_episode_is_ever_repaired(kind: ModeResultKind) -> None:
    """Stop is a person's decision and FAILED means it cannot run at all."""
    supervisor = MissionSupervisor()

    assert supervisor.judge(kind, "whatever", now_s=0.0) is None


def test_a_lost_arrow_buys_a_bounded_pause_rather_than_ending_the_run() -> None:
    supervisor = MissionSupervisor()

    repair = supervisor.judge(ModeResultKind.ABANDONED, "arrow lost", now_s=0.0)

    assert repair is not None
    assert repair.index == 1
    assert repair.reason == "arrow lost"
    assert 0.0 < repair.deadline_s <= MissionLimits().reacquire_deadline_s
    assert repair.confirm_frames >= 2, "one flicker must not resume a walk"


def test_repairs_are_capped_in_number() -> None:
    supervisor = MissionSupervisor(limits=MissionLimits(max_repairs=2, thrash_window_s=0.0))
    now = 0.0

    for expected in (1, 2):
        repair = supervisor.judge(ModeResultKind.ABANDONED, "lost", now_s=now)
        assert repair is not None and repair.index == expected
        now += 100.0
        supervisor.note_resumed(now_s=now - 99.9)

    assert supervisor.judge(ModeResultKind.ABANDONED, "lost", now_s=now) is None


def test_repairs_are_capped_in_total_time() -> None:
    """A mission that has spent this much of itself waiting is not progressing."""
    limits = MissionLimits(max_repairs=99, repair_budget_s=5.0, thrash_window_s=0.0)
    supervisor = MissionSupervisor(limits=limits)

    assert supervisor.judge(ModeResultKind.ABANDONED, "lost", now_s=0.0) is not None
    supervisor.note_resumed(now_s=6.0)  # six seconds spent repairing

    assert supervisor.judge(ModeResultKind.ABANDONED, "lost", now_s=6.0) is None


def test_repairs_arriving_faster_than_progress_end_the_mission() -> None:
    """The thrash bound. A character wedged against geometry recovers, fails,
    recovers, fails - and without this it would do that for the whole budget."""
    limits = MissionLimits(max_repairs=99, max_repairs_in_window=3, thrash_window_s=30.0)
    supervisor = MissionSupervisor(limits=limits)

    for index in range(3):
        repair = supervisor.judge(ModeResultKind.ABANDONED, "stuck", now_s=float(index))
        assert repair is not None
        supervisor.note_resumed(now_s=float(index) + 0.1)

    assert supervisor.judge(ModeResultKind.ABANDONED, "stuck", now_s=3.0) is None
    # ...and the same mission recovers its appetite once the window has passed.
    assert supervisor.judge(ModeResultKind.ABANDONED, "stuck", now_s=60.0) is not None


# ---------------------------------------------------------------------------
# The whole worker, with the real authority and the real actuator
# ---------------------------------------------------------------------------


@dataclass
class LosableePipeline(ScriptedPipeline):
    """A scripted pipeline whose arrow can be taken away and given back.

    Only perception is scripted - the navigator, the controller, the supervisor,
    the input authority and the actuator underneath it are all the production
    objects, which is the point.
    """

    #: Frames to report a readable arrow for before it vanishes.
    visible_frames: int = 40
    #: ...and how many frames it stays gone for.
    lost_frames: int = 10_000
    #: Everything after that is globally observed again.
    losses: int = 0

    def _arrow_valid(self) -> bool:
        return not (
            self.visible_frames < self.frames_seen <= self.visible_frames + self.lost_frames
        )

    def analyze(self, frame: Any, *, map_id: str, approach_valid: bool) -> Any:
        result = super().analyze(frame, map_id=map_id, approach_valid=approach_valid)
        if self._arrow_valid():
            inputs = result.inputs
            return replace(
                result,
                inputs=replace(
                    inputs,
                    arrow=replace(inputs.arrow, provenance=EvidenceProvenance.GLOBAL),
                ),
            )
        self.losses += 1
        return replace(
            result,
            inputs=replace(
                result.inputs,
                arrow=ArrowObservation(
                    profile_id="test",
                    track_id=None,
                    bbox_px=None,
                    centroid_px=None,
                    tip_px=None,
                    axis_unit_xy=None,
                    confidence=0.0,
                    valid=False,
                    abstain_reason="scripted-loss",
                    provenance=EvidenceProvenance.LOST,
                ),
                direction=DirectionObservation(
                    error_deg=None,
                    confidence=0.0,
                    cue_id="scripted",
                    cue_disagreement_deg=None,
                    valid=False,
                    abstain_reason="scripted-loss",
                    provenance=EvidenceProvenance.LOST,
                ),
            ),
        )


#: A controller whose bounded budgets are short enough to reach a terminal
#: inside a test. Every *rule* is the production one; only the clock the rules
#: are measured against is compressed, which is what lets this exercise the
#: real loop rather than a mock of it.
def _impatient(capabilities: Any) -> Navigator:
    return Navigator(
        capabilities=capabilities,
        follower=ArrowFollowerController(
            limits=SteeringLimits(coast_grace_s=0.05, search_budget_s=0.25),
            response=capabilities.turn_response,
        ),
    )


class RecoveryRig(Rig):
    """The live-feedback rig, with a losable arrow and a bounded supervisor."""

    def __init__(
        self,
        *,
        pipeline: LosableePipeline,
        limits: MissionLimits,
    ) -> None:
        super().__init__()
        self.pipeline = pipeline
        self.context.pipeline = pipeline
        self.worker = make_live_worker(
            lambda: pipeline,
            lambda: CAPABLE,
            prologue=None,
            mission_limits=limits,
            navigator_factory=_impatient,
        )


def _await(predicate: Any, timeout_s: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_a_recoverable_loss_pauses_and_continues_the_same_mission() -> None:
    """The headline behaviour: one authorization survives a lost arrow.

    No second Start, no second chord, no synthetic authorization - the worker
    call that the user's own Ctrl+N started is still the one running.
    """
    pipeline = LosableePipeline(visible_frames=30, lost_frames=45)
    rig = RecoveryRig(
        pipeline=pipeline,
        limits=MissionLimits(max_repairs=4, reacquire_deadline_s=10.0, thrash_window_s=0.0),
    )
    try:
        rig.start()
        assert _await(lambda: pipeline.losses > 0), "the arrow was never taken away"
        # The pause: the character is stopped and nothing is held.
        assert _await(lambda: any("paused" in note for note in rig.statuses)), rig.statuses
        assert _await(lambda: any("reacquired" in note for note in rig.statuses)), rig.statuses

        # Still the same worker call, still walking afterwards.
        assert rig.result is None, "the mission ended instead of continuing"
        assert _await(lambda: rig.pipeline.frames_seen > 120)
    finally:
        rig.stop()

    assert rig.authority.ledger_empty()
    assert not rig.authority.release_uncertain


def test_nothing_is_held_while_the_mission_waits() -> None:
    """A pause with a key still down is not a pause, it is an unsupervised walk."""
    pipeline = LosableePipeline(visible_frames=25, lost_frames=10_000)
    rig = RecoveryRig(
        pipeline=pipeline,
        limits=MissionLimits(max_repairs=2, reacquire_deadline_s=0.6, thrash_window_s=0.0),
    )
    try:
        rig.start()
        assert _await(lambda: any("paused" in note for note in rig.statuses)), rig.statuses
        # Sampled during the wait: forward must have been let go.
        assert _await(lambda: rig.authority.ledger_empty(), timeout_s=5.0)
    finally:
        rig.stop()

    downs, ups = rig.edges(InputKey.W)
    assert downs >= 1, "the character never walked at all"
    assert ups >= 1, "forward was never released for the pause"
    assert rig.authority.ledger_empty()


def test_an_unrecoverable_loss_ends_the_mission_bounded_and_released() -> None:
    """When the world stays unnavigable, the repairs run out and it stops safely."""
    pipeline = LosableePipeline(visible_frames=20, lost_frames=10_000)
    rig = RecoveryRig(
        pipeline=pipeline,
        limits=MissionLimits(
            max_repairs=2,
            reacquire_deadline_s=0.4,
            repair_budget_s=5.0,
            thrash_window_s=0.0,
        ),
    )
    try:
        rig.start()
        assert _await(lambda: rig.result is not None, timeout_s=20.0), "the mission never ended"
    finally:
        rig.stop()

    assert rig.result.kind is ModeResultKind.ABANDONED
    assert rig.authority.ledger_empty()
    assert not rig.authority.release_uncertain
    _downs, ups = rig.edges(InputKey.W)
    assert ups >= 1, "forward was never released"


def test_stop_preempts_a_mission_that_is_waiting_to_reacquire() -> None:
    """Stop works from the supervisor's own state, not only from pursuit."""
    pipeline = LosableePipeline(visible_frames=20, lost_frames=10_000)
    rig = RecoveryRig(
        pipeline=pipeline,
        limits=MissionLimits(max_repairs=8, reacquire_deadline_s=30.0, thrash_window_s=0.0),
    )
    try:
        rig.start()
        assert _await(lambda: any("paused" in note for note in rig.statuses)), rig.statuses
        rig._cancelled.set()
        assert _await(lambda: rig.result is not None, timeout_s=8.0), "Stop did not land"
    finally:
        rig.stop()

    assert rig.authority.ledger_empty()
    assert not rig.authority.release_uncertain


def test_a_new_episode_keeps_what_is_still_true_and_drops_what_is_not() -> None:
    """``begin_episode`` is the contract the supervisor resumes through."""
    navigator = Navigator(capabilities=CAPABLE)
    terrain = navigator.terrain
    navigator._phase = NavigationPhase.RECOVERY
    navigator._arrival_latches = 1

    navigator.begin_episode("arrow lost")

    assert navigator.phase is NavigationPhase.ACQUIRE
    assert navigator.arrival_latches == 0
    assert not navigator.recovery.active
    # Which way already failed is the most valuable thing the failed episode
    # learned; an episode that forgets it walks into the same wall again.
    assert navigator.terrain is terrain
    assert navigator.capabilities is CAPABLE
