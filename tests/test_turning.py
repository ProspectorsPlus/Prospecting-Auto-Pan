"""The turn actuator: automatic characterization, the response model, priors.

These tests drive a *simulated camera* rather than a game: a gain, a sign, a
latency and an optional dead zone. That is enough to exercise every branch the
real characterizer has - sign discovery, magnitude escalation, backend
fallback, contamination, and both budget exhaustions - deterministically and
with no input authority anywhere in sight.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from prospector_engine.contracts import EvidenceStatus
from prospector_engine.turning import (
    BACKEND_ORDER,
    ControlFingerprint,
    TurnBackend,
    TurnCharacterizer,
    TurnLimits,
    TurnObservation,
    TurnResponse,
    TurnResponseCache,
    wrap_deg,
)

FINGERPRINT = ControlFingerprint(
    os_name="macos",
    backend="unset",
    client_fingerprint="roblox-test",
    camera_sensitivity="default",
    control_mode="shift_lock",
    viewport_identity=(1280, 720),
    profile_id="green_arrow_v1",
    profile_revision=1,
    supported_min_fps=30,
)


class Camera:
    """A camera with a gain, a sign, a latency and an optional dead zone.

    ``error_deg`` is the signed heading error to the arrow, so rotating the
    camera right (positive command, when ``positive_is_right``) *reduces* it.
    """

    def __init__(
        self,
        *,
        gain_per_unit: float = 0.25,
        positive_is_right: bool = True,
        dead_zone_units: int = 0,
        responds_to: TurnBackend | None = None,
        latency_frames: int = 1,
        jitter_deg: float = 0.0,
    ) -> None:
        self.gain = gain_per_unit
        self.positive_is_right = positive_is_right
        self.dead_zone = dead_zone_units
        self.responds_to = responds_to
        self.latency_frames = latency_frames
        self.jitter = jitter_deg
        self.error_deg = 40.0
        self.now_s = 0.0
        self.sequence = 0
        self._queued: list[tuple[int, int]] = []
        self.pulses: list[tuple[TurnBackend, int]] = []

    def pulse(self, backend: TurnBackend, units: int) -> None:
        self.pulses.append((backend, units))
        if self.responds_to is not None and backend is not self.responds_to:
            return
        if abs(units) <= self.dead_zone:
            return
        self._queued.append((self.latency_frames, units))

    def tick(self) -> TurnObservation:
        self.sequence += 1
        self.now_s += 1.0 / 60.0
        remaining: list[tuple[int, int]] = []
        for countdown, units in self._queued:
            if countdown <= 1:
                rotation = units * self.gain
                if not self.positive_is_right:
                    rotation = -rotation
                self.error_deg = wrap_deg(self.error_deg - rotation)
            else:
                remaining.append((countdown - 1, units))
        self._queued = remaining
        if self.jitter:
            self.error_deg = wrap_deg(self.error_deg + self.jitter)
        return TurnObservation(
            frame_sequence=self.sequence,
            now_s=self.now_s,
            error_deg=self.error_deg,
            confidence=0.9,
            stationary=True,
        )


def _run(
    characterizer: TurnCharacterizer, camera: Camera, *, max_frames: int = 900
) -> TurnResponse | None:
    for _ in range(max_frames):
        probe = characterizer.step(camera.tick())
        if probe.done:
            return probe.response
        if probe.failed:
            return None
        if probe.kind.value == "pulse":
            assert probe.backend is not None
            camera.pulse(probe.backend, probe.units)
    raise AssertionError("characterization did not terminate")


# ---------------------------------------------------------------------------
# Convergence
# ---------------------------------------------------------------------------


def test_it_measures_gain_sign_and_latency_from_probes_alone() -> None:
    camera = Camera(gain_per_unit=0.3, positive_is_right=True)
    response = _run(TurnCharacterizer(FINGERPRINT), camera)
    assert response is not None
    assert response.status is EvidenceStatus.VALIDATED
    assert response.positive_is_right is True
    assert response.degrees_per_unit == pytest.approx(0.3, rel=0.25)
    assert response.usable
    assert response.samples >= 2


def test_an_inverted_camera_is_measured_not_assumed() -> None:
    camera = Camera(gain_per_unit=0.3, positive_is_right=False)
    response = _run(TurnCharacterizer(FINGERPRINT), camera)
    assert response is not None
    assert response.positive_is_right is False
    # A left request must produce a command that the inverted camera turns left.
    units = response.units_for(-6.0)
    assert units > 0


def test_a_dead_zone_escalates_the_probe_ladder_rather_than_giving_up() -> None:
    camera = Camera(gain_per_unit=0.3, dead_zone_units=30)
    response = _run(TurnCharacterizer(FINGERPRINT), camera)
    assert response is not None
    assert max(abs(units) for _b, units in camera.pulses) > 30


def test_a_backend_that_does_nothing_falls_through_to_the_next_one() -> None:
    camera = Camera(gain_per_unit=0.3, responds_to=TurnBackend.MOUSE_YAW)
    response = _run(TurnCharacterizer(FINGERPRINT), camera)
    assert response is not None
    assert response.backend is TurnBackend.MOUSE_YAW
    assert {backend for backend, _u in camera.pulses} == set(BACKEND_ORDER)


def test_no_backend_responding_fails_closed_with_one_sentence() -> None:
    camera = Camera(gain_per_unit=0.0, responds_to=None, dead_zone_units=10_000)
    characterizer = TurnCharacterizer(FINGERPRINT)
    assert _run(characterizer, camera) is None
    assert characterizer.failure
    assert "turn" in (characterizer.failure or "").lower()


def test_the_probe_budget_is_bounded() -> None:
    camera = Camera(gain_per_unit=0.0, dead_zone_units=10_000)
    limits = TurnLimits(max_probes=5, max_duration_s=1e6)
    characterizer = TurnCharacterizer(FINGERPRINT, limits=limits)
    assert _run(characterizer, camera) is None
    assert characterizer.probes_issued <= 5


def test_the_time_budget_is_bounded() -> None:
    camera = Camera(gain_per_unit=0.0, dead_zone_units=10_000)
    limits = TurnLimits(max_duration_s=0.5, max_probes=10_000)
    characterizer = TurnCharacterizer(FINGERPRINT, limits=limits)
    assert _run(characterizer, camera) is None
    assert "time budget" in (characterizer.failure or "")


# ---------------------------------------------------------------------------
# Gating
# ---------------------------------------------------------------------------


def test_probing_never_starts_while_the_character_is_moving() -> None:
    characterizer = TurnCharacterizer(FINGERPRINT)
    for sequence in range(40):
        probe = characterizer.step(
            TurnObservation(sequence, sequence / 60.0, 30.0, 0.9, stationary=False)
        )
        assert probe.kind.value == "wait"


def test_probing_never_starts_without_a_heading_reading() -> None:
    characterizer = TurnCharacterizer(FINGERPRINT)
    for sequence in range(40):
        probe = characterizer.step(
            TurnObservation(sequence, sequence / 60.0, None, 0.0, stationary=True)
        )
        assert probe.kind.value == "wait"


def test_a_lost_focus_abandons_the_probe_in_flight() -> None:
    camera = Camera()
    characterizer = TurnCharacterizer(FINGERPRINT)
    issued = False
    for _ in range(60):
        probe = characterizer.step(camera.tick())
        if probe.kind.value == "pulse":
            issued = True
            break
    assert issued
    unfocused = replace(camera.tick(), focus_ok=False)
    assert characterizer.step(unfocused).kind.value == "wait"


def test_a_contaminated_probe_is_discarded_rather_than_folded_in() -> None:
    """A 90-degree jump during a 3-degree probe is somebody else's motion."""
    camera = Camera(gain_per_unit=0.3, jitter_deg=0.0)
    characterizer = TurnCharacterizer(FINGERPRINT)
    for _ in range(60):
        probe = characterizer.step(camera.tick())
        if probe.kind.value == "pulse":
            assert probe.backend is not None
            camera.pulse(probe.backend, probe.units)
            camera.error_deg = wrap_deg(camera.error_deg + 120.0)
            break
    reasons = []
    for _ in range(10):
        result = characterizer.step(camera.tick())
        reasons.append(result.reason)
        if result.kind.value == "pulse":
            break
    assert any("contaminated" in reason for reason in reasons), reasons


