"""One filtered heading, its rate, and how much either can be trusted.

The controller used to steer on ``direction.error_deg`` straight off the
frame. That number is honest and it is noisy: the detector re-fits the arrow
axis every frame, so a heading that is genuinely constant arrives as a few
degrees of jitter with the occasional single-frame flyer when a leaf briefly
outscores the arrowhead. Feeding it to a proportional controller produces
left/right chatter around zero and an overreaction to every flyer.

Three things live here, and nothing else.

**A circular One-Euro filter.** Angles wrap, so the filter is applied to the
*difference* from the current estimate rather than to the value: every update
is ``filtered += alpha * wrap(raw - filtered)``. One-Euro rather than a plain
EMA because the trade-off it makes is exactly the one steering needs - heavy
smoothing while the heading is steady, almost none while it is genuinely
sweeping - so a real turn is not smoothed into a lag.

**An outlier gate.** A single frame that disagrees with the estimate by more
than the gate is rejected rather than absorbed. It is a *gate*, not a veto: a
run of them means the world really did move, and after
``max_consecutive_outliers`` the filter snaps to the new heading instead of
sulking on a stale one.

**A memory horizon.** The estimate keeps its value and its identity for
``max_age_s`` after the last accepted reading, so an occluded arrow leaves
something to steer by. Past the horizon the filter reports that it has
nothing, which is a different fact from reporting zero.

The filter never invents a reading. :meth:`HeadingFilter.observe` is the only
way a value enters, and every consumer can tell a fresh estimate from a
remembered one by its ``age_s``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Final

from prospector_engine.contracts import EvidenceStatus, Provenance

__all__ = [
    "HeadingConfig",
    "HeadingEstimate",
    "HeadingFilter",
    "wrap_deg",
]

_TWO_PI: Final[float] = 2.0 * math.pi


def wrap_deg(degrees: float) -> float:
    """Fold an angle into ``(-180, 180]``. The only wrap rule in this module."""
    wrapped = (float(degrees) + 180.0) % 360.0 - 180.0
    return 180.0 if wrapped == -180.0 else wrapped


def _alpha(cutoff_hz: float, delta_s: float) -> float:
    """The One-Euro smoothing factor for a cutoff and an actual time step.

    Derived from the time step rather than assumed per frame, which is what
    makes the filter behave the same at 30 and at 90 Hz.
    """
    if delta_s <= 0.0 or cutoff_hz <= 0.0:
        return 1.0
    tau = 1.0 / (_TWO_PI * cutoff_hz)
    return 1.0 / (1.0 + tau / delta_s)


@dataclass(frozen=True)
class HeadingConfig:
    """Bounds on the filter. Provisional configuration, not measurements."""

    #: Lowest cutoff, used while the heading is steady. Lower is smoother.
    min_cutoff_hz: float = 1.5
    #: How fast the cutoff opens with observed angular speed. Higher tracks a
    #: real sweep more closely at the cost of passing more jitter through.
    beta: float = 0.05
    #: Cutoff for the derivative estimate itself.
    derivative_cutoff_hz: float = 1.0
    #: A single reading further than this from the estimate is rejected.
    outlier_gate_deg: float = 45.0
    #: Consecutive rejections after which the filter accepts that the world
    #: moved and snaps to the new heading rather than holding a stale one.
    max_consecutive_outliers: int = 2
    #: Readings below this confidence are not evidence and never enter.
    min_confidence: float = 0.35
    #: How long an estimate stays usable after its last accepted reading. Kept
    #: in step with the controller's coast grace and the tracker's identity
    #: horizon; see ``SteeringLimits.coast_grace_s``.
    max_age_s: float = 2.5
    #: Accepted readings before the estimate is called settled. Until then the
    #: controller may steer on it but must not treat it as stable.
    settle_samples: int = 3
    provenance: Provenance = field(
        default_factory=lambda: Provenance(
            status=EvidenceStatus.PROVISIONAL,
            source="prospector_engine/heading.py; mission section 'continuous smooth steering'",
            note=(
                "One-Euro constants are chosen bounds. They are tuned from replay "
                "and native evidence, never from the rendered fixtures."
            ),
        )
    )


@dataclass(frozen=True)
class HeadingEstimate:
    """What the filter believes right now, and how it came to believe it."""

    #: The filtered signed heading error, in degrees.
    error_deg: float
    #: The most recent accepted raw reading, for comparison in a trace.
    raw_deg: float
    #: Filtered angular velocity, degrees per second.
    rate_deg_s: float
    #: Confidence of the most recent accepted reading.
    confidence: float
    #: Root-mean-square residual between raw readings and the estimate, in
    #: degrees. A cheap, honest measure of how noisy the last second was.
    spread_deg: float
    #: The arrow identity this estimate belongs to.
    track_id: int | None
    #: When the last accepted reading arrived.
    updated_at_s: float
    #: How old that reading is, at the moment the estimate was taken.
    age_s: float
    #: Accepted readings since the last reset.
    samples: int
    #: Whether the reading that produced this estimate was accepted this tick.
    fresh: bool
    #: Whether this tick's reading was rejected as an angular outlier.
    outlier: bool = False

    @property
    def settled(self) -> bool:
        return self.samples >= HeadingConfig.settle_samples

    def describe(self) -> str:
        age = "" if self.fresh else f", {self.age_s * 1000:.0f} ms old"
        return (
            f"{self.error_deg:+.1f} deg (+-{self.spread_deg:.1f}, "
            f"{self.rate_deg_s:+.0f} deg/s, conf {self.confidence:.2f}{age})"
        )


class HeadingFilter:
    """A circular One-Euro filter with an outlier gate and a memory horizon.

    Not thread-safe and not meant to be: exactly one controller owns one
    filter, and it is updated from the perception loop that also decides.
    """

    def __init__(self, config: HeadingConfig | None = None) -> None:
        self._config = config or HeadingConfig()
        self.reset()

    # -- lifecycle --------------------------------------------------------
    def reset(self) -> None:
        """Forget everything. Called on a target change or a hard release."""
        self._filtered_deg: float | None = None
        self._raw_deg: float = 0.0
        self._rate_deg_s: float = 0.0
        self._last_s: float | None = None
        self._confidence: float = 0.0
        self._variance: float = 0.0
        self._samples: int = 0
        self._outliers: int = 0
        self._track_id: int | None = None

    @property
    def config(self) -> HeadingConfig:
        return self._config

    @property
    def track_id(self) -> int | None:
        return self._track_id

    @property
    def has_estimate(self) -> bool:
        return self._filtered_deg is not None

    def age_s(self, now_s: float) -> float:
        """Seconds since the last accepted reading; ``inf`` when there is none."""
        if self._last_s is None:
            return math.inf
        return max(0.0, now_s - self._last_s)

    def usable(self, now_s: float) -> bool:
        """Whether the remembered heading is still inside its horizon."""
        return self.has_estimate and self.age_s(now_s) <= self._config.max_age_s

    # -- the tick ---------------------------------------------------------
    def observe(
        self,
        error_deg: float | None,
        *,
        confidence: float,
        track_id: int | None,
        now_s: float,
    ) -> HeadingEstimate | None:
        """Fold one reading in. Returns the estimate, or ``None`` when there is none.

        A reading is refused - not absorbed - when its confidence is below the
        floor or when it disagrees with the estimate by more than the gate. A
        refusal is still reported: the returned estimate carries ``fresh=False``
        so the caller can see it is steering on memory.
        """
        config = self._config
        if track_id is not None and track_id != self._track_id and self._track_id is not None:
            # A different target is a different angle. Nothing about the old
            # one transfers, including its rate.
            self.reset()
        if error_deg is None or confidence < config.min_confidence:
            return self.coast(now_s)

        raw = wrap_deg(error_deg)
        if self._filtered_deg is None or self._last_s is None:
            self._adopt(raw, confidence=confidence, track_id=track_id, now_s=now_s)
            return self.coast(now_s)

        residual = wrap_deg(raw - self._filtered_deg)
        if (
            abs(residual) > config.outlier_gate_deg
            and self.age_s(now_s) <= config.max_age_s
            and self._outliers < config.max_consecutive_outliers
        ):
            self._outliers += 1
            estimate = self.coast(now_s)
            return None if estimate is None else _as_outlier(estimate)
        if abs(residual) > config.outlier_gate_deg:
            # The gate has been argued with often enough. The world moved.
            self._adopt(raw, confidence=confidence, track_id=track_id, now_s=now_s)
            return self.coast(now_s)

        if now_s <= self._last_s:
            # The same instant twice: a duplicated frame, or a second reader on
            # one frame. There is no new information in it, and folding it in
            # against a delta-t of nearly zero manufactures a rate spike and
            # snaps the estimate to the raw reading. Freeze instead.
            return self.coast(now_s)

        delta_s = now_s - self._last_s
        raw_rate = wrap_deg(raw - self._raw_deg) / delta_s
        rate_alpha = _alpha(config.derivative_cutoff_hz, delta_s)
        self._rate_deg_s += rate_alpha * (raw_rate - self._rate_deg_s)

        cutoff = config.min_cutoff_hz + config.beta * abs(self._rate_deg_s)
        # Confidence scales the step down and never up: an uncertain reading may
        # move the estimate less, never more.
        alpha = _alpha(cutoff, delta_s) * max(0.0, min(1.0, confidence))
        self._filtered_deg = wrap_deg(self._filtered_deg + alpha * residual)
        self._variance += 0.2 * (residual * residual - self._variance)
        self._raw_deg = raw
        self._last_s = now_s
        self._confidence = float(confidence)
        self._samples += 1
        self._outliers = 0
        if track_id is not None:
            self._track_id = track_id
        return self.coast(now_s)

    def coast(self, now_s: float) -> HeadingEstimate | None:
        """The current estimate without folding anything in.

        Used on a frame with no usable reading, and used internally so that a
        fresh update and a remembered one are built by exactly one piece of
        code. Returns ``None`` past the memory horizon.
        """
        if self._filtered_deg is None or self._last_s is None:
            return None
        age = self.age_s(now_s)
        if age > self._config.max_age_s:
            return None
        return HeadingEstimate(
            error_deg=self._filtered_deg,
            raw_deg=self._raw_deg,
            rate_deg_s=self._rate_deg_s,
            confidence=self._confidence,
            spread_deg=math.sqrt(max(0.0, self._variance)),
            track_id=self._track_id,
            updated_at_s=self._last_s,
            age_s=age,
            samples=self._samples,
            fresh=age <= 1e-6,
        )

    # -- internals --------------------------------------------------------
    def _adopt(
        self, raw: float, *, confidence: float, track_id: int | None, now_s: float
    ) -> None:
        """Take a reading as the whole truth. First sample, or a proven move."""
        self._filtered_deg = raw
        self._raw_deg = raw
        self._rate_deg_s = 0.0
        self._last_s = now_s
        self._confidence = float(confidence)
        self._variance = 0.0
        self._samples += 1
        self._outliers = 0
        if track_id is not None:
            self._track_id = track_id


def _as_outlier(estimate: HeadingEstimate) -> HeadingEstimate:
    """Mark an estimate as having refused this tick's reading."""
    return HeadingEstimate(
        error_deg=estimate.error_deg,
        raw_deg=estimate.raw_deg,
        rate_deg_s=estimate.rate_deg_s,
        confidence=estimate.confidence,
        spread_deg=estimate.spread_deg,
        track_id=estimate.track_id,
        updated_at_s=estimate.updated_at_s,
        age_s=estimate.age_s,
        samples=estimate.samples,
        fresh=False,
        outlier=True,
    )
