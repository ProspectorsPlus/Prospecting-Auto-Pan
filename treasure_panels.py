#!/usr/bin/env python3
"""The Current Analysis panel and the diagnostics drawer.

Split from the dashboard because they answer different questions and are read
by different people at different times. The analysis panel answers "what is it
doing right now" in a sentence; the drawer answers "why", in as much detail as
anyone could want. Neither may paraphrase the other into a different claim, so
both render from the *same* packet.

The rule that shapes every panel here: **progressive disclosure, never
deletion.** Every engineering number the previous dashboard showed is still
present. Frame ids, candidate scores, cue angles, track ids, revisions and
timings simply moved from the front page into Frame Details and the drawer,
because a number nobody can find is not more useful than one nobody can read.

Colour semantics, applied consistently:

* **green** - verified and currently healthy. Not "did not crash".
* **amber** - working but degraded, or waiting on something.
* **red** - a fault happening *now*. Never a historical stop, because a session
  that ended normally rendered in red taught people to ignore red.
"""

from __future__ import annotations

import contextlib
import sys
import tkinter as tk
from collections.abc import Callable
from tkinter import ttk
from typing import Any, ClassVar

from prospector_engine.contracts import (
    CaptureMetrics,
    DiagnosticObservation,
    PacketKind,
    TelemetrySnapshot,
)
from treasure_overlay import (
    BAD,
    BG,
    BONE,
    INFO,
    JADE,
    MUTED,
    OK,
    SURFACE_ALT,
    WARN,
)

__all__ = ["AnalysisPanel", "DiagnosticsDrawer", "Disclosure", "Tooltip", "mono_font"]


def mono_font() -> tuple[str, int]:
    return ("Menlo" if sys.platform == "darwin" else "Consolas", 10)


class Tooltip:
    """A plain hover tooltip.

    Every control whose consequences are not obvious from its label carries
    one, because the previous labels ("Record: off", "Pin Window") described
    the mechanism rather than the effect, and the effect is what a person needs
    before clicking something that might send input to a game.
    """

    #: Every tooltip ever attached, by widget, so live text can replace the
    #: text of an existing one instead of stacking a second binding on the
    #: same widget - which would show two tooltips and leak one per refresh.
    _BY_WIDGET: ClassVar[dict[tk.Widget, Tooltip]] = {}

    @classmethod
    def retarget(cls, widget: tk.Widget, text: str) -> None:
        """Update a widget's tooltip text, attaching one if it has none."""
        existing = cls._BY_WIDGET.get(widget)
        if existing is None:
            cls(widget, text)
            return
        existing.text = text

    def __init__(self, widget: tk.Widget, text: str, *, delay_ms: int = 350) -> None:
        self.widget = widget
        self.text = text
        Tooltip._BY_WIDGET[widget] = self
        self._delay_ms = delay_ms
        self._after: str | None = None
        self._window: tk.Toplevel | None = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self.hide, add="+")
        widget.bind("<ButtonPress>", self.hide, add="+")

    def _schedule(self, _event: Any = None) -> None:
        self.hide()
        self._after = self.widget.after(self._delay_ms, self.show)

    def show(self) -> None:
        if self._window is not None:
            return
        try:
            x = self.widget.winfo_rootx() + 12
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        except tk.TclError:  # pragma: no cover - widget destroyed mid-hover
            return
        window = tk.Toplevel(self.widget)
        window.wm_overrideredirect(True)
        window.wm_geometry(f"+{x}+{y}")
        tk.Label(
            window,
            text=self.text,
            bg=SURFACE_ALT,
            fg=BONE,
            wraplength=340,
            justify="left",
            padx=8,
            pady=6,
            relief="flat",
        ).pack()
        self._window = window

    def hide(self, _event: Any = None) -> None:
        if self._after is not None:
            self.widget.after_cancel(self._after)
            self._after = None
        if self._window is not None:
            self._window.destroy()
            self._window = None


