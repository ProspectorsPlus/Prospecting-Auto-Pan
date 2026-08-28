"""Explicit coordinate spaces and the transforms between them.

The bug this module exists to make impossible: a rectangle named ``*_px`` was
being handed to an API that wanted **logical points**. On a 2x Retina display
that silently captured a desktop region twice the intended size, starting at
twice the intended offset - Roblox plus the Dock plus whatever else happened to
be there.

Four spaces, never interchangeable:

``DISPLAY_LOGICAL``
    OS window-management units, display-absolute. macOS points; on Windows a
    Per-Monitor-V2 process receives physical pixels here, so the two coincide
    numerically but not conceptually. Origins may be negative on multi-monitor
    layouts. This is what ``CGWindowListCopyWindowInfo``, Accessibility,
    ``GetClientRect``/``ClientToScreen``, and ``mss`` all speak.

``CLIENT_LOGICAL``
    The same units, but relative to the client content area's top-left, so the
    title bar and window frame are already excluded.

``CLIENT_BACKING``
    Native device pixels of that same client area: ``logical * backing_scale``.
    What a Retina capture actually contains.

``CANONICAL``
    The fixed 1280x720 BGR raster every detector, calibration constant, and
    overlay coordinate is expressed in. Decoupling this from the display means
    a calibrated pixel means the same thing on every machine.

Every transform here is an axis-aligned scale-and-translate, which is all any
of them needs, and which makes the inverse exact rather than approximate.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from enum import Enum

__all__ = [
    "CANONICAL_SIZE_PX",
    "Affine2D",
    "CoordinateSpace",
    "DisplayInfo",
    "LogicalRect",
    "ViewportGeometry",
    "ViewportState",
    "WindowIdentity",
]

CANONICAL_SIZE_PX: tuple[int, int] = (1280, 720)
"""The processing raster every detector and calibrated pixel is defined in."""


class CoordinateSpace(Enum):
    """Named so a transform can say what it converts, in both directions."""

    DISPLAY_LOGICAL = "display_logical"
    CLIENT_LOGICAL = "client_logical"
    CLIENT_BACKING = "client_backing"
    CANONICAL = "canonical"
    PREVIEW = "preview"


# ---------------------------------------------------------------------------
# Transforms
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Affine2D:
    """``(x, y) -> (sx*x + tx, sy*y + ty)`` with an exact inverse."""

    scale_x: float
    scale_y: float
    translate_x: float
    translate_y: float
    source: CoordinateSpace
    target: CoordinateSpace

    def __post_init__(self) -> None:
        if self.scale_x == 0.0 or self.scale_y == 0.0:
            raise ValueError("Affine2D scale must be non-zero to stay invertible")

    @classmethod
    def identity(cls, space: CoordinateSpace) -> Affine2D:
        return cls(1.0, 1.0, 0.0, 0.0, space, space)

    def apply(self, x: float, y: float) -> tuple[float, float]:
        return (self.scale_x * x + self.translate_x, self.scale_y * y + self.translate_y)

    def apply_point(self, point: tuple[float, float]) -> tuple[float, float]:
        return self.apply(point[0], point[1])

    def apply_size(self, width: float, height: float) -> tuple[float, float]:
        """Sizes take the scale but not the translation."""
        return (abs(self.scale_x) * width, abs(self.scale_y) * height)

    def inverse(self) -> Affine2D:
        return Affine2D(
            scale_x=1.0 / self.scale_x,
            scale_y=1.0 / self.scale_y,
            translate_x=-self.translate_x / self.scale_x,
            translate_y=-self.translate_y / self.scale_y,
            source=self.target,
            target=self.source,
        )

    def then(self, other: Affine2D) -> Affine2D:
        """Apply ``self`` first, then ``other``. Spaces must line up."""
        if self.target is not other.source:
            raise ValueError(
                f"cannot compose {self.source.value}->{self.target.value} with "
                f"{other.source.value}->{other.target.value}"
            )
        return Affine2D(
            scale_x=other.scale_x * self.scale_x,
            scale_y=other.scale_y * self.scale_y,
            translate_x=other.scale_x * self.translate_x + other.translate_x,
            translate_y=other.scale_y * self.translate_y + other.translate_y,
            source=self.source,
            target=other.target,
        )

    @classmethod
    def letterbox(
        cls,
        source_size: tuple[float, float],
        target_size: tuple[float, float],
        *,
        source: CoordinateSpace,
        target: CoordinateSpace,
    ) -> Affine2D:
        """Uniform aspect-preserving fit, centred, with the bars accounted for.

        Uniform because a non-uniform stretch would make an angle measured in
        canonical space differ from the angle on screen, which is the one thing
        a direction estimator cannot tolerate.
        """
        source_width, source_height = source_size
        target_width, target_height = target_size
        if source_width <= 0 or source_height <= 0:
            raise ValueError(f"non-positive source size {source_size}")
        scale = min(target_width / source_width, target_height / source_height)
        inner_width, inner_height = source_width * scale, source_height * scale
        return cls(
            scale_x=scale,
            scale_y=scale,
            translate_x=(target_width - inner_width) / 2.0,
            translate_y=(target_height - inner_height) / 2.0,
            source=source,
            target=target,
        )

    @property
    def is_uniform(self) -> bool:
        return math.isclose(self.scale_x, self.scale_y, rel_tol=1e-9)

    def describe(self) -> str:
        return (
            f"{self.source.value}->{self.target.value} "
            f"scale=({self.scale_x:.5g},{self.scale_y:.5g}) "
            f"offset=({self.translate_x:.5g},{self.translate_y:.5g})"
        )


# ---------------------------------------------------------------------------
# Rectangles
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LogicalRect:
    """A rectangle in logical units. Floats, because points are not integers."""

    x: float
    y: float
    width: float
    height: float

    @property
    def right(self) -> float:
        return self.x + self.width

    @property
    def bottom(self) -> float:
        return self.y + self.height

    @property
    def size(self) -> tuple[float, float]:
        return (self.width, self.height)

    @property
    def origin(self) -> tuple[float, float]:
        return (self.x, self.y)

    @property
    def aspect(self) -> float:
        return self.width / self.height if self.height else 0.0

    def inset(
        self, *, left: float = 0, top: float = 0, right: float = 0, bottom: float = 0
    ) -> LogicalRect:
        return LogicalRect(
            x=self.x + left,
            y=self.y + top,
            width=self.width - left - right,
            height=self.height - top - bottom,
        )

    def contains(self, x: float, y: float) -> bool:
        return self.x <= x < self.right and self.y <= y < self.bottom

    def rounded_size_px(self, scale: float) -> tuple[int, int]:
        return (round(self.width * scale), round(self.height * scale))

    def approx_equals(self, other: LogicalRect, tolerance: float = 1.0) -> bool:
        return all(
            abs(a - b) <= tolerance
            for a, b in (
                (self.x, other.x),
                (self.y, other.y),
                (self.width, other.width),
                (self.height, other.height),
            )
        )


# ---------------------------------------------------------------------------
# Display and window identity
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DisplayInfo:
    """The display a window is on, and how logical units map to device pixels.

    ``backing_scale`` and ``dpi_scale`` are different things and conflating them
    is a bug waiting to happen:

    * ``backing_scale`` is **backing pixels per logical unit**, and is the only
      one that appears in coordinate math. It is 2.0 on a Retina Mac, and 1.0
      on Windows, where a Per-Monitor-V2 process already receives device pixels
      from every window API.
    * ``dpi_scale`` is the *user's* UI scaling setting (1.25 at 125%). It
      affects how large the game's own interface is drawn, so it belongs in
      diagnostics and in profile provenance, but never in a transform.
    """

    display_id: str
    bounds_logical: LogicalRect
    backing_scale: float
    dpi_scale: float = 1.0

    @property
    def is_retina(self) -> bool:
        return self.backing_scale > 1.0

    def describe(self) -> str:
        scaling = f", UI {self.dpi_scale * 100:.0f}%" if self.dpi_scale != 1.0 else ""
        return (
            f"display {self.display_id} {self.bounds_logical.width:g}x"
            f"{self.bounds_logical.height:g} @{self.backing_scale:g}x{scaling}"
        )


@dataclass(frozen=True)
class WindowIdentity:
    """Enough to notice that the window we are capturing was replaced.

    Roblox recreates its window on a fullscreen transition or a rejoin, and the
    replacement can occupy the same rectangle. Comparing geometry alone would
    miss that, so the window and process ids are carried too.
    """

    window_id: int
    process_id: int
    owner: str
    title: str = ""

    def same_window(self, other: WindowIdentity | None) -> bool:
        if other is None:
            return False
        return self.window_id == other.window_id and self.process_id == other.process_id

    def describe(self) -> str:
        return f"{self.owner}#{self.window_id} (pid {self.process_id})"


# ---------------------------------------------------------------------------
# Viewport
# ---------------------------------------------------------------------------


class ViewportState(Enum):
    """The truthful state of the capture viewport.

    "Viewport ok" must never coexist with "unsupported viewport size", so there
    is exactly one of these and every consumer - detector readiness, coordinator
    readiness, the GUI, and Live gating - reads the same value.
    """

    UNPINNED = "unpinned"
    """No window has been adopted or pinned yet."""

    CANONICAL_VERIFIED = "canonical_verified"
    """The client is the canonical size, read back and confirmed. Detection is
    fully supported."""

    ADOPTED_NONCANONICAL = "adopted_noncanonical"
    """A real client area, normalized into the canonical raster by letterboxing.
    Safe to observe and record; calibrated pixel constants do NOT apply, because
    the game lays its UI out for the client's own size."""

    CAPTURE_MISMATCH = "capture_mismatch"
    """Geometry and the delivered frames disagree - a resize or display change
    landed mid-flight. Fail closed and reacquire."""

    INVALID = "invalid"
    """No usable window: not found, minimized, zero-sized, or on another Space."""

    @property
    def can_capture(self) -> bool:
        return self in (ViewportState.CANONICAL_VERIFIED, ViewportState.ADOPTED_NONCANONICAL)

    @property
    def supports_calibrated_pixels(self) -> bool:
        """Only a canonical client makes a calibrated pixel constant meaningful."""
        return self is ViewportState.CANONICAL_VERIFIED


