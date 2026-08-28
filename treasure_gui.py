#!/usr/bin/env python3
"""Treasure Navigator dashboard: the Tk shell around the coordinator.

The UI submits :class:`RuntimeIntent` objects and renders
:class:`TelemetrySnapshot` and :class:`DiagnosticObservation` packets. It owns
no run state, calls no input, and captures nothing of its own.

Three rules shape the whole layout.

**Clicking Tk removes focus from Roblox.** So there is no actionable Start
Live, Reset, or Pan Test button. Those are guidance ("focus Roblox, then press
F1"), and the real hotkeys submit their intents only while Roblox is positively
focused (plan 11.2).

**Stop is always reachable.** *Stop & Release (F2)* is pinned in the header at
a fixed size, outside every resizable region, so no window size, UI scale or
layout state can push it off screen.

**Every control says what it does to the game.** The old labels described
mechanisms - "Pin Window", "Record: off", "Arm Live" - and left the
consequences to be guessed. Connecting and resizing are now separate
operations, because capture must not depend on a resize succeeding, and each
control carries a tooltip stating whether it can move the window or send input.

Design note: the dark surface, bone text, jade interactive and gold emphasis
semantics are implemented here independently through ``ttk.Style``. No CSS,
markup, or text was copied from any other project (plan 12).
"""

from __future__ import annotations

import contextlib
import queue
import sys
import tkinter as tk
from dataclasses import dataclass
from tkinter import font as tkfont
from tkinter import ttk
from typing import Any

