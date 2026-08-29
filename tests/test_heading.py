"""The heading filter: smoothing, outliers, rate, and the memory horizon.

Four questions, and the tests are grouped by them.

* Does a steady heading come out steadier than it went in? That is the whole
  reason the filter exists - the raw estimate jitters by a few degrees every
  frame and a proportional controller turns that into left/right chatter.
* Does a *real* sweep still get through? A filter that smooths a genuine turn
  into a lag is worse than none at all, because the controller then corrects
  for a heading the camera already left.
* Is one implausible frame refused, and a sustained disagreement accepted?
* Does the estimate stop existing at the end of its horizon, rather than
  quietly ageing into something the controller still trusts?
"""

from __future__ import annotations

import math

import pytest

from prospector_engine.heading import HeadingConfig, HeadingFilter, wrap_deg


def _filter(**overrides: object) -> HeadingFilter:
    return HeadingFilter(HeadingConfig(**overrides))  # type: ignore[arg-type]


def _feed(
    filtered: HeadingFilter,
    values: list[float],
    *,
    fps: float = 60.0,
    confidence: float = 0.9,
    track_id: int = 1,
    start_s: float = 0.0,
) -> list[float]:
    """Feed a series and return the filtered value after each accepted sample."""
    out: list[float] = []
    for index, value in enumerate(values):
        estimate = filtered.observe(
            value,
            confidence=confidence,
            track_id=track_id,
            now_s=start_s + index / fps,
        )
        if estimate is not None:
            out.append(estimate.error_deg)
    return out


# ---------------------------------------------------------------------------
# Smoothing
# ---------------------------------------------------------------------------


def test_the_first_reading_is_adopted_whole_rather_than_smoothed_towards() -> None:
    """There is nothing to smooth towards yet, and pretending otherwise would
    make every run start by steering at zero."""
    estimate = _filter().observe(40.0, confidence=0.9, track_id=1, now_s=0.0)

    assert estimate is not None
    assert estimate.error_deg == pytest.approx(40.0)
    assert estimate.samples == 1


def test_jitter_comes_out_smaller_than_it_went_in() -> None:
    raw = [30.0 + (3.0 if index % 2 else -3.0) for index in range(60)]
    filtered = _feed(_filter(), raw)

    tail = filtered[20:]
    spread = max(tail) - min(tail)
    assert spread < 2.0, f"jitter survived the filter: {spread:.2f} degrees peak to peak"
    assert abs(sum(tail) / len(tail) - 30.0) < 1.0, "the filter introduced a bias"


def test_a_genuine_sweep_is_tracked_rather_than_smoothed_away() -> None:
    """Ninety degrees a second is what an ordinary camera turn looks like."""
    filtered = _filter()
    for index in range(40):
        now = index / 60.0
        estimate = filtered.observe(60.0 - 90.0 * now, confidence=0.9, track_id=1, now_s=now)

    assert estimate is not None
    truth = 60.0 - 90.0 * (39 / 60.0)
    assert abs(estimate.error_deg - truth) < 6.0, "the filter lagged a real turn"
    assert estimate.rate_deg_s == pytest.approx(-90.0, abs=12.0)


def test_lower_confidence_moves_the_estimate_less() -> None:
    confident = _filter()
    unsure = _filter()
    confident.observe(0.0, confidence=1.0, track_id=1, now_s=0.0)
    unsure.observe(0.0, confidence=1.0, track_id=1, now_s=0.0)

    high = confident.observe(20.0, confidence=1.0, track_id=1, now_s=1 / 60)
    low = unsure.observe(20.0, confidence=0.4, track_id=1, now_s=1 / 60)

    assert high is not None and low is not None
    assert low.error_deg < high.error_deg
    assert low.error_deg > 0.0, "an uncertain reading still counts for something"


def test_a_reading_below_the_confidence_floor_is_not_evidence_at_all() -> None:
    filtered = _filter()
    filtered.observe(10.0, confidence=0.9, track_id=1, now_s=0.0)

    estimate = filtered.observe(90.0, confidence=0.1, track_id=1, now_s=1 / 60)

    assert estimate is not None
    assert estimate.error_deg == pytest.approx(10.0), "a collapsed reading moved the estimate"
    assert not estimate.fresh


# ---------------------------------------------------------------------------
# Outliers
# ---------------------------------------------------------------------------