@dataclass(frozen=True)
class ViewportGeometry:
    """Everything needed to map between all four spaces, in one immutable value.

    A frame carries the exact instance used to produce it, so the image shown,
    the geometry drawn over it, and the observation derived from it can never
    refer to three different window states.
    """

    state: ViewportState
    window: WindowIdentity | None
    display: DisplayInfo | None
    frame_logical: LogicalRect | None
    client_logical: LogicalRect | None
    canonical_px: tuple[int, int] = CANONICAL_SIZE_PX
    requested_client_logical: tuple[float, float] | None = None
    verified_at_s: float = 0.0
    detail: str = ""

    # -- derived sizes ----------------------------------------------------
    @property
    def backing_scale(self) -> float:
        return self.display.backing_scale if self.display is not None else 1.0

    @property
    def client_backing_px(self) -> tuple[int, int]:
        if self.client_logical is None:
            return (0, 0)
        return self.client_logical.rounded_size_px(self.backing_scale)

    @property
    def client_insets_logical(self) -> tuple[float, float, float, float]:
        """``(left, top, right, bottom)`` of window frame minus client content."""
        if self.frame_logical is None or self.client_logical is None:
            return (0.0, 0.0, 0.0, 0.0)
        frame, client = self.frame_logical, self.client_logical
        return (
            client.x - frame.x,
            client.y - frame.y,
            frame.right - client.right,
            frame.bottom - client.bottom,
        )

    @property
    def client_rect_in_window_logical(self) -> LogicalRect:
        """The client area relative to the window's own top-left.

        This is what a window-specific capture backend crops with, so it is
        derived here rather than recomputed by each backend.
        """
        if self.client_logical is None:
            return LogicalRect(0.0, 0.0, 0.0, 0.0)
        left, top, _right, _bottom = self.client_insets_logical
        return LogicalRect(left, top, self.client_logical.width, self.client_logical.height)

    @property
    def is_canonical(self) -> bool:
        return self.state is ViewportState.CANONICAL_VERIFIED

    # -- transforms -------------------------------------------------------
    @property
    def canonical_from_client_logical(self) -> Affine2D:
        """Client-relative logical units into the canonical raster.

        Letterboxed and uniform: an angle measured in canonical space is the
        same angle on screen, which a direction estimator depends on.
        """
        if self.client_logical is None:
            return Affine2D.identity(CoordinateSpace.CANONICAL)
        return Affine2D.letterbox(
            self.client_logical.size,
            (float(self.canonical_px[0]), float(self.canonical_px[1])),
            source=CoordinateSpace.CLIENT_LOGICAL,
            target=CoordinateSpace.CANONICAL,
        )

    @property
    def client_logical_from_canonical(self) -> Affine2D:
        return self.canonical_from_client_logical.inverse()

    @property
    def canonical_from_client_backing(self) -> Affine2D:
        """Native device pixels of the client into the canonical raster."""
        scale = self.backing_scale
        backing_to_logical = Affine2D(
            1.0 / scale,
            1.0 / scale,
            0.0,
            0.0,
            CoordinateSpace.CLIENT_BACKING,
            CoordinateSpace.CLIENT_LOGICAL,
        )
        return backing_to_logical.then(self.canonical_from_client_logical)

    @property
    def display_logical_from_client_logical(self) -> Affine2D:
        origin = self.client_logical.origin if self.client_logical else (0.0, 0.0)
        return Affine2D(
            1.0,
            1.0,
            origin[0],
            origin[1],
            CoordinateSpace.CLIENT_LOGICAL,
            CoordinateSpace.DISPLAY_LOGICAL,
        )

    @property
    def display_logical_from_canonical(self) -> Affine2D:
        """Canonical straight to the desktop - the only path pointer moves use."""
        return self.client_logical_from_canonical.then(self.display_logical_from_client_logical)

    def preview_from_canonical(self, canvas_px: tuple[int, int]) -> Affine2D:
        """Canonical into a preview canvas of the given size, letterboxed."""
        width, height = canvas_px
        return Affine2D.letterbox(
            (float(self.canonical_px[0]), float(self.canonical_px[1])),
            (float(max(1, width)), float(max(1, height))),
            source=CoordinateSpace.CANONICAL,
            target=CoordinateSpace.PREVIEW,
        )

    def canonical_letterbox_px(self) -> tuple[int, int, int, int]:
        """The content rect inside the canonical raster: ``(x, y, w, h)``.

        Non-canonical clients do not fill 1280x720. Detectors and overlays need
        to know which part is real image and which part is bar.
        """
        if self.client_logical is None:
            return (0, 0, *self.canonical_px)
        transform = self.canonical_from_client_logical
        width, height = transform.apply_size(*self.client_logical.size)
        return (
            round(transform.translate_x),
            round(transform.translate_y),
            round(width),
            round(height),
        )

    # -- identity and validity -------------------------------------------
    def identity(self) -> tuple[object, ...]:
        """Fields whose change invalidates every in-flight coordinate."""
        return (
            self.state.value,
            self.window.window_id if self.window else None,
            self.window.process_id if self.window else None,
            self.display.display_id if self.display else None,
            self.backing_scale,
            None
            if self.client_logical is None
            else (
                round(self.client_logical.x, 2),
                round(self.client_logical.y, 2),
                round(self.client_logical.width, 2),
                round(self.client_logical.height, 2),
            ),
        )

    def same_source(self, other: ViewportGeometry | None) -> bool:
        return other is not None and self.identity() == other.identity()

    @property
    def valid(self) -> bool:
        return self.state.can_capture and self.client_logical is not None

    def with_state(self, state: ViewportState, detail: str) -> ViewportGeometry:
        return replace(self, state=state, detail=detail)

    def describe(self) -> str:
        if self.client_logical is None:
            return f"{self.state.value}: {self.detail or 'no client'}"
        client = self.client_logical
        backing = self.client_backing_px
        return (
            f"{self.state.value}: client {client.width:g}x{client.height:g} pt "
            f"at ({client.x:g},{client.y:g}), backing {backing[0]}x{backing[1]} px, "
            f"scale {self.backing_scale:g}x, "
            f"canonical {self.canonical_px[0]}x{self.canonical_px[1]}"
        )

    @classmethod
    def unpinned(cls, detail: str = "no window adopted") -> ViewportGeometry:
        return cls(
            state=ViewportState.UNPINNED,
            window=None,
            display=None,
            frame_logical=None,
            client_logical=None,
            detail=detail,
        )

    @classmethod
    def invalid(cls, detail: str) -> ViewportGeometry:
        return cls(
            state=ViewportState.INVALID,
            window=None,
            display=None,
            frame_logical=None,
            client_logical=None,
            detail=detail,
        )
