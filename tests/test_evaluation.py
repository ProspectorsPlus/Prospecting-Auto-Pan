"""The offline evaluator's arithmetic, and its refusal to flatter itself.

An evaluator that reports a pass it cannot support is worse than none, so the
properties asserted here are mostly *negative*: an abstain-everything detector
must not look good, an aggregate must not cover a failing stratum, and a bound
computed from six episodes must not read like a bound computed from six
hundred.
"""

from __future__ import annotations

import math

import pytest

from prospector_engine.contracts import CapturedFrame
from prospector_engine.evaluation import (
    DatasetSplit,
    LabelledFrame,
    evaluate,
    rule_of_three_upper,
    wilson_interval,
)
from tests.fakes import make_frame


def _labelled(
    index: int,
    heading: float | None,
    stratum: str = "s",
    *,
    episode: str = "e1",
    present: bool = True,
    unknown: bool = False,
) -> LabelledFrame:
    return LabelledFrame(
        frame=make_frame(index),
        heading_deg=heading,
        stratum=stratum,
        session_id="session",
        episode_id=episode,
        arrow_present=present,
        unknown=unknown,
    )


def _split(frames: list[LabelledFrame], name: str = "test") -> DatasetSplit:
    return DatasetSplit(name, tuple(frames))


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------


def test_the_wilson_interval_brackets_the_point_estimate() -> None:
    low, high = wilson_interval(9, 10)
    assert low < 0.9 < high
    assert low >= 0.0 and high <= 1.0


def test_a_perfect_score_still_has_an_honest_lower_bound() -> None:
    """Ten out of ten is not proof of a 99% success rate."""
    low, high = wilson_interval(10, 10)
    assert high == pytest.approx(1.0)
    assert low < 0.75, "a small perfect sample must not certify a high rate"


def test_zero_events_gives_the_rule_of_three_bound() -> None:
    assert rule_of_three_upper(6) == pytest.approx(0.5)
    assert rule_of_three_upper(600) == pytest.approx(0.005)
    assert rule_of_three_upper(0) == 1.0


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def test_a_perfect_detector_reports_full_coverage_and_no_error() -> None:
    frames = [_labelled(i, float(i * 30)) for i in range(12)]
    lookup = {frame.frame.sequence: frame.heading_deg for frame in frames}

    report = evaluate(_split(frames), lambda frame: (lookup[frame.sequence], True))

    stratum = report.strata[0]
    assert stratum.coverage == 1.0
    assert stratum.median_abs_deg == 0.0
    assert stratum.polarity_flips == 0
    assert stratum.signed_bias_deg == 0.0


def test_abstaining_on_everything_scores_zero_coverage_not_perfect_precision() -> None:
    """The failure mode a coverage-blind metric rewards."""
    frames = [_labelled(i, float(i * 30)) for i in range(12)]

    report = evaluate(_split(frames), lambda _frame: (None, False))

    stratum = report.strata[0]
    assert stratum.coverage == 0.0
    assert stratum.accepted == 0
    passed, failures = stratum.passes_provisional_targets()
    assert not passed
    assert any("coverage" in failure for failure in failures)


def test_a_polarity_flip_is_counted_and_costs_precision() -> None:
    frames = [_labelled(i, 0.0) for i in range(10)]
    flipped = {3, 7}

    report = evaluate(
        _split(frames),
        lambda frame: ((180.0 if frame.sequence in flipped else 0.0), True),
    )

    stratum = report.strata[0]
    assert stratum.polarity_flips == 2
    assert stratum.precision == pytest.approx(0.8)
    assert stratum.polarity_flip_upper > 0.0


def test_the_error_is_wrapped_across_the_seam() -> None:
    frames = [_labelled(1, 179.0)]

    report = evaluate(_split(frames), lambda _frame: (-179.0, True))

    assert report.strata[0].median_abs_deg == pytest.approx(2.0)


def test_signed_bias_survives_symmetric_errors_as_zero() -> None:
    frames = [_labelled(1, 0.0), _labelled(2, 0.0)]
    answers = {1: 5.0, 2: -5.0}

    report = evaluate(_split(frames), lambda frame: (answers[frame.sequence], True))

    assert report.strata[0].signed_bias_deg == pytest.approx(0.0)
    assert report.strata[0].median_abs_deg == pytest.approx(5.0)