class Disclosure:
    """A labelled section that folds away without discarding anything.

    Collapsed by default where the content is engineering detail. The label
    always says what is inside, so nothing is hidden - it is filed.
    """

    def __init__(self, parent: tk.Misc, title: str, *, expanded: bool = False) -> None:
        self.frame = ttk.Frame(parent, style="Card.TFrame")
        self.frame.columnconfigure(0, weight=1)
        self._expanded = expanded
        self._title = title
        self.button = ttk.Button(
            self.frame, text=self._label(), style="Link.TButton", command=self.toggle
        )
        self.button.grid(row=0, column=0, sticky="w")
        self.body = ttk.Frame(self.frame, style="Card.TFrame")
        self.body.columnconfigure(0, weight=1)
        if expanded:
            self.body.grid(row=1, column=0, sticky="nsew", pady=(4, 0))

    def _label(self) -> str:
        return ("v  " if self._expanded else ">  ") + self._title

    @property
    def expanded(self) -> bool:
        return self._expanded

    def toggle(self) -> None:
        self._expanded = not self._expanded
        self.button.configure(text=self._label())
        if self._expanded:
            self.body.grid(row=1, column=0, sticky="nsew", pady=(4, 0))
        else:
            self.body.grid_remove()

    def grid(self, **options: Any) -> None:
        self.frame.grid(**options)


