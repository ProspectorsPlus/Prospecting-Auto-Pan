"""Coordinate spaces, transforms, and capture-region conformance.

The bug these exist to prevent: a rectangle in device pixels was handed to an
API that wanted logical points, so on a 2x display the capture region was twice
the intended size at twice the intended offset and ran off into the desktop.

The conformance tests below use synthetic images with known markers and prove
that what comes out of normalization is exactly the intended client region,
correctly placed, with nothing borrowed from its surroundings.
"""

from __future__ import annotations

import numpy as np
import pytest

from prospector_engine.capture import (
    FrameBufferPool,
    MssCaptureSource,
    normalize_into_canonical,
)
from prospector_engine.geometry import (
    CANONICAL_SIZE_PX,
    Affine2D,
    CoordinateSpace,
    DisplayInfo,
    LogicalRect,
    ViewportGeometry,
    ViewportState,
    WindowIdentity,
)
from tests.fakes import make_geometry

# ---------------------------------------------------------------------------
# Affine transforms
# ---------------------------------------------------------------------------


def test_a_transform_and_its_inverse_round_trip_exactly() -> None:
    transform = Affine2D(
        2.0, 3.0, -17.5, 4.25, CoordinateSpace.CLIENT_LOGICAL, CoordinateSpace.CANONICAL
    )
    for point in [(0.0, 0.0), (12.5, -3.0), (1279.0, 719.0)]:
        forward = transform.apply_point(point)
        back = transform.inverse().apply_point(forward)
        assert back[0] == pytest.approx(point[0])
        assert back[1] == pytest.approx(point[1])


def test_composition_respects_coordinate_spaces() -> None:
    backing_to_logical = Affine2D(
        0.5, 0.5, 0.0, 0.0, CoordinateSpace.CLIENT_BACKING, CoordinateSpace.CLIENT_LOGICAL
    )
    logical_to_canonical = Affine2D(
        2.0, 2.0, 10.0, 20.0, CoordinateSpace.CLIENT_LOGICAL, CoordinateSpace.CANONICAL
    )
    composed = backing_to_logical.then(logical_to_canonical)

    assert composed.source is CoordinateSpace.CLIENT_BACKING
    assert composed.target is CoordinateSpace.CANONICAL
    assert composed.apply(100.0, 200.0) == (110.0, 220.0)


def test_composing_mismatched_spaces_is_refused() -> None:
    """The whole point of naming the spaces is that they cannot be mixed."""
    a = Affine2D(1, 1, 0, 0, CoordinateSpace.CLIENT_BACKING, CoordinateSpace.CLIENT_LOGICAL)
    b = Affine2D(1, 1, 0, 0, CoordinateSpace.CANONICAL, CoordinateSpace.PREVIEW)
    with pytest.raises(ValueError, match="cannot compose"):
        a.then(b)


def test_a_zero_scale_transform_is_refused_because_it_is_not_invertible() -> None:
    with pytest.raises(ValueError, match="non-zero"):
        Affine2D(0.0, 1.0, 0, 0, CoordinateSpace.CANONICAL, CoordinateSpace.PREVIEW)


def test_letterboxing_is_uniform_and_centred() -> None:
    """Uniform, because a stretch would change the angles a controller reads."""
    transform = Affine2D.letterbox(
        (1800.0, 1053.0),
        (1280.0, 720.0),
        source=CoordinateSpace.CLIENT_LOGICAL,
        target=CoordinateSpace.CANONICAL,
    )
    assert transform.is_uniform
    assert transform.scale_x == pytest.approx(720.0 / 1053.0)
    assert transform.translate_y == pytest.approx(0.0)
    assert transform.translate_x > 0  # pillarboxed


# ---------------------------------------------------------------------------
# Viewport geometry
# ---------------------------------------------------------------------------


def _retina_geometry() -> ViewportGeometry:
    return ViewportGeometry(
        state=ViewportState.ADOPTED_NONCANONICAL,
        window=WindowIdentity(1, 2, "Roblox"),
        display=DisplayInfo("1", LogicalRect(0, 0, 1800, 1169), 2.0),
        frame_logical=LogicalRect(0, 39, 1800, 1081),
        client_logical=LogicalRect(0, 67, 1800, 1053),
    )


def test_the_client_is_the_frame_minus_its_insets() -> None:
    geometry = _retina_geometry()
    assert geometry.client_insets_logical == (0.0, 28.0, 0.0, 0.0)
    assert geometry.client_rect_in_window_logical == LogicalRect(0.0, 28.0, 1800.0, 1053.0)


