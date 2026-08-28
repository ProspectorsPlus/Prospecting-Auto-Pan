"""Turn actuators, the measured response model, and automatic characterization.

Turning the camera and strafing the character are different actuators with
different failure modes, so they are different types here. ``A``/``D`` remain
lateral movement and are used by recovery; camera yaw comes from one of the
two *turn* backends below.

Three rules run through the module.

**The response is measured, never configured.** How far the camera rotates per
mouse unit - or per millisecond of held arrow key - is a property of this OS,
this client, this sensitivity slider and this control mode together. A number
typed into a settings file is a guess about someone else's machine.
:class:`TurnCharacterizer` measures it here, in this run, with the character
stationary and every probe bounded and released.

**One pulse in flight.** A turn command is not issued until the previous one
has been *observed*: the response has a measured latency, and queuing three
pulses inside that latency is how a controller overshoots and then hunts. The
rule is enforced by :meth:`TurnResponse.settled_after_s`, not by a comment.

**A prior is not a proof.** A cached response may seed the first probe, which
saves a second of wall clock. It may never replace it: the cache is keyed by a
fingerprint and re-confirmed with at least one live probe every run, because
the user can move the sensitivity slider between runs and nothing tells us.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Any

from prospector_engine.contracts import EvidenceStatus, Provenance, monotonic_s

__all__ = [
    "ControlFingerprint",
    "TurnBackend",
    "TurnCharacterizer",
    "TurnLimits",
    "TurnObservation",
    "TurnPlan",
    "TurnProbe",
    "TurnResponse",
    "TurnResponseCache",
    "wrap_deg",
]


def wrap_deg(degrees: float) -> float:
    """Wrap to (-180, 180]. Correct across the +-180 seam."""
    wrapped = (degrees + 180.0) % 360.0 - 180.0
    return 180.0 if wrapped == -180.0 else wrapped


class TurnBackend(Enum):
    """How the camera is rotated.

    Ordered by preference. Arrow keys are tried first because they are
    quantised, self-limiting and cannot drag the pointer out of the window;
    relative mouse yaw is the fallback, and is the only one that works if the
    client does not bind the arrow keys to the camera.
    """

    ARROW_KEYS = "arrow_keys"
    MOUSE_YAW = "mouse_yaw"

    @property
    def unit_name(self) -> str:
        """What one command unit means for this backend."""
        return "ms held" if self is TurnBackend.ARROW_KEYS else "mouse units"

    @property
    def label(self) -> str:
        return "arrow keys" if self is TurnBackend.ARROW_KEYS else "mouse yaw"


#: The order :class:`TurnCharacterizer` tries backends in.
BACKEND_ORDER: tuple[TurnBackend, ...] = (TurnBackend.ARROW_KEYS, TurnBackend.MOUSE_YAW)


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ControlFingerprint:
    """Everything a measured turn response is only valid *for*.

    Carrying it means a measurement cannot silently follow the user to a
    different machine, a resized window, a swapped profile, or a changed
    sensitivity slider - each of which changes what the number was a
    measurement of.
    """

    os_name: str
    backend: str
    client_fingerprint: str
    camera_sensitivity: str
    control_mode: str
    viewport_identity: tuple[object, ...]
    profile_id: str
    profile_revision: int
    supported_min_fps: int

    _FIELDS = (
        "os_name",
        "backend",
        "client_fingerprint",
        "camera_sensitivity",
        "control_mode",
        "viewport_identity",
        "profile_id",
        "profile_revision",
        "supported_min_fps",
    )

    def matches(self, other: ControlFingerprint) -> bool:
        return self == other

    def matches_except_backend(self, other: ControlFingerprint) -> bool:
        """Same machine, window, profile and settings - whatever the backend.

        The characterizer's job is to *choose* the backend, so it must be able
        to recognise a prior for this machine before it knows which actuator
        that prior is about.
        """
        return replace(self, backend="") == replace(other, backend="")

    def mismatches(self, other: ControlFingerprint) -> tuple[str, ...]:
        """Which fields differ, for a message a person can act on."""
        return tuple(
            f"{name}: {getattr(self, name)!r} != {getattr(other, name)!r}"
            for name in self._FIELDS
            if getattr(self, name) != getattr(other, name)
        )

    def cache_key(self) -> str:
        """A stable string key for the on-disk prior cache."""
        parts = [str(getattr(self, name)) for name in self._FIELDS]
        return "|".join(parts)


#: Backwards-compatible name for the steering module's original spelling.
CalibrationFingerprint = ControlFingerprint


# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TurnLimits:
    """Bounds on what a probe or a correction may ask for.

    Every one of these is a *ceiling*, not a target. The characterizer starts
    at the smallest probe that could possibly be observed and escalates only
    while the camera has not moved, so a machine with a very sensitive camera
    never sees the larger magnitudes at all.
    """

    #: Probe magnitudes, smallest first, in the backend's own units.
    mouse_probe_units: tuple[int, ...] = (6, 12, 24, 48, 96)
    #: Key-hold probes are capped at what **one** evidence-bound lease can
    #: hold. During characterization a probe is a single command with no
    #: renewal - there is no accepted newer frame to renew it with, because the
    #: whole point is to observe the frames that follow it - so a request
    #: longer than ``AuthorityConfig.max_evidence_age_ms`` would silently be
    #: cut short and the measured gain would be wrong by whatever fraction was
    #: lost. A camera that needs more than 100 ms of held key to move at all
    #: fails this backend and falls through to mouse yaw, which is the honest
    #: outcome (D-038).
    key_probe_ms: tuple[int, ...] = (25, 40, 60, 85, 100)
    #: Rotation below which a probe counts as "the camera did not move".
    min_observable_deg: float = 1.2
    #: Rotation above which a probe is discarded as contaminated - the player
    #: moved the mouse, or the character was bumped.
    max_probe_deg: float = 75.0
    #: Fresh accepted frames to wait after a probe before reading the result.
    observe_frames: int = 3
    #: Wall-clock ceiling on waiting for those frames.
    observe_timeout_s: float = 0.8
    #: Consecutive stationary frames required before the first probe.
    settle_frames: int = 3
    #: Probes per direction. Two directions, so this many pairs.
    repeats_per_direction: int = 2
    #: Whole-characterization budgets.
    max_probes: int = 24
    max_duration_s: float = 25.0
    #: Fraction of probes that must produce the expected sign to be usable.
    min_reliability: float = 0.75
    #: Ceiling on one correction, so a bad estimate cannot spin the camera.
    max_correction_deg: float = 14.0

    provenance: Provenance = field(
        default_factory=lambda: Provenance(
            status=EvidenceStatus.PROVISIONAL,
            source="TREASURE_NAVIGATION_PLAN.md section 9.1; mission section C",
            note="probe ladders and budgets are chosen bounds, not measurements",
        )
    )


# ---------------------------------------------------------------------------
# The measured response
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TurnResponse:
    """What one backend was measured to do, in this run, on this machine.

    ``degrees_per_unit`` is always positive: it is a magnitude. Which way a
    positive command turns lives in ``positive_is_right``, separately, because
    the sign and the scale fail independently and conflating them makes a
    sign error look like a gain error.
    """

    backend: TurnBackend
    fingerprint: ControlFingerprint
    degrees_per_unit: float
    positive_is_right: bool
    min_effective_units: int
    max_units: int
    latency_s: float
    reliability: float
    samples: int
    measured_at_s: float
    status: EvidenceStatus = EvidenceStatus.PENDING
    provenance: Provenance = field(
        default_factory=lambda: Provenance(
            status=EvidenceStatus.PENDING,
            source="TurnCharacterizer: bounded stationary probes in this run",
            note="no probes have been observed",
        )
    )
    #: How long the measurement stays good without re-observation. The player
    #: can change the sensitivity slider or leave Shift Lock at any moment.
    max_age_s: float = 300.0

    @property
    def usable(self) -> bool:
        return (
            self.status is EvidenceStatus.VALIDATED
            and self.degrees_per_unit > 0.0
            and self.min_effective_units > 0
            and self.samples > 0
            and self.reliability > 0.0
        )

    def expired(self, now_s: float) -> bool:
        return now_s - self.measured_at_s > self.max_age_s

    def valid_for(self, fingerprint: ControlFingerprint, *, now_s: float) -> tuple[bool, str]:
        """Whether this measurement still covers the situation it is used in."""
        if not self.usable:
            return (False, f"the turn response is {self.status.value}")
        mismatches = self.fingerprint.mismatches(fingerprint)
        if mismatches:
            return (False, f"conditions changed since it was measured: {mismatches[0]}")
        if self.expired(now_s):
            return (False, "the turn response has not been re-measured recently")
        return (True, "measured")

    def settled_after_s(self, issued_at_s: float) -> float:
        """When a pulse issued at ``issued_at_s`` may be read back.

        One pulse is in flight at a time. A second pulse issued before this
        instant would be steering on evidence that predates the first.
        """
        return issued_at_s + self.latency_s

    def units_for(self, degrees: float) -> int:
        """Command units for a requested rotation. Signed, bounded, never zero-rounded.

        Below the measured minimum effective movement the request is rounded
        *up* to that minimum rather than down to zero: a command that cannot
        move anything is worse than the smallest one that can, and the deadband
        upstream is what decides whether to ask at all.
        """
        if not self.usable or degrees == 0.0:
            return 0
        magnitude = abs(degrees) / self.degrees_per_unit
        units = max(self.min_effective_units, round(magnitude))
        units = min(units, self.max_units)
        sign = 1 if degrees >= 0 else -1
        if not self.positive_is_right:
            sign = -sign
        return sign * units

    def plan_for(self, degrees: float, limits: TurnLimits) -> TurnPlan:
        """Turn a requested rotation into one bounded actuator command."""
        clamped = max(-limits.max_correction_deg, min(limits.max_correction_deg, degrees))
        units = self.units_for(clamped)
        if units == 0:
            return TurnPlan.none(self.backend, requested_deg=degrees)
        expected = abs(units) * self.degrees_per_unit
        expected = expected if clamped >= 0 else -expected
        if self.backend is TurnBackend.ARROW_KEYS:
            # For a held key the unit *is* the hold, so the sign selects which
            # key and the magnitude is the duration.
            return TurnPlan(
                backend=self.backend,
                turn_axis=1 if units > 0 else -1,
                yaw_delta_px=0,
                hold_ms=abs(units),
                requested_deg=degrees,
                expected_deg=expected,
            )
        return TurnPlan(
            backend=self.backend,
            turn_axis=0,
            yaw_delta_px=units,
            hold_ms=0,
            requested_deg=degrees,
            expected_deg=expected,
        )

    def with_observation(self, commanded_units: int, observed_deg: float) -> TurnResponse:
        """Fold one live observation into the measured gain.

        A bounded exponential update with a hard ratio clamp: one strange frame
        may nudge the model, never redefine it. This is what makes the loop
        adaptive without making it unstable - the clamp is the difference
        between learning and chasing noise.
        """
        if not self.usable or commanded_units == 0 or abs(observed_deg) < 1e-6:
            return self
        signed_units = commanded_units if self.positive_is_right else -commanded_units
        if (signed_units > 0) != (observed_deg > 0):
            # The camera went the other way. That is a reliability event, not a
            # gain event: refuse to fold a contradiction into the magnitude.
            return replace(
                self,
                reliability=max(0.0, self.reliability * 0.8),
                samples=self.samples + 1,
            )
        measured = abs(observed_deg) / abs(commanded_units)
        ratio = measured / self.degrees_per_unit
        ratio = max(0.5, min(2.0, ratio))
        blended = self.degrees_per_unit * (0.85 + 0.15 * ratio)
        return replace(
            self,
            degrees_per_unit=blended,
            samples=self.samples + 1,
            reliability=min(1.0, self.reliability * 0.98 + 0.02),
        )

    def describe(self) -> str:
        if not self.usable:
            return f"{self.backend.label}: {self.status.value}"
        direction = "right" if self.positive_is_right else "left"
        return (
            f"{self.backend.label}: {self.degrees_per_unit:.3g} deg per "
            f"{self.backend.unit_name}, + turns {direction}, "
            f"min {self.min_effective_units}, latency {self.latency_s * 1000:.0f} ms, "
            f"{self.reliability * 100:.0f}% over {self.samples} probes"
        )

    def as_json(self) -> dict[str, Any]:
        return {
            "backend": self.backend.value,
            "degrees_per_unit": self.degrees_per_unit,
            "positive_is_right": self.positive_is_right,
            "min_effective_units": self.min_effective_units,
            "max_units": self.max_units,
            "latency_s": self.latency_s,
            "reliability": self.reliability,
            "samples": self.samples,
        }


@dataclass(frozen=True)
class TurnPlan:
    """One bounded actuator command, already expressed in the backend's terms."""

    backend: TurnBackend
    turn_axis: int
    yaw_delta_px: int
    hold_ms: int
    requested_deg: float
    expected_deg: float

    @classmethod
    def none(cls, backend: TurnBackend, *, requested_deg: float = 0.0) -> TurnPlan:
        return cls(backend, 0, 0, 0, requested_deg, 0.0)

    @property
    def moves(self) -> bool:
        return self.turn_axis != 0 or self.yaw_delta_px != 0

    @property
    def units(self) -> int:
        """Magnitude in this backend's units, signed by the command direction."""
        if self.backend is TurnBackend.ARROW_KEYS:
            return self.turn_axis * self.hold_ms
        return self.yaw_delta_px


