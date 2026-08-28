#!/usr/bin/env python3
"""Treasure Navigator dashboard: the Tk shell around the coordinator.

The UI submits :class:`RuntimeIntent` objects and renders
:class:`TelemetrySnapshot` objects. It owns no run state, calls no input, and
captures nothing of its own - the preview draws the *same* frame the decision
used, so the overlay can never disagree with the decision it illustrates.

One rule shapes the whole layout: **clicking Tk removes focus from Roblox.**
So there is no actionable Start Live, Reset, or Pan Test button. Those become
non-clickable guidance ("Refocus Roblox, then press F1"), and the real hotkeys
submit their intents only while Roblox is positively focused (plan 11.2).

Design note: the dark surface, bone text, jade interactive and gold emphasis
semantics are implemented here independently through ``ttk.Style``. No CSS,
markup, or text was copied from any other project (plan 12).
"""

from __future__ import annotations

import contextlib
import math
import queue
import sys
import tkinter as tk
from dataclasses import dataclass
from tkinter import font as tkfont
from tkinter import ttk
from typing import Any

import numpy as np

from prospector_engine import __version__
from prospector_engine.capture import CaptureService, EvidenceRegistry, ViewportGuard
from prospector_engine.contracts import (
    CaptureMetrics,
    DiagnosticObservation,
    IntentType,
    ModeResult,
    ModeResultKind,
    RunMode,
    TelemetrySnapshot,
    monotonic_s,
)
from prospector_engine.coordinator import (
    CoordinatorConfig,
    RuntimeCoordinator,
    WorkerContext,
    WorkerFactory,
)
from prospector_engine.engine import (
    DEFAULT_PIXELS,
    ServiceContext,
    run_dig_loop,
    run_pan_swap,
    run_reset,
)
from prospector_engine.geometry import Affine2D
from prospector_engine.input_authority import (
    AuthorityConfig,
    DeadmanClient,
    HealthSources,
    InputAuthority,
)
from prospector_engine.navigation import (
    NavigationGates,
    PerceptionPipeline,
    make_live_worker,
    make_shadow_worker,
)
from prospector_engine.ports import PlatformPort, create_platform_port, current_platform_name
from prospector_engine.telemetry import (
    AppPaths,
    EvidenceRecorder,
    LatestSlot,
    resolve_app_paths,
)
from prospector_engine.vision import ArrowSegmenter, load_profiles

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


def _font_family() -> str:
    candidates = ["SF Pro Text", "Helvetica Neue", "Segoe UI", "DejaVu Sans", "TkDefaultFont"]
    available = set(tkfont.families())
    for name in candidates:
        if name in available:
            return name
    return "TkDefaultFont"


# ---------------------------------------------------------------------------
# Worker factories
# ---------------------------------------------------------------------------


def _service_worker(kind: str) -> WorkerFactory:
    """Wrap a bounded engine service as a coordinator mode worker."""

    def worker(context: WorkerContext) -> ModeResult:
        session = context.service
        if session is None:
            return ModeResult(ModeResultKind.FAILED, f"{kind} started without an input session")
        service_context = ServiceContext(
            frames=context.frames,
            session=session,
            cancel=context.cancellation,
            deadline_s=monotonic_s() + 90.0,
            on_status=context.on_status,
        )
        kind_map = {
            "SUCCESS": ModeResultKind.COMPLETED,
            "CANCELLED": ModeResultKind.CANCELLED,
        }
        if kind == "reset":
            result = run_reset(service_context)
            return ModeResult(
                kind_map.get(result.outcome.name, ModeResultKind.FAILED),
                f"reset: {result.detail}",
                result.evidence,
            )
        if kind == "dig_loop":
            # The dig loop runs for as long as it is productive, so it gets the
            # service's own deadline rather than the 90 s the one-shot services use.
            service_context.deadline_s = monotonic_s() + 31 * 60.0
            dig = run_dig_loop(service_context)
            digs_ok = dig.outcome.name in ("CANCELLED", "TIMEOUT", "PAN_FULL", "CUE_LOST")
            return ModeResult(
                ModeResultKind.CANCELLED if digs_ok else ModeResultKind.FAILED,
                f"dig loop: {dig.taps} taps, {dig.pan_swaps} pan swaps - {dig.detail}",
                dig.evidence,
            )
        swap = run_pan_swap(service_context)
        return ModeResult(
            kind_map.get(swap.outcome.name, ModeResultKind.FAILED),
            f"pan swap: {swap.detail}",
            swap.evidence,
        )

    return worker


