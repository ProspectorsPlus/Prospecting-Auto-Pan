#!/usr/bin/env python3
"""The Shadow view: one frame, drawn with the geometry the decision came from.

Split out of ``treasure_gui.py`` because that file had grown past the point
where a reviewer can hold it in their head, and the renderer is a clean seam: it
takes one :class:`~prospector_engine.contracts.DiagnosticObservation` and a Tk
canvas, and touches nothing else in the application.

Two rules shape the implementation:

* **Nothing is recreated per frame.** The image is a single ``PhotoImage``
  pasted in place and the overlay is a fixed set of canvas items whose
  coordinates are updated. Deleting and rebuilding the canvas every frame is
  what makes a Tk preview feel slow, and it would compete with perception for
  the GIL.
* **The image and the overlay come from the same observation**, which carries
  its own frame, so the drawing can never lag the picture it is drawn over.

The palette lives here and is imported by the dashboard: dark surface, bone
text, jade for detections, gold for the value being estimated. Implemented
independently through ``ttk.Style`` and Tk item options - no CSS, markup, or
text was copied from any other project (plan section 12).
"""

from __future__ import annotations

import math
import sys
import time
import tkinter as tk
from enum import Enum
from typing import Any, ClassVar

import numpy as np

from prospector_engine.contracts import (
    CommandOutcome,
    DiagnosticObservation,
    PacketKind,
    RunMode,
)
from prospector_engine.geometry import Affine2D


class OverlayMode(Enum):
    """How much of the reasoning to draw over the frame.

    Two modes rather than a checkbox per element, because the two audiences are
    different: someone watching a route wants the turn, and someone diagnosing
    a detector wants everything that produced it. A single crowded overlay
    serves neither.
    """

    MINIMAL = "Minimal"
    FULL = "Full Diagnostics"

    @property
    def draws_candidates(self) -> bool:
        return self is OverlayMode.FULL

    @property
    def draws_diagnostics(self) -> bool:
        """Whether detector internals belong on screen at all.

        Minimal answers "where am I going and what is being pressed"; Full
        answers "why did the detector say that". Cue arms, cue labels, outlier
        text, the arrow's own axis, its tip and notches and every candidate box
        belong only to the second question, and drawing them over a route is
        what made Minimal unreadable.
        """
        return self is OverlayMode.FULL


# --- Palette ---------------------------------------------------------------
BG = "#14171a"
SURFACE = "#1b1f24"
SURFACE_ALT = "#232830"
BONE = "#e8e4dc"
MUTED = "#8b9199"
JADE = "#2fae7e"
GOLD = "#d9a441"
OK = "#4caf7d"
WARN = "#d9a441"
BAD = "#d9534f"
INFO = "#4a9bd1"

STATUS_COLOURS = {
    "ok": OK,
    "fresh": OK,
    "empty": OK,
    "known-safe": OK,
    "invalid": BAD,
    "not-focused": WARN,
    "stale": WARN,
    "stopped": WARN,
    "unhealthy": BAD,
    "held": WARN,
    "uncertain": BAD,
    "none": MUTED,
}

MODE_COLOURS = {
    RunMode.IDLE: MUTED,
    RunMode.SHADOW: INFO,
    RunMode.LIVE: GOLD,
    RunMode.SERVICE: JADE,
    RunMode.SAFE_STOP: BAD,
}