def test_one_implausible_frame_is_refused_without_losing_the_target() -> None:
    """The reading is refused; the *estimate* is still returned.

    That combination is deliberate. The arrow is visible and tracked - only
    this frame's angle is implausible - so the controller should keep pursuing
    on what it knows rather than treating the frame as an occlusion. The
    estimate says ``outlier`` and ``fresh=False`` so nothing can mistake it for
    new evidence.
    """
    filtered = _filter()
    _feed(filtered, [30.0] * 5)

    estimate = filtered.observe(150.0, confidence=0.9, track_id=1, now_s=5 / 60)

    assert estimate is not None
    assert estimate.outlier and not estimate.fresh
    assert abs(estimate.error_deg - 30.0) < 2.0, "the flyer moved the estimate"


def test_a_sustained_disagreement_is_the_world_moving_and_is_accepted() -> None:
    """The gate is a gate, not a veto. A camera really can whip round."""
    filtered = _filter(max_consecutive_outliers=2)
    _feed(filtered, [30.0] * 5)

    for index in range(5):
        filtered.observe(150.0, confidence=0.9, track_id=1, now_s=(5 + index) / 60)

    estimate = filtered.coast(10 / 60)
    assert estimate is not None
    assert abs(wrap_deg(estimate.error_deg - 150.0)) < 5.0


def test_the_gate_does_not_apply_before_there_is_anything_to_disagree_with() -> None:
    estimate = _filter().observe(179.0, confidence=0.9, track_id=1, now_s=0.0)

    assert estimate is not None
    assert estimate.error_deg == pytest.approx(179.0)


# ---------------------------------------------------------------------------
# Wrapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("pair", [(179.0, -179.0), (-179.0, 179.0), (170.0, -170.0)])
def test_the_filter_takes_the_short_way_round_the_circle(pair: tuple[float, float]) -> None:
    first, second = pair
    filtered = _filter()
    filtered.observe(first, confidence=0.9, track_id=1, now_s=0.0)
    estimate = filtered.observe(second, confidence=0.9, track_id=1, now_s=1 / 60)

    assert estimate is not None
    # Two degrees apart across the wrap: the estimate must stay next to both,
    # never take the 358-degree route through zero.
    assert min(abs(wrap_deg(estimate.error_deg - first)), 999) < 15.0


# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------


def test_the_estimate_survives_an_occlusion_and_says_how_old_it_is() -> None:
    filtered = _filter(max_age_s=2.5)
    _feed(filtered, [45.0] * 10)

    remembered = filtered.coast(1.5)

    assert remembered is not None
    assert not remembered.fresh
    assert remembered.age_s == pytest.approx(1.5 - 9 / 60, abs=0.01)
    assert abs(remembered.error_deg - 45.0) < 3.0


def test_past_the_horizon_the_filter_reports_nothing_rather_than_zero() -> None:
    filtered = _filter(max_age_s=2.0)
    _feed(filtered, [45.0] * 10)

    assert filtered.coast(1.9) is not None
    assert filtered.coast(2.5) is None, "a forgotten heading must not read as zero"
    assert not filtered.usable(2.5)


def test_a_new_target_identity_forgets_the_old_angle_entirely() -> None:
    filtered = _filter()
    _feed(filtered, [45.0] * 10)

    estimate = filtered.observe(-60.0, confidence=0.9, track_id=2, now_s=1.0)

    assert estimate is not None
    assert estimate.error_deg == pytest.approx(-60.0), "the old target's angle carried over"
    assert estimate.rate_deg_s == 0.0
    assert filtered.track_id == 2


def test_reset_forgets_everything() -> None:
    filtered = _filter()
    _feed(filtered, [45.0] * 10)
    filtered.reset()

    assert not filtered.has_estimate
    assert filtered.coast(0.2) is None
    assert math.isinf(filtered.age_s(0.2))


# ---------------------------------------------------------------------------
# Cadence
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fps", [30.0, 60.0, 90.0, 120.0])
def test_the_same_signal_converges_the_same_way_at_every_cadence(fps: float) -> None:
    """Every rate is applied against measured time, so the answer after one
    second of the same signal is the same however many frames that took."""
    filtered = _filter()
    filtered.observe(0.0, confidence=0.9, track_id=1, now_s=0.0)
    frames = int(fps)
    for index in range(1, frames + 1):
        estimate = filtered.observe(40.0, confidence=0.9, track_id=1, now_s=index / fps)

    assert estimate is not None
    assert estimate.error_deg == pytest.approx(40.0, abs=1.0)
