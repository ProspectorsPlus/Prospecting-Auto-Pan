"""The bounded Fit & Lock viewport state machine, and coordinate honesty.

Two properties are being defended here.

**Connecting must not depend on resizing.** ``connect()`` binds to whatever the
client already is and never touches it; ``fit_and_lock()`` is the separate,
optional operation that asks for a resize. Capture that only works after a
successful resize is capture that does not work.

**An achieved size is believed only after it stops moving.** A window answers a
resize late, partially, or not at all, so the machine requires three
consecutive agreeing read-backs, has a monotonic deadline, and has a hard
attempt cap - which is what makes an oscillating resize loop impossible rather
than unlikely.
"""

from __future__ import annotations

import pytest

from prospector_engine.capture import CaptureConfig, ViewportGuard
from prospector_engine.contracts import FitPhase
from prospector_engine.geometry import (
    CANONICAL_SIZE_PX,
    CoordinateSpace,
    ViewportGeometry,
    ViewportState,
)
from tests.fakes import FakePlatformPort, VirtualClock, make_geometry

CANONICAL = (1280.0, 720.0)


def _guard(port: FakePlatformPort, **config: object) -> ViewportGuard:
    return ViewportGuard(
        port,
        CANONICAL,
        config=CaptureConfig(**config),  # type: ignore[arg-type]
    )


def _instant(_seconds: float) -> None:
    """The fit machine's only wall-clock dependency, removed for tests."""