class DiagnosticCanvas:
    """The Shadow view: the frame, with the geometry the decision was made from.

    Two rules shape the implementation:

    * **Nothing is recreated per frame.** The image is a single ``PhotoImage``
      pasted in place and the overlay is a fixed set of canvas items whose
      coordinates are updated. Deleting and rebuilding the canvas every frame
      is what makes a Tk preview feel slow, and it would compete with
      perception for the GIL.
    * **The image and the overlay come from the same observation**, which holds
      its own frame, so the drawing can never lag the picture.
    """

    #: Reference arm colours. The forward arm is deliberately dashed and muted:
    #: automatic setup checks that the heading holds still with this anchor, but
    #: E-ANCHOR and E-FORWARD - the offline labelling of the avatar's true pivot
    #: - are still pending, so it stays a hypothesis on screen.
    FORWARD_COLOUR = "#c3ccd6"
    DESIRED_COLOUR = GOLD
    ARROW_COLOUR = JADE
    REJECT_COLOUR = "#8a4b4b"
    ARC_COLOUR = "#d0743c"
    #: Everything a frozen packet is drawn in: one flat, obviously dead grey.
    FROZEN_COLOUR = "#5d646d"

    #: The action layer. Purple because nothing else on the overlay is - the
    #: reference arms are bone, the desired direction gold, detections jade -
    #: so "what is being pressed" can never be confused with "what was seen".
    #: The underlay is a near-black casing drawn under every purple stroke:
    #: vivid purple over bright sand or open water is otherwise unreadable, and
    #: this panel exists to be read at a glance while a character is moving.
    ACTION_COLOUR = "#c65cff"
    ACTION_UNDERLAY = "#0a0410"
    ACTION_UNDERLAY_WIDTH = 11
    ACTION_STROKE_WIDTH = 7
    #: Fixed in *view* pixels, not canonical ones, so the glyphs stay the same
    #: readable size whatever the preview is scaled to.
    ACTION_RAY_PX = 84.0
    ACTION_LABEL_FONT = ("Helvetica", 13, "bold")

    #: Glyph -> screen bearing in degrees (0 up, positive clockwise). Turning
    #: sits diagonally above the walk axes because it is steering *while*
    #: moving, and putting it on the strafe axes would read as A/D.
    ACTION_BEARINGS: ClassVar[dict[str, float]] = {
        "W": 0.0,
        "D": 90.0,
        "S": 180.0,
        "A": 270.0,
        "<": 315.0,
        ">": 45.0,
    }

    ARM_LENGTH_PX = 190.0

    #: Full Diagnostics redraws its overlay at most this often. The image
    #: still pastes at the preview cadence; the candidate polygons, cue arms
    #: and captions - the expensive part - run at a separately capped rate so
    #: they can never slow the frame.
    FULL_OVERLAY_INTERVAL_S = 0.05

    def __init__(self, canvas: tk.Canvas, mode: OverlayMode = OverlayMode.MINIMAL) -> None:
        self.canvas = canvas
        self.mode = mode
        self._photo: Any = None
        self._photo_size: tuple[int, int] = (0, 0)
        self._scaled: Any = None
        self._rgb: Any = None
        self._image_item: int | None = None
        self._items: dict[str, int] = {}
        self._reject_items: list[int] = []
        self._last_sequence = -1
        self._last_key: Any = None
        self._last_overlay_at_s = 0.0
        #: Timings of the most recent render, for the preview trace.
        self.last_paste_ms = 0.0
        self.last_overlay_ms = 0.0
        self.last_overlay_skipped = False

    #: Everything Full Diagnostics can draw that Minimal must not. Named
    #: explicitly rather than derived, because "hide what the other mode owns"
    #: has to keep working when someone adds an item and forgets the else-branch.
    DIAGNOSTIC_ITEM_NAMES: ClassVar[tuple[str, ...]] = (
        "contour",
        "bbox",
        "centroid",
        "shaft",
        "tip",
        "tip2",
        "notch_0",
        "notch_1",
    )

    def set_mode(self, mode: OverlayMode) -> None:
        """Switch modes and force a redraw, so the change is visible at once.

        Diagnostic items are hidden *here* rather than on the next packet.
        Waiting for a redraw leaves the candidate boxes and cue arms on screen
        for as long as no new frame arrives - which is exactly the case that
        matters, because a stopped run has no next frame.
        """
        self.mode = mode
        self._last_sequence = -1
        self._last_key = None
        if not mode.draws_diagnostics:
            self.hide_diagnostic_items()

    def hide_diagnostic_items(self) -> None:
        """Hide every detector-internal item, whatever drew it."""
        for name in self.DIAGNOSTIC_ITEM_NAMES:
            self._hide(name)
        for name in list(self._items):
            if name.startswith("cue_"):
                self._hide(name)
        for item in self._reject_items:
            self.canvas.itemconfigure(item, state="hidden")

    # -- item helpers -----------------------------------------------------
    def _line(self, name: str, **options: Any) -> int:
        if name not in self._items:
            self._items[name] = self.canvas.create_line(0, 0, 0, 0, **options)
        return self._items[name]

    def _oval(self, name: str, **options: Any) -> int:
        if name not in self._items:
            self._items[name] = self.canvas.create_oval(0, 0, 0, 0, **options)
        return self._items[name]

    def _text(self, name: str, **options: Any) -> int:
        if name not in self._items:
            self._items[name] = self.canvas.create_text(0, 0, **options)
        return self._items[name]

    def _polygon(self, name: str, **options: Any) -> int:
        if name not in self._items:
            self._items[name] = self.canvas.create_polygon(0, 0, 0, 0, 0, 0, **options)
        return self._items[name]

    def _hide(self, name: str) -> None:
        item = self._items.get(name)
        if item is not None:
            self.canvas.itemconfigure(item, state="hidden")

    def _show(self, name: str) -> None:
        item = self._items.get(name)
        if item is not None:
            self.canvas.itemconfigure(item, state="normal")

    # -- rendering --------------------------------------------------------
    def render(self, observation: DiagnosticObservation) -> bool:
        """Draw one packet, refusing anything an older world produced.

        The key comparison - not the frame number - is what makes it impossible
        to draw observation N over frame N+1, or to draw a cancelled worker's
        straggler over the session that replaced it.
        """
        if not observation.key.supersedes(self._last_key):
            return False
        self._last_key = observation.key
        self._last_sequence = observation.frame_sequence

        width = max(64, self.canvas.winfo_width())
        height = max(64, self.canvas.winfo_height())
        transform = observation.geometry.preview_from_canonical((width, height))
        started = time.perf_counter()
        self._draw_image(observation, (width, height), transform)
        pasted = time.perf_counter()
        self.last_paste_ms = (pasted - started) * 1000.0
        now = time.monotonic()
        # A frozen packet is never throttled. Full Diagnostics caps its overlay
        # redraws, and a Stop that lands inside that window would otherwise
        # leave the action layer on screen until the next frame - and a stopped
        # run has no next frame, so "until the next frame" means "forever".
        frozen = observation.packet_kind is not PacketKind.FRAME
        if (
            self.mode is OverlayMode.FULL
            and not frozen
            and now - self._last_overlay_at_s < self.FULL_OVERLAY_INTERVAL_S
        ):
            # Latest-only at a capped cadence: the picture is current and the
            # diagnostics catch up on the next tick; nothing queues.
            self.last_overlay_skipped = True
            self.last_overlay_ms = 0.0
            return True
        self.last_overlay_skipped = False
        self._draw_overlay(observation, transform)
        self._last_overlay_at_s = now
        self.last_overlay_ms = (time.perf_counter() - pasted) * 1000.0
        return True

    def render_frame_only(self, frame: Any) -> bool:
        """Draw a raw frame with no overlay, for before Shadow has started."""
        if frame.sequence == self._last_sequence:
            return False
        self._last_sequence = frame.sequence
        width = max(64, self.canvas.winfo_width())
        height = max(64, self.canvas.winfo_height())
        transform = frame.geometry.preview_from_canonical((width, height))
        self._paste(frame, (width, height), transform)
        for name in list(self._items):
            if name != "caption":
                self._hide(name)
        for item in self._reject_items:
            self.canvas.itemconfigure(item, state="hidden")
        caption = self._text(
            "caption",
            anchor="nw",
            fill=MUTED,
            font=("Menlo" if sys.platform == "darwin" else "Consolas", 10),
            justify="left",
        )
        self.canvas.coords(caption, 10, 8)
        self.canvas.itemconfigure(
            caption,
            state="normal",
            text=(
                f"frame #{frame.sequence}  {frame.geometry.state.value}\n"
                "Shadow is not running: no perception, no overlay."
            ),
        )
        self.canvas.tag_raise(caption)
        return True

    def _draw_image(
        self,
        observation: DiagnosticObservation,
        canvas_px: tuple[int, int],
        transform: Affine2D,
    ) -> None:
        self._paste(observation.frame, canvas_px, transform)

    def _paste(self, frame: Any, canvas_px: tuple[int, int], transform: Affine2D) -> None:
        """Resize and colour-convert into reused buffers, then paste in place.

        Resizing *before* the colour conversion means the conversion runs over
        a quarter of the pixels, and both write into buffers allocated once per
        preview size. ``Image.frombuffer`` needs C-contiguous memory, which a
        reversed-stride NumPy view is not - hence cv2 rather than ``[..., ::-1]``.
        """
        import cv2
        from PIL import Image, ImageTk

        canonical_w, canonical_h = frame.canonical_size_px
        target_w = max(1, round(canonical_w * transform.scale_x))
        target_h = max(1, round(canonical_h * transform.scale_y))

        if self._scaled is None or self._scaled.shape[:2] != (target_h, target_w):
            self._scaled = np.empty((target_h, target_w, 3), dtype=np.uint8)
            self._rgb = np.empty((target_h, target_w, 3), dtype=np.uint8)
        source = np.asarray(frame.bgr)
        if (target_w, target_h) == (canonical_w, canonical_h):
            np.copyto(self._scaled, source)
        else:
            # INTER_AREA downsamples cleanly; this is a monitor, not an archive,
            # so the preview must never become the expensive part of a frame.
            cv2.resize(
                source, (target_w, target_h), dst=self._scaled, interpolation=cv2.INTER_AREA
            )
        cv2.cvtColor(self._scaled, cv2.COLOR_BGR2RGB, dst=self._rgb)
        image = Image.frombuffer("RGB", (target_w, target_h), self._rgb, "raw", "RGB", 0, 1)

        if self._photo is None or self._photo_size != (target_w, target_h):
            self._photo = ImageTk.PhotoImage(image)
            self._photo_size = (target_w, target_h)
            if self._image_item is not None:
                self.canvas.delete(self._image_item)
            self._image_item = self.canvas.create_image(
                canvas_px[0] // 2, canvas_px[1] // 2, image=self._photo
            )
            self.canvas.tag_lower(self._image_item)
        elif self._image_item is not None:
            # Paste in place: no new PhotoImage, no canvas item churn.
            self._photo.paste(image)
            self.canvas.coords(self._image_item, canvas_px[0] // 2, canvas_px[1] // 2)

    def _draw_overlay(self, observation: DiagnosticObservation, transform: Affine2D) -> None:
        anchor = observation.anchor_px
        stale = observation.age_s > 0.25
        # A TRANSITION or TERMINAL packet is the last frame held up after the
        # run ended. It keeps its picture - going blank on Stop hides what was
        # on screen when it happened - but every vector is drawn in the frozen
        # grey, and the detector's internals and the action layer are gone
        # entirely. A stopped navigator must not be able to look like a running
        # one (mission section 6).
        frozen = observation.packet_kind is not PacketKind.FRAME
        forward_colour = self.FROZEN_COLOUR if frozen else self.FORWARD_COLOUR
        desired_colour = self.FROZEN_COLOUR if frozen else self.DESIRED_COLOUR

        # -- direction arms -----------------------------------------------
        if anchor is None:
            for name in (
                "forward_arm",
                "forward_label",
                "desired_arm",
                "arc",
                "angle_text",
                "anchor_dot",
                "no_desired",
            ):
                self._hide(name)
            for index in range(len(observation.cues)):
                self._hide(f"cue_{index}")
                self._hide(f"cue_{index}_label")
        else:
            anchor_view = transform.apply_point(anchor)
            dot = self._oval("anchor_dot", outline=BONE, width=2)
            self.canvas.coords(
                dot,
                anchor_view[0] - 4,
                anchor_view[1] - 4,
                anchor_view[0] + 4,
                anchor_view[1] + 4,
            )
            self._show("anchor_dot")

            forward_item = self._line(
                "forward_arm", fill=self.FORWARD_COLOUR, width=4, arrow="last", dash=(7, 5)
            )
            self.canvas.itemconfigure(forward_item, fill=forward_colour)
            self._set_arm(forward_item, anchor, observation.forward_deg, transform)
            forward_label = self._text(
                "forward_label", fill=self.FORWARD_COLOUR, font=("Helvetica", 10, "bold")
            )
            self.canvas.itemconfigure(forward_label, fill=forward_colour)
            label_point = (anchor[0], anchor[1] - self.ARM_LENGTH_PX - 16)
            label_view = transform.apply_point(label_point)
            self.canvas.coords(forward_label, label_view[0], label_view[1])
            self.canvas.itemconfigure(
                forward_label, text="forward (screen anchor)", state="normal"
            )

            self._draw_cue_arms(observation, anchor, transform)

            if observation.desired_deg is None:
                self._hide("desired_arm")
                self._hide("arc")
                self._hide("angle_text")
                marker = self._text(
                    "no_desired", fill=BAD, font=("Helvetica", 11, "bold"), anchor="w"
                )
                marker_view = transform.apply_point((anchor[0] + 16, anchor[1] + 26))
                self.canvas.coords(marker, marker_view[0], marker_view[1])
                self.canvas.itemconfigure(
                    marker,
                    state="normal",
                    text=f"no desired direction: {observation.direction.abstain_reason}",
                )
            else:
                self._hide("no_desired")
                desired_item = self._line(
                    "desired_arm", fill=self.DESIRED_COLOUR, width=4, arrow="last"
                )
                self.canvas.itemconfigure(desired_item, fill=desired_colour)
                self._set_arm(desired_item, anchor, observation.desired_deg, transform)
                self._show("desired_arm")
                self._draw_arc(observation, anchor, transform)

        # -- notches, the geometry the signed direction came from -----------
        draws_internals = self.mode.draws_diagnostics and not frozen
        notches = observation.arrow.notch_px if draws_internals else None
        for index in (0, 1):
            key = f"notch_{index}"
            if notches is None or index >= len(notches):
                self._hide(key)
                continue
            nx, ny = transform.apply_point(notches[index])
            marker = self._line(key, fill=GOLD, width=2)
            self.canvas.coords(marker, nx - 5, ny - 5, nx + 5, ny + 5)
            self._show(key)

        # -- arrow geometry ------------------------------------------------
        arrow = observation.arrow
        if arrow.valid and observation.contour_px and draws_internals:
            points: list[float] = []
            for x, y in observation.contour_px:
                view_x, view_y = transform.apply(float(x), float(y))
                points.extend((view_x, view_y))
            polygon = self._polygon("contour", outline=self.ARROW_COLOUR, fill="", width=2)
            if len(points) >= 6:
                self.canvas.coords(polygon, *points)
                self._show("contour")
            else:
                self._hide("contour")
        else:
            self._hide("contour")

        if arrow.valid and arrow.bbox_px is not None and draws_internals:
            x, y, w, h = arrow.bbox_px
            top_left = transform.apply(float(x), float(y))
            bottom_right = transform.apply(float(x + w), float(y + h))
            box = self._line("bbox", fill=self.ARROW_COLOUR, width=1, dash=(3, 3))
            self.canvas.coords(
                box,
                top_left[0],
                top_left[1],
                bottom_right[0],
                top_left[1],
                bottom_right[0],
                bottom_right[1],
                top_left[0],
                bottom_right[1],
                top_left[0],
                top_left[1],
            )
            self._show("bbox")
        else:
            self._hide("bbox")

        if arrow.valid and arrow.centroid_px is not None and draws_internals:
            cx, cy = transform.apply_point(arrow.centroid_px)
            centroid = self._oval("centroid", outline=self.ARROW_COLOUR, fill=self.ARROW_COLOUR)
            self.canvas.coords(centroid, cx - 3, cy - 3, cx + 3, cy + 3)
            self._show("centroid")
        else:
            self._hide("centroid")

        if (
            arrow.valid
            and arrow.tip_px is not None
            and arrow.tail_px is not None
            and draws_internals
        ):
            tail_view = transform.apply_point(arrow.tail_px)
            tip_view = transform.apply_point(arrow.tip_px)
            shaft = self._line("shaft", fill=GOLD, width=2, arrow="last")
            self.canvas.coords(shaft, tail_view[0], tail_view[1], tip_view[0], tip_view[1])
            self._show("shaft")
        else:
            self._hide("shaft")

        if arrow.valid and arrow.tip_px is not None and draws_internals:
            tx, ty = transform.apply_point(arrow.tip_px)
            tip = self._line("tip", fill=GOLD, width=2)
            self.canvas.coords(tip, tx - 7, ty, tx + 7, ty)
            tip2 = self._line("tip2", fill=GOLD, width=2)
            self.canvas.coords(tip2, tx, ty - 7, tx, ty + 7)
            self._show("tip")
            self._show("tip2")
        else:
            self._hide("tip")
            self._hide("tip2")

        # -- rejected candidates -------------------------------------------
        # Minimal mode draws none of these on purpose: someone watching a route
        # wants the turn, not the eight blobs the detector considered.
        rejected = (
            [c for c in observation.candidates if not c.accepted] if draws_internals else []
        )
        while len(self._reject_items) < len(rejected):
            self._reject_items.append(
                self.canvas.create_rectangle(
                    0, 0, 0, 0, outline=self.REJECT_COLOUR, width=1, dash=(2, 4)
                )
            )
        for index, item in enumerate(self._reject_items):
            if index < len(rejected):
                x, y, w, h = rejected[index].bbox_px
                a = transform.apply(float(x), float(y))
                b = transform.apply(float(x + w), float(y + h))
                self.canvas.coords(item, a[0], a[1], b[0], b[1])
                self.canvas.itemconfigure(item, state="normal")
            else:
                self.canvas.itemconfigure(item, state="hidden")

        # -- captions -------------------------------------------------------
        # A backdrop, because white text over a bright game frame is unreadable
        # and this panel exists to be read.
        backdrop = self._items.get("caption_bg")
        if backdrop is None:
            backdrop = self.canvas.create_rectangle(
                0, 0, 0, 0, fill=BG, outline="", stipple="gray75"
            )
            self._items["caption_bg"] = backdrop
        frozen = observation.packet_kind is not PacketKind.FRAME
        caption = self._text(
            "caption",
            anchor="nw",
            fill=MUTED if (stale or frozen) else BONE,
            font=("Menlo" if sys.platform == "darwin" else "Consolas", 10),
            justify="left",
        )
        self.canvas.coords(caption, 12, 10)
        self.canvas.itemconfigure(caption, text=self._caption(observation), state="normal")
        bounds = self.canvas.bbox(caption)
        if bounds is not None:
            self.canvas.coords(
                backdrop, bounds[0] - 6, bounds[1] - 5, bounds[2] + 6, bounds[3] + 5
            )
            self.canvas.itemconfigure(backdrop, state="normal")
        self.canvas.tag_raise(backdrop)
        self.canvas.tag_raise(caption)

        # Last, so it is above the caption backdrop and every diagnostic item.
        self._draw_action_layer(observation, transform)

    # -- the action layer -------------------------------------------------
    #: Every canvas item the action layer can own, so it can be cleared
    #: wholesale without knowing what the last frame happened to draw. Bounded
    #: by construction: six bearings plus a yaw bar, a jump pill and a badge.
    ACTION_ITEM_NAMES: ClassVar[tuple[str, ...]] = (
        *(
            f"action_{part}_{glyph}"
            for glyph in ("W", "A", "S", "D", "LT", "RT")
            for part in ("under", "stroke", "box", "text")
        ),
        "action_badge",
        "action_badge_bg",
        "action_yaw_under",
        "action_yaw_stroke",
        "action_yaw_box",
        "action_yaw_text",
        "action_jump_box",
        "action_jump_text",
    )

    #: Glyph -> the suffix used in item names. "<" and ">" cannot appear in a
    #: name that is also read back in tests, so they get words.
    ACTION_KEYS: ClassVar[dict[str, str]] = {
        "W": "W",
        "A": "A",
        "S": "S",
        "D": "D",
        "<": "LT",
        ">": "RT",
    }

    def _clear_action_layer(self) -> None:
        """Hide every action item. The only thing a stopped packet needs."""
        for name in self.ACTION_ITEM_NAMES:
            self._hide(name)

    def _draw_action_layer(
        self, observation: DiagnosticObservation, transform: Affine2D
    ) -> None:
        """Draw what is actually being pressed, above everything else.

        Reads ``observation.command_view`` and nothing else. That value is
        constructed *after* the command was proposed or applied, so a request
        the authority refused can never reach this method as though it had
        landed, and a frozen packet arrives with its glyphs already emptied.
        """
        view = observation.command_view
        anchor = observation.anchor_px
        frozen = observation.packet_kind is not PacketKind.FRAME
        if anchor is None or frozen or not view.active:
            self._clear_action_layer()
            return

        origin = transform.apply_point(anchor)
        # Dashed for a proposal, solid for a command that is really held. The
        # colour is identical on purpose: the same action, two provenances.
        dash = (10, 6) if view.outcome is CommandOutcome.WOULD else ()
        drawn: list[int] = []

        for glyph in view.glyphs:
            if glyph == "JUMP":
                continue
            bearing = self.ACTION_BEARINGS.get(glyph)
            suffix = self.ACTION_KEYS.get(glyph)
            if bearing is None or suffix is None:
                continue
            radians = math.radians(bearing)
            end = (
                origin[0] + math.sin(radians) * self.ACTION_RAY_PX,
                origin[1] - math.cos(radians) * self.ACTION_RAY_PX,
            )
            under = self._line(
                f"action_under_{suffix}",
                fill=self.ACTION_UNDERLAY,
                width=self.ACTION_UNDERLAY_WIDTH,
                capstyle="round",
            )
            self.canvas.coords(under, origin[0], origin[1], end[0], end[1])
            self._show(f"action_under_{suffix}")
            stroke = self._line(
                f"action_stroke_{suffix}",
                fill=self.ACTION_COLOUR,
                width=self.ACTION_STROKE_WIDTH,
                capstyle="round",
                arrow="last",
            )
            self.canvas.itemconfigure(stroke, dash=dash)
            self.canvas.coords(stroke, origin[0], origin[1], end[0], end[1])
            self._show(f"action_stroke_{suffix}")
            drawn.extend((under, stroke))
            drawn.extend(
                self._action_label(f"action_box_{suffix}", f"action_text_{suffix}", glyph, end)
            )

        if "JUMP" in view.glyphs:
            above = (origin[0], origin[1] - self.ACTION_RAY_PX - 34)
            drawn.extend(
                self._action_label("action_jump_box", "action_jump_text", "JUMP", above)
            )
        else:
            self._hide("action_jump_box")
            self._hide("action_jump_text")

        if view.yaw_px:
            # Relative mouse yaw has no key, so it gets a bar whose direction
            # is its sign and whose label carries the magnitude.
            reach = self.ACTION_RAY_PX * 0.8
            end_x = origin[0] + (reach if view.yaw_px > 0 else -reach)
            end_y = origin[1] - self.ACTION_RAY_PX * 0.42
            under = self._line(
                "action_yaw_under",
                fill=self.ACTION_UNDERLAY,
                width=self.ACTION_UNDERLAY_WIDTH,
                capstyle="round",
            )
            self.canvas.coords(under, origin[0], end_y, end_x, end_y)
            self._show("action_yaw_under")
            stroke = self._line(
                "action_yaw_stroke",
                fill=self.ACTION_COLOUR,
                width=self.ACTION_STROKE_WIDTH,
                capstyle="round",
                arrow="last",
            )
            self.canvas.itemconfigure(stroke, dash=dash)
            self.canvas.coords(stroke, origin[0], end_y, end_x, end_y)
            self._show("action_yaw_stroke")
            drawn.extend((under, stroke))
            drawn.extend(
                self._action_label(
                    "action_yaw_box",
                    "action_yaw_text",
                    f"MOUSE {view.yaw_px:+d}",
                    (end_x, end_y),
                )
            )
        else:
            for name in (
                "action_yaw_under",
                "action_yaw_stroke",
                "action_yaw_box",
                "action_yaw_text",
            ):
                self._hide(name)

        drawn.extend(
            self._action_label(
                "action_badge_bg",
                "action_badge",
                view.label,
                (origin[0], origin[1] + self.ACTION_RAY_PX + 30),
            )
        )
        # Raised last, and after the caption, so nothing the detector drew and
        # nothing the caption backdrop covers can sit on top of it.
        for item in drawn:
            self.canvas.tag_raise(item)

    def _action_label(
        self, box_name: str, text_name: str, text: str, centre: tuple[float, float]
    ) -> list[int]:
        """A boxed key label: dark plate, purple border, bold glyph."""
        label = self._text(
            text_name,
            fill="#ffffff",
            font=self.ACTION_LABEL_FONT,
            anchor="center",
            justify="center",
        )
        self.canvas.coords(label, centre[0], centre[1])
        self.canvas.itemconfigure(label, text=text, state="normal")
        bounds = self.canvas.bbox(label)
        box = self._items.get(box_name)
        if box is None:
            box = self.canvas.create_rectangle(
                0, 0, 0, 0, fill=self.ACTION_UNDERLAY, outline=self.ACTION_COLOUR, width=2
            )
            self._items[box_name] = box
        if bounds is not None:
            self.canvas.coords(box, bounds[0] - 7, bounds[1] - 5, bounds[2] + 7, bounds[3] + 5)
        self.canvas.itemconfigure(box, state="normal")
        self.canvas.tag_raise(box)
        self.canvas.tag_raise(label)
        return [box, label]

    def _set_arm(
        self,
        item: int,
        anchor: tuple[float, float],
        heading_deg: float | None,
        transform: Affine2D,
        length: float | None = None,
    ) -> None:
        if heading_deg is None:
            self.canvas.itemconfigure(item, state="hidden")
            return
        radians = math.radians(heading_deg)
        reach = self.ARM_LENGTH_PX if length is None else length
        # Screen-space heading: 0 is up, positive clockwise.
        end = (
            anchor[0] + math.sin(radians) * reach,
            anchor[1] - math.cos(radians) * reach,
        )
        start_view = transform.apply_point(anchor)
        end_view = transform.apply_point(end)
        self.canvas.coords(item, start_view[0], start_view[1], end_view[0], end_view[1])
        self.canvas.itemconfigure(item, state="normal")

    CUE_COLOUR = "#4a9bd1"

    def _draw_cue_arms(
        self,
        observation: DiagnosticObservation,
        anchor: tuple[float, float],
        transform: Affine2D,
    ) -> None:
        """One thin arm per candidate cue. Full Diagnostics only.

        When the fusion cue abstains because its components disagree, this is
        what shows *how* they disagree - which is the difference between a
        useful diagnostic and a blank screen. It is also a dozen blue rays and
        labels radiating from the character, which is why Minimal draws none of
        it: this used to run unconditionally and was the crowding it caused
        that made a stopped navigator look busy.
        """
        if not self.mode.draws_diagnostics or observation.packet_kind is not PacketKind.FRAME:
            for index in range(len(observation.cues)):
                self._hide(f"cue_{index}")
                self._hide(f"cue_{index}_label")
            return
        for index, cue in enumerate(observation.cues):
            key = f"cue_{index}"
            if cue.heading_deg is None:
                self._hide(key)
                self._hide(f"{key}_label")
                continue
            # A cue consensus rejected is drawn faintly rather than hidden: the
            # whole point of showing the components is to see the disagreement.
            rejected = cue.weight <= 0.0
            item = self._line(
                key,
                fill=self.REJECT_COLOUR if rejected else self.CUE_COLOUR,
                width=1,
                arrow="last",
                dash=(2, 6) if rejected else (2, 3),
            )
            name, heading = cue.cue_id, cue.heading_deg
            self._set_arm(item, anchor, heading, transform, length=self.ARM_LENGTH_PX * 0.72)
            radians = math.radians(heading)
            label_point = (
                anchor[0] + math.sin(radians) * self.ARM_LENGTH_PX * 0.78,
                anchor[1] - math.cos(radians) * self.ARM_LENGTH_PX * 0.78,
            )
            view = transform.apply_point(label_point)
            label = self._text(
                f"{key}_label", fill=self.CUE_COLOUR, font=("Helvetica", 9), anchor="center"
            )
            self.canvas.coords(label, view[0], view[1])
            self.canvas.itemconfigure(
                label,
                text=f"{name} {heading:+.0f}°" + ("  (outlier)" if rejected else ""),
                state="normal",
            )

    def _draw_arc(
        self,
        observation: DiagnosticObservation,
        anchor: tuple[float, float],
        transform: Affine2D,
    ) -> None:
        error = observation.signed_error_deg
        if error is None:
            self._hide("arc")
            self._hide("angle_text")
            return
        radius = self.ARM_LENGTH_PX * 0.55
        points: list[float] = []
        steps = 24
        for index in range(steps + 1):
            heading = (observation.forward_deg or 0.0) + error * (index / steps)
            radians = math.radians(heading)
            point = (
                anchor[0] + math.sin(radians) * radius,
                anchor[1] - math.cos(radians) * radius,
            )
            view = transform.apply_point(point)
            points.extend(view)
        arc = self._line("arc", fill=self.ARC_COLOUR, width=2, smooth=True)
        self.canvas.coords(arc, *points)
        self._show("arc")

        mid_heading = (observation.forward_deg or 0.0) + error / 2.0
        mid_radians = math.radians(mid_heading)
        label_point = (
            anchor[0] + math.sin(mid_radians) * (radius + 26),
            anchor[1] - math.cos(mid_radians) * (radius + 26),
        )
        label_view = transform.apply_point(label_point)
        text = self._text("angle_text", fill=self.ARC_COLOUR, font=("Helvetica", 13, "bold"))
        self.canvas.coords(text, label_view[0], label_view[1])
        self.canvas.itemconfigure(text, text=f"{error:+.1f}°")
        self._show("angle_text")

    def _caption(self, observation: DiagnosticObservation) -> str:
        arrow = observation.arrow
        direction = observation.direction
        if observation.packet_kind is not PacketKind.FRAME:
            # A frozen picture may stay on screen. It must say so, carry its
            # age, and never look like a live reading (mission section 6).
            return (
                f"FROZEN - {observation.plain_summary}\n"
                f"last frame #{observation.frame_sequence}, "
                f"{observation.age_s:.1f} s old\n"
                "no command is in effect"
            )
        if self.mode is OverlayMode.MINIMAL:
            summary = observation.plain_summary or "no reading"
            return f"{summary}\nframe #{observation.frame_sequence}  {self.mode.value}"
        lines = [
            f"frame #{observation.frame_sequence}  age {observation.age_s * 1000:5.1f} ms"
            f"  {observation.geometry.state.value}",
            f"profile {observation.profile_id} [{observation.profile_status}]"
            f"  cue {observation.strategy_id}",
        ]
        if arrow.valid:
            lines.append(f"arrow conf {arrow.confidence:.2f}  track {arrow.track_id}")
        else:
            lines.append(f"arrow ABSTAIN: {arrow.abstain_reason}")
        if direction.valid and direction.error_deg is not None:
            disagreement = (
                f"  cue spread {direction.cue_disagreement_deg:.1f}°"
                if direction.cue_disagreement_deg is not None
                else ""
            )
            lines.append(
                f"turn {direction.error_deg:+.1f}°  conf {direction.confidence:.2f}"
                f"{disagreement}"
            )
        else:
            lines.append(f"direction ABSTAIN: {direction.abstain_reason}")
        agreeing = [
            f"{cue.cue_id} {cue.heading_deg:+.0f}"
            for cue in observation.cues
            if cue.valid and cue.heading_deg is not None
        ]
        lines.append("cues: " + (", ".join(agreeing) if agreeing else "all abstained"))
        if direction.valid and direction.sign_margin_deg:
            lines.append(
                f"polarity margin {direction.sign_margin_deg:.0f}°"
                f"  anisotropy {direction.anisotropy:.2f}"
            )
        lines.append(f"forward ref: {observation.forward_source}")
        if observation.candidates:
            rejected = sum(1 for c in observation.candidates if not c.accepted)
            lines.append(f"candidates {len(observation.candidates)} ({rejected} rejected)")
        lines.append(
            f"perception {observation.perception_ms:4.1f} ms"
            f"  decision {observation.decision_ms:4.1f} ms"
        )
        if observation.phase is not None:
            lines.append(f"phase {observation.phase.name}")
        return "\n".join(lines)
