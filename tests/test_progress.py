"""The conservative progress guard.

What it is allowed to conclude is deliberately narrow. It may say "forward is
being held and nothing is moving, release it"; it may never say "there is a
wall to the left, go right". Obstacle mapping is a later phase and this pass
builds only the input side of that boundary.

Three properties are asserted throughout:

* elapsed time alone never declares anything;
* ambiguous motion abstains rather than guessing;
* a suspicion releases forward *before* it is confirmed, so the confirming
  evidence is not measuring the motion it is judging.
"""

from __future__ import annotations

import pytest

from prospector_engine.contracts import EvidenceStatus, MotionObservation, Provenance
from prospector_engine.motion import (
    ForwardCommandLedger,
    LocomotionBaseline,
    ProgressConfig,
    ProgressGuard,
    ProgressState,
)

CALIBRATED = LocomotionBaseline(
    condition_id="test-open-ground",
    min_forward_speed_norm=0.20,
    status=EvidenceStatus.VALIDATED,
    provenance=Provenance(
        status=EvidenceStatus.VALIDATED,
        source="test fixture standing in for E-MOTION open-ground trials",
        note="only a test may assert this; production needs armed trials",
    ),
)


def _motion(
    forward: float, *, confidence: float = 0.9, coverage: float = 0.8, yaw: float = 0.0
) -> MotionObservation:
    return MotionObservation(
        forward_speed_norm=forward,
        lateral_speed_norm=0.0,
        confidence=confidence,
        inlier_count=80,
        inlier_ratio=0.9,
        spatial_coverage=coverage,
        residual=0.4,
        yaw_contamination=yaw,
        valid=True,
    )


def _guard(**config: object) -> ProgressGuard:
    return ProgressGuard(CALIBRATED, ProgressConfig(**config))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# The applied-forward ledger
# ---------------------------------------------------------------------------


def test_the_ledger_records_what_was_applied_not_what_was_asked_for() -> None:
    ledger = ForwardCommandLedger()
    ledger.note_applied(0.0, forward=True)
    assert ledger.holding()
    assert ledger.held_continuously_for(0.5) == pytest.approx(0.5)

    ledger.note_applied(0.5, forward=False)
    assert not ledger.holding()
    assert ledger.held_continuously_for(1.0) == 0.0


def test_the_ledger_stays_bounded_over_a_long_session() -> None:
    ledger = ForwardCommandLedger(window_s=1.0)
    for step in range(500):
        ledger.note_applied(step * 0.1, forward=True)
        ledger.note_applied(step * 0.1 + 0.05, forward=False)
    assert len(ledger._intervals) <= 64


# ---------------------------------------------------------------------------
# Abstention
# ---------------------------------------------------------------------------


def test_without_a_baseline_the_guard_can_conclude_nothing() -> None:
    """E-MOTION is PENDING, so this is the production state today."""
    guard = ProgressGuard()
    guard.note_applied(0.0, forward=True)

    verdict = guard.update(_motion(0.0), now_s=1.0)

    assert verdict.state is ProgressState.UNKNOWN
    assert not verdict.release_forward
    assert "E-MOTION" in verdict.reason


def test_elapsed_time_alone_never_declares_an_obstacle() -> None:
    """Holding W for a long time is not evidence of a wall."""
    guard = _guard()
    guard.note_applied(0.0, forward=True)

    for step in range(60):
        verdict = guard.update(None, now_s=step * 0.1)

    assert verdict.state is ProgressState.UNKNOWN
    assert not verdict.release_forward


def test_forward_that_was_never_applied_cannot_look_like_a_wall() -> None:
    guard = _guard()
    # The navigator asked, but the authority never accepted: no lease exists.
    verdict = guard.update(_motion(0.0), now_s=2.0)

    assert verdict.state is ProgressState.UNKNOWN
    assert "not being held" in verdict.reason


def test_forward_held_only_briefly_is_not_yet_evidence() -> None:
    guard = _guard(min_applied_forward_ms=250)
    guard.note_applied(0.0, forward=True)

    verdict = guard.update(_motion(0.0), now_s=0.1)

    assert verdict.state is ProgressState.UNKNOWN
    assert not verdict.release_forward


@pytest.mark.parametrize(
    ("label", "motion"),
    [
        ("low confidence", _motion(0.0, confidence=0.2)),
        ("poor coverage", _motion(0.0, coverage=0.1)),
        ("yaw contaminated", _motion(0.0, yaw=0.9)),
    ],
)
def test_ambiguous_motion_abstains_rather_than_declaring_no_progress(
    label: str, motion: MotionObservation
) -> None:
    guard = _guard(min_applied_forward_ms=0)
    guard.note_applied(0.0, forward=True)

    verdict = guard.update(motion, now_s=1.0)

    assert verdict.state is ProgressState.UNKNOWN, label
    assert not verdict.release_forward