class AnalysisPanel:
    """What the navigator is doing, in a sentence, then in numbers.

    Order matters and is deliberate: the plain-language line first, then the
    four values a person acts on, then everything else behind a disclosure.
    Reading "turn right 60 degrees" should not require parsing a frame id.
    """

    def __init__(self, parent: tk.Misc, fonts: dict[str, Any]) -> None:
        self.frame = ttk.Frame(parent, style="Card.TFrame", padding=10)
        self.frame.columnconfigure(0, weight=1)
        self.frame.rowconfigure(3, weight=1)

        ttk.Label(self.frame, text="CURRENT ANALYSIS", style="Muted.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        self.headline_var = tk.StringVar(value="Not running - press Start Shadow Analysis")
        self.headline = ttk.Label(
            self.frame,
            textvariable=self.headline_var,
            style="Card.TLabel",
            font=fonts["headline"],
            wraplength=380,
            justify="left",
        )
        self.headline.grid(row=1, column=0, sticky="w", pady=(4, 8))

        values = ttk.Frame(self.frame, style="Card.TFrame")
        values.grid(row=2, column=0, sticky="ew")
        values.columnconfigure(1, weight=1)
        self.value_vars: dict[str, tk.StringVar] = {}
        rows = [
            ("arrow", "Arrow confidence"),
            ("forward", "Forward confidence"),
            ("profile", "Profile"),
            ("phase", "Phase"),
            ("output", "Control output"),
        ]
        for index, (key, label) in enumerate(rows):
            ttk.Label(values, text=label, style="Muted.TLabel").grid(
                row=index, column=0, sticky="w", padx=(0, 10)
            )
            variable = tk.StringVar(value="-")
            self.value_vars[key] = variable
            ttk.Label(
                values, textvariable=variable, style="Card.TLabel", font=fonts["body"]
            ).grid(row=index, column=1, sticky="w")

        self.details = Disclosure(self.frame, "Frame Details")
        self.details.grid(row=3, column=0, sticky="nsew", pady=(10, 0))
        self.details_var = tk.StringVar(value="no frame yet")
        ttk.Label(
            self.details.body,
            textvariable=self.details_var,
            style="Card.TLabel",
            font=fonts["mono"],
            wraplength=380,
            justify="left",
        ).grid(row=0, column=0, sticky="w")

    def render(self, observation: DiagnosticObservation | None) -> None:
        if observation is None:
            self.headline_var.set("Not running - press Start Shadow Analysis")
            self.headline.configure(foreground=MUTED)
            for variable in self.value_vars.values():
                variable.set("-")
            self.details_var.set("no frame yet")
            return

        frozen = observation.packet_kind is not PacketKind.FRAME
        headline = observation.plain_summary or "No reading"
        if frozen:
            headline = f"{headline}  (frozen frame, {observation.age_s:.1f}s old)"
        self.headline_var.set(headline)
        self.headline.configure(foreground=MUTED if frozen else _headline_colour(headline))

        arrow, direction = observation.arrow, observation.direction
        self.value_vars["arrow"].set(
            f"{arrow.confidence:.2f}   track {arrow.track_id}   age {arrow.track_age}"
            if arrow.valid
            else f"abstained - {arrow.abstain_reason}"
        )
        self.value_vars["forward"].set(
            f"{direction.confidence:.2f}   sign margin {direction.sign_margin_deg:.0f} deg"
            if direction.valid
            else f"abstained - {direction.abstain_reason}"
        )
        self.value_vars["profile"].set(
            f"{observation.profile_id} [{observation.profile_status}]"
        )
        self.value_vars["phase"].set(
            f"{observation.phase.name if observation.phase else 'idle'}"
            + (
                f"   control {observation.control_state.value}"
                if observation.control_state
                else ""
            )
        )
        command = observation.command
        self.value_vars["output"].set(
            "none - analysis only"
            if command is None
            else f"{command.kind.value}: forward {command.forward_axis}, "
            f"yaw {command.yaw_delta_px:+d} px"
        )
        self.details_var.set(_frame_details(observation))


def _headline_colour(headline: str) -> str:
    lowered = headline.lower()
    if lowered.startswith("aligned"):
        return OK
    if "uncertain" in lowered or "no arrow" in lowered or "rejected" in lowered:
        return WARN
    if "blocked" in lowered:
        return WARN
    return BONE


def _frame_details(observation: DiagnosticObservation) -> str:
    key = observation.key
    lines = [
        f"frame      #{key.frame_sequence}  content {key.content_id}  "
        f"age {observation.age_s * 1000:.0f} ms",
        f"identity   run {key.run_id}  gen {key.coordinator_generation}  "
        f"session {key.mode_session_id}",
        f"revisions  source {key.source_epoch}  geometry {key.geometry_revision}  "
        f"profile {key.profile_revision}",
        f"viewport   {observation.geometry.state.value}",
        f"forward    {observation.forward_source}",
        f"timing     capture {observation.capture_ms:.1f}  "
        f"perception {observation.perception_ms:.1f}  "
        f"decision {observation.decision_ms:.1f} ms",
    ]
    if observation.arrow.score_terms:
        terms = "  ".join(f"{n} {v:.2f}" for n, v in observation.arrow.score_terms)
        lines.append(f"score      {terms}")
        lines.append(f"margin     {observation.arrow.score_margin:.3f}")
    if observation.cues:
        for cue in observation.cues:
            heading = "-" if cue.heading_deg is None else f"{cue.heading_deg:+7.1f}"
            note = f"  {cue.note}" if cue.note else ""
            lines.append(f"cue        {cue.cue_id:16s} {heading}  w {cue.weight:.2f}{note}")
    if observation.candidates:
        rejected = [c for c in observation.candidates if not c.accepted]
        lines.append(
            f"candidates {len(observation.candidates)} considered, {len(rejected)} rejected"
        )
        for candidate in rejected[:4]:
            lines.append(
                f"  rejected {candidate.bbox_px} score {candidate.score:.2f} "
                f"- {candidate.rejected_reason}"
            )
    if observation.blockers:
        lines.append(f"live       blocked by {len(observation.blockers)} checks")
    return "\n".join(lines)


class DiagnosticsDrawer:
    """Five tabs holding every engineering value the dashboard ever showed.

    Nothing was removed to make the front page readable; it was moved here.
    """

    TABS = ("Perception", "Performance", "Safety", "Capture", "Raw Log")

    def __init__(self, parent: tk.Misc, fonts: dict[str, Any]) -> None:
        self.frame = ttk.Frame(parent, style="Card.TFrame", padding=8)
        self.frame.columnconfigure(0, weight=1)
        self.frame.rowconfigure(0, weight=1)
        self.notebook = ttk.Notebook(self.frame, style="T.TNotebook")
        self.notebook.grid(row=0, column=0, sticky="nsew")
        self.texts: dict[str, tk.Text] = {}
        for name in self.TABS:
            page = ttk.Frame(self.notebook, style="Card.TFrame")
            page.columnconfigure(0, weight=1)
            page.rowconfigure(0, weight=1)
            widget = tk.Text(
                page,
                height=7,
                bg=SURFACE_ALT,
                fg=BONE,
                insertbackground=BONE,
                font=fonts["mono"],
                relief="flat",
                wrap="none",
            )
            widget.grid(row=0, column=0, sticky="nsew")
            widget.configure(state="disabled")
            self.notebook.add(page, text=name)
            self.texts[name] = widget

    def _set(self, tab: str, text: str) -> None:
        widget = self.texts[tab]
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", text)
        widget.configure(state="disabled")

    def render(
        self,
        snapshot: TelemetrySnapshot | None,
        observation: DiagnosticObservation | None,
        metrics: CaptureMetrics,
        events: Any,
        extra: dict[str, str],
    ) -> None:
        self._set("Perception", _perception_text(observation))
        self._set("Performance", _performance_text(metrics))
        self._set("Safety", _safety_text(snapshot, extra))
        self._set("Capture", _capture_text(metrics, snapshot))
        self._set(
            "Raw Log",
            "\n".join(
                f"{stamp:12.3f}  {name}: {detail}" if detail else f"{stamp:12.3f}  {name}"
                for stamp, name, detail in events.verbatim(200)
            ),
        )


def _perception_text(observation: DiagnosticObservation | None) -> str:
    if observation is None:
        return "No observation yet. Start Shadow Analysis to run perception."
    lines = [_frame_details(observation), ""]
    arrow = observation.arrow
    if arrow.valid:
        lines.append(
            f"accepted   bbox {arrow.bbox_px}  centroid "
            f"({arrow.centroid_px[0]:.1f}, {arrow.centroid_px[1]:.1f})"
            if arrow.centroid_px
            else "accepted"
        )
        lines.append(
            f"           tip {arrow.tip_px}  tail {arrow.tail_px}  notches {arrow.notch_px}"
        )
        lines.append(f"           scale_norm {arrow.scale_norm:.4f}")
    lines.append(
        f"direction  {observation.direction.cue_id}  "
        f"spread {observation.direction.cue_disagreement_deg}  "
        f"anisotropy {observation.direction.anisotropy:.2f}"
    )
    if observation.arrival is not None:
        lines.append(
            f"arrival    hits {observation.arrival.support_hits}/"
            f"{observation.arrival.support_window}  valid {observation.arrival.valid}"
        )
    if observation.motion is not None:
        lines.append(
            f"motion     forward {observation.motion.forward_speed_norm}  "
            f"confidence {observation.motion.confidence:.2f}  "
            f"yaw contamination {observation.motion.yaw_contamination:.2f}"
        )
    return "\n".join(lines)


def _performance_text(metrics: CaptureMetrics) -> str:
    governor = metrics.governor
    return "\n".join(
        [
            f"governor   {governor.describe()}",
            f"ceiling    {metrics.governor.tier.fps} Hz current",
            f"           changes {governor.changes}  probes {governor.probes}  "
            f"failed probes {governor.failed_probes}  "
            f"live eligible {governor.live_eligible}",
            "",
            f"requested  {metrics.requested_hz} Hz",
            f"source     {metrics.source_fps:6.1f}/s   (deliveries, duplicates included)",
            f"unique     {metrics.unique_fps:6.1f}/s",
            f"processed  {metrics.processed_fps:6.1f}/s   (perception)",
            f"control    {metrics.control_fps:6.1f}/s   (decisions)",
            f"preview    {metrics.preview_fps:6.1f}/s   (dashboard only, never gates Live)",
            "",
            _latency_line("capture", metrics.capture),
            _latency_line("normalize", metrics.normalize),
            _latency_line("perception", metrics.perception),
            _latency_line("decision", metrics.decision),
            _latency_line("preview", metrics.preview),
            _latency_line("end-to-end", metrics.end_to_end),
            "",
            f"cpu        {metrics.cpu_percent:.0f}%",
            f"rss        {metrics.rss_current_mb:.0f} MB current, "
            f"{metrics.rss_peak_mb:.0f} MB peak (lifetime)",
        ]
    )


def _latency_line(label: str, summary: Any) -> str:
    return (
        f"{label:10s} p50 {summary.p50_ms:6.2f}  p95 {summary.p95_ms:6.2f}  "
        f"p99 {summary.p99_ms:6.2f}  max {summary.max_ms:6.2f} ms   n={summary.samples}"
    )


def _safety_text(snapshot: TelemetrySnapshot | None, extra: dict[str, str]) -> str:
    if snapshot is None:
        return "No telemetry yet."
    lines = [f"{key:12s} {value}" for key, value in sorted(snapshot.readiness.items())]
    lines.append("")
    lines.extend(f"{key:12s} {value}" for key, value in sorted(extra.items()))
    if snapshot.live_blockers:
        lines.append("")
        lines.append("Live blockers:")
        lines.extend(f"  - {blocker}" for blocker in snapshot.live_blockers)
    if snapshot.warnings:
        lines.append("")
        lines.extend(f"WARNING  {warning}" for warning in snapshot.warnings)
    if snapshot.last_session_note:
        lines.append("")
        lines.append(snapshot.last_session_note)
    return "\n".join(lines)


def _capture_text(metrics: CaptureMetrics, snapshot: TelemetrySnapshot | None) -> str:
    lines = [
        f"backend    {metrics.backend}",
        f"epoch      {metrics.epoch}   (source replacements this run)",
        f"tier       {metrics.tier.fps} Hz   slot depth {metrics.slot_depth}",
        f"frame age  {0.0 if metrics.frame_age_ms is None else metrics.frame_age_ms:.1f} ms",
        "",
        "Session counters (reset on start, source replacement and reacquisition):",
        f"  duplicates   {metrics.duplicate_frames.describe()}"
        f"   lifetime {metrics.duplicate_frames.lifetime_total}",
        f"  superseded   {metrics.superseded_frames.describe()}"
        f"   lifetime {metrics.superseded_frames.lifetime_total}",
        f"  unobserved   {metrics.dropped_observations.describe()}"
        f"   lifetime {metrics.dropped_observations.lifetime_total}",
        f"  stale        {metrics.stale_frames.describe()}"
        f"   lifetime {metrics.stale_frames.lifetime_total}",
        f"  pool exhaust {metrics.pool_exhausted.describe()}"
        f"   lifetime {metrics.pool_exhausted.lifetime_total}",
        f"  reacquired   {metrics.reacquisitions}",
        "",
        "A superseded frame is one the pipeline replaced before a consumer took",
        "it. That is the design: the latest frame wins and a stale decision is",
        "worse than a skipped one. It is not a capture failure.",
    ]
    if snapshot is not None and snapshot.fit is not None:
        lines.extend(["", f"viewport fit  {snapshot.fit.describe()}"])
    if snapshot is not None and snapshot.viewport is not None:
        lines.append(f"viewport      {snapshot.viewport.describe()}")
    return "\n".join(lines)


#: Status words the summary cards colour. Anything unlisted renders bone.
SUMMARY_COLOURS: dict[str, str] = {
    "connected": OK,
    "verified": OK,
    "ready": OK,
    "running": OK,
    "healthy": OK,
    "ok": OK,
    "armed": WARN,
    "custom": WARN,
    "degraded": WARN,
    "waiting": WARN,
    "blocked": WARN,
    #: Amber, not red. Not being connected yet is a state to act on, not a
    #: fault; red is reserved for something going wrong *now*, so that red
    #: keeps meaning something.
    "disconnected": WARN,
    "idle": MUTED,
    "fault": BAD,
    "unavailable": BAD,
    "release uncertain": BAD,
    "analysis only": INFO,
}


def summary_colour(text: str) -> str:
    lowered = text.lower()
    for key, colour in SUMMARY_COLOURS.items():
        if lowered.startswith(key):
            return colour
    return BONE


def card(parent: tk.Misc) -> ttk.Frame:
    return ttk.Frame(parent, style="Card.TFrame", padding=(10, 8))


# ---------------------------------------------------------------------------
# Viewport progress
# ---------------------------------------------------------------------------


def fit_progress_text(fit: Any, active: bool) -> str:
    """One line for the viewport card: what the fit is doing, or what it got."""
    from prospector_engine.contracts import FitPhase

    if fit is None or fit.phase is FitPhase.IDLE:
        return "window not sized yet"
    if fit.phase is FitPhase.REQUESTED:
        return "Requesting size..." if active else "requested"
    if fit.phase is FitPhase.SETTLING:
        stage = "Waiting for OS..." if fit.stable_readbacks == 0 else "Reading back..."
        return f"{stage} ({fit.stable_readbacks}/{fit.required_readbacks} stable)"
    if fit.phase is FitPhase.CANONICAL_VERIFIED:
        got = fit.achieved_client_logical
        return f"Sized to {got[0]:g}x{got[1]:g}" if got else "Sized to 1280x720"
    if fit.phase is FitPhase.ACHIEVED_CLAMPED:
        got = fit.achieved_client_logical
        size = f"{got[0]:g}x{got[1]:g}" if got else "an achieved size"
        return f"The window clamped to {size} - usable, not the canonical size"
    detail = fit.detail.lower()
    if "accessibility" in detail:
        return "Accessibility permission denied - enable it for this terminal or app"
    if "fullscreen" in detail or "space" in detail:
        return "Fullscreen/Space limitation - exit fullscreen and retry"
    if "not found" in detail or "not settable" in detail or "minimized" in detail:
        return "Wrong/unsupported window state - open Roblox windowed and retry"
    return f"Fit failed: {fit.detail}"


# ---------------------------------------------------------------------------
# Stable-geometry building blocks
# ---------------------------------------------------------------------------


class Ticker:
    """One cancellable ``after`` handle per polling loop.

    The dashboard used to schedule its next tick at the *end* of the render
    function, which meant calling that function directly - to refresh a window
    on demand, say - started a second loop that ran forever alongside the
    first. Clicking a button four times gave four loops and four times the CPU.

    Here scheduling and rendering are different methods. :meth:`start` is
    idempotent, :meth:`render_once` never schedules, and :meth:`stop` cancels
    the outstanding handle so a closed window leaves nothing behind.
    """

    def __init__(
        self, widget: tk.Misc, interval_ms: int, render: Callable[[], None], *, name: str = ""
    ) -> None:
        self._widget = widget
        self._interval_ms = interval_ms
        self._render = render
        self._handle: str | None = None
        self._running = False
        self.name = name or getattr(render, "__name__", "ticker")
        self.ticks = 0

    @property
    def running(self) -> bool:
        return self._running

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._schedule()

    def stop(self) -> None:
        self._running = False
        handle, self._handle = self._handle, None
        if handle is not None:
            with contextlib.suppress(tk.TclError):
                self._widget.after_cancel(handle)

    def render_once(self) -> None:
        """Render without touching the schedule. Safe to call from anywhere."""
        self.ticks += 1
        self._render()

    def _schedule(self) -> None:
        if not self._running:
            return
        with contextlib.suppress(tk.TclError):
            self._handle = self._widget.after(self._interval_ms, self._tick)

    def _tick(self) -> None:
        self._handle = None
        if not self._running:
            return
        try:
            self.render_once()
        finally:
            self._schedule()


def fixed_label(
    parent: tk.Misc,
    variable: tk.StringVar,
    *,
    width: int,
    style: str = "Card.TLabel",
    font: Any = None,
    anchor: str = "w",
) -> ttk.Label:
    """A label whose *requested* width does not depend on its text.

    This is the whole of the root-resizing bug. A ttk.Label sizes itself to its
    content, a grid sizes itself to its children, and a toplevel sizes itself to
    its grid - so a status string growing from "Ready" to a sentence about
    Accessibility permissions pushed the window wider, every time it changed.
    Pinning the width in characters and clipping breaks that chain at the leaf.
    """
    options: dict[str, Any] = {"textvariable": variable, "style": style, "anchor": anchor}
    if font is not None:
        options["font"] = font
    label = ttk.Label(parent, width=width, **options)
    return label


class MessageBox:
    """A fixed-height area for one actionable sentence, however long it is.

    Long text wraps and, past the reserved height, is clipped rather than
    growing the layout. A message the user cannot fully read is a bug worth
    fixing in the message; a window that resizes itself is a bug in the window.
    """

    def __init__(
        self,
        parent: tk.Misc,
        fonts: dict[str, Any],
        *,
        height_px: int = 46,
        width_px: int = 700,
    ) -> None:
        self.frame = tk.Frame(parent, bg=SURFACE_ALT, height=height_px, width=width_px)
        self.frame.grid_propagate(False)
        self.frame.pack_propagate(False)
        self._var = tk.StringVar(value="")
        self.label = tk.Label(
            self.frame,
            textvariable=self._var,
            bg=SURFACE_ALT,
            fg=MUTED,
            font=fonts["small"],
            justify="left",
            anchor="w",
            wraplength=max(120, width_px - 24),
            padx=10,
            pady=6,
        )
        self.label.place(x=0, y=0, relwidth=1.0, relheight=1.0)

    def set(self, text: str, colour: str = MUTED) -> None:
        if self._var.get() == text and self.label.cget("fg") == colour:
            return
        self._var.set(text)
        self.label.configure(fg=colour)

    @property
    def text(self) -> str:
        return self._var.get()

    def resize(self, width_px: int) -> None:
        self.frame.configure(width=width_px)
        self.label.configure(wraplength=max(120, width_px - 24))


# ---------------------------------------------------------------------------
# Automatic setup
# ---------------------------------------------------------------------------

#: Every stage the user is shown, with the short label that names it. The
#: two input-emitting stages are listed too, so the sequence a user sees is the
#: sequence that actually runs rather than the read-only half of it.
SETUP_STEPS: tuple[tuple[str, str], ...] = (
    ("find_roblox", "Find Roblox"),
    ("fit_viewport", "Size window"),
    ("restart_capture", "Rebind capture"),
    ("stabilize_capture", "Check frames"),
    ("select_profile", "Identify map"),
    ("establish_reference", "Check direction"),
    ("shadow_qualify", "Qualify"),
    ("verify_input", "Test input"),
    ("verify_control_mode", "Camera mode"),
    ("characterize_turn", "Measure turning"),
)


class SetupPanel:
    """What automatic setup is doing, as a stage strip and one sentence.

    Ten fixed cells, laid out once. A stage changing colour cannot change the
    panel's requested size, which is why the strip is boxes rather than a text
    widget that is rewritten - the old commissioning window rewrote a Text on a
    timer and that is what made the layout breathe.
    """

    def __init__(self, parent: tk.Misc, fonts: dict[str, Any]) -> None:
        self.frame = ttk.Frame(parent, style="Card.TFrame", padding=(10, 8))
        self.frame.columnconfigure(0, weight=1)
        header = ttk.Frame(self.frame, style="Card.TFrame")
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(1, weight=1)
        ttk.Label(header, text="AUTOMATIC SETUP", style="Muted.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        self.stage_var = tk.StringVar(value="not started")
        fixed_label(header, self.stage_var, width=34, style="Card.TLabel").grid(
            row=0, column=2, sticky="e"
        )

        strip = ttk.Frame(self.frame, style="Card.TFrame")
        strip.grid(row=1, column=0, sticky="ew", pady=(6, 4))
        self.cells: dict[str, tk.Label] = {}
        for index, (key, label) in enumerate(SETUP_STEPS):
            strip.columnconfigure(index, weight=1, uniform="setup")
            cell = tk.Label(
                strip,
                text=label,
                bg=SURFACE_ALT,
                fg=MUTED,
                font=fonts["small"],
                padx=6,
                pady=5,
                width=13,
                anchor="center",
            )
            cell.grid(row=0, column=index, sticky="ew", padx=2)
            self.cells[key] = cell
        self._last_key: tuple[str, str] | None = None

    def render(self, progress: Any) -> None:
        """Colour the strip from one progress packet. Emit-on-change."""
        stage = getattr(progress, "stage", None)
        stage_value = getattr(stage, "value", "idle")
        detail = getattr(progress, "detail", "")
        key = (stage_value, detail)
        if key == self._last_key:
            return
        self._last_key = key
        order = [name for name, _label in SETUP_STEPS]
        current = order.index(stage_value) if stage_value in order else -1
        failed = stage_value == "failed"
        done = stage_value == "ready"
        for index, (name, _label) in enumerate(SETUP_STEPS):
            if done:
                colour, foreground = SURFACE_ALT, OK
            elif current < 0:
                colour, foreground = SURFACE_ALT, MUTED
            elif index < current:
                colour, foreground = SURFACE_ALT, OK
            elif index == current:
                colour, foreground = JADE, BG
            else:
                colour, foreground = SURFACE_ALT, MUTED
            self.cells[name].configure(bg=colour, fg=foreground)
        if failed:
            failure = getattr(progress, "failure", None)
            failed_stage = getattr(getattr(failure, "stage", None), "value", "")
            if failed_stage in self.cells:
                self.cells[failed_stage].configure(bg=BAD, fg=BONE)
        self.stage_var.set(
            "ready" if done else (stage_value.replace("_", " ") if stage_value else "idle")
        )
