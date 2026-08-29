"""How long the detector stays blind after the arrow moves, and what it must not do.

**Rendered frames are training stress, never held-out validation** (plan §7.2,
CLAUDE.md §8). No gate is passed on anything in this file. What it pins is a
property the real corpus cannot express, because that corpus is sampled at
about 5 fps and the property is about consecutive frames 17 ms apart:

    a perfectly visible arrow that moved must be seen again on the next frame.

The measured blackout before ``_resume_outside_gate``, at 60 fps with a clean
unambiguous arrow in every single frame:

    100 px jump   67 ms      scale 70->110   200 ms
    180 px jump  233 ms      scale 70->130   367 ms
    250 px jump  367 ms      scale 70->160   567 ms + a new identity
    400 px jump  567 ms      scale 70->200   567 ms + a new identity

The gates only widened with elapsed time, and elapsed time only accumulated
while the track was missing - so the detector refused a visible arrow until the
gate had crawled out far enough to reach it. That is deliberate blindness, and
at 250 px it lasted longer than a fifth of a second.

The second half of the file is the reason the gate existed in the first place.
Resuming must never hand the identity to a same-coloured blob, so the same
families are run with the arrow deleted and clutter left in place.
"""

from __future__ import annotations

import pytest

from prospector_engine.arrow import DetectorConfig
from tests.tracking_families import (
    JUMP_FAMILY_PX,
    SCALE_FAMILY_PX,
    TURN_FAMILY_DEG,
    cluttered_jump_report,
    distractor_report,
    distractor_vanish,
    jump_recovery,
    scale_recovery,
    sweep_recovery,
)

#: The old behaviour, reachable exactly: resuming is bounded by how recently
#: the track was seen, so a zero age disables it and nothing else changes.
WITHOUT_RESUME = DetectorConfig(resume_max_age_s=0.0)

#: One frame at 60 Hz. "Seen again on the next frame" is the whole budget.
MAX_BLIND_FRAMES = 1


# ---------------------------------------------------------------------------
# Recovery: the arrow moved and is still in plain sight
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("distance_px", JUMP_FAMILY_PX)
def test_a_jump_is_seen_again_on_the_next_frame(distance_px: float) -> None:
    recovery = jump_recovery(distance_px)
    assert recovery.blind_frames is not None, f"never recovered from {distance_px:.0f} px"
    assert recovery.blind_frames <= MAX_BLIND_FRAMES, recovery.describe()
    assert recovery.kept_identity, f"{recovery.describe()} - the identity was renumbered"


@pytest.mark.parametrize("to_px", SCALE_FAMILY_PX)
def test_a_scale_step_is_seen_again_on_the_next_frame(to_px: float) -> None:
    recovery = scale_recovery(to_px)
    assert recovery.blind_frames is not None, f"never recovered from scale {to_px:.0f}"
    assert recovery.blind_frames <= MAX_BLIND_FRAMES, recovery.describe()
    assert recovery.kept_identity, f"{recovery.describe()} - the identity was renumbered"


@pytest.mark.parametrize("step_deg", TURN_FAMILY_DEG)
def test_a_fast_camera_turn_is_followed_rather_than_lost(step_deg: float) -> None:
    recovery = sweep_recovery(step_deg)
    assert recovery.blind_frames is not None
    assert recovery.blind_frames <= MAX_BLIND_FRAMES, recovery.describe()
    assert recovery.kept_identity, recovery.describe()
    # The heading must be right, not merely present. A sweep that reacquires
    # after a blackout used to come back pointing 180 degrees the wrong way,
    # because the polarity guard kept the direction the arrow had before it
    # swung - which is the worst possible thing to hand a steering controller.
    assert recovery.error_deg is not None and recovery.error_deg < 10.0, recovery.describe()


@pytest.mark.parametrize("distance_px", [180.0, 250.0, 400.0])
def test_the_blackout_this_removed_is_reproducible(distance_px: float) -> None:
    """The regression itself, kept executable so it cannot come back quietly."""
    before = jump_recovery(distance_px, config=WITHOUT_RESUME)
    after = jump_recovery(distance_px)
    assert before.blind_frames is not None
    assert before.blind_frames > 10, (
        f"the old blackout no longer reproduces at {distance_px:.0f} px "
        f"({before.describe()}) - this test is measuring nothing"
    )
    assert after.blind_frames is not None
    assert after.blind_frames < before.blind_frames


# ---------------------------------------------------------------------------
# ...and what it must never do to get there
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("distractors", [1, 3, 6])
def test_resuming_never_invents_an_arrow_that_is_not_there(distractors: int) -> None:
    """The case the positional gate existed for.

    The arrow is deleted and same-coloured blobs are left exactly where they
    were. Resuming may not turn any of them into the arrow: whatever the
    detector said before, it must say no more than it said without resuming.
    """
    with_resume = distractor_vanish(distractors, seed=11)
    without = distractor_vanish(distractors, seed=11, config=WITHOUT_RESUME)
    assert with_resume.false_valid_frames == 0, with_resume.describe()
    assert with_resume.false_valid_frames <= without.false_valid_frames
    assert not with_resume.switched


def test_resuming_makes_no_layout_worse() -> None:
    """Across every rendered layout, resuming never adds a false detection.

    Two of these layouts do report the arrow after it is gone, with resuming
    switched off as well as on: a blob happens to land near where the arrow
    was, and the *ordinary* positional gate accepts it. That is a real and
    separate weakness of the detector - it is what the same-coloured sand
    sequences show in the real corpus - and it is recorded here rather than
    hidden, because a test that only ran the layouts that pass would be
    describing a detector nobody has.
    """
    with_resume = {row.label: row for row in distractor_report()}
    without = {row.label: row for row in distractor_report(WITHOUT_RESUME)}
    worse = [
        label
        for label, row in with_resume.items()
        if row.false_valid_frames > without[label].false_valid_frames or row.switched
    ]
    assert not worse, f"resuming made these layouts worse: {worse}"


def test_a_jump_through_clutter_recovers_onto_the_arrow_not_a_blob() -> None:
    """Recovering fast onto the wrong thing is worse than staying blind."""
    with_resume = cluttered_jump_report()
    without = {row.label: row for row in cluttered_jump_report(WITHOUT_RESUME)}
    for row in with_resume:
        if row.blind_frames == 0:
            assert row.position_error_px is not None
            assert row.kept_identity, row.describe()
    # Somewhere in the set, resuming must actually have helped - otherwise the
    # clutter has quietly disabled the thing this file exists to check.
    improved = [
        row
        for row in with_resume
        if row.blind_frames is not None
        and without[row.label].blind_frames is not None
        and row.blind_frames < without[row.label].blind_frames  # type: ignore[operator]
    ]
    assert improved, "resuming helped no cluttered layout at all"


def test_resuming_is_bounded_by_how_recently_the_arrow_was_seen() -> None:
    """The bound that makes the rule sound rather than merely convenient.

    A resume is the claim "the arrow moved between two consecutive frames".
    Two frames 200 ms apart support no such claim, and letting one fire there
    took the same-coloured sand sequence from zero false locks to one.
    """
    config = DetectorConfig()
    assert 0.0 < config.resume_max_age_s < 0.1
    slow = jump_recovery(250.0, config=DetectorConfig(resume_max_age_s=0.0))
    assert slow.blind_frames is not None and slow.blind_frames > 10