# ---------------------------------------------------------------------------
# Suspicion, then confirmation
# ---------------------------------------------------------------------------


def test_progress_is_reported_when_the_character_is_moving() -> None:
    guard = _guard(min_applied_forward_ms=0)
    guard.note_applied(0.0, forward=True)

    verdict = guard.update(_motion(0.6), now_s=0.5)

    assert verdict.state is ProgressState.PROGRESSING
    assert not verdict.release_forward


def test_sustained_low_progress_releases_forward_before_confirming() -> None:
    guard = _guard(min_applied_forward_ms=0, suspect_after_ms=100)
    guard.note_applied(0.0, forward=True)

    guard.update(_motion(0.0), now_s=0.30)
    verdict = guard.update(_motion(0.0), now_s=0.50)

    assert verdict.state is ProgressState.NO_PROGRESS_SUSPECTED
    assert verdict.release_forward, "release comes first, always"
    assert verdict.state is not ProgressState.NO_PROGRESS_CONFIRMED


def test_confirmation_needs_fresh_evidence_after_the_release() -> None:
    guard = _guard(min_applied_forward_ms=0, suspect_after_ms=100, confirm_frames=4)
    guard.note_applied(0.0, forward=True)
    guard.update(_motion(0.0), now_s=0.30)
    guard.update(_motion(0.0), now_s=0.50)
    guard.note_applied(0.50, forward=False)

    states = [guard.update(_motion(0.0), now_s=0.6 + i * 0.05).state for i in range(4)]

    assert states[:3] == [ProgressState.NO_PROGRESS_SUSPECTED] * 3
    assert states[-1] is ProgressState.NO_PROGRESS_CONFIRMED


def test_ambiguous_evidence_cannot_confirm_and_does_not_escalate() -> None:
    guard = _guard(min_applied_forward_ms=0, suspect_after_ms=100, confirm_frames=2)
    guard.note_applied(0.0, forward=True)
    guard.update(_motion(0.0), now_s=0.30)
    guard.update(_motion(0.0), now_s=0.50)

    for step in range(8):
        verdict = guard.update(_motion(0.0, confidence=0.2), now_s=0.6 + step * 0.05)

    assert verdict.state is ProgressState.NO_PROGRESS_SUSPECTED
    assert verdict.release_forward
    assert "waiting for usable evidence" in verdict.reason


def test_a_recent_yaw_suppresses_the_whole_judgement() -> None:
    guard = _guard(min_applied_forward_ms=0, suspect_after_ms=50)
    guard.note_applied(0.0, forward=True)
    guard.note_yaw(0.5)

    verdict = guard.update(_motion(0.0), now_s=0.55)

    assert verdict.state is ProgressState.UNKNOWN


def test_resetting_the_guard_forgets_everything() -> None:
    guard = _guard(min_applied_forward_ms=0, suspect_after_ms=50)
    guard.note_applied(0.0, forward=True)
    guard.update(_motion(0.0), now_s=0.3)
    guard.update(_motion(0.0), now_s=0.6)

    guard.reset()

    assert guard.state is ProgressState.UNKNOWN
    assert not guard.ledger.holding()


# ---------------------------------------------------------------------------
# The boundary with the later terrain work
# ---------------------------------------------------------------------------


def test_the_guard_records_a_traversability_contract_but_acts_on_none_of_it() -> None:
    guard = _guard(min_applied_forward_ms=0)
    guard.note_applied(0.0, forward=True)
    for step in range(5):
        guard.update(
            _motion(0.5),
            now_s=step * 0.1,
            commanded_heading_deg=45.0,
            anchor_px=(640.0, 430.0),
        )

    history = guard.history()
    assert len(history) == 5
    latest = history[-1]
    assert latest.commanded_heading_deg == 45.0
    assert latest.commanded_forward
    assert latest.anchor_px == (640.0, 430.0)
    assert latest.motion is not None


def test_the_traversability_record_is_bounded() -> None:
    guard = _guard(min_applied_forward_ms=0)
    guard.note_applied(0.0, forward=True)
    for step in range(1000):
        guard.update(_motion(0.5), now_s=step * 0.01)

    assert len(guard.history()) <= 240


def test_the_guard_never_recommends_a_maneuver() -> None:
    """It can say "stop". It cannot say "go around", and has no field to."""
    from dataclasses import fields

    from prospector_engine.motion import ProgressVerdict

    names = {field.name for field in fields(ProgressVerdict)}
    assert "release_forward" in names
    for forbidden in ("lateral", "jump", "detour", "side", "recovery", "reverse"):
        assert not any(forbidden in name for name in names), forbidden