def _port(**kwargs: object) -> FakePlatformPort:
    clock = VirtualClock()
    return FakePlatformPort(clock, **kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Connect never resizes
# ---------------------------------------------------------------------------


def test_connecting_binds_to_the_client_without_resizing_it() -> None:
    port = _port(geometry=make_geometry(size=(1600.0, 900.0)))
    guard = _guard(port)

    geometry = guard.connect()

    assert port.pin_calls == 0, "connect must never ask the OS to resize"
    assert geometry.state is ViewportState.ADOPTED_NONCANONICAL
    assert geometry.client_logical is not None
    assert geometry.client_logical.size == (1600.0, 900.0)
    assert guard.fit.phase is FitPhase.IDLE


def test_an_adopted_client_still_normalizes_into_the_canonical_raster() -> None:
    """Canonical processing resolution is independent of physical client size."""
    port = _port(geometry=make_geometry(size=(1600.0, 900.0)))
    geometry = _guard(port).connect()

    assert geometry.canonical_px == CANONICAL_SIZE_PX
    transform = geometry.canonical_from_client_logical
    assert transform.source is CoordinateSpace.CLIENT_LOGICAL
    assert transform.target is CoordinateSpace.CANONICAL
    assert transform.is_uniform, "a non-uniform fit would change measured angles"


# ---------------------------------------------------------------------------
# The fit state machine
# ---------------------------------------------------------------------------


def test_a_clean_resize_reaches_canonical_verified() -> None:
    port = _port(geometry=make_geometry(size=(1600.0, 900.0)))
    fit = _guard(port).fit_and_lock(sleep=_instant)

    assert fit.phase is FitPhase.CANONICAL_VERIFIED
    assert fit.achieved_client_logical == CANONICAL
    assert fit.stable_readbacks >= fit.required_readbacks
    assert fit.ok and not fit.clamped


def test_the_achieved_size_is_believed_only_after_three_stable_readbacks() -> None:
    """A window that is still settling must not be recorded as verified."""
    port = _port(geometry=make_geometry(size=(1600.0, 900.0)))
    port.settle_reads = 2  # two reads still report the old size
    guard = _guard(port)

    fit = guard.fit_and_lock(sleep=_instant)

    assert fit.phase is FitPhase.CANONICAL_VERIFIED
    assert fit.required_readbacks == 3
    # Old size, old size, then three agreeing reads of the new one.
    assert port.geometry_reads >= 5


def test_an_os_clamp_is_reported_truthfully_and_the_result_is_adopted() -> None:
    """Roblox enforces a minimum size. That is an answer, not a failure."""
    port = _port(geometry=make_geometry(size=(1600.0, 900.0)))
    port.min_client_logical = (1366.0, 768.0)
    guard = _guard(port)

    fit = guard.fit_and_lock(sleep=_instant)

    assert fit.phase is FitPhase.ACHIEVED_CLAMPED
    assert fit.clamped and fit.ok
    assert fit.requested_client_logical == CANONICAL
    assert fit.achieved_client_logical == (1366.0, 768.0)
    assert "clamped" in fit.detail
    assert guard.state is ViewportState.ADOPTED_NONCANONICAL
    assert not guard.geometry.state.supports_calibrated_pixels


def test_a_refused_resize_fails_without_looping() -> None:
    port = _port(geometry=make_geometry(size=(1600.0, 900.0)))
    port.pin_should_fail = True
    guard = _guard(port, fit_max_attempts=2)

    fit = guard.fit_and_lock(sleep=_instant)

    assert fit.phase is FitPhase.FAILED
    assert not fit.ok
    assert port.pin_calls <= 2, "the attempt cap is the whole point"


def test_a_size_that_never_stops_moving_fails_at_the_deadline() -> None:
    """No three reads agree, so nothing is ever believed - and it still ends."""
    port = _port(geometry=make_geometry(size=(1600.0, 900.0)))
    port.jitter_px = 3.0
    guard = _guard(port, fit_max_attempts=2, fit_deadline_s=0.05)

    fit = guard.fit_and_lock(sleep=_instant)

    assert fit.phase is FitPhase.FAILED
    assert port.pin_calls <= 2


def test_a_window_that_ignores_the_request_is_a_clamp_not_a_retry_loop() -> None:
    """The size stayed put and is stable. That is an answer; adopt it."""
    port = _port(geometry=make_geometry(size=(1600.0, 900.0)))
    port.settle_reads = 10_000  # the request is accepted but never applied
    guard = _guard(port, fit_max_attempts=3)

    fit = guard.fit_and_lock(sleep=_instant)

    assert fit.phase is FitPhase.ACHIEVED_CLAMPED
    assert fit.achieved_client_logical == (1600.0, 900.0)
    assert port.pin_calls == 1, "an adopted answer must not be re-requested"


def test_twenty_bounded_fit_cycles_do_not_oscillate() -> None:
    """The provisional E-VIEW acceptance shape: 20 cycles, no resize loop."""
    port = _port(geometry=make_geometry(size=(1600.0, 900.0)))
    guard = _guard(port)

    phases = [guard.fit_and_lock(sleep=_instant).phase for _ in range(20)]

    assert set(phases) == {FitPhase.CANONICAL_VERIFIED}
    # One request per cycle, never a retry storm inside one.
    assert port.pin_calls == 20
    # The basis changed exactly once: the first fit. Nineteen no-ops after it.
    assert guard.revision == 1


# ---------------------------------------------------------------------------
# Geometry revisions
# ---------------------------------------------------------------------------


def test_the_geometry_revision_advances_only_when_the_basis_changes() -> None:
    port = _port(geometry=make_geometry(size=(1600.0, 900.0)))
    guard = _guard(port)

    guard.connect()
    first = guard.revision
    guard.connect()
    assert guard.revision == first, "re-reading the same window is not a change"

    port.set_geometry(make_geometry(size=(1280.0, 720.0)))
    guard.connect()
    assert guard.revision == first + 1


@pytest.mark.parametrize(
    "change",
    [
        pytest.param({"window_id": 5555}, id="window-replacement"),
        pytest.param({"display_id": "second"}, id="display-migration"),
        pytest.param({"backing_scale": 2.0}, id="scale-change"),
        pytest.param({"size": (1024.0, 768.0)}, id="resize"),
        pytest.param({"origin": (-1920.0, 0.0)}, id="negative-origin-monitor"),
    ],
)
def test_every_source_change_advances_the_revision(change: dict[str, object]) -> None:
    port = _port(geometry=make_geometry())
    guard = _guard(port)
    guard.connect()
    before = guard.revision

    port.set_geometry(make_geometry(**change))  # type: ignore[arg-type]
    guard.check()

    assert guard.revision == before + 1


def test_losing_the_window_advances_the_revision_and_invalidates() -> None:
    port = _port(geometry=make_geometry())
    guard = _guard(port)
    guard.connect()
    before = guard.revision

    port.set_geometry(ViewportGeometry.invalid("window closed"))
    geometry = guard.check()

    assert not geometry.valid
    assert guard.revision == before + 1


def test_a_notified_revision_change_reaches_its_listener() -> None:
    seen: list[tuple[int, str]] = []
    port = _port(geometry=make_geometry(size=(1600.0, 900.0)))
    guard = ViewportGuard(
        port,
        CANONICAL,
        config=CaptureConfig(),
        on_revision=lambda r, why: seen.append((r, why)),
    )

    guard.connect()
    port.set_geometry(make_geometry(size=(1280.0, 720.0)))
    guard.check()

    assert [reason for _revision, reason in seen] == ["connect", "check"]
    assert [revision for revision, _reason in seen] == [1, 2]


# ---------------------------------------------------------------------------
# Coordinate honesty across DPI and Retina conditions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("backing_scale", [1.0, 2.0])
@pytest.mark.parametrize("dpi_scale", [1.0, 1.25, 1.5, 1.75, 2.0])
def test_transform_round_trips_stay_inside_half_a_canonical_pixel(
    backing_scale: float, dpi_scale: float
) -> None:
    """Provisional E-VIEW acceptance: round-trip error <= 0.5 canonical px."""
    from prospector_engine.geometry import DisplayInfo, LogicalRect

    client = LogicalRect(-1920.0, 137.0, 1600.0, 900.0)
    geometry = make_geometry(origin=(-1920.0, 137.0), size=(1600.0, 900.0))
    geometry = type(geometry)(
        state=geometry.state,
        window=geometry.window,
        display=DisplayInfo(
            "m", LogicalRect(-1920.0, 0.0, 1920.0, 1080.0), backing_scale, dpi_scale
        ),
        frame_logical=geometry.frame_logical,
        client_logical=client,
        canonical_px=CANONICAL_SIZE_PX,
    )

    forward = geometry.canonical_from_client_backing
    backward = forward.inverse()
    for point in ((0.0, 0.0), (640.0, 360.0), (1279.0, 719.0)):
        source = backward.apply_point(point)
        round_tripped = forward.apply_point(source)
        assert abs(round_tripped[0] - point[0]) <= 0.5
        assert abs(round_tripped[1] - point[1]) <= 0.5


def test_a_retina_client_reports_logical_points_and_backing_pixels_separately() -> None:
    """Confusing the two is the original bug; they are asserted apart."""
    port = _port(geometry=make_geometry(size=(1280.0, 720.0), backing_scale=2.0))
    guard = _guard(port)

    fit = guard.fit_and_lock(sleep=_instant)

    assert fit.achieved_client_logical == (1280.0, 720.0)
    assert fit.achieved_client_backing_px == (2560, 1440)
