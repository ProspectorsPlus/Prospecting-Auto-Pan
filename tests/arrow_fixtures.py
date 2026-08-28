"""Synthetic arrow scenes with known ground truth.

Every parameter here is fitted to the owner's real crops, so a synthetic frame
is a stress test of the *measured* appearance rather than an invention:

* the polygon reproduces solidity 0.858, extent 0.541 and 7 vertices, against
  measured 0.851-0.961, 0.467-0.686 and 5-8;
* the fill is the measured chartreuse gradient (BGR roughly 74/255/182 at the
  bright end) with a darker bevel face;
* backgrounds are sampled from the four measured terrains, including the grass
  whose green chromaticity (0.520) matches the arrow's (0.518) to three
  decimals - the case colour alone cannot solve.

**These frames are training stress, never held-out validation.** Plan 7.2 is
explicit: synthetic gamma and brightness changes may not appear in a held-out
split, and no gate may be passed on rendered data.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

__all__ = [
    "TERRAINS",
    "ArrowScene",
    "arrow_polygon",
    "render_scene",
]

#: Canonical block-arrow outline, pointing +x, derived by fitting the measured
#: solidity/extent/vertex counts (DECISIONS.md D-024).
_LENGTH = 1.35
_HEAD_X = 0.60
_HEAD_HALF = 0.50
_TAIL_HALF = 0.30

#: Mean BGR of the arrow's bright face and its shaded bevel, measured from the
#: supplied crops.
ARROW_BRIGHT_BGR = (74.0, 255.0, 182.0)
ARROW_SHADED_BGR = (52.0, 196.0, 120.0)

#: (mean BGR, texture amplitude). Grass is the hard one: same chromaticity as
#: the arrow, separable only by luminance and shape.
TERRAINS: dict[str, tuple[tuple[float, float, float], float]] = {
    "dirt": ((39.0, 77.0, 109.0), 6.0),
    "grass": ((31.0, 128.0, 87.0), 26.0),
    "water": ((139.0, 142.0, 56.0), 14.0),
    "pale": ((139.0, 165.0, 204.0), 10.0),
    "night_grass": ((14.0, 52.0, 33.0), 12.0),
}


def arrow_polygon() -> NDArray[np.float64]:
    """The canonical outline, centred on its own centroid, pointing +x."""
    points = np.array(
        [
            (_LENGTH, 0.0),
            (_HEAD_X, _HEAD_HALF),
            (_HEAD_X, _TAIL_HALF),
            (0.0, _TAIL_HALF),
            (0.0, -_TAIL_HALF),
            (_HEAD_X, -_TAIL_HALF),
            (_HEAD_X, -_HEAD_HALF),
        ],
        dtype=np.float64,
    )
    return points - points.mean(axis=0)


@dataclass(frozen=True)
class ArrowScene:
    """One rendered frame plus the ground truth that generated it."""

    bgr: NDArray[np.uint8]
    heading_deg: float
    centre_px: tuple[float, float]
    scale_px: float
    terrain: str
    clipped: bool
    alpha: float


def _rotate(points: NDArray[np.float64], degrees: float) -> NDArray[np.float64]:
    """Rotate into screen space, where 0 degrees is up and +90 is right."""
    radians = math.radians(degrees)
    # Screen heading h maps a +x model vector to (sin h, -cos h).
    forward = np.array([math.sin(radians), -math.cos(radians)])
    left = np.array([forward[1], -forward[0]])
    return points[:, 0:1] * forward + points[:, 1:2] * left


def _background(
    size: tuple[int, int], terrain: str, rng: np.random.Generator, brightness: float
) -> NDArray[np.uint8]:
    width, height = size
    mean, amplitude = TERRAINS[terrain]
    field = np.zeros((height, width, 3), dtype=np.float32)
    for index in range(3):
        field[:, :, index] = mean[index] * brightness
    # Low-frequency shading plus high-frequency texture: grass is textured, a
    # painted flat colour would make the boundary term trivially separable.
    ys = np.linspace(-1.0, 1.0, height, dtype=np.float32)[:, None]
    xs = np.linspace(-1.0, 1.0, width, dtype=np.float32)[None, :]
    shade = 1.0 + 0.16 * (xs * 0.7 + ys * 0.5)
    field *= shade[:, :, None]
    field += rng.normal(0.0, amplitude, size=field.shape).astype(np.float32)
    return np.clip(field, 0, 255).astype(np.uint8)


def render_scene(
    *,
    heading_deg: float,
    size: tuple[int, int] = (1280, 720),
    centre_px: tuple[float, float] | None = None,
    scale_px: float = 90.0,
    terrain: str = "grass",
    brightness: float = 1.0,
    alpha: float = 1.0,
    blur_px: int = 0,
    erode_px: int = 0,
    foreshorten: float = 1.0,
    seed: int = 0,
    distractors: int = 0,
    occluders: int = 0,
    arrow: bool = True,
) -> ArrowScene:
    """Render one arrow over textured terrain, with known ground truth.

    ``foreshorten`` squashes the shape across its own axis, reproducing the
    perspective the owner's crops show (measured elongation rises from 1.3 to
    2.9 as the camera flattens). ``alpha`` blends the arrow with the scene, as
    it genuinely is when it overlaps the player or the sky.
    """
    import cv2

    width, height = size
    rng = np.random.default_rng(seed)
    canvas = _background(size, terrain, rng, brightness).astype(np.float32)
    centre = centre_px if centre_px is not None else (width / 2.0, height / 2.0)

    # Clutter and occlusion are different problems and are asked for
    # separately. A distractor is a same-colour blob placed *away* from the
    # arrow: the detector must not prefer it. An occluder is allowed to overlap
    # the arrow, which genuinely destroys the outline - the honest answer there
    # is an abstention, and that stratum is reported on its own.
    keep_clear = scale_px * _LENGTH * 1.35
    for index in range(distractors + occluders):
        overlapping = index >= distractors
        for _attempt in range(24):
            cx = float(rng.uniform(0.12, 0.88) * width)
            cy = float(rng.uniform(0.12, 0.88) * height)
            if overlapping or math.dist((cx, cy), centre) >= keep_clear:
                break
        radius = float(rng.uniform(0.6, 2.4) * scale_px)
        blob = np.zeros((height, width), dtype=np.uint8)
        cv2.ellipse(
            blob,
            (int(cx), int(cy)),
            (int(radius), int(radius * rng.uniform(0.4, 1.6))),
            float(rng.uniform(0, 180)),
            0,
            360,
            255,
            -1,
        )
        shade = 0.82 + 0.18 * ((index % 3) / 2.0)
        for channel in range(3):
            layer = canvas[:, :, channel]
            layer[blob > 0] = ARROW_BRIGHT_BGR[channel] * shade * brightness

    clipped = False
    if arrow:
        model = arrow_polygon()
        model = np.stack([model[:, 0], model[:, 1] * foreshorten], axis=1)
        points = _rotate(model, heading_deg) * scale_px + np.array(centre)
        polygon = points.astype(np.int32)
        clipped = bool(
            polygon[:, 0].min() <= 0
            or polygon[:, 1].min() <= 0
            or polygon[:, 0].max() >= width - 1
            or polygon[:, 1].max() >= height - 1
        )

        mask = np.zeros((height, width), dtype=np.uint8)
        cv2.fillPoly(mask, [polygon], 255)
        if erode_px > 0:
            mask = cv2.erode(mask, np.ones((erode_px, erode_px), np.uint8))

        # A face gradient across the arrow, matching the measured bright-to-
        # shaded falloff, so the interior is not a flat colour.
        ys, xs = np.mgrid[0:height, 0:width].astype(np.float32)
        along = (xs - centre[0]) * math.sin(math.radians(heading_deg)) - (
            ys - centre[1]
        ) * math.cos(math.radians(heading_deg))
        ramp = np.clip(0.5 + 0.5 * along / max(1.0, scale_px * _LENGTH), 0.0, 1.0)
        fill = np.zeros((height, width, 3), dtype=np.float32)
        for channel in range(3):
            dark, bright = ARROW_SHADED_BGR[channel], ARROW_BRIGHT_BGR[channel]
            fill[:, :, channel] = (dark + (bright - dark) * ramp) * brightness

        selected = mask > 0
        blended = canvas.copy()
        for channel in range(3):
            layer = blended[:, :, channel]
            layer[selected] = (
                alpha * fill[:, :, channel][selected] + (1.0 - alpha) * layer[selected]
            )
        canvas = blended

    image = np.clip(canvas, 0, 255).astype(np.uint8)
    if blur_px > 0:
        k = blur_px | 1
        image = cv2.GaussianBlur(image, (k, k), 0)
    return ArrowScene(
        bgr=image,
        heading_deg=heading_deg,
        centre_px=(float(centre[0]), float(centre[1])),
        scale_px=scale_px,
        terrain=terrain,
        clipped=clipped,
        alpha=alpha,
    )