# ---------------------------------------------------------------------------
# Characterization
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TurnObservation:
    """One frame handed to the characterizer. Everything it may look at."""

    frame_sequence: int
    now_s: float
    #: Signed heading error in degrees, or ``None`` when perception abstained.
    error_deg: float | None
    confidence: float
    #: True when the character is known not to be moving. Probes are only
    #: valid while stationary, so this gates every measurement.
    stationary: bool
    focus_ok: bool = True


class ProbeKind(Enum):
    WAIT = "wait"
    PULSE = "pulse"
    DONE = "done"
    FAILED = "failed"


@dataclass(frozen=True)
class TurnProbe:
    """What the characterizer wants done next. The caller performs it."""

    kind: ProbeKind
    backend: TurnBackend | None = None
    #: Signed magnitude in the backend's units. The caller converts it into a
    #: bounded command; it never scales or reinterprets it.
    units: int = 0
    reason: str = ""
    response: TurnResponse | None = None

    @property
    def done(self) -> bool:
        return self.kind is ProbeKind.DONE

    @property
    def failed(self) -> bool:
        return self.kind is ProbeKind.FAILED


@dataclass
class _Sample:
    backend: TurnBackend
    units: int
    observed_deg: float
    latency_s: float


class TurnCharacterizer:
    """Measures a usable :class:`TurnResponse`, or says why it could not.

    A pure state machine: it consumes :class:`TurnObservation` values and emits
    :class:`TurnProbe` requests. It never touches an input session, which is
    what lets the whole convergence and failure behaviour be tested with no
    input authority in sight and no game running.

    The procedure per backend, with the character stationary throughout:

    1. Wait for ``settle_frames`` consecutive frames whose heading error is
       stable - a moving error means something else is turning the camera.
    2. Issue the smallest probe in the ladder, one direction.
    3. Wait ``observe_frames`` fresh frames, then read the rotation.
    4. If nothing moved, escalate to the next magnitude. If the ladder is
       exhausted, this backend is unproven; try the next one.
    5. Repeat in the other direction, then repeat the pair, so the sign is
       confirmed rather than inferred from one probe.

    Every probe is released by the caller before the next observation, and the
    whole thing is bounded by ``max_probes`` and ``max_duration_s``.
    """

    def __init__(
        self,
        fingerprint: ControlFingerprint,
        *,
        limits: TurnLimits | None = None,
        prior: TurnResponse | None = None,
        backends: Sequence[TurnBackend] = BACKEND_ORDER,
    ) -> None:
        self._fingerprint = fingerprint
        self._limits = limits or TurnLimits()
        self._backends = tuple(backends)
        self._prior = prior
        self.reset()

    # -- lifecycle --------------------------------------------------------
    def reset(self) -> None:
        self._backend_index = 0
        self._ladder_index = 0
        self._direction = 1
        self._pairs_done = 0
        self._settled = 0
        self._last_error: float | None = None
        self._pending: tuple[int, float, float] | None = None  # units, error, issued_at
        self._pending_frames = 0
        self._samples: list[_Sample] = []
        self._probes = 0
        self._started_s: float | None = None
        self._failure: str | None = None
        self._result: TurnResponse | None = None
        # A cached prior for this exact fingerprint starts the ladder at the
        # magnitude it says should work, so confirmation costs one probe rather
        # than a climb. It never skips the confirmation itself.
        if self._prior is not None and self._prior.fingerprint.matches_except_backend(
            self._fingerprint
        ):
            for index, backend in enumerate(self._backends):
                if backend is self._prior.backend:
                    self._backend_index = index
                    self._ladder_index = self._ladder_start_for(self._prior)
                    break

    @property
    def result(self) -> TurnResponse | None:
        return self._result

    @property
    def failure(self) -> str | None:
        return self._failure

    @property
    def backend(self) -> TurnBackend | None:
        if self._backend_index >= len(self._backends):
            return None
        return self._backends[self._backend_index]

    @property
    def probes_issued(self) -> int:
        return self._probes

    def _ladder(self) -> tuple[int, ...]:
        backend = self.backend
        if backend is TurnBackend.ARROW_KEYS:
            return self._limits.key_probe_ms
        return self._limits.mouse_probe_units

    def _ladder_start_for(self, prior: TurnResponse) -> int:
        """Where in the ladder a prior says the first probe should start."""
        ladder = (
            self._limits.key_probe_ms
            if prior.backend is TurnBackend.ARROW_KEYS
            else self._limits.mouse_probe_units
        )
        target = prior.min_effective_units
        for index, magnitude in enumerate(ladder):
            if magnitude >= target:
                return index
        return 0

    # -- the tick ---------------------------------------------------------
    def step(self, observation: TurnObservation) -> TurnProbe:
        """One observation in, one instruction out."""
        if self._result is not None:
            return TurnProbe(ProbeKind.DONE, response=self._result, reason="already measured")
        if self._failure is not None:
            return TurnProbe(ProbeKind.FAILED, reason=self._failure)
        if self._started_s is None:
            self._started_s = observation.now_s

        if observation.now_s - self._started_s > self._limits.max_duration_s:
            return self._fail("the turn actuator did not respond within the time budget")
        if self._probes >= self._limits.max_probes:
            return self._fail("the turn actuator did not respond within the probe budget")
        if not observation.focus_ok:
            self._abandon_pending()
            return TurnProbe(ProbeKind.WAIT, reason="waiting for Roblox to be focused")
        if not observation.stationary:
            self._abandon_pending()
            return TurnProbe(ProbeKind.WAIT, reason="waiting for the character to be still")
        if observation.error_deg is None:
            self._abandon_pending()
            return TurnProbe(ProbeKind.WAIT, reason="waiting for a usable heading reading")

        error = wrap_deg(observation.error_deg)
        if self._pending is not None:
            return self._observe(error, observation)
        return self._maybe_probe(error, observation)

    # -- internals --------------------------------------------------------
    def _abandon_pending(self) -> None:
        """Drop an in-flight probe whose result can no longer be trusted."""
        self._pending = None
        self._pending_frames = 0
        self._settled = 0
        self._last_error = None

    def _maybe_probe(self, error: float, observation: TurnObservation) -> TurnProbe:
        if self._last_error is not None and abs(wrap_deg(error - self._last_error)) <= 1.0:
            self._settled += 1
        else:
            self._settled = 1
        self._last_error = error
        if self._settled < self._limits.settle_frames:
            return TurnProbe(
                ProbeKind.WAIT,
                reason=f"settling ({self._settled}/{self._limits.settle_frames})",
            )

        backend = self.backend
        if backend is None:
            return self._fail("no turn backend could be proven")
        ladder = self._ladder()
        if self._ladder_index >= len(ladder):
            return self._next_backend("no probe magnitude produced camera rotation")
        magnitude = ladder[self._ladder_index]
        units = magnitude * self._direction
        self._pending = (units, error, observation.now_s)
        self._pending_frames = 0
        self._probes += 1
        return TurnProbe(
            ProbeKind.PULSE,
            backend=backend,
            units=units,
            reason=(
                f"{backend.label}: {abs(units)} {backend.unit_name} "
                f"{'right' if self._direction > 0 else 'left'}"
            ),
        )

    def _observe(self, error: float, observation: TurnObservation) -> TurnProbe:
        units, before, issued_at = self._pending or (0, 0.0, 0.0)
        self._pending_frames += 1
        elapsed = observation.now_s - issued_at
        if (
            self._pending_frames < self._limits.observe_frames
            and elapsed < self._limits.observe_timeout_s
        ):
            return TurnProbe(ProbeKind.WAIT, reason="observing the probe")

        # The heading error is measured *to* the arrow. Rotating the camera
        # right reduces a positive error, so the camera's rotation is the
        # negative of the error change.
        rotation = -wrap_deg(error - before)
        self._pending = None
        self._pending_frames = 0
        self._settled = 0
        self._last_error = None

        if abs(rotation) > self._limits.max_probe_deg:
            # Something other than this probe moved the camera. Discard it -
            # folding it in would corrupt the gain with someone else's motion.
            return TurnProbe(ProbeKind.WAIT, reason="probe contaminated; retrying")
        if abs(rotation) < self._limits.min_observable_deg:
            self._ladder_index += 1
            return TurnProbe(ProbeKind.WAIT, reason="no rotation observed; escalating")

        self._samples.append(
            _Sample(
                backend=self.backend or TurnBackend.MOUSE_YAW,
                units=units,
                observed_deg=rotation,
                latency_s=max(0.0, elapsed),
            )
        )
        self._direction = -self._direction
        if self._direction > 0:
            self._pairs_done += 1
        if self._pairs_done >= self._limits.repeats_per_direction:
            return self._finish()
        return TurnProbe(ProbeKind.WAIT, reason="probing the other direction")

    def _next_backend(self, reason: str) -> TurnProbe:
        self._backend_index += 1
        self._ladder_index = 0
        self._direction = 1
        self._pairs_done = 0
        self._samples.clear()
        self._abandon_pending()
        if self.backend is None:
            return self._fail(f"no turn actuator responded: {reason}")
        return TurnProbe(ProbeKind.WAIT, reason=f"{reason}; trying {self.backend.label}")

    def _fail(self, reason: str) -> TurnProbe:
        self._failure = reason
        return TurnProbe(ProbeKind.FAILED, reason=reason)

    def _finish(self) -> TurnProbe:
        samples = [s for s in self._samples if s.backend is self.backend]
        if not samples:
            return self._next_backend("no usable probes")
        right_positive = [s for s in samples if s.units > 0]
        agreeing = sum(1 for s in right_positive if s.observed_deg > 0)
        disagreeing = len(right_positive) - agreeing
        left_negative = [s for s in samples if s.units < 0]
        agreeing += sum(1 for s in left_negative if s.observed_deg < 0)
        disagreeing += sum(1 for s in left_negative if s.observed_deg >= 0)
        positive_is_right = agreeing >= disagreeing
        consistent = agreeing if positive_is_right else disagreeing
        reliability = consistent / max(1, len(samples))
        if reliability < self._limits.min_reliability:
            return self._next_backend(
                f"the camera turned inconsistently ({reliability * 100:.0f}% agreement)"
            )

        gains = [abs(s.observed_deg) / abs(s.units) for s in samples if s.units]
        gains.sort()
        gain = gains[len(gains) // 2]
        if gain <= 0.0:
            return self._next_backend("the measured rotation per unit was zero")
        latencies = sorted(s.latency_s for s in samples)
        latency = latencies[len(latencies) // 2]
        ladder = self._ladder()
        smallest = ladder[min(self._ladder_index, len(ladder) - 1)]
        min_units = max(1, round(self._limits.min_observable_deg / gain))
        min_units = min(min_units, smallest)
        backend = self.backend or TurnBackend.MOUSE_YAW
        self._result = TurnResponse(
            backend=backend,
            fingerprint=replace(self._fingerprint, backend=backend.value),
            degrees_per_unit=gain,
            positive_is_right=positive_is_right,
            min_effective_units=min_units,
            max_units=ladder[-1],
            latency_s=max(0.03, latency),
            reliability=reliability,
            samples=len(samples),
            measured_at_s=monotonic_s(),
            status=EvidenceStatus.VALIDATED,
            provenance=Provenance(
                status=EvidenceStatus.VALIDATED,
                source="TurnCharacterizer: bounded stationary probes in this run",
                note=(
                    f"{len(samples)} probes on {backend.label}, both directions, "
                    f"character stationary; session-scoped and re-measured every run"
                ),
            ),
        )
        return TurnProbe(ProbeKind.DONE, backend=backend, response=self._result)


# ---------------------------------------------------------------------------
# Prior cache
# ---------------------------------------------------------------------------


class TurnResponseCache:
    """A small on-disk store of *priors*, never of proofs.

    A hit shortens characterization; it can never replace it. Nothing here is
    read without the fingerprint matching exactly, and nothing read is used
    until at least one live probe has confirmed it in the current run.
    """

    VERSION = 1
    MAX_ENTRIES = 24

    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self, fingerprint: ControlFingerprint) -> TurnResponse | None:
        entries = self._read()
        raw = entries.get(fingerprint.cache_key())
        if not isinstance(raw, dict):
            return None
        try:
            backend = TurnBackend(str(raw["backend"]))
            return TurnResponse(
                backend=backend,
                fingerprint=replace(fingerprint, backend=backend.value),
                degrees_per_unit=float(raw["degrees_per_unit"]),
                positive_is_right=bool(raw["positive_is_right"]),
                min_effective_units=int(raw["min_effective_units"]),
                max_units=int(raw["max_units"]),
                latency_s=float(raw["latency_s"]),
                reliability=float(raw["reliability"]),
                samples=int(raw["samples"]),
                measured_at_s=monotonic_s(),
                # A prior is PROVISIONAL by construction. Only the live
                # characterizer may mint a VALIDATED response.
                status=EvidenceStatus.PROVISIONAL,
                provenance=Provenance(
                    status=EvidenceStatus.PROVISIONAL,
                    source=f"turn-response cache {self._path.name}",
                    note="a prior from a previous run; re-confirmed by live probes",
                ),
            )
        except (KeyError, TypeError, ValueError):
            return None

    def save(self, response: TurnResponse) -> None:
        if not response.usable:
            return
        entries = self._read()
        entries[response.fingerprint.cache_key()] = response.as_json()
        # Bounded: drop the oldest keys rather than growing without limit.
        while len(entries) > self.MAX_ENTRIES:
            entries.pop(next(iter(entries)))
        payload = {"version": self.VERSION, "entries": entries}
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self._path.with_suffix(".tmp")
            temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            temporary.replace(self._path)
        except OSError:
            # A cache that cannot be written costs a second of probing next
            # run. It is never a reason to fail a navigation session.
            return

    def _read(self) -> dict[str, Any]:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(raw, dict) or raw.get("version") != self.VERSION:
            return {}
        entries = raw.get("entries")
        return entries if isinstance(entries, dict) else {}