from prospector_engine import __version__
from prospector_engine.capture import CaptureService, EvidenceRegistry, ViewportGuard
from prospector_engine.contracts import (
    CadenceMode,
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
from prospector_engine.input_authority import (
    AuthorityConfig,
    DeadmanClient,
    HealthSources,
    InputAuthority,
)
from prospector_engine.navigation import (
    NavigationGates,
    PerceptionPipeline,
    commissioning_blockers,
    commissioning_steps,
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
from prospector_engine.trace import PreviewTrace
from prospector_engine.vision import ArrowSegmenter, ProfileAuthority, load_profiles
from treasure_overlay import (
    BAD,
    BG,
    BONE,
    GOLD,
    JADE,
    MUTED,
    SURFACE,
    SURFACE_ALT,
    WARN,
    DiagnosticCanvas,
    OverlayMode,
)
from treasure_panels import (
    AnalysisPanel,
    CommissioningWindow,
    DiagnosticsDrawer,
    Tooltip,
    fit_progress_text,
    mono_font,
    summary_colour,
)

#: The header badge, in the vocabulary the mission specifies. Each says what is
#: happening to the game, not what the code is doing.
MODE_BADGES: dict[RunMode, tuple[str, str]] = {
    RunMode.IDLE: ("OFF", MUTED),
    RunMode.SHADOW: ("SHADOW - analysis only", "#4a9bd1"),
    RunMode.LIVE: ("LIVE", GOLD),
    RunMode.SERVICE: ("SERVICE - bounded task", JADE),
    RunMode.SAFE_STOP: ("STOPPING - releasing input", WARN),
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


# ---------------------------------------------------------------------------
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
    profiles: ProfileAuthority

    @property
    def library(self) -> Any:
        return self.profiles.library

    def shutdown(self) -> dict[str, str]:
        report = self.coordinator.shutdown()
        self.deadman.close()
        return report


def build_application(profile_id: str = "green_arrow_v1") -> Application:
    paths = resolve_app_paths().ensure()
    port = create_platform_port()
    deadman = DeadmanClient(config=AuthorityConfig())
    reports: queue.Queue[str] = queue.Queue(maxsize=32)
    preview: LatestSlot[Any] = LatestSlot()

    registry = EvidenceRegistry("pending")
    guard = ViewportGuard(port)
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
    profiles = ProfileAuthority(library, profile_id)
    gates = NavigationGates(os_name=current_platform_name(), profile_id=profiles.active_id)
    pipeline = PerceptionPipeline(segmenter=ArrowSegmenter(profiles.active), profiles=profiles)

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
        profiles=profiles,
    )
    # The evidence gates and the yaw calibration are both reasons Live cannot
    # start, and from the user's side they are the same question.
    # One keyed blocker per pending gate. No default controller is built just
    # to ask it why it cannot steer: its calibration is the E-YAW gate.
    coordinator.set_gate_blockers(commissioning_blockers(gates))
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
        profiles=profiles,
    )


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


class Dashboard:
    """The Tk shell. Renders packets; never decides anything."""

    #: Four independent cadences. The preview is allowed to be fast because it
    #: is cheap; the status text is deliberately slow because nobody can read
    #: 60 Hz of numbers, and re-laying out text is the expensive part of Tk.
    #: None of these ever gates Live: preview cadence and control cadence are
    #: separate measurements for exactly this reason.
    PREVIEW_INTERVAL_MS = 16
    STATUS_INTERVAL_MS = 150
    METRICS_INTERVAL_MS = 500
    DRAWER_INTERVAL_MS = 700

    #: Below this the layout starts clipping. The Stop control is placed
    #: outside every resizable region so it survives regardless.
    MIN_WIDTH = 960
    MIN_HEIGHT = 640

    def __init__(self, root: tk.Tk, app: Application) -> None:
        self.root = root
        self.app = app
        self.recorder: EvidenceRecorder | None = None
        self._recording_started_s: float | None = None
        self._diagnostics: DiagnosticCanvas | None = None
        self._last_observation: DiagnosticObservation | None = None
        self._overlay_mode = OverlayMode.MINIMAL
        self._commissioning: CommissioningWindow | None = None

        root.title(f"Treasure Navigator {__version__}")
        root.configure(bg=BG)
        root.minsize(self.MIN_WIDTH, self.MIN_HEIGHT)
        root.columnconfigure(0, weight=1)
        # Row 4 is the only row that grows. Everything above it - header,
        # summaries, controls, guidance - keeps its natural height, which is
        # what stops a resize from clipping a control's label.
        root.rowconfigure(4, weight=1)

        self._style()
        self._build_header()
        self._build_summaries()
        self._build_controls()
        self._build_body()
        self._build_drawer()

        root.after(self.PREVIEW_INTERVAL_MS, self._tick_preview)
        root.after(self.STATUS_INTERVAL_MS, self._tick_status)
        root.after(self.METRICS_INTERVAL_MS, self._tick_metrics)
        root.after(self.DRAWER_INTERVAL_MS, self._tick_drawer)

    # -- styling ----------------------------------------------------------
    def _style(self) -> None:
        family = _font_family()
        self.fonts = {
            "title": (family, 19, "bold"),
            "mode": (family, 17, "bold"),
            "headline": (family, 15, "bold"),
            "body": (family, 12),
            "small": (family, 10),
            "mono": mono_font(),
        }
        self.f_title = self.fonts["title"]
        self.f_body = self.fonts["body"]
        self.f_small = self.fonts["small"]
        self.f_mono = self.fonts["mono"]

        style = ttk.Style(self.root)
        theme = "clam" if "clam" in style.theme_names() else style.theme_use()
        style.theme_use(theme)
        style.configure("T.TFrame", background=BG)
        style.configure("Card.TFrame", background=SURFACE, relief="flat")
        style.configure("T.TLabel", background=BG, foreground=BONE, font=self.f_body)
        style.configure("Card.TLabel", background=SURFACE, foreground=BONE, font=self.f_body)
        style.configure("Muted.TLabel", background=SURFACE, foreground=MUTED, font=self.f_small)
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
            "Link.TButton",
            background=SURFACE,
            foreground=MUTED,
            font=self.f_small,
            padding=(0, 2),
            borderwidth=0,
        )
        style.map(
            "Link.TButton", foreground=[("active", BONE)], background=[("active", SURFACE)]
        )
        style.configure(
            "Stop.TButton", background=BAD, foreground=BONE, font=self.f_body, padding=(14, 9)
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
        style.configure("T.TNotebook", background=BG, borderwidth=0, tabmargins=0)
        style.configure(
            "T.TNotebook.Tab",
            background=SURFACE,
            foreground=MUTED,
            padding=(14, 6),
            borderwidth=0,
        )
        style.map(
            "T.TNotebook.Tab",
            background=[("selected", SURFACE_ALT), ("active", SURFACE_ALT)],
            foreground=[("selected", BONE), ("active", BONE)],
        )
        self.root.option_add("*TCombobox*Listbox.background", SURFACE_ALT)
        self.root.option_add("*TCombobox*Listbox.foreground", BONE)
        self.root.option_add("*TCombobox*Listbox.selectBackground", JADE)
        self.root.option_add("*TCombobox*Listbox.selectForeground", BG)

    # -- header -----------------------------------------------------------
    def _build_header(self) -> None:
        header = ttk.Frame(self.root, style="T.TFrame", padding=(14, 12, 14, 4))
        header.grid(row=0, column=0, sticky="ew")
        # Column 1 absorbs every pixel of extra width, so the badge and the
        # Stop control keep their positions at any window size or UI scale.
        header.columnconfigure(1, weight=1)
        ttk.Label(header, text="Treasure Navigator", style="T.TLabel", font=self.f_title).grid(
            row=0, column=0, sticky="w"
        )
        self.mode_var = tk.StringVar(value="OFF")
        self.mode_label = ttk.Label(
            header, textvariable=self.mode_var, style="T.TLabel", font=self.fonts["mode"]
        )
        self.mode_label.grid(row=0, column=1, sticky="e", padx=(0, 14))
        self.stop_button = ttk.Button(
            header,
            text="Stop & Release All Input  (F2)",
            style="Stop.TButton",
            command=self._stop,
        )
        self.stop_button.grid(row=0, column=2, sticky="e")
        Tooltip(
            self.stop_button,
            "Stops navigation and releases every held key or mouse button. Safe at any time.",
        )

    # -- summaries --------------------------------------------------------
    def _build_summaries(self) -> None:
        strip = ttk.Frame(self.root, style="T.TFrame", padding=(10, 6))
        strip.grid(row=1, column=0, sticky="ew")
        self.summary_vars: dict[str, tk.StringVar] = {}
        self.summary_labels: dict[str, ttk.Label] = {}
        self.summary_details: dict[str, tk.StringVar] = {}
        cards = [
            ("roblox", "ROBLOX", "Whether capture is bound to the game window."),
            ("capture", "CAPTURE", "Frames arriving from the bound window."),
            ("analysis", "ANALYSIS", "Perception and decision throughput."),
            ("live", "LIVE SAFETY", "Whether keyboard and camera output may be enabled."),
        ]
        for index, (key, title, tip) in enumerate(cards):
            strip.columnconfigure(index, weight=1)
            frame = ttk.Frame(strip, style="Card.TFrame", padding=(10, 8))
            frame.grid(row=0, column=index, sticky="nsew", padx=4)
            ttk.Label(frame, text=title, style="Muted.TLabel").pack(anchor="w")
            headline = tk.StringVar(value="-")
            label = ttk.Label(frame, textvariable=headline, style="Card.TLabel")
            label.pack(anchor="w")
            detail = tk.StringVar(value="")
            ttk.Label(frame, textvariable=detail, style="Muted.TLabel", justify="left").pack(
                anchor="w"
            )
            self.summary_vars[key] = headline
            self.summary_labels[key] = label
            self.summary_details[key] = detail
            Tooltip(frame, tip)

    # -- controls ---------------------------------------------------------
    def _build_controls(self) -> None:
        row = ttk.Frame(self.root, style="T.TFrame", padding=(10, 2))
        row.grid(row=2, column=0, sticky="ew")
        for index in range(6):
            row.columnconfigure(index, weight=1)

        self.connect_button = self._control(
            row,
            0,
            "Connect Roblox",
            self._connect,
            "Binds capture to the Roblox window. Does not resize it or send input.",
        )
        self.fit_button = self._control(
            row,
            1,
            "Fit & Verify Viewport",
            self._fit,
            "Asks the OS to resize the Roblox client to 1280x720 and reads back what "
            "was achieved. A clamp is a valid answer. Not required for Shadow.",
        )
        self.shadow_button = self._control(
            row,
            2,
            "Start Shadow Analysis",
            self._shadow,
            "Runs detection and navigation decisions without sending any game input.",
        )
        self.collect_button = self._control(
            row,
            3,
            "Collect Calibration Evidence",
            self._collect_evidence,
            "Opens the guided commissioning steps 3-6 and starts a bounded diagnostic "
            "recording of real frames. Sends no input.",
        )
        self.blockers_button = self._control(
            row,
            4,
            "Calibrate Live Control",
            self._show_blockers,
            "Opens the guided commissioning window: every step, what is done, what is "
            "pending, and exactly why Live is not yet enabled. Runs nothing by itself.",
        )
        self.arm_button = self._control(
            row,
            5,
            "Enable Live Control...",
            self._arm,
            "Temporarily authorizes keyboard and camera output after all required "
            "checks pass. It does not begin movement. After arming, focus Roblox "
            "and press F1.",
        )

        second = ttk.Frame(self.root, style="T.TFrame", padding=(10, 4))
        second.grid(row=3, column=0, sticky="ew")
        second.columnconfigure(0, weight=2)
        second.columnconfigure(1, weight=1)
        second.columnconfigure(2, weight=1)
        self.live_guide = tk.Label(
            second,
            text="Live is not armed. Enable Live Control, then focus Roblox and press F1.",
            bg=SURFACE_ALT,
            fg=MUTED,
            font=self.f_small,
            padx=10,
            pady=7,
            anchor="w",
            justify="left",
        )
        self.live_guide.grid(row=0, column=0, sticky="ew", padx=4)
        self.record_button = ttk.Button(
            second,
            text="Start Diagnostic Recording",
            style="T.TButton",
            command=self._toggle_record,
        )
        self.record_button.grid(row=0, column=3, sticky="ew", padx=4)
        Tooltip(
            self.record_button,
            "Stores a bounded set of Roblox frames, observations, decisions, "
            "commands, and events for debugging. It does not enable Live or send input.",
        )
        self.return_button = ttk.Button(
            second, text="Return to Shadow", style="T.TButton", command=self._return_to_shadow
        )
        Tooltip(
            self.return_button,
            "Releases movement immediately and keeps analysis running.",
        )
        self.recover_button = ttk.Button(
            second, text="Recover release", style="T.TButton", command=self._recover
        )
        Tooltip(
            self.recover_button,
            "Emits release edges only. Required before Live can be offered again "
            "after a release that could not be confirmed safe.",
        )
        tk.Label(
            second,
            text="Focus Roblox -> F6 dig  |  F4 reset  |  F5 pan test  |  F3 pixel",
            bg=SURFACE_ALT,
            fg=MUTED,
            font=self.f_small,
            padx=10,
            pady=7,
        ).grid(row=0, column=2, sticky="ew", padx=4)

    def _control(
        self, parent: tk.Misc, column: int, text: str, command: Any, tip: str
    ) -> ttk.Button:
        button = ttk.Button(parent, text=text, style="T.TButton", command=command)
        button.grid(row=0, column=column, sticky="ew", padx=4)
        Tooltip(button, tip)
        return button

    # -- body -------------------------------------------------------------
    def _build_body(self) -> None:
        body = ttk.Frame(self.root, style="T.TFrame", padding=(10, 6))
        body.grid(row=4, column=0, sticky="nsew")
        body.columnconfigure(0, weight=3)
        body.columnconfigure(1, weight=2)
        body.rowconfigure(0, weight=1)

        left = ttk.Frame(body, style="Card.TFrame", padding=10)
        left.grid(row=0, column=0, sticky="nsew", padx=4)
        left.columnconfigure(0, weight=1)
        left.rowconfigure(1, weight=1)

        top = ttk.Frame(left, style="Card.TFrame")
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(0, weight=1)
        ttk.Label(top, text="SHADOW VIEW", style="Muted.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        self.overlay_var = tk.StringVar(value=OverlayMode.MINIMAL.value)
        overlay = ttk.Combobox(
            top,
            textvariable=self.overlay_var,
            values=[mode.value for mode in OverlayMode],
            state="readonly",
            width=18,
            style="T.TCombobox",
        )
        overlay.grid(row=0, column=2, sticky="e")
        ttk.Label(top, text="CADENCE / OVERLAY", style="Muted.TLabel").grid(
            row=1, column=1, columnspan=2, sticky="e"
        )
        overlay.bind("<<ComboboxSelected>>", self._on_overlay_selected)
        Tooltip(
            overlay,
            "Minimal draws the forward reference, the desired direction and the "
            "signed turn. Full Diagnostics adds contours, notches, rejected "
            "candidates and the score breakdown.",
        )

        self.cadence_var = tk.StringVar(value=CadenceMode.AUTO.value)
        cadence = ttk.Combobox(
            top,
            textvariable=self.cadence_var,
            values=[mode.value for mode in CadenceMode],
            state="readonly",
            width=11,
            style="T.TCombobox",
        )
        cadence.grid(row=0, column=1, sticky="e", padx=(0, 8))
        cadence.bind("<<ComboboxSelected>>", self._on_cadence_selected)
        Tooltip(
            cadence,
            "How much cadence to ask for.\n\n"
            + "\n".join(f"{mode.value}: {mode.description}" for mode in CadenceMode)
            + "\n\nThe governor still refuses to hold a tier it is not achieving, "
            "so asking for more than the machine can sustain produces an honest "
            "downshift rather than a misleading label.",
        )

        self.canvas = tk.Canvas(left, bg=SURFACE_ALT, highlightthickness=0, height=340)
        self.canvas.grid(row=1, column=0, sticky="nsew", pady=(6, 0))
        self._diagnostics = DiagnosticCanvas(self.canvas)
        self.legend_var = tk.StringVar(value="")
        ttk.Label(
            left,
            textvariable=self.legend_var,
            style="Muted.TLabel",
            wraplength=720,
            justify="left",
        ).grid(row=2, column=0, sticky="w", pady=(6, 0))
        self._update_legend()

        right = ttk.Frame(body, style="T.TFrame")
        right.grid(row=0, column=1, sticky="nsew", padx=4)
        right.columnconfigure(0, weight=1)
        right.rowconfigure(0, weight=1)
        self.analysis = AnalysisPanel(right, self.fonts)
        self.analysis.frame.grid(row=0, column=0, sticky="nsew")
        self._build_profile_selector(right)

    def _build_profile_selector(self, parent: tk.Misc) -> None:
        frame = ttk.Frame(parent, style="Card.TFrame", padding=10)
        frame.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        frame.columnconfigure(0, weight=1)
        ttk.Label(frame, text="ARROW PROFILE", style="Muted.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        authority = self.app.profiles
        self._profile_choices = authority.choices()
        self.profile_var = tk.StringVar(value=authority.label_for(authority.active_id))
        self.profile_combo = ttk.Combobox(
            frame,
            textvariable=self.profile_var,
            values=[label for _stable_id, label in self._profile_choices],
            state="readonly",
            style="T.TCombobox",
        )
        self.profile_combo.grid(row=1, column=0, sticky="ew", pady=(4, 6))
        self.profile_combo.bind("<<ComboboxSelected>>", self._on_profile_selected)
        Tooltip(
            self.profile_combo,
            "Selects which arrow the detector looks for. The change is applied at "
            "the next frame boundary and invalidates any Live arm.",
        )
        automatic = any(p.selectable_automatically for p in authority.library.all())
        ttk.Label(
            frame,
            text=(
                "Automatic classification is DISABLED: no profile has passed E-PROF. "
                "Selection stays explicit."
                if not automatic
                else "Automatic classification available for validated profiles."
            ),
            style="Muted.TLabel",
            wraplength=340,
            justify="left",
        ).grid(row=2, column=0, sticky="w")

    def _build_drawer(self) -> None:
        container = ttk.Frame(self.root, style="T.TFrame", padding=(10, 4, 10, 10))
        container.grid(row=5, column=0, sticky="ew")
        container.columnconfigure(0, weight=1)
        self.drawer = DiagnosticsDrawer(container, self.fonts)
        self.drawer.frame.grid(row=0, column=0, sticky="ew")
        # Paint once at construction so the first frame a user sees is
        # populated rather than a row of dashes.
        self._last_metrics = self.app.capture.metrics()
        self._render_summaries(None, self._last_metrics)
        self._tick_drawer_once()

    # -- intents ----------------------------------------------------------
    def _submit(self, intent_type: IntentType) -> None:
        coordinator = self.app.coordinator
        coordinator.submit(coordinator.next_intent(intent_type, "gui"))

    def _connect(self) -> None:
        self._submit(IntentType.CONNECT_WINDOW)

    def _fit(self) -> None:
        self._submit(IntentType.FIT_VIEWPORT)

    def _shadow(self) -> None:
        self._submit(IntentType.START_SHADOW)

    def _stop(self) -> None:
        self._submit(IntentType.STOP)

    def _arm(self) -> None:
        """The one physical arming gesture. Never simulated, never persisted."""
        blockers = self.app.coordinator.live_blockers()
        if blockers:
            self._show_blockers()
            return
        self._submit(IntentType.ARM_LIVE_FROM_UI)

    def _return_to_shadow(self) -> None:
        self._submit(IntentType.RETURN_TO_SHADOW)

    def _recover(self) -> None:
        self._submit(IntentType.RECOVER_RELEASE)

    def _on_cadence_selected(self, _event: Any = None) -> None:
        for mode in CadenceMode:
            if mode.value == self.cadence_var.get():
                self.app.capture.set_cadence_mode(mode)
                self.app.coordinator.events.add("cadence.mode", mode.value)
                return

    def _on_overlay_selected(self, _event: Any = None) -> None:
        for mode in OverlayMode:
            if mode.value == self.overlay_var.get():
                self._overlay_mode = mode
        self._update_legend()
        if self._diagnostics is not None:
            self._diagnostics.set_mode(self._overlay_mode)

    def _update_legend(self) -> None:
        self.legend_var.set(
            "dashed grey = assumed player-forward reference (E-FORWARD PENDING)   "
            "gold = desired map-arrow direction   orange arc = signed turn"
            if self._overlay_mode is OverlayMode.MINIMAL
            else "dashed grey = assumed forward (E-FORWARD PENDING)   gold = desired "
            "direction   orange arc = signed turn   jade = accepted contour   "
            "gold crosses = notches and tip   dull red = rejected candidates and "
            "outlier cues"
        )

    def _on_profile_selected(self, _event: Any = None) -> None:
        """Swap the profile by **stable id**, never by parsing the label.

        The selector renders labels the authority produced, so the mapping back
        is a lookup rather than a string split - which is how the dropdown came
        to disagree with the running pipeline in the first place.
        """
        chosen = self.profile_var.get()
        for stable_id, label in self._profile_choices:
            if label == chosen:
                self.app.profiles.request(stable_id)
                self._submit(IntentType.SELECT_PROFILE)
                return

    def _commissioning_rows(self) -> tuple[Any, tuple[Any, ...], str]:
        """What the commissioning window renders, read fresh from the runtime."""
        viewport = self.app.guard.geometry
        steps = commissioning_steps(
            self.app.gates,
            connected=viewport.valid,
            viewport_canonical=viewport.is_canonical,
            viewport_usable=viewport.state.can_capture,
        )
        blockers = self.app.coordinator.blockers()
        focus_note = ""
        if any(b.code == "FOCUS" for b in blockers):
            focus_note = (
                "Roblox is not frontmost: expected while you read this. Shadow keeps "
                "running. Focus Roblox before pressing F1."
            )
        return (steps, blockers, focus_note)

    def _show_blockers(self) -> None:
        if self._commissioning is not None and self._commissioning.alive:
            self._commissioning.window.lift()
            self._commissioning.refresh()
            return
        self._commissioning = CommissioningWindow(
            self.root, self.fonts, self._commissioning_rows
        )

    def _collect_evidence(self) -> None:
        """Steps 3-6: open the guide and record real frames. Never sends input."""
        self._show_blockers()
        if self.recorder is None:
            self._toggle_record()

    def _toggle_record(self) -> None:
        if self.recorder is not None:
            self.recorder.stop()
            self.recorder = None
            self._recording_started_s = None
            self.record_button.configure(text="Start Diagnostic Recording")
            self.app.coordinator.set_recording("off")
            return
        session_dir = self.app.paths.recordings / f"session-{int(monotonic_s())}"
        self.recorder = EvidenceRecorder(session_dir)
        self.recorder.start()
        self._recording_started_s = monotonic_s()
        self.record_button.configure(text="Stop Recording")
        self.app.coordinator.set_recording("recording")

    # -- rendering --------------------------------------------------------
    def _tick_preview(self) -> None:
        """Draw the newest packet, if it supersedes what is already drawn."""
        started = monotonic_s()
        observation = self.app.coordinator.observations.peek()
        if observation is not None and self._diagnostics is not None:
            if self._diagnostics.render(observation):
                self._last_observation = observation
                self.app.capture.note_preview_ms((monotonic_s() - started) * 1000.0)
                self.app.capture.trace.record_preview(
                    PreviewTrace(
                        frame_sequence=observation.frame_sequence,
                        at_s=started,
                        paste_ms=self._diagnostics.last_paste_ms,
                        overlay_ms=self._diagnostics.last_overlay_ms,
                        overlay_mode=self._overlay_mode.value,
                        skipped=self._diagnostics.last_overlay_skipped,
                    )
                )
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
        self.analysis.render(self._last_observation)
        self._drain_reports()
        self._tick_recording_label()
        self.root.after(self.STATUS_INTERVAL_MS, self._tick_status)

    def _tick_recording_label(self) -> None:
        if self.recorder is None or self._recording_started_s is None:
            return
        elapsed = int(monotonic_s() - self._recording_started_s)
        self.record_button.configure(text=f"Recording - {elapsed // 60:02d}:{elapsed % 60:02d}")
        self.app.coordinator.set_recording(f"recording {elapsed // 60:02d}:{elapsed % 60:02d}")

    def _tick_metrics(self) -> None:
        self._last_metrics = self.app.capture.metrics()
        self._render_summaries(self.app.coordinator.snapshot(), self._last_metrics)
        self.root.after(self.METRICS_INTERVAL_MS, self._tick_metrics)

    def _tick_drawer(self) -> None:
        self._tick_drawer_once()
        self.root.after(self.DRAWER_INTERVAL_MS, self._tick_drawer)

    def _tick_drawer_once(self) -> None:
        metrics = getattr(self, "_last_metrics", None) or self.app.capture.metrics()
        self.drawer.render(
            self.app.coordinator.snapshot(),
            self._last_observation,
            metrics,
            self.app.coordinator.events,
            {
                "recorder": self._recorder_summary(),
                "packets": f"{self.app.coordinator.observation_count} published, "
                f"{self.app.coordinator.stale_packets} refused as stale",
                "profile": f"{self.app.profiles.active_id} rev {self.app.profiles.revision}",
                "geometry": f"revision {self.app.guard.revision}",
            },
        )

    def _recorder_summary(self) -> str:
        if self.recorder is None:
            return "off"
        stats = self.recorder.stats
        return (
            f"{stats.accepted} frames, {stats.chunks_written} chunks, "
            f"{stats.bytes_written / 1_048_576:.1f} MB, dropped {stats.dropped_ordinary}"
            + (" TRUNCATED" if stats.truncated else "")
        )

    def _render_status(self, snapshot: TelemetrySnapshot) -> None:
        text, colour = MODE_BADGES[snapshot.mode]
        armed = snapshot.arm_state not in ("none", "-")
        if snapshot.mode is RunMode.IDLE and armed:
            text, colour = "LIVE ARMED", GOLD
        if self.app.authority.release_uncertain:
            text, colour = "FAULT - input released", BAD
        self.mode_var.set(text)
        self.mode_label.configure(foreground=colour)

        self.shadow_button.configure(
            text="Shadow running - no input"
            if snapshot.mode is RunMode.SHADOW
            else "Start Shadow Analysis"
        )
        pending = sum(1 for b in snapshot.blockers if b.status != "expected")
        self.blockers_button.configure(
            text=f"Calibrate Live Control ({pending} open)"
            if pending
            else "Calibrate Live Control"
        )
        self.fit_button.configure(
            state="disabled" if snapshot.fit_active else "normal",
            text="Fitting viewport..." if snapshot.fit_active else "Fit & Verify Viewport",
        )

        if snapshot.mode is RunMode.LIVE:
            self.live_guide.configure(text="Live navigation running.", fg=GOLD)
            self.return_button.grid(row=0, column=1, sticky="ew", padx=4)
        elif armed:
            self.live_guide.configure(
                text=f"Live armed ({snapshot.arm_state}) - focus Roblox, then press F1", fg=GOLD
            )
            self.return_button.grid_remove()
        else:
            self.live_guide.configure(
                text="Live is not armed. Enable Live Control, then focus Roblox and press F1.",
                fg=MUTED,
            )
            self.return_button.grid_remove()

        if self.app.authority.release_uncertain:
            self.recover_button.grid(row=0, column=1, sticky="ew", padx=4)
        else:
            self.recover_button.grid_remove()

    def _render_summaries(
        self, snapshot: TelemetrySnapshot | None, metrics: CaptureMetrics
    ) -> None:
        viewport = self.app.guard.geometry
        connected = viewport.valid
        focus = snapshot.focus if snapshot else None
        fit = snapshot.fit if snapshot else None
        fit_active = bool(snapshot.fit_active) if snapshot else False
        self._set_summary(
            "roblox",
            "Connected" if connected else "Disconnected",
            (
                f"{'canonical' if viewport.is_canonical else 'custom'} viewport   "
                f"focus {'yes' if focus else 'no' if focus is False else 'unknown'}\n"
                f"{fit_progress_text(fit, fit_active)}"
                if connected
                else "press Connect Roblox"
            ),
        )
        self._set_summary(
            "capture",
            f"{metrics.unique_fps:.0f} unique fps" if metrics.unique_fps else "Waiting",
            f"target {metrics.requested_hz} Hz   "
            f"age {0.0 if metrics.frame_age_ms is None else metrics.frame_age_ms:.0f} ms   "
            f"{metrics.backend}",
        )
        analysis_state = "Running" if metrics.processed_fps > 0 else "Idle"
        phase = snapshot.phase.name.lower() if snapshot and snapshot.phase else "no phase"
        self._set_summary(
            "analysis",
            f"{analysis_state}   {metrics.processed_fps:.0f} fps",
            f"latency p95 {metrics.end_to_end.p95_ms:.0f} ms   {phase}",
        )
        if snapshot is None:
            live_head, live_detail = "Waiting", ""
        elif snapshot.mode is RunMode.LIVE:
            live_head, live_detail = "Running", "movement is being sent to Roblox"
        elif snapshot.blockers:
            real = [b for b in snapshot.blockers if b.status != "expected"]
            live_head = (
                f"Blocked - {len(real)} open" if real else "Ready when Roblox is focused"
            )
            first = real[0] if real else snapshot.blockers[0]
            live_detail = f"{first.code}: {first.summary}"
        elif snapshot.arm_state not in ("none", "-"):
            live_head, live_detail = "Armed", "focus Roblox, then press F1"
        else:
            live_head, live_detail = "Ready", "Enable Live Control to arm"
        self._set_summary("live", live_head, live_detail)

    def _set_summary(self, key: str, headline: str, detail: str) -> None:
        self.summary_vars[key].set(headline)
        self.summary_labels[key].configure(foreground=summary_colour(headline))
        self.summary_details[key].set(detail)

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
