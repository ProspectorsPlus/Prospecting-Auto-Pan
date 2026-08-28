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


# ---------------------------------------------------------------------------
# Fitting is a transaction, not a race
# ---------------------------------------------------------------------------


def test_a_mid_resize_readback_is_not_a_capture_mismatch() -> None:
    """The race that made Fit look like it did nothing.

    ``check()`` is an honest reporter: it sees a window that is not the size we
    adopted and says so. During a resize that is exactly what was asked for, so
    classifying it as CAPTURE_MISMATCH restarted capture, churned the source
    epoch and blanked the preview - for a change that was going to succeed.
    """
    port = _port()
    guard = _guard(port)
    guard.connect()
    before = guard.revision

    with guard.transaction("fit"):
        port.set_geometry(make_geometry(size=(1024.0, 768.0)))
        for _ in range(5):
            geometry = guard.check()
            assert geometry.state is not ViewportState.CAPTURE_MISMATCH
        assert guard.revision == before, "an expected intermediate must not churn the basis"


def test_a_frame_delivered_against_the_old_geometry_is_not_a_mismatch_either() -> None:
    port = _port()
    guard = _guard(port)
    guard.connect()

    with guard.transaction("fit"):
        result = guard.confirm_capture((640, 360))
        assert result.state is not ViewportState.CAPTURE_MISMATCH

    # Outside the transaction the same delivery is reported honestly.
    assert guard.confirm_capture((640, 360)).state is ViewportState.CAPTURE_MISMATCH


def test_the_fence_is_bounded_so_a_dead_transaction_cannot_blind_the_guard() -> None:
    port = _port()
    guard = _guard(port, fit_transaction_deadline_s=0.0)
    guard.connect()

    with guard.transaction("fit"):
        port.set_geometry(make_geometry(size=(1024.0, 768.0)))
        assert not guard.fenced, "an expired fence must stop suppressing"
        assert guard.check().state is ViewportState.CAPTURE_MISMATCH


def test_the_fence_is_reentrant_and_unwinds_completely() -> None:
    guard = _guard(_port())
    with guard.transaction("outer"):
        with guard.transaction("inner"):
            assert guard.fenced
        assert guard.fenced, "the outer transaction is still open"
    assert not guard.fenced
    assert guard.fence_reason == ""


def test_the_fence_unwinds_even_when_the_transaction_raises() -> None:
    guard = _guard(_port())
    with pytest.raises(RuntimeError), guard.transaction("boom"):
        raise RuntimeError("scripted")
    assert not guard.fenced


def test_a_fit_runs_inside_its_own_transaction() -> None:
    """The window passes through sizes nobody should react to."""
    port = _port()
    port.settle_reads = 2
    guard = _guard(port)
    guard.connect()
    seen: list[bool] = []

    real_geometry = port.window_geometry

    def observing_geometry() -> object:
        seen.append(guard.fenced)
        return real_geometry()

    port.window_geometry = observing_geometry  # type: ignore[method-assign]
    guard.fit_and_lock(sleep=_instant)

    assert seen and all(seen), "every read-back during a fit is inside the fence"
    assert not guard.fenced, "and the fence is gone afterwards"


def test_repeated_fits_while_geometry_is_read_stay_stable() -> None:
    """Twenty fits with a concurrent reader: no mismatch, no revision churn."""
    import threading

    port = _port()
    port.settle_reads = 1
    guard = _guard(port)
    guard.connect()
    stop = threading.Event()
    mismatches: list[str] = []

    def poll() -> None:
        while not stop.is_set():
            state = guard.check().state
            if state is ViewportState.CAPTURE_MISMATCH:
                mismatches.append(state.value)

    reader = threading.Thread(target=poll, daemon=True)
    reader.start()
    try:
        for _ in range(20):
            fit = guard.fit_and_lock(sleep=_instant)
            assert fit.ok
    finally:
        stop.set()
        reader.join(2.0)

    assert mismatches == [], f"{len(mismatches)} false mismatches during fitting"
    assert guard.geometry.state is ViewportState.CANONICAL_VERIFIED


def test_a_second_fit_while_one_is_running_is_refused_not_queued() -> None:
    import threading

    port = _port()
    port.settle_reads = 3
    guard = _guard(port, fit_readback_interval_s=0.02)
    guard.connect()
    results: list[object] = []

    def run() -> None:
        results.append(guard.fit_and_lock())

    first = threading.Thread(target=run, daemon=True)
    first.start()
    try:
        # The concurrent call returns the in-flight fit rather than starting a
        # second resize; the port's pin counter is what proves it.
        guard.fit_and_lock()
    finally:
        first.join(5.0)

    assert port.pin_calls <= 2, f"{port.pin_calls} resize requests for one fit"