# ---------------------------------------------------------------------------
# The response model
# ---------------------------------------------------------------------------


def _measured(**overrides: object) -> TurnResponse:
    base: dict[str, object] = {
        "backend": TurnBackend.MOUSE_YAW,
        "fingerprint": replace(FINGERPRINT, backend="mouse_yaw"),
        "degrees_per_unit": 0.5,
        "positive_is_right": True,
        "min_effective_units": 2,
        "max_units": 96,
        "latency_s": 0.05,
        "reliability": 1.0,
        "samples": 4,
        "measured_at_s": 0.0,
        "status": EvidenceStatus.VALIDATED,
    }
    base.update(overrides)
    return TurnResponse(**base)  # type: ignore[arg-type]


def test_a_request_below_the_actuator_resolution_rounds_up_not_to_zero() -> None:
    response = _measured(min_effective_units=4)
    assert abs(response.units_for(0.2)) == 4


def test_units_are_bounded_by_the_measured_saturation() -> None:
    response = _measured(max_units=20)
    assert abs(response.units_for(1000.0)) == 20


def test_a_pending_response_produces_no_command_at_all() -> None:
    response = _measured(status=EvidenceStatus.PENDING)
    assert response.units_for(30.0) == 0
    assert not response.plan_for(30.0, TurnLimits()).moves