def test_backing_pixels_are_derived_from_logical_units_and_the_scale() -> None:
    geometry = _retina_geometry()
    assert geometry.backing_scale == 2.0
    assert geometry.client_backing_px == (3600, 2106)


def test_canonical_and_client_coordinates_round_trip() -> None:
    geometry = _retina_geometry()
    to_client = geometry.client_logical_from_canonical
    to_canonical = geometry.canonical_from_client_logical
    for point in [(0.0, 0.0), (640.0, 360.0), (1279.0, 719.0)]:
        assert to_canonical.apply_point(to_client.apply_point(point))[0] == pytest.approx(
            point[0]
        )
        assert to_canonical.apply_point(to_client.apply_point(point))[1] == pytest.approx(
            point[1]
        )


def test_the_canonical_centre_maps_to_the_client_centre_on_screen() -> None:
    geometry = _retina_geometry()
    assert geometry.client_logical is not None
    client = geometry.client_logical
    x, y = geometry.display_logical_from_canonical.apply(640.0, 360.0)
    assert x == pytest.approx(client.x + client.width / 2.0)
    assert y == pytest.approx(client.y + client.height / 2.0)


def test_a_canonical_client_needs_no_letterbox() -> None:
    geometry = make_geometry(size=(1280.0, 720.0))
    assert geometry.state is ViewportState.CANONICAL_VERIFIED
    assert geometry.canonical_letterbox_px() == (0, 0, 1280, 720)
    assert geometry.canonical_from_client_logical.scale_x == pytest.approx(1.0)


def test_viewport_state_decides_whether_calibrated_pixels_apply() -> None:
    """ "Viewport ok" and "unsupported viewport size" cannot both be true."""
    assert ViewportState.CANONICAL_VERIFIED.can_capture
    assert ViewportState.CANONICAL_VERIFIED.supports_calibrated_pixels
    assert ViewportState.ADOPTED_NONCANONICAL.can_capture
    assert not ViewportState.ADOPTED_NONCANONICAL.supports_calibrated_pixels
    for state in (
        ViewportState.UNPINNED,
        ViewportState.CAPTURE_MISMATCH,
        ViewportState.INVALID,
    ):
        assert not state.can_capture
        assert not state.supports_calibrated_pixels


def test_window_replacement_is_noticed_even_at_the_same_rectangle() -> None:
    """Roblox recreates its window on a rejoin, sometimes in the same place."""
    first = make_geometry(window_id=1)
    same = make_geometry(window_id=1)
    replaced = make_geometry(window_id=2)
    assert first.same_source(same)
    assert not first.same_source(replaced)


def test_display_migration_changes_the_viewport_identity() -> None:
    a = make_geometry(display_id="1")
    b = make_geometry(display_id="2")
    assert not a.same_source(b)


def test_a_scale_change_changes_the_viewport_identity() -> None:
    a = make_geometry(backing_scale=1.0)
    b = make_geometry(backing_scale=2.0)
    assert not a.same_source(b)


@pytest.mark.parametrize(
    "origin", [(0.0, 0.0), (-1440.0, -900.0), (2560.0, 0.0), (-100.5, 37.25)]
)
def test_negative_and_offset_monitor_origins_are_handled(origin: tuple[float, float]) -> None:
    geometry = make_geometry(origin=origin, size=(1280.0, 720.0))
    x, y = geometry.display_logical_from_canonical.apply(0.0, 0.0)
    assert x == pytest.approx(origin[0])
    assert y == pytest.approx(origin[1])


@pytest.mark.parametrize("dpi_scale", [1.0, 1.25, 1.5, 1.75, 2.0])
def test_windows_style_dpi_scaling_does_not_enter_the_transform(dpi_scale: float) -> None:
    """On Windows a PMv2 process already gets device pixels, so UI scaling is
    diagnostic only and must never multiply a coordinate."""
    display = DisplayInfo("win", LogicalRect(0, 0, 1920, 1080), 1.0, dpi_scale=dpi_scale)
    geometry = ViewportGeometry(
        state=ViewportState.CANONICAL_VERIFIED,
        window=WindowIdentity(1, 2, "Roblox"),
        display=display,
        frame_logical=LogicalRect(0, 0, 1296, 759),
        client_logical=LogicalRect(8, 31, 1280, 720),
    )
    assert geometry.client_backing_px == (1280, 720)
    assert geometry.canonical_from_client_logical.scale_x == pytest.approx(1.0)
    x, y = geometry.display_logical_from_canonical.apply(0.0, 0.0)
    assert (x, y) == (8.0, 31.0)