def test_a_retina_fit_reports_points_and_backing_pixels_separately() -> None:
    """A 1280x720 point client is 2560x1440 device pixels; both are recorded."""
    port = _port(geometry=make_geometry(backing_scale=2.0, canonical_px=(2560, 1440)))
    guard = _guard(port)

    fit = guard.fit_and_lock(sleep=_instant)

    assert fit.achieved_client_logical == (1280.0, 720.0)
    assert fit.achieved_client_backing_px == (2560, 1440)
    assert "px" in fit.describe() and "pt" in fit.describe()


# ---------------------------------------------------------------------------
# The guard-versus-source boundary, found natively
# ---------------------------------------------------------------------------
#
# Every defect below produced the same symptom on the real client - automatic
# setup failing with ``capture_stale`` on a window that was sitting there
# correctly fitted - and none of them was visible to a test that drove the
# guard alone. They are pinned here so a fix cannot be undone silently.


def test_a_verified_fit_survives_the_next_poll() -> None:
    """``window_geometry()`` can never report CANONICAL_VERIFIED.

    That verdict belongs to the guard: the port only ever reports the window
    as it finds it, ADOPTED_NONCANONICAL. ``check()`` compared *full*
    identities - state included - so every poll after a successful fit
    compared "verified" against "adopted", called the difference a
    CAPTURE_MISMATCH, and un-did the fit it had just achieved.
    """
    from prospector_engine.capture import ViewportGuard
    from prospector_engine.geometry import ViewportState
    from tests.fakes import FakePlatformPort, VirtualClock, make_geometry

    port = FakePlatformPort(VirtualClock(), geometry=make_geometry(size=(1280.0, 720.0)))
    guard = ViewportGuard(port)
    guard.connect()
    fit = guard.fit_and_lock()
    assert fit.phase.name in {"CANONICAL_VERIFIED", "VERIFIED"}, fit.phase
    assert guard.geometry.state is ViewportState.CANONICAL_VERIFIED

    for _ in range(10):
        state = guard.check().state
        assert state is ViewportState.CANONICAL_VERIFIED, (
            f"a poll downgraded the verified fit to {state.value}"
        )


def test_polling_an_unchanged_window_does_not_churn_the_revision() -> None:
    """The revision storm: forty state flips a second, each rebuilding capture."""
    from prospector_engine.capture import ViewportGuard
    from tests.fakes import FakePlatformPort, VirtualClock, make_geometry

    port = FakePlatformPort(VirtualClock(), geometry=make_geometry(size=(1280.0, 720.0)))
    guard = ViewportGuard(port)
    guard.connect()
    guard.fit_and_lock()
    settled = guard.revision
    for _ in range(20):
        guard.check()
    assert guard.revision == settled, "an unchanged window advanced the revision"


def test_state_alone_is_not_a_change_of_coordinate_basis() -> None:
    """State says how we adopted; the rect says how pixels map to points."""
    from prospector_engine.geometry import ViewportState
    from tests.fakes import make_geometry

    adopted = make_geometry(size=(1280.0, 720.0))
    verified = adopted.with_state(ViewportState.CANONICAL_VERIFIED, "fitted")
    assert verified.same_source(adopted), "an adoption verdict invalidated coordinates"
    assert verified.coordinate_basis() == adopted.coordinate_basis()


def test_a_lost_pin_can_be_re_adopted() -> None:
    """Losing the adoption used to be permanent.

    One transient bad read un-adopted the viewport, and from then on every
    check reported "window found but not adopted" however healthy the window
    became - so every delivered frame was rejected as a mismatch forever.
    """
    from prospector_engine.capture import ViewportGuard
    from prospector_engine.geometry import ViewportGeometry, ViewportState
    from tests.fakes import FakePlatformPort, VirtualClock, make_geometry

    good = make_geometry(size=(1280.0, 720.0))
    port = FakePlatformPort(VirtualClock(), geometry=good)
    guard = ViewportGuard(port)
    guard.connect()

    port.set_geometry(ViewportGeometry.invalid("one bad read"))
    assert not guard.check().valid

    port.set_geometry(good)
    healed = guard.check()
    assert healed.valid, "a healthy window stayed un-adopted forever"
    assert healed.state is not ViewportState.UNPINNED