def _pixel_info_worker(port: PlatformPort, on_report: Any) -> WorkerFactory:
    """F3: read the pixel under the cursor. Emits no input, reads one frame."""

    def worker(context: WorkerContext) -> ModeResult:
        envelope = context.frames.latest()
        cursor = port.cursor_client_px()
        if envelope is None or cursor is None:
            on_report("Pixel probe: Roblox client not verified, or cursor outside it.")
            return ModeResult(ModeResultKind.FAILED, "no client rect or cursor outside it")
        r, g, b = envelope.frame.sample_mean_rgb(cursor, DEFAULT_PIXELS.sample_box_px)
        message = (
            f"PIXEL=({cursor[0]},{cursor[1]}) RGB=({int(r)},{int(g)},{int(b)}) "
            f"[canonical client basis]"
        )
        on_report(message)
        return ModeResult(ModeResultKind.COMPLETED, message)

    return worker


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

    #: Reference arm colours. The assumed arm is deliberately dashed and muted:
    #: E-ANCHOR and E-FORWARD are pending, so it is a hypothesis on screen.
    FORWARD_COLOUR = "#c3ccd6"
    DESIRED_COLOUR = GOLD
    ARROW_COLOUR = JADE
    REJECT_COLOUR = "#8a4b4b"
    ARC_COLOUR = "#d0743c"

    ARM_LENGTH_PX = 190.0

    def __init__(self, canvas: tk.Canvas) -> None:
        self.canvas = canvas
        self._photo: Any = None
        self._photo_size: tuple[int, int] = (0, 0)
        self._scaled: Any = None
        self._rgb: Any = None
        self._image_item: int | None = None
        self._items: dict[str, int] = {}
        self._reject_items: list[int] = []
        self._last_sequence = -1

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
        """Draw one observation. Returns False if it was already drawn."""
        if observation.frame_sequence == self._last_sequence:
            return False
        self._last_sequence = observation.frame_sequence

        width = max(64, self.canvas.winfo_width())
        height = max(64, self.canvas.winfo_height())
        transform = observation.geometry.preview_from_canonical((width, height))
        self._draw_image(observation, (width, height), transform)
        self._draw_overlay(observation, transform)
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
            self._set_arm(forward_item, anchor, observation.forward_deg, transform)
            forward_label = self._text(
                "forward_label", fill=self.FORWARD_COLOUR, font=("Helvetica", 10, "bold")
            )
            label_point = (anchor[0], anchor[1] - self.ARM_LENGTH_PX - 16)
            label_view = transform.apply_point(label_point)
            self.canvas.coords(forward_label, label_view[0], label_view[1])
            self.canvas.itemconfigure(forward_label, text="forward (assumed)", state="normal")

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
                self._set_arm(desired_item, anchor, observation.desired_deg, transform)
                self._show("desired_arm")
                self._draw_arc(observation, anchor, transform)

        # -- arrow geometry ------------------------------------------------
        arrow = observation.arrow
        if arrow.valid and observation.contour_px:
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

        if arrow.valid and arrow.bbox_px is not None:
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

        if arrow.valid and arrow.centroid_px is not None:
            cx, cy = transform.apply_point(arrow.centroid_px)
            centroid = self._oval("centroid", outline=self.ARROW_COLOUR, fill=self.ARROW_COLOUR)
            self.canvas.coords(centroid, cx - 3, cy - 3, cx + 3, cy + 3)
            self._show("centroid")
        else:
            self._hide("centroid")

        if arrow.valid and arrow.tip_px is not None:
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
        rejected = [c for c in observation.candidates if not c.accepted]
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
        caption = self._text(
            "caption",
            anchor="nw",
            fill=MUTED if stale else BONE,
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
        """One thin arm per candidate cue.

        When the fusion cue abstains because its components disagree, this is
        what shows *how* they disagree - which is the difference between a
        useful diagnostic and a blank screen.
        """
        for index, (name, cue) in enumerate(observation.cues):
            key = f"cue_{index}"
            if not cue.valid or cue.error_deg is None or name == observation.strategy_id:
                self._hide(key)
                self._hide(f"{key}_label")
                continue
            item = self._line(key, fill=self.CUE_COLOUR, width=1, arrow="last", dash=(2, 3))
            heading = (observation.forward_deg or 0.0) + cue.error_deg
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
                label, text=f"{name} {cue.error_deg:+.0f}°", state="normal"
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

    @staticmethod
    def _caption(observation: DiagnosticObservation) -> str:
        arrow = observation.arrow
        direction = observation.direction
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
            f"{name} {cue.error_deg:+.0f}"
            for name, cue in observation.cues
            if cue.valid and cue.error_deg is not None
        ]
        lines.append("cues: " + (", ".join(agreeing) if agreeing else "all abstained"))
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