def test_an_arrow_absent_frame_that_acquires_is_a_false_acquisition() -> None:
    frames = [_labelled(i, None, present=False) for i in range(8)]

    report = evaluate(_split(frames), lambda frame: (0.0, frame.sequence < 2))

    stratum = report.strata[0]
    assert stratum.absent_frames == 8
    assert stratum.false_acquisitions == 2
    assert stratum.eligible == 0, "an absent frame is not an eligible detection"


def test_unknown_labels_are_reported_separately_rather_than_dropped() -> None:
    frames = [_labelled(1, 0.0), _labelled(2, None, unknown=True)]

    report = evaluate(_split(frames), lambda _frame: (0.0, True))

    stratum = report.strata[0]
    assert stratum.unknown == 1
    assert stratum.eligible == 1
    assert stratum.frames == 2


def test_wrong_track_dwell_counts_consecutive_flipped_frames() -> None:
    frames = [_labelled(i, 0.0) for i in range(6)]
    wrong = {2, 3, 4}

    report = evaluate(
        _split(frames), lambda frame: ((180.0 if frame.sequence in wrong else 0.0), True)
    )

    assert report.strata[0].wrong_track_max_frames == 3


def test_reacquisition_latency_is_measured_from_the_gap_it_closed() -> None:
    frames = [_labelled(i, 0.0) for i in range(8)]
    missing = {1, 2, 3}

    report = evaluate(
        _split(frames),
        lambda frame: (None, False) if frame.sequence in missing else (0.0, True),
    )

    assert report.strata[0].reacquire_p95_frames == pytest.approx(3.0)


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def test_an_aggregate_pass_never_covers_a_failing_stratum() -> None:
    good = [_labelled(i, 0.0, "easy", episode=f"g{i}") for i in range(30)]
    bad = [_labelled(100 + i, 0.0, "hard", episode=f"b{i}") for i in range(30)]
    hard = {frame.frame.sequence for frame in bad}

    report = evaluate(
        _split(good + bad),
        lambda frame: (170.0, True) if frame.sequence in hard else (0.0, True),
    )

    assert len(report.strata) == 2
    assert not report.passes()
    worst = report.worst_stratum
    assert worst is not None and worst.stratum == "hard"


def test_bounds_are_computed_over_episodes_not_frames() -> None:
    """Six hundred frames of one route are one route, not six hundred samples."""
    single = [_labelled(i, 0.0, episode="e1") for i in range(120)]
    many = [_labelled(i, 0.0, episode=f"e{i}") for i in range(120)]

    one = evaluate(_split(single), lambda _frame: (0.0, True)).strata[0]
    spread = evaluate(_split(many), lambda _frame: (0.0, True)).strata[0]

    assert one.episodes == 1
    assert spread.episodes == 120
    assert one.polarity_flip_upper > spread.polarity_flip_upper


def test_the_report_serialises_with_its_caveat_attached() -> None:
    import json

    frames = [_labelled(1, 0.0)]
    report = evaluate(_split(frames), lambda _frame: (0.0, True))

    payload = json.loads(report.to_json())
    assert "caveat" in payload
    assert "held-out" in payload["caveat"]
    assert payload["strata"][0]["stratum"] == "s"


def test_a_stratum_that_meets_every_target_says_so() -> None:
    frames = [_labelled(i, 0.0, episode=f"e{i}") for i in range(900)]

    report = evaluate(_split(frames), lambda _frame: (0.0, True))

    passed, failures = report.strata[0].passes_provisional_targets()
    assert passed, failures
    assert report.passes()


def test_predict_may_not_accept_without_producing_a_value() -> None:
    """Accepted-with-no-heading is a detector bug and must not count as coverage."""
    frames = [_labelled(i, 0.0) for i in range(10)]

    report = evaluate(_split(frames), lambda _frame: (None, True))

    assert report.strata[0].accepted == 0
    assert report.strata[0].coverage == 0.0


def test_the_evaluator_needs_no_detector_to_run() -> None:
    """It is an instrument: any callable that answers the contract works."""
    frames = [_labelled(1, 45.0)]
    report = evaluate(_split(frames), lambda _frame: (45.0 + 1e-9, True))
    assert math.isclose(report.strata[0].median_abs_deg, 0.0, abs_tol=1e-6)


def _unused(frame: CapturedFrame) -> None:  # pragma: no cover - typing anchor
    del frame