def test_preview_mapping_is_letterboxed_into_the_canvas() -> None:
    geometry = make_geometry()
    transform = geometry.preview_from_canonical((640, 480))
    assert transform.is_uniform
    assert transform.scale_x == pytest.approx(0.5)
    assert transform.translate_y == pytest.approx((480 - 360) / 2)


# ---------------------------------------------------------------------------
# Capture-region conformance
# ---------------------------------------------------------------------------


def _marked_client(width: int, height: int) -> np.ndarray:
    """A client image whose corners and border are individually identifiable."""
    image = np.zeros((height, width, 3), dtype=np.uint8)
    image[:, :] = (40, 40, 40)
    image[0:8, 0:8] = (255, 0, 0)  # BGR: blue   top-left
    image[0:8, width - 8 :] = (0, 255, 0)  # green  top-right
    image[height - 8 :, 0:8] = (0, 0, 255)  # red    bottom-left
    image[height - 8 :, width - 8 :] = (0, 255, 255)  # yellow bottom-right
    return image


def test_normalization_places_the_whole_client_and_nothing_else() -> None:
    """The canonical raster must contain exactly the client, letterboxed.

    Every corner marker has to land where the transform says it does, and the
    bars have to be black - which is what proves no neighbouring desktop pixel
    was included.
    """
    geometry = _retina_geometry()
    client = geometry.client_logical
    assert client is not None
    source = _marked_client(round(client.width), round(client.height))
    pool = FrameBufferPool(4)

    canonical = normalize_into_canonical(source, geometry, pool)

    assert canonical is not None
    assert canonical.shape == (720, 1280, 3)
    inner_x, inner_y, inner_w, inner_h = geometry.canonical_letterbox_px()

    # Bars are black on both sides of the content.
    assert canonical[:, :inner_x].max() == 0
    assert canonical[:, inner_x + inner_w :].max() == 0

    # Each corner marker is where the transform predicts.
    def sample(fraction_x: float, fraction_y: float) -> tuple[int, int, int]:
        x = inner_x + int(fraction_x * (inner_w - 1))
        y = inner_y + int(fraction_y * (inner_h - 1))
        return tuple(int(v) for v in canonical[y, x])  # type: ignore[return-value]

    assert sample(0.002, 0.002)[0] > 200  # blue top-left
    assert sample(0.998, 0.002)[1] > 200  # green top-right
    assert sample(0.002, 0.998)[2] > 200  # red bottom-left
    assert sample(0.998, 0.998)[1] > 200 and sample(0.998, 0.998)[2] > 200  # yellow


def test_a_canonical_client_is_copied_without_bars() -> None:
    geometry = make_geometry(size=(1280.0, 720.0))
    source = _marked_client(1280, 720)
    canonical = normalize_into_canonical(source, geometry, FrameBufferPool(2))

    assert canonical is not None
    assert geometry.canonical_letterbox_px() == (0, 0, 1280, 720)
    assert tuple(int(v) for v in canonical[2, 2]) == (255, 0, 0)
    assert tuple(int(v) for v in canonical[717, 1277]) == (0, 255, 255)


def test_normalization_reports_pool_exhaustion_instead_of_allocating() -> None:
    geometry = make_geometry()
    pool = FrameBufferPool(2)
    held = [pool.acquire(720, 1280), pool.acquire(720, 1280)]
    assert all(buffer is not None for buffer in held)

    assert normalize_into_canonical(_marked_client(1280, 720), geometry, pool) is None


def test_the_mss_fallback_asks_for_the_client_rect_in_logical_units() -> None:
    """The exact defect D-017 records: device pixels into a logical-unit API.

    ``mss`` reports and accepts the display's logical space, so the region it is
    given must be the client rect in logical units - never the backing size.
    """
    geometry = _retina_geometry()
    recorded: dict[str, int] = {}

    class _RecordingSct:
        def grab(self, region: dict[str, int]) -> np.ndarray:
            recorded.update(region)
            return np.zeros((region["height"], region["width"], 4), dtype=np.uint8)

        def close(self) -> None:
            pass

    source = MssCaptureSource()
    source._sct = _RecordingSct()
    source._geometry = geometry
    source._pool = FrameBufferPool(2)

    frame = source.poll()

    assert frame is not None
    client = geometry.client_logical
    assert client is not None
    assert recorded == {
        "left": round(client.x),
        "top": round(client.y),
        "width": round(client.width),
        "height": round(client.height),
    }
    # Not the backing size, which on this 2x display would be twice as large.
    assert recorded["width"] != geometry.client_backing_px[0]
    assert frame.size_px == CANONICAL_SIZE_PX