def test_a_correction_is_clamped_before_it_becomes_units() -> None:
    limits = TurnLimits(max_correction_deg=10.0)
    plan = _measured().plan_for(90.0, limits)
    assert abs(plan.expected_deg) <= limits.max_correction_deg + 1e-6


def test_arrow_key_plans_use_the_turn_axis_and_mouse_plans_do_not() -> None:
    keys = _measured(backend=TurnBackend.ARROW_KEYS, degrees_per_unit=0.1).plan_for(
        6.0, TurnLimits()
    )
    assert keys.turn_axis == 1 and keys.yaw_delta_px == 0 and keys.hold_ms > 0
    mouse = _measured().plan_for(6.0, TurnLimits())
    assert mouse.turn_axis == 0 and mouse.yaw_delta_px > 0


def test_one_pulse_is_in_flight_at_a_time() -> None:
    response = _measured(latency_s=0.12)
    assert response.settled_after_s(10.0) == pytest.approx(10.12)


def test_a_live_observation_nudges_the_gain_within_a_clamp() -> None:
    response = _measured(degrees_per_unit=0.5)
    faster = response.with_observation(commanded_units=10, observed_deg=50.0)
    assert 0.5 < faster.degrees_per_unit < 0.6  # bounded, not chased


def test_a_reversed_observation_lowers_reliability_and_not_the_gain() -> None:
    response = _measured(degrees_per_unit=0.5, reliability=1.0)
    contradicted = response.with_observation(commanded_units=10, observed_deg=-5.0)
    assert contradicted.degrees_per_unit == pytest.approx(0.5)
    assert contradicted.reliability < 1.0


def test_a_response_expires_and_says_so() -> None:
    response = _measured(measured_at_s=0.0, max_age_s=10.0)
    ok, why = response.valid_for(replace(FINGERPRINT, backend="mouse_yaw"), now_s=1.0)
    assert ok, why
    ok, why = response.valid_for(replace(FINGERPRINT, backend="mouse_yaw"), now_s=999.0)
    assert not ok and "re-measured" in why


def test_a_changed_condition_invalidates_the_response_and_names_the_field() -> None:
    response = _measured()
    moved = replace(FINGERPRINT, backend="mouse_yaw", viewport_identity=(1024, 768))
    ok, why = response.valid_for(moved, now_s=1.0)
    assert not ok and "viewport_identity" in why


# ---------------------------------------------------------------------------
# Priors
# ---------------------------------------------------------------------------


def test_a_cached_prior_is_provisional_and_never_validated(tmp_path: Path) -> None:
    cache = TurnResponseCache(tmp_path / "turn.json")
    cache.save(_measured())
    prior = cache.load(replace(FINGERPRINT, backend="mouse_yaw"))
    assert prior is not None
    assert prior.status is EvidenceStatus.PROVISIONAL
    assert not prior.usable  # a prior alone can never steer


def test_a_prior_for_a_different_fingerprint_is_not_offered(tmp_path: Path) -> None:
    cache = TurnResponseCache(tmp_path / "turn.json")
    cache.save(_measured())
    other = replace(FINGERPRINT, backend="mouse_yaw", camera_sensitivity="raised")
    assert cache.load(other) is None


def test_a_prior_starts_the_ladder_higher_but_still_confirms_live(tmp_path: Path) -> None:
    cache = TurnResponseCache(tmp_path / "turn.json")
    cache.save(_measured(backend=TurnBackend.MOUSE_YAW, min_effective_units=40))
    prior = cache.load(replace(FINGERPRINT, backend="mouse_yaw"))
    camera = Camera(gain_per_unit=0.3)
    characterizer = TurnCharacterizer(FINGERPRINT, prior=prior)
    response = _run(characterizer, camera)
    assert response is not None
    assert response.status is EvidenceStatus.VALIDATED
    assert camera.pulses  # it still probed
    assert camera.pulses[0][0] is TurnBackend.MOUSE_YAW
    assert abs(camera.pulses[0][1]) >= 40


def test_a_corrupt_cache_file_is_ignored(tmp_path: Path) -> None:
    path = tmp_path / "turn.json"
    path.write_text("{not json", encoding="utf-8")
    assert TurnResponseCache(path).load(FINGERPRINT) is None