# ---------------------------------------------------------------------------
# Application wiring
# ---------------------------------------------------------------------------


@dataclass
class Application:
    """Everything the process owns, constructed once and shut down once."""

    port: PlatformPort
    guard: ViewportGuard
    registry: EvidenceRegistry
    capture: CaptureService
    authority: InputAuthority
    coordinator: RuntimeCoordinator
    deadman: DeadmanClient
    gates: NavigationGates
    preview: LatestSlot[Any]
    reports: queue.Queue[str]
    paths: AppPaths
    #: One pipeline shared with the running worker, so a profile change in the
    #: UI takes effect on the very next frame instead of the next session.
    pipeline: PerceptionPipeline
    library: Any

    def shutdown(self) -> dict[str, str]:
        report = self.coordinator.shutdown()
        self.deadman.close()
        return report


def build_application(profile_id: str = "yellow_map_v0") -> Application:
    paths = resolve_app_paths().ensure()
    port = create_platform_port()
    guard = ViewportGuard(port)
    deadman = DeadmanClient(config=AuthorityConfig())
    reports: queue.Queue[str] = queue.Queue(maxsize=32)
    preview: LatestSlot[Any] = LatestSlot()

    registry = EvidenceRegistry("pending")
    capture = CaptureService(guard, registry)

    def capture_age_s() -> float | None:
        return capture.latest_age_s()

    authority = InputAuthority(
        port,
        deadman=deadman,
        health=HealthSources(
            focus=port.focus_state,
            client_rect=lambda: guard.geometry if guard.geometry.valid else None,
            capture_age_s=capture_age_s,
        ),
        config=AuthorityConfig(),
    )
    registry = EvidenceRegistry(authority.run_id, on_token=authority.register_evidence)
    capture = CaptureService(
        guard, registry, source_factory=port.create_capture_source, on_frame=preview.publish
    )

    library = load_profiles()
    profile = library.get(profile_id) or library.all()[0]
    gates = NavigationGates(os_name=current_platform_name(), profile_id=profile.profile_id)
    pipeline = PerceptionPipeline(segmenter=ArrowSegmenter(profile))

    def pipeline_factory() -> PerceptionPipeline:
        return pipeline

    def report(message: str) -> None:
        # Drop-oldest is fine here: this is a diagnostic read-out, not evidence.
        with contextlib.suppress(queue.Full):
            reports.put_nowait(message)

    workers: dict[IntentType, WorkerFactory] = {
        IntentType.START_SHADOW: make_shadow_worker(pipeline_factory, gates),
        IntentType.START_LIVE: make_live_worker(pipeline_factory, gates),
        IntentType.RESET_CHARACTER: _service_worker("reset"),
        IntentType.PAN_SWAP_TEST: _service_worker("pan_swap"),
        IntentType.DIG_LOOP: _service_worker("dig_loop"),
        IntentType.PIXEL_INFO: _pixel_info_worker(port, report),
    }
    coordinator = RuntimeCoordinator(
        authority=authority,
        guard=guard,
        capture=capture,
        registry=registry,
        workers=workers,
        config=CoordinatorConfig(),
        paths=paths,
        pipeline_provider=lambda: pipeline,
    )
    return Application(
        port=port,
        guard=guard,
        registry=registry,
        capture=capture,
        authority=authority,
        coordinator=coordinator,
        deadman=deadman,
        gates=gates,
        preview=preview,
        reports=reports,
        paths=paths,
        pipeline=pipeline,
        library=library,
    )


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


