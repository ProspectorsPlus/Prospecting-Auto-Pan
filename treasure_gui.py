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
from treasure_overlay import (
    BAD,
    BG,
    BONE,
    GOLD,
    JADE,
    MODE_COLOURS,
    MUTED,
    OK,
    STATUS_COLOURS,
    SURFACE,
    SURFACE_ALT,
    WARN,
    DiagnosticCanvas,
)


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
            f"dup {metrics.duplicate_frames.session_total}   "
            f"superseded {metrics.superseded_frames.describe()}   "
            f"unobserved {metrics.dropped_observations.session_total}   "
            f"stale {metrics.stale_frames.session_total}   "
            f"reacq {metrics.reacquisitions}\n"
            f"capture {metrics.capture.p50_ms:4.1f}/{metrics.capture.p95_ms:4.1f}   "
            f"perception {metrics.perception.p50_ms:4.1f}/{metrics.perception.p95_ms:4.1f}   "
            f"end-to-end {metrics.end_to_end.p50_ms:4.1f}/{metrics.end_to_end.p95_ms:4.1f} ms "
            f"(p50/p95)\n"
            f"cpu {metrics.cpu_percent:3.0f}%   rss {metrics.rss_current_mb:4.0f} MB "
            f"(peak {metrics.rss_peak_mb:.0f})"
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