class Dashboard:
    """The Tk shell. Renders snapshots; never decides anything."""

    #: Three independent cadences. The preview is allowed to be fast because it
    #: is cheap; the status text is deliberately slow because nobody can read
    #: 60 Hz of numbers, and re-laying out text is the expensive part of Tk.
    PREVIEW_INTERVAL_MS = 16  # up to ~60 Hz, capped by what actually arrives
    STATUS_INTERVAL_MS = 150  # ~7 Hz
    METRICS_INTERVAL_MS = 500

    def __init__(self, root: tk.Tk, app: Application) -> None:
        self.root = root
        self.app = app
        self.recorder: EvidenceRecorder | None = None
        self._diagnostics: DiagnosticCanvas | None = None
        self._last_observation: DiagnosticObservation | None = None

        root.title(f"Treasure Navigator {__version__}")
        root.configure(bg=BG)
        root.minsize(940, 620)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(3, weight=1)

        self._style()
        self._build_header()
        self._build_readiness()
        self._build_controls()
        self._build_body()
        self._build_footer()

        root.after(self.PREVIEW_INTERVAL_MS, self._tick_preview)
        root.after(self.STATUS_INTERVAL_MS, self._tick_status)
        root.after(self.METRICS_INTERVAL_MS, self._tick_metrics)

    # -- styling ----------------------------------------------------------
    def _style(self) -> None:
        family = _font_family()
        self.f_title = (family, 20, "bold")
        self.f_mode = (family, 26, "bold")
        self.f_body = (family, 12)
        self.f_small = (family, 10)
        self.f_mono = ("Menlo" if sys.platform == "darwin" else "Consolas", 10)

        style = ttk.Style(self.root)
        with_theme = "clam" if "clam" in style.theme_names() else style.theme_use()
        style.theme_use(with_theme)
        style.configure("T.TFrame", background=BG)
        style.configure("Card.TFrame", background=SURFACE, relief="flat")
        style.configure("T.TLabel", background=BG, foreground=BONE, font=self.f_body)
        style.configure("Card.TLabel", background=SURFACE, foreground=BONE, font=self.f_body)
        style.configure("Muted.TLabel", background=SURFACE, foreground=MUTED, font=self.f_small)
        style.configure(
            "Guide.TLabel", background=SURFACE_ALT, foreground=GOLD, font=self.f_small
        )
        style.configure(
            "T.TButton",
            background=SURFACE_ALT,
            foreground=BONE,
            font=self.f_body,
            padding=(12, 8),
            borderwidth=0,
        )
        style.map(
            "T.TButton",
            background=[("active", JADE), ("disabled", SURFACE)],
            foreground=[("active", BG), ("disabled", MUTED)],
        )
        style.configure(
            "Stop.TButton", background=BAD, foreground=BONE, font=self.f_body, padding=(12, 8)
        )
        style.map("Stop.TButton", background=[("active", "#b8433f")])
        style.configure(
            "T.TCombobox",
            fieldbackground=SURFACE_ALT,
            background=SURFACE_ALT,
            foreground=BONE,
            arrowcolor=BONE,
            selectbackground=SURFACE_ALT,
            selectforeground=BONE,
            padding=(8, 6),
        )
        style.map(
            "T.TCombobox",
            fieldbackground=[("readonly", SURFACE_ALT)],
            foreground=[("readonly", BONE)],
            selectbackground=[("readonly", SURFACE_ALT)],
            selectforeground=[("readonly", BONE)],
        )
        self.root.option_add("*TCombobox*Listbox.background", SURFACE_ALT)
        self.root.option_add("*TCombobox*Listbox.foreground", BONE)
        self.root.option_add("*TCombobox*Listbox.selectBackground", JADE)
        self.root.option_add("*TCombobox*Listbox.selectForeground", BG)

    def _card(self, parent: tk.Misc, **grid: Any) -> ttk.Frame:
        frame = ttk.Frame(parent, style="Card.TFrame", padding=10)
        frame.grid(sticky="nsew", padx=6, pady=6, **grid)
        return frame

    # -- layout -----------------------------------------------------------
    def _build_header(self) -> None:
        header = ttk.Frame(self.root, style="T.TFrame", padding=(14, 12, 14, 0))
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(1, weight=1)
        ttk.Label(header, text="Treasure Navigator", style="T.TLabel", font=self.f_title).grid(
            row=0, column=0, sticky="w"
        )
        self.mode_var = tk.StringVar(value="OFF")
        self.mode_label = ttk.Label(
            header, textvariable=self.mode_var, style="T.TLabel", font=self.f_mode
        )
        self.mode_label.grid(row=0, column=1, sticky="e", padx=(0, 14))
        ttk.Button(header, text="STOP  (F2)", style="Stop.TButton", command=self._stop).grid(
            row=0, column=2, sticky="e"
        )

    def _build_readiness(self) -> None:
        strip = ttk.Frame(self.root, style="T.TFrame", padding=(8, 6))
        strip.grid(row=1, column=0, sticky="ew")
        self.readiness_labels: dict[str, ttk.Label] = {}
        keys = [
            "viewport",
            "focus",
            "capture",
            "watchdog",
            "deadman",
            "ledger",
            "release",
            "arm",
            "pixels",
        ]
        for index, key in enumerate(keys):
            strip.columnconfigure(index, weight=1)
            card = ttk.Frame(strip, style="Card.TFrame", padding=(8, 6))
            card.grid(row=0, column=index, sticky="nsew", padx=3)
            ttk.Label(card, text=key.upper(), style="Muted.TLabel").pack(anchor="w")
            label = ttk.Label(card, text="-", style="Card.TLabel")
            label.pack(anchor="w")
            self.readiness_labels[key] = label

    def _build_controls(self) -> None:
        row = ttk.Frame(self.root, style="T.TFrame", padding=(8, 2))
        row.grid(row=2, column=0, sticky="ew")
        for index in range(6):
            row.columnconfigure(index, weight=1)

        ttk.Button(row, text="Pin Window", style="T.TButton", command=self._pin).grid(
            row=0, column=0, sticky="ew", padx=3
        )
        ttk.Button(row, text="Start Shadow", style="T.TButton", command=self._shadow).grid(
            row=0, column=1, sticky="ew", padx=3
        )
        self.arm_button = ttk.Button(
            row, text="Arm Live...", style="T.TButton", command=self._arm
        )
        self.arm_button.grid(row=0, column=2, sticky="ew", padx=3)
        self.record_button = ttk.Button(
            row, text="Record: off", style="T.TButton", command=self._toggle_record
        )
        self.record_button.grid(row=0, column=3, sticky="ew", padx=3)

        # Only shown while an unsafe-release latch is set. It emits up-edges
        # only; it is the explicit handshake plan 4.4 requires before Live can
        # be offered again.
        self.recover_button = ttk.Button(
            row, text="Recover release", style="T.TButton", command=self._recover
        )

        # Guidance, not buttons: clicking Tk would steal focus from Roblox.
        self.live_guide = tk.Label(
            row,
            text="Live: arm first, then refocus Roblox -> F1",
            bg=SURFACE_ALT,
            fg=MUTED,
            font=self.f_small,
            padx=10,
            pady=8,
        )
        self.live_guide.grid(row=0, column=4, sticky="ew", padx=3)
        tk.Label(
            row,
            text="Focus Roblox -> F6 dig  |  F4 reset  |  F5 pan test  |  F3 pixel",
            bg=SURFACE_ALT,
            fg=MUTED,
            font=self.f_small,
            padx=10,
            pady=8,
        ).grid(row=0, column=5, sticky="ew", padx=3)

    def _build_body(self) -> None:
        body = ttk.Frame(self.root, style="T.TFrame", padding=(8, 4))
        body.grid(row=3, column=0, sticky="nsew")
        body.columnconfigure(0, weight=3)
        body.columnconfigure(1, weight=2)
        body.rowconfigure(0, weight=1)

        left = self._card(body, row=0, column=0)
        left.columnconfigure(0, weight=1)
        left.rowconfigure(1, weight=1)
        ttk.Label(
            left, text="SHADOW VIEW - live perception overlay", style="Muted.TLabel"
        ).grid(row=0, column=0, sticky="w")
        self.canvas = tk.Canvas(left, bg=SURFACE_ALT, highlightthickness=0, height=380)
        self.canvas.grid(row=1, column=0, sticky="nsew", pady=(6, 0))
        self._diagnostics = DiagnosticCanvas(self.canvas)
        legend = (
            "dashed grey = assumed player-forward reference (E-FORWARD PENDING)   "
            "gold = desired map-arrow direction   orange arc = signed turn   "
            "jade = detected arrow   dull red = rejected candidates"
        )
        ttk.Label(left, text=legend, style="Muted.TLabel", wraplength=760, justify="left").grid(
            row=2, column=0, sticky="w", pady=(6, 0)
        )

        right = self._card(body, row=0, column=1)
        right.columnconfigure(0, weight=1)
        right.rowconfigure(4, weight=1)
        ttk.Label(right, text="DECISION (this frame)", style="Muted.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        self.decision_var = tk.StringVar(value="no observation yet - start Shadow")
        ttk.Label(
            right,
            textvariable=self.decision_var,
            style="Card.TLabel",
            font=self.f_mono,
            wraplength=380,
            justify="left",
        ).grid(row=1, column=0, sticky="w", pady=(4, 8))

        ttk.Label(right, text="PIPELINE", style="Muted.TLabel").grid(
            row=2, column=0, sticky="w"
        )
        self.metrics_var = tk.StringVar(value="capture not started")
        self.metrics_label = ttk.Label(
            right,
            textvariable=self.metrics_var,
            style="Card.TLabel",
            font=self.f_mono,
            wraplength=380,
            justify="left",
        )
        self.metrics_label.grid(row=3, column=0, sticky="w", pady=(4, 8))

        self.profile_var = tk.StringVar()
        self._build_profile_selector(right)

        self.warnings_var = tk.StringVar(value="")
        self.warnings_label = ttk.Label(
            right,
            textvariable=self.warnings_var,
            style="Card.TLabel",
            wraplength=380,
            justify="left",
            foreground=MUTED,
        )
        self.warnings_label.grid(row=5, column=0, sticky="w", pady=(8, 0))

    def _build_profile_selector(self, parent: ttk.Frame) -> None:
        library = self.app.library
        frame = ttk.Frame(parent, style="Card.TFrame")
        frame.grid(row=4, column=0, sticky="nsew", pady=(8, 0))
        frame.columnconfigure(0, weight=1)
        ttk.Label(frame, text="ARROW PROFILE (explicit selection)", style="Muted.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        names = [f"{p.profile_id} [{p.status.value}]" for p in library.all()]
        self.profile_var.set(names[0] if names else "none")
        combo = ttk.Combobox(
            frame,
            textvariable=self.profile_var,
            values=names,
            state="readonly",
            style="T.TCombobox",
        )
        combo.grid(row=1, column=0, sticky="ew", pady=(4, 6))
        automatic = any(p.selectable_automatically for p in library.all())
        note = (
            "Automatic classification is DISABLED: no profile has passed E-PROF. "
            "Selection stays explicit."
            if not automatic
            else "Automatic classification available for validated profiles."
        )
        ttk.Label(frame, text=note, style="Muted.TLabel", wraplength=340, justify="left").grid(
            row=2, column=0, sticky="w"
        )
        pixels_note = (
            "Dig / pan-swap pixels: PENDING reverification. They were calibrated in the old "
            "window-frame basis; re-derive with --calibrate before unattended use."
        )
        ttk.Label(
            frame, text=pixels_note, style="Muted.TLabel", wraplength=340, justify="left"
        ).grid(row=3, column=0, sticky="w", pady=(6, 0))

    def _build_footer(self) -> None:
        footer = ttk.Frame(self.root, style="T.TFrame", padding=(8, 4, 8, 10))
        footer.grid(row=4, column=0, sticky="ew")
        footer.columnconfigure(0, weight=1)
        card = ttk.Frame(footer, style="Card.TFrame", padding=8)
        card.grid(row=0, column=0, sticky="ew")
        card.columnconfigure(0, weight=1)
        ttk.Label(card, text="RECENT EVENTS", style="Muted.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        self.events = tk.Text(
            card,
            height=6,
            bg=SURFACE_ALT,
            fg=BONE,
            insertbackground=BONE,
            font=self.f_mono,
            relief="flat",
            wrap="word",
        )
        self.events.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        self.events.configure(state="disabled")

    # -- intents ----------------------------------------------------------
    def _submit(self, intent_type: IntentType) -> None:
        coordinator = self.app.coordinator
        coordinator.submit(coordinator.next_intent(intent_type, "gui"))

    def _pin(self) -> None:
        self._submit(IntentType.PIN_WINDOW)

    def _shadow(self) -> None:
        self._submit(IntentType.START_SHADOW)

    def _stop(self) -> None:
        self._submit(IntentType.STOP)

    def _arm(self) -> None:
        """The one physical arming gesture. Never simulated, never persisted."""
        self._submit(IntentType.ARM_LIVE_FROM_UI)

    def _on_profile_selected(self, _event: Any) -> None:
        """Swap the profile on the *running* pipeline, not just the next one.

        The dashboard and the worker share one pipeline instance, so the change
        lands on the very next frame and the observation reports which profile
        actually produced it.
        """
        selected = self.profile_var.get().split(" ")[0]
        profile = self.app.library.get(selected)
        if profile is None:
            return
        self.app.pipeline.set_profile(profile)
        self.app.coordinator.events.add(
            "profile.selected", f"{profile.profile_id} [{profile.status.value}]"
        )

    def _recover(self) -> None:
        self._submit(IntentType.RECOVER_RELEASE)

    def _toggle_record(self) -> None:
        if self.recorder is not None:
            self.recorder.stop()
            self.recorder = None
            self.record_button.configure(text="Record: off")
            return
        session_dir = self.app.paths.recordings / f"session-{int(monotonic_s())}"
        self.recorder = EvidenceRecorder(session_dir)
        self.recorder.start()
        self.record_button.configure(text="Record: ON")

    # -- rendering --------------------------------------------------------
    def _tick_preview(self) -> None:
        """Draw the newest observation, if there is a newer one than last time."""
        started = monotonic_s()
        observation = self.app.coordinator.observations.peek()
        if observation is not None and self._diagnostics is not None:
            drawn = self._diagnostics.render(observation)
            if drawn:
                self._last_observation = observation
                self.app.capture.note_preview_ms((monotonic_s() - started) * 1000.0)
                if self.recorder is not None:
                    self.recorder.offer(
                        observation.frame, {"sequence": observation.frame_sequence}
                    )
        elif observation is None:
            self._render_idle_preview()
        self.root.after(self.PREVIEW_INTERVAL_MS, self._tick_preview)

    def _render_idle_preview(self) -> None:
        """Show the raw frame when no observation exists yet (Shadow not started)."""
        envelope = self.app.preview.peek()
        if envelope is None or self._diagnostics is None:
            return
        started = monotonic_s()
        if self._diagnostics.render_frame_only(envelope.frame):
            self.app.capture.note_preview_ms((monotonic_s() - started) * 1000.0)

    def _tick_status(self) -> None:
        snapshot = self.app.coordinator.snapshot()
        if snapshot is not None:
            self._render_status(snapshot)
        self._render_decision()
        self._drain_reports()
        self.root.after(self.STATUS_INTERVAL_MS, self._tick_status)

    def _tick_metrics(self) -> None:
        self._render_metrics(self.app.capture.metrics())
        self.root.after(self.METRICS_INTERVAL_MS, self._tick_metrics)

    def _render_status(self, snapshot: TelemetrySnapshot) -> None:
        mode_text = {
            RunMode.IDLE: "OFF",
            RunMode.SHADOW: "SHADOW",
            RunMode.LIVE: "LIVE",
            RunMode.SERVICE: "SERVICE",
            RunMode.SAFE_STOP: "SAFE-STOP",
        }[snapshot.mode]
        self.mode_var.set(mode_text)
        self.mode_label.configure(foreground=MODE_COLOURS[snapshot.mode])

        for key, label in self.readiness_labels.items():
            value = snapshot.readiness.get(key, "-")
            label.configure(text=value, foreground=STATUS_COLOURS.get(value, BONE))

        if self.app.authority.release_uncertain:
            self.recover_button.grid(
                row=1, column=0, columnspan=6, sticky="ew", padx=3, pady=(4, 0)
            )
        else:
            self.recover_button.grid_remove()

        token = self.app.coordinator.arm_token()
        if token is None:
            self.live_guide.configure(
                text="Live: arm first, then refocus Roblox -> F1", fg=MUTED
            )
        else:
            remaining = token.remaining_s(monotonic_s())
            self.live_guide.configure(
                text=f"ARMED {remaining:.0f}s - refocus Roblox, then press F1", fg=GOLD
            )

        self.events.configure(state="normal")
        self.events.delete("1.0", "end")
        self.events.insert("1.0", "\n".join(self.app.coordinator.events.as_lines(10)))
        self.events.configure(state="disabled")

        warnings = "\n".join(snapshot.warnings)
        self.warnings_var.set(warnings)
        self.warnings_label.configure(foreground=BAD if warnings else MUTED)

    def _render_decision(self) -> None:
        observation = self._last_observation
        if observation is None:
            self.decision_var.set("no observation yet - start Shadow")
            return
        direction = observation.direction
        arrow = observation.arrow
        lines = [
            f"frame #{observation.frame_sequence}   age {observation.age_s * 1000:.0f} ms",
            f"viewport  {observation.geometry.state.value}",
            f"profile   {observation.profile_id} [{observation.profile_status}]",
            f"cue       {observation.strategy_id}",
        ]
        if arrow.valid:
            lines.append(f"arrow     confidence {arrow.confidence:.2f}, track {arrow.track_id}")
        else:
            lines.append(f"arrow     ABSTAIN - {arrow.abstain_reason}")
        if direction.valid and direction.error_deg is not None:
            lines.append(
                f"turn      {direction.error_deg:+.1f} deg  "
                f"(confidence {direction.confidence:.2f})"
            )
        else:
            lines.append(f"direction ABSTAIN - {direction.abstain_reason}")
        lines.append(f"forward   {observation.forward_source}")
        if observation.phase is not None:
            lines.append(f"phase     {observation.phase.name}")
        if observation.command is not None:
            lines.append(f"command   {observation.command.reason}")
        elif observation.abstain_reason:
            lines.append(f"no command: {observation.abstain_reason}")
        lines.append(
            f"timing    capture {observation.capture_ms:.1f}  "
            f"perception {observation.perception_ms:.1f}  "
            f"decision {observation.decision_ms:.1f} ms"
        )
        result = self.app.coordinator.last_result
        if result is not None:
            lines.append(f"last      {result.kind.name}: {result.detail}")
        self.decision_var.set("\n".join(lines))

    def _render_metrics(self, metrics: CaptureMetrics) -> None:
        colour = OK if metrics.healthy else (WARN if metrics.unique_fps > 0 else BAD)
        self.metrics_var.set(
            f"{metrics.backend}  tier {metrics.tier.fps} Hz\n"
            f"unique {metrics.unique_fps:5.1f}/s   processed {metrics.processed_fps:5.1f}/s   "
            f"preview {metrics.preview_fps:5.1f}/s\n"
            f"age {0.0 if metrics.frame_age_ms is None else metrics.frame_age_ms:5.1f} ms   "
            f"dup {metrics.duplicate_frames}   drop {metrics.dropped_frames}   "
            f"stale {metrics.stale_frames}   reacq {metrics.reacquisitions}\n"
            f"capture {metrics.capture.p50_ms:4.1f}/{metrics.capture.p95_ms:4.1f}   "
            f"perception {metrics.perception.p50_ms:4.1f}/{metrics.perception.p95_ms:4.1f}   "
            f"end-to-end {metrics.end_to_end.p50_ms:4.1f}/{metrics.end_to_end.p95_ms:4.1f} ms "
            f"(p50/p95)\n"
            f"cpu {metrics.cpu_percent:3.0f}%   rss {metrics.rss_mb:4.0f} MB"
            + (f"\nDEGRADED: {metrics.degraded_reason}" if metrics.degraded_reason else "")
        )
        self.metrics_label.configure(foreground=colour)

    def _drain_reports(self) -> None:
        while True:
            try:
                message = self.app.reports.get_nowait()
            except queue.Empty:
                return
            self.app.coordinator.events.add("pixel-probe", message)

    def on_close(self) -> None:
        if self.recorder is not None:
            self.recorder.stop()
        self.app.shutdown()
        self.root.destroy()


def main() -> int:
    app = build_application()
    try:
        app.deadman.start()
    except Exception as exc:
        print(f"[deadman] unavailable: {exc!r} - Live will refuse to start.", file=sys.stderr)
    app.capture.start()
    app.coordinator.start()

    def submit_from_hotkey(intent: Any) -> None:
        app.coordinator.submit(intent)

    hotkeys = app.port.create_hotkey_source(submit_from_hotkey)
    try:
        hotkeys.start()
    except Exception as exc:
        print(f"[hotkeys] unavailable: {exc!r}", file=sys.stderr)

    root = tk.Tk()
    dashboard = Dashboard(root, app)
    root.protocol("WM_DELETE_WINDOW", dashboard.on_close)
    try:
        root.mainloop()
    finally:
        hotkeys.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
