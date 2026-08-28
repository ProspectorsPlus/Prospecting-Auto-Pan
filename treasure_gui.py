#!/usr/bin/env python3
"""Treasure Navigator dashboard: the Tk shell around the coordinator.

The UI submits :class:`RuntimeIntent` objects and renders
:class:`TelemetrySnapshot`, :class:`SetupProgress` and
:class:`DiagnosticObservation` packets. It owns no run state, calls no input,
and captures nothing of its own.

Four rules shape the whole layout.

**There is one button for the normal flow.** *Start Navigator* finds Roblox,
sizes its window, rebinds capture, identifies the equipped map, checks the
direction reading and starts observing. Connecting, fitting, "collecting
calibration evidence" and "calibrating live control" were four buttons that
between them did one useful thing and one dead-ended in a read-only window;
they are gone.

**Clicking Tk removes focus from Roblox.** So there is no actionable Start
Live, Reset, or Pan Test button. Those are guidance ("focus Roblox, then press
F1"), and the real hotkeys submit their intents only while Roblox is positively
focused (plan 11.2).

**Stop is always reachable.** *Stop & Release (F2)* is pinned in the header at
a fixed size, outside every resizable region, so no window size, UI scale or
layout state can push it off screen.

**The window does not resize itself.** Every dynamic string lives in a widget
whose *requested* size is fixed - a width in characters, or a fixed-height box
that wraps and clips. Conditional controls keep their grid cell and change
state rather than being removed. Each polling loop owns exactly one cancellable
``after`` handle, created once. Those four rules are why the layout no longer
breathes, and each of them is asserted in ``tests/test_gui.py``.

Design note: the dark surface, bone text, jade interactive and gold emphasis
semantics are implemented here independently through ``ttk.Style``. No CSS,
markup, or text was copied from any other project (plan 12).
"""

from __future__ import annotations

import contextlib
import json
import queue
import sys
import tkinter as tk
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from tkinter import font as tkfont
from tkinter import ttk
from typing import Any

from prospector_engine import __version__
from prospector_engine.autosetup import (
    AutomaticSetup,
    CaptureSample,
    ControlModeSample,
    PerceptionSample,
    ProfileVote,
    SetupConfig,
    WindowProbe,
)
from prospector_engine.capture import CaptureService, EvidenceRegistry, ViewportGuard
from prospector_engine.contracts import (
    CadenceMode,
    CapturedFrame,
    CaptureMetrics,
    DiagnosticObservation,
    IntentType,
    ModeResult,
    ModeResultKind,
    RunMode,
    SetupProgress,
    SetupStage,
    TelemetrySnapshot,
    ViewportFit,
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
from prospector_engine.geometry import ViewportGeometry
from prospector_engine.input_authority import (
    AuthorityConfig,
    DeadmanClient,
    HealthSources,
    InputAuthority,
)
from prospector_engine.navigation import (
    NavigationCapabilities,
    PerceptionPipeline,
    make_live_prologue,
    make_live_worker,
    make_shadow_worker,
)
from prospector_engine.ports import PlatformPort, create_platform_port, current_platform_name
from prospector_engine.steering import SteeringLimits
from prospector_engine.telemetry import (
    AppPaths,
    EvidenceRecorder,
    LatestSlot,
    resolve_app_paths,
)
from prospector_engine.trace import PreviewTrace
from prospector_engine.turning import ControlFingerprint, TurnResponse, TurnResponseCache
from prospector_engine.vision import ArrowSegmenter, ProfileAuthority, load_profiles
from treasure_overlay import (
    BAD,
    BG,
    BONE,
    GOLD,
    JADE,
    MUTED,
    OK,
    SURFACE,
    SURFACE_ALT,
    WARN,
    DiagnosticCanvas,
    OverlayMode,
)
from treasure_panels import (
    AnalysisPanel,
    DiagnosticsDrawer,
    Disclosure,
    MessageBox,
    SetupPanel,
    Ticker,
    Tooltip,
    fit_progress_text,
    fixed_label,
    mono_font,
    summary_colour,
)

#: The header badge, in the vocabulary the mission specifies. Each says what is
#: happening to the game, not what the code is doing.
MODE_BADGES: dict[RunMode, tuple[str, str]] = {
    RunMode.IDLE: ("OFF", MUTED),
    RunMode.SHADOW: ("OBSERVING - no input", "#4a9bd1"),
    RunMode.LIVE: ("NAVIGATING", GOLD),
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
# Automatic setup, wired to the real engine
# ---------------------------------------------------------------------------


class EngineSetupPort:
    """The real capture, viewport and perception objects, behind the setup port.

    Every method is a thin, honest adapter. The interesting choices - what
    counts as ambiguous, how a permission failure is distinguished from a
    missing window - live here rather than in the machine, because they are
    facts about this OS and these objects.
    """

    def __init__(
        self,
        *,
        port: PlatformPort,
        guard: ViewportGuard,
        capture: CaptureService,
        pipeline: PerceptionPipeline,
        profiles: ProfileAuthority,
        authority: InputAuthority,
    ) -> None:
        self._port = port
        self._guard = guard
        self._capture = capture
        self._pipeline = pipeline
        self._profiles = profiles
        self._authority = authority
        self._sequence = 0

    # -- window -----------------------------------------------------------
    def locate_window(self) -> WindowProbe:
        geometry = self._guard.connect()
        if geometry.valid:
            return WindowProbe(True, geometry.describe(), identity=geometry.identity())
        detail = (geometry.detail or "").lower()
        trusted = getattr(self._port, "accessibility_trusted", None)
        permission = bool(trusted is not None and not trusted())
        return WindowProbe(
            False,
            geometry.detail or "no Roblox window",
            ambiguous="ambiguous" in detail or "more than one" in detail,
            permission_denied=permission,
            fullscreen="fullscreen" in detail or "space" in detail,
        )

    def release_all_input(self, reason: str) -> None:
        self._authority.release_all(reason)

    def fit_viewport(self) -> ViewportFit:
        return self._guard.fit_and_lock()

    def viewport(self) -> ViewportGeometry:
        return self._guard.geometry

    # -- capture ----------------------------------------------------------
    def restart_capture(self, reason: str) -> None:
        if not self._capture.running:
            self._capture.start()
            return
        self._capture.restart_source(reason)

    def capture_sample(self) -> CaptureSample:
        envelope = self._capture.latest()
        geometry = self._guard.geometry
        expected = geometry.canonical_px if geometry.valid else None
        if envelope is None:
            return CaptureSample(
                sequence=0,
                age_s=None,
                delivered_px=None,
                expected_px=expected,
                processed_fps=self._capture.processed_fps,
                error=self._capture.health() or "no frame has arrived yet",
            )
        frame = envelope.frame
        return CaptureSample(
            sequence=frame.sequence,
            age_s=frame.age_s(monotonic_s()),
            delivered_px=frame.canonical_size_px,
            expected_px=expected,
            processed_fps=self._capture.processed_fps,
            error=frame.capture_error or self._capture.health(),
        )

    # -- perception -------------------------------------------------------
    def _newest(self) -> CapturedFrame | None:
        envelope = self._capture.wait_for_new(self._sequence, 0.2)
        if envelope is None:
            return None
        self._sequence = envelope.frame.sequence
        return envelope.frame

    def profile_vote(self) -> ProfileVote | None:
        frame = self._newest()
        if frame is None:
            return None
        candidates = self._profiles.library.selectable()
        return ProfileVote(frame.sequence, self._pipeline.score_profiles(frame, candidates))

    def lock_profile(self, profile_id: str) -> None:
        """Stage the swap; the pipeline adopts it at the next frame boundary.

        Deliberately not routed through the coordinator: a profile *intent*
        also spends an arm token and safe-stops Live, which is right when a
        person changes the dropdown mid-run and wrong when automatic setup is
        choosing the profile before anything has started.
        """
        self._profiles.request(profile_id)
        self._pipeline.forget_classifiers()

    def perception_sample(self) -> PerceptionSample | None:
        frame = self._newest()
        if frame is None:
            return None
        result = self._pipeline.analyze(frame, map_id="setup", approach_valid=False)
        inputs = result.inputs
        return PerceptionSample(
            frame_sequence=frame.sequence,
            arrow_valid=inputs.arrow.valid,
            direction_valid=inputs.direction.valid,
            error_deg=inputs.direction.error_deg,
            confidence=inputs.direction.confidence,
            track_id=inputs.arrow.track_id,
            processed_fps=self._capture.processed_fps,
            frame_age_ms=frame.age_s(monotonic_s()) * 1000.0,
        )


def shift_lock_probe(
    pipeline: PerceptionPipeline,
) -> Callable[[CapturedFrame], ControlModeSample]:
    """Confirm the locked-camera control mode without ever toggling it.

    The cue is the cursor: in Shift Lock, Roblox replaces the pointer with a
    centred crosshair and holds it there. We do not have a classifier for that
    glyph and will not pretend to - what we *can* observe is that the system
    pointer stays pinned to the middle of the client while the camera is free
    to move, which is exactly what the locked mode does and what the free mode
    does not.

    When the pointer cannot be read at all the honest answer is "cannot
    confirm", and setup stops with a sentence telling the user to switch Shift
    Lock on. It never presses Shift to find out (D-037).
    """
    del pipeline

    def probe(frame: CapturedFrame) -> ControlModeSample:
        cursor = getattr(probe, "cursor", None)
        if cursor is None:
            return ControlModeSample(False, 0.0, "none", "the pointer position is unknown")
        point = cursor()
        if point is None:
            return ControlModeSample(
                False, 0.0, "pointer", "the pointer is outside the Roblox client"
            )
        width, height = frame.canonical_size_px
        dx = abs(point[0] - width / 2.0) / max(1.0, width)
        dy = abs(point[1] - height / 2.0) / max(1.0, height)
        centred = dx < 0.06 and dy < 0.06
        return ControlModeSample(
            centred,
            0.9 if centred else 0.0,
            "pointer",
            "the pointer is held at the centre of the client, as Shift Lock does"
            if centred
            else "the pointer is not centred - Shift Lock does not look switched on",
        )

    return probe


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
    preview: LatestSlot[Any]
    reports: queue.Queue[str]
    paths: AppPaths
    #: One pipeline shared with the running worker, so a profile change in the
    #: UI takes effect on the very next frame instead of the next session.
    pipeline: PerceptionPipeline
    profiles: ProfileAuthority
    turn_cache: TurnResponseCache
    #: What this run has proven, read live. Automatic setup and the live
    #: prologue write into the same cell, so the dashboard and the workers
    #: cannot disagree about what has been measured.
    capabilities_provider: Callable[[], NavigationCapabilities]

    @property
    def capabilities(self) -> NavigationCapabilities:
        return self.capabilities_provider()

    @property
    def turn_response(self) -> TurnResponse | None:
        return self.capabilities.turn_response

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
    pipeline = PerceptionPipeline(segmenter=ArrowSegmenter(profiles.active), profiles=profiles)
    turn_cache = TurnResponseCache(paths.config / "turn-response.json")

    state: dict[str, Any] = {
        "capabilities": NavigationCapabilities.observing(
            os_name=current_platform_name(), profile_id=profiles.active_id
        )
    }

    def capabilities_factory() -> NavigationCapabilities:
        current: NavigationCapabilities = state["capabilities"]
        return replace(current, profile_id=profiles.active_id)

    def pipeline_factory() -> PerceptionPipeline:
        return pipeline

    def report(message: str) -> None:
        # Drop-oldest is fine here: this is a diagnostic read-out, not evidence.
        with contextlib.suppress(queue.Full):
            reports.put_nowait(message)

    def control_fingerprint() -> ControlFingerprint:
        geometry = guard.geometry
        return ControlFingerprint(
            os_name=current_platform_name(),
            backend="unset",
            client_fingerprint=f"roblox@{geometry.canonical_px[0]}x{geometry.canonical_px[1]}",
            camera_sensitivity="unknown",
            control_mode="shift_lock",
            viewport_identity=geometry.identity(),
            profile_id=profiles.active_id,
            profile_revision=profiles.revision,
            supported_min_fps=SteeringLimits().min_processed_fps,
        )

    setup_port = EngineSetupPort(
        port=port,
        guard=guard,
        capture=capture,
        pipeline=pipeline,
        profiles=profiles,
        authority=authority,
    )
    probe = shift_lock_probe(pipeline)
    probe.cursor = port.cursor_client_px  # type: ignore[attr-defined]

    def make_setup(
        cancelled: Callable[[], bool], publish: Callable[[SetupProgress], None]
    ) -> Any:
        return AutomaticSetup(
            setup_port,
            config=SetupConfig(),
            publish=publish,
            cancelled=cancelled,
            candidates=tuple(p.profile_id for p in library.selectable()),
        )

    def run_setup(
        cancelled: Callable[[], bool], publish: Callable[[SetupProgress], None]
    ) -> SetupProgress:
        machine = make_setup(cancelled, publish)
        progress = machine.run_observation()
        if progress.ok:
            reference = machine.reference
            state["capabilities"] = replace(
                capabilities_factory(),
                reference_ok=True,
                profile_id=profiles.active_id,
            )
            if reference is not None:
                pipeline.reference = replace(
                    pipeline.reference, measured_jitter_deg=reference.jitter_deg
                )
        else:
            state["capabilities"] = replace(capabilities_factory(), reference_ok=False)
        return progress

    def remember(response: TurnResponse) -> None:
        state["capabilities"] = replace(
            capabilities_factory(), control_mode_ok=True, turn_response=response
        )
        turn_cache.save(response)

    prologue = make_live_prologue(
        fingerprint_factory=control_fingerprint,
        control_mode_probe=probe,
        capabilities_factory=capabilities_factory,
        setup_factory=lambda cancelled: make_setup(cancelled, lambda _p: None),
        prior_factory=turn_cache.load,
        on_measured=remember,
    )

    workers: dict[IntentType, WorkerFactory] = {
        IntentType.START_SHADOW: make_shadow_worker(pipeline_factory, capabilities_factory),
        IntentType.START_LIVE: make_live_worker(
            pipeline_factory, capabilities_factory, prologue=prologue
        ),
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
        setup_runner=run_setup,
    )
    application = Application(
        port=port,
        guard=guard,
        registry=registry,
        capture=capture,
        authority=authority,
        coordinator=coordinator,
        deadman=deadman,
        preview=preview,
        reports=reports,
        paths=paths,
        pipeline=pipeline,
        profiles=profiles,
        turn_cache=turn_cache,
        capabilities_provider=capabilities_factory,
    )
    return application


# ---------------------------------------------------------------------------
# Window geometry, remembered
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WindowLayout:
    """A remembered window size. Bounded on read, so a bad file cannot hide it."""

    width: int = 1180
    height: int = 800

    MIN_WIDTH = 1000
    MIN_HEIGHT = 700
    MAX_WIDTH = 4000
    MAX_HEIGHT = 3000

    @classmethod
    def load(cls, path: Path) -> WindowLayout:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            width = int(raw["width"])
            height = int(raw["height"])
        except (OSError, ValueError, KeyError, TypeError):
            return cls()
        return cls(
            width=max(cls.MIN_WIDTH, min(cls.MAX_WIDTH, width)),
            height=max(cls.MIN_HEIGHT, min(cls.MAX_HEIGHT, height)),
        )

    def save(self, path: Path) -> None:
        with contextlib.suppress(OSError):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps({"width": self.width, "height": self.height}), encoding="utf-8"
            )


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


class Dashboard:
    """The Tk shell. Renders packets; never decides anything.

    Five independent cadences, each owned by exactly one :class:`Ticker`. The
    preview is allowed to be fast because it is cheap; the status text is
    deliberately slow because nobody can read 60 Hz of numbers and re-laying
    out text is the expensive part of Tk. None of these ever gates navigation:
    preview cadence and control cadence are separate measurements for exactly
    this reason, and the control loop runs on its own thread regardless of
    whether this window is drawing at all.
    """

    PREVIEW_INTERVAL_MS = 33
    STATUS_INTERVAL_MS = 150
    SETUP_INTERVAL_MS = 120
    METRICS_INTERVAL_MS = 500
    DRAWER_INTERVAL_MS = 700

    #: Below this the layout starts clipping. The Stop control is placed
    #: outside every resizable region so it survives regardless.
    MIN_WIDTH = WindowLayout.MIN_WIDTH
    MIN_HEIGHT = WindowLayout.MIN_HEIGHT

    #: Fixed character widths for the strings that change most often. These are
    #: the leaves of the layout tree: pinning them here is what stops a status
    #: string from resizing the window (see the module docstring).
    SUMMARY_HEADLINE_CHARS = 22
    SUMMARY_DETAIL_CHARS = 30
    READOUT_VALUE_CHARS = 26

    def __init__(self, root: tk.Tk, app: Application) -> None:
        self.root = root
        self.app = app
        self.recorder: EvidenceRecorder | None = None
        self._recording_started_s: float | None = None
        self._diagnostics: DiagnosticCanvas | None = None
        self._last_observation: DiagnosticObservation | None = None
        self._overlay_mode = OverlayMode.MINIMAL
        self._last_drawer_key: tuple[Any, ...] | None = None
        self._layout_path = app.paths.config / "window.json"
        self._layout = WindowLayout.load(self._layout_path)
        self._closing = False

        root.title(f"Treasure Navigator {__version__}")
        root.configure(bg=BG)
        root.minsize(self.MIN_WIDTH, self.MIN_HEIGHT)
        root.geometry(f"{self._layout.width}x{self._layout.height}")
        root.columnconfigure(0, weight=1)
        # Row 5 is the only row that grows. Everything above it keeps its
        # natural height, which is what stops a resize from clipping a control.
        for row in range(7):
            root.rowconfigure(row, weight=1 if row == 5 else 0)

        self._style()
        self._build_header()
        self._build_primary()
        self._build_setup()
        self._build_summaries()
        self._build_message()
        self._build_body()
        self._build_advanced()
        self._build_drawer()

        self.tickers: dict[str, Ticker] = {
            "preview": Ticker(
                root, self.PREVIEW_INTERVAL_MS, self._render_preview, name="preview"
            ),
            "status": Ticker(root, self.STATUS_INTERVAL_MS, self._render_status, name="status"),
            "setup": Ticker(root, self.SETUP_INTERVAL_MS, self._render_setup, name="setup"),
            "metrics": Ticker(
                root, self.METRICS_INTERVAL_MS, self._render_metrics, name="metrics"
            ),
            "drawer": Ticker(root, self.DRAWER_INTERVAL_MS, self._render_drawer, name="drawer"),
        }
        for ticker in self.tickers.values():
            ticker.start()
        root.bind("<Configure>", self._on_configure)

    # -- styling ----------------------------------------------------------
    def _style(self) -> None:
        family = _font_family()
        self.fonts = {
            "title": (family, 19, "bold"),
            "mode": (family, 16, "bold"),
            "headline": (family, 14, "bold"),
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
            "Primary.TButton",
            background=JADE,
            foreground=BG,
            font=(family, 13, "bold"),
            padding=(16, 10),
            borderwidth=0,
        )
        style.map(
            "Primary.TButton",
            background=[("active", "#3ac694"), ("disabled", SURFACE)],
            foreground=[("disabled", MUTED)],
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
        header = ttk.Frame(self.root, style="T.TFrame", padding=(14, 12, 14, 6))
        header.grid(row=0, column=0, sticky="ew")
        # Column 1 absorbs every pixel of extra width, so the badge and the
        # Stop control keep their positions at any window size or UI scale.
        header.columnconfigure(1, weight=1)
        ttk.Label(header, text="Treasure Navigator", style="T.TLabel", font=self.f_title).grid(
            row=0, column=0, sticky="w"
        )
        self.mode_var = tk.StringVar(value="OFF")
        self.mode_label = fixed_label(
            header,
            self.mode_var,
            width=26,
            style="T.TLabel",
            font=self.fonts["mode"],
            anchor="e",
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

    # -- primary controls -------------------------------------------------
    def _build_primary(self) -> None:
        """Three controls, and nothing else that a normal run needs."""
        row = ttk.Frame(self.root, style="T.TFrame", padding=(14, 2))
        row.grid(row=1, column=0, sticky="ew")
        row.columnconfigure(3, weight=1)

        self.start_button = ttk.Button(
            row, text="Start Navigator", style="Primary.TButton", command=self._start
        )
        self.start_button.grid(row=0, column=0, sticky="w")
        Tooltip(
            self.start_button,
            "Finds Roblox, sizes its window to 1280x720, rebinds capture, identifies the "
            "equipped map and starts following the arrow. Sends no input until you arm it.",
        )
        self.observe_button = ttk.Button(
            row, text="Observe Only", style="T.TButton", command=self._observe
        )
        self.observe_button.grid(row=0, column=1, sticky="w", padx=(10, 0))
        Tooltip(
            self.observe_button,
            "Runs detection and navigation decisions and shows what it would do, "
            "without ever sending input to the game.",
        )
        # The one physical arming gesture. It is deliberately a separate,
        # deliberate click rather than something Start Navigator does for you:
        # automatic setup may reach READY on its own, and it may never arm.
        self.arm_button = ttk.Button(
            row, text="Arm Live", style="T.TButton", command=self._arm, state="disabled"
        )
        self.arm_button.grid(row=0, column=2, sticky="w", padx=(10, 0))
        Tooltip(
            self.arm_button,
            "Authorizes keyboard and camera output for thirty seconds. It does not "
            "begin movement: after arming, focus Roblox and press F1. Stop & Release "
            "cancels it at any time.",
        )
        self.guide = MessageBox(row, self.fonts, height_px=40, width_px=440)
        self.guide.frame.grid(row=0, column=3, sticky="e", padx=(14, 0))
        self.guide.set("Open Roblox windowed with a map equipped, then press Start Navigator.")

    # -- setup ------------------------------------------------------------
    def _build_setup(self) -> None:
        container = ttk.Frame(self.root, style="T.TFrame", padding=(12, 6))
        container.grid(row=2, column=0, sticky="ew")
        container.columnconfigure(0, weight=1)
        self.setup_panel = SetupPanel(container, self.fonts)
        self.setup_panel.frame.grid(row=0, column=0, sticky="ew")

    # -- summaries --------------------------------------------------------
    def _build_summaries(self) -> None:
        strip = ttk.Frame(self.root, style="T.TFrame", padding=(10, 4))
        strip.grid(row=3, column=0, sticky="ew")
        self.summary_vars: dict[str, tk.StringVar] = {}
        self.summary_labels: dict[str, ttk.Label] = {}
        self.summary_details: dict[str, tk.StringVar] = {}
        cards = [
            ("roblox", "ROBLOX WINDOW", "Whether capture is bound to the game window."),
            ("capture", "CAPTURE", "Frames arriving from the bound window."),
            ("navigation", "NAVIGATION", "What the navigator is doing right now."),
            ("live", "INPUT SAFETY", "Whether keyboard and camera output may be enabled."),
        ]
        for index, (key, title, tip) in enumerate(cards):
            strip.columnconfigure(index, weight=1, uniform="summary")
            frame = ttk.Frame(strip, style="Card.TFrame", padding=(10, 8))
            frame.grid(row=0, column=index, sticky="nsew", padx=4)
            frame.columnconfigure(0, weight=1)
            ttk.Label(frame, text=title, style="Muted.TLabel").grid(row=0, column=0, sticky="w")
            headline = tk.StringVar(value="-")
            label = fixed_label(frame, headline, width=self.SUMMARY_HEADLINE_CHARS)
            label.grid(row=1, column=0, sticky="w")
            detail = tk.StringVar(value="")
            fixed_label(
                frame,
                detail,
                width=self.SUMMARY_DETAIL_CHARS,
                style="Muted.TLabel",
                font=self.f_small,
            ).grid(row=2, column=0, sticky="w")
            self.summary_vars[key] = headline
            self.summary_labels[key] = label
            self.summary_details[key] = detail
            Tooltip(frame, tip)

    # -- one actionable message ------------------------------------------
    def _build_message(self) -> None:
        container = ttk.Frame(self.root, style="T.TFrame", padding=(14, 2))
        container.grid(row=4, column=0, sticky="ew")
        container.columnconfigure(0, weight=1)
        self.message = MessageBox(container, self.fonts, height_px=44, width_px=1100)
        self.message.frame.grid(row=0, column=0, sticky="ew")

    # -- body -------------------------------------------------------------
    def _build_body(self) -> None:
        body = ttk.Frame(self.root, style="T.TFrame", padding=(10, 6))
        body.grid(row=5, column=0, sticky="nsew")
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
        ttk.Label(top, text="WHAT THE NAVIGATOR SEES", style="Muted.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        self.legend_var = tk.StringVar(value="")
        self.canvas = tk.Canvas(
            left, bg="#0b0d10", highlightthickness=0, width=640, height=360, bd=0
        )
        self.canvas.grid(row=1, column=0, sticky="nsew", pady=(6, 4))
        self._diagnostics = DiagnosticCanvas(self.canvas, self._overlay_mode)
        tk.Label(
            left,
            textvariable=self.legend_var,
            bg=SURFACE,
            fg=MUTED,
            font=self.f_small,
            anchor="w",
            justify="left",
            wraplength=620,
        ).grid(row=2, column=0, sticky="ew")
        self._update_legend()

        right = ttk.Frame(body, style="T.TFrame")
        right.grid(row=0, column=1, sticky="nsew", padx=4)
        right.columnconfigure(0, weight=1)
        right.rowconfigure(1, weight=1)
        self._build_readout(right)
        self.analysis = AnalysisPanel(right, self.fonts)
        self.analysis.frame.grid(row=1, column=0, sticky="nsew", pady=(8, 0))

    def _build_readout(self, parent: tk.Misc) -> None:
        """The numbers a person acts on, one row each, fixed widths."""
        frame = ttk.Frame(parent, style="Card.TFrame", padding=10)
        frame.grid(row=0, column=0, sticky="ew")
        frame.columnconfigure(1, weight=1)
        ttk.Label(frame, text="LIVE READOUT", style="Muted.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 6)
        )
        self.readout_vars: dict[str, tk.StringVar] = {}
        rows = [
            ("viewport", "Viewport"),
            ("profile", "Map profile"),
            ("state", "Navigation"),
            ("error", "Alignment error"),
            ("turning", "Turning by"),
            ("rates", "Capture / control"),
            ("leases", "Held inputs"),
            ("recovery", "Last action"),
        ]
        for index, (key, label) in enumerate(rows, start=1):
            ttk.Label(frame, text=label, style="Muted.TLabel").grid(
                row=index, column=0, sticky="w", padx=(0, 10)
            )
            variable = tk.StringVar(value="-")
            self.readout_vars[key] = variable
            fixed_label(frame, variable, width=self.READOUT_VALUE_CHARS).grid(
                row=index, column=1, sticky="w"
            )

    # -- advanced ---------------------------------------------------------
    def _build_advanced(self) -> None:
        container = ttk.Frame(self.root, style="T.TFrame", padding=(12, 4))
        container.grid(row=6, column=0, sticky="ew")
        container.columnconfigure(0, weight=1)
        self.advanced = Disclosure(container, "Advanced and diagnostics")
        self.advanced.grid(row=0, column=0, sticky="ew")
        body = self.advanced.body
        for column in range(4):
            body.columnconfigure(column, weight=1)

        self.retry_button = ttk.Button(
            body, text="Retry Automatic Setup", style="T.TButton", command=self._retry
        )
        self.retry_button.grid(row=0, column=0, sticky="ew", padx=4, pady=4)
        Tooltip(
            self.retry_button,
            "Runs automatic setup again from the beginning. Sends no input.",
        )
        self.record_button = ttk.Button(
            body, text="Record Diagnostics", style="T.TButton", command=self._toggle_record
        )
        self.record_button.grid(row=0, column=1, sticky="ew", padx=4, pady=4)
        Tooltip(
            self.record_button,
            "Stores frames, observations and decisions for debugging. This is debugging "
            "evidence, not calibration: nothing here changes how the navigator behaves.",
        )
        self.recover_button = ttk.Button(
            body, text="Recover Release", style="T.TButton", command=self._recover
        )
        self.recover_button.grid(row=0, column=2, sticky="ew", padx=4, pady=4)
        Tooltip(
            self.recover_button,
            "Emits release edges only. Needed before input can be offered again after a "
            "release that could not be confirmed safe.",
        )
        self.return_button = ttk.Button(
            body, text="Return to Observing", style="T.TButton", command=self._return_to_shadow
        )
        self.return_button.grid(row=0, column=3, sticky="ew", padx=4, pady=4)
        Tooltip(self.return_button, "Releases movement immediately and keeps analysis running.")

        selectors = ttk.Frame(body, style="Card.TFrame")
        selectors.grid(row=1, column=0, columnspan=4, sticky="ew", padx=4, pady=(2, 6))
        for column in range(6):
            selectors.columnconfigure(column, weight=1 if column in (1, 3, 5) else 0)
        self._build_profile_selector(selectors)

        ttk.Label(selectors, text="Cadence", style="Muted.TLabel").grid(
            row=0, column=2, sticky="e", padx=(12, 4)
        )
        self.cadence_var = tk.StringVar(value=CadenceMode.AUTO.value)
        cadence = ttk.Combobox(
            selectors,
            textvariable=self.cadence_var,
            values=[mode.value for mode in CadenceMode],
            state="readonly",
            width=11,
            style="T.TCombobox",
        )
        cadence.grid(row=0, column=3, sticky="w")
        cadence.bind("<<ComboboxSelected>>", self._on_cadence_selected)
        Tooltip(
            cadence,
            "How much cadence to ask for.\n\n"
            + "\n".join(f"{mode.value}: {mode.description}" for mode in CadenceMode)
            + "\n\nThe governor still refuses to hold a tier it is not achieving.",
        )

        ttk.Label(selectors, text="Overlay", style="Muted.TLabel").grid(
            row=0, column=4, sticky="e", padx=(12, 4)
        )
        self.overlay_var = tk.StringVar(value=OverlayMode.MINIMAL.value)
        overlay = ttk.Combobox(
            selectors,
            textvariable=self.overlay_var,
            values=[mode.value for mode in OverlayMode],
            state="readonly",
            width=18,
            style="T.TCombobox",
        )
        overlay.grid(row=0, column=5, sticky="w")
        overlay.bind("<<ComboboxSelected>>", self._on_overlay_selected)
        Tooltip(
            overlay,
            "Minimal draws the forward reference, the desired direction and the signed "
            "turn. Full Diagnostics adds contours, notches and rejected candidates.",
        )

        tk.Label(
            body,
            text="Focus Roblox -> F1 navigate  |  F2 stop  |  F3 pixel  |  "
            "F4 reset  |  F5 pan test  |  F6 dig",
            bg=SURFACE,
            fg=MUTED,
            font=self.f_small,
            anchor="w",
        ).grid(row=2, column=0, columnspan=4, sticky="ew", padx=4, pady=(0, 4))

    def _build_profile_selector(self, parent: tk.Misc) -> None:
        """Manual override only. Automatic selection is the normal path.

        The selector renders labels the authority produced and maps back by
        **stable id**, never by parsing a label - which is how the dropdown
        came to disagree with the running pipeline in the first place.
        """
        authority = self.app.profiles
        self._profile_choices = authority.choices()
        ttk.Label(parent, text="Map profile", style="Muted.TLabel").grid(
            row=0, column=0, sticky="e", padx=(0, 4)
        )
        self.profile_var = tk.StringVar(value=authority.label_for(authority.active_id))
        self.profile_combo = ttk.Combobox(
            parent,
            textvariable=self.profile_var,
            values=[label for _id, label in self._profile_choices],
            state="readonly",
            width=34,
            style="T.TCombobox",
        )
        self.profile_combo.grid(row=0, column=1, sticky="w")
        self.profile_combo.bind("<<ComboboxSelected>>", self._on_profile_selected)
        Tooltip(
            self.profile_combo,
            "Automatic setup identifies the equipped map from consecutive frames. "
            "Choose one here only to override that.",
        )

    def _build_drawer(self) -> None:
        self.drawer = DiagnosticsDrawer(self.advanced.body, self.fonts)
        self.drawer.frame.grid(row=3, column=0, columnspan=4, sticky="ew", padx=4, pady=(0, 4))
        self._last_metrics = self.app.capture.metrics()
        self._render_summaries(None, self._last_metrics)
        self._render_drawer()

    # -- intents ----------------------------------------------------------
    def _submit(self, intent_type: IntentType) -> None:
        coordinator = self.app.coordinator
        coordinator.submit(coordinator.next_intent(intent_type, "gui"))

    def _start(self) -> None:
        self._submit(IntentType.START_NAVIGATOR)

    def _retry(self) -> None:
        self._submit(IntentType.RETRY_SETUP)

    def _observe(self) -> None:
        self._submit(IntentType.START_SHADOW)

    def _stop(self) -> None:
        self._submit(IntentType.STOP)

    def _return_to_shadow(self) -> None:
        self._submit(IntentType.RETURN_TO_SHADOW)

    def _recover(self) -> None:
        self._submit(IntentType.RECOVER_RELEASE)

    def _arm(self) -> None:
        """The one physical arming gesture. Never simulated, never persisted.

        Refused, with the reason, while anything is still blocking - arming
        into a blocked runtime would spend the token on a readiness check that
        was always going to fail, and the user would have to arm twice.
        """
        blocking = [b for b in self.app.coordinator.blockers() if b.status != "expected"]
        if blocking:
            self.message.set(f"{blocking[0].summary}. {blocking[0].remedy}", WARN)
            return
        self._submit(IntentType.ARM_LIVE_FROM_UI)

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
            "dashed grey = player-forward reference   gold = direction to the map arrow   "
            "orange arc = signed turn"
            if self._overlay_mode is OverlayMode.MINIMAL
            else "dashed grey = player-forward reference   gold = direction to the arrow   "
            "orange arc = signed turn   jade = accepted contour   gold crosses = notches "
            "and tip   dull red = rejected candidates and outlier cues"
        )

    def _on_profile_selected(self, _event: Any = None) -> None:
        chosen = self.profile_var.get()
        for stable_id, label in self._profile_choices:
            if label == chosen:
                self.app.profiles.request(stable_id)
                self._submit(IntentType.SELECT_PROFILE)
                return

    def _toggle_record(self) -> None:
        if self.recorder is not None:
            self.recorder.stop()
            self.recorder = None
            self._recording_started_s = None
            self.record_button.configure(text="Record Diagnostics")
            self.app.coordinator.set_recording("off")
            return
        session_dir = self.app.paths.recordings / f"session-{int(monotonic_s())}"
        self.recorder = EvidenceRecorder(session_dir)
        self.recorder.start()
        self._recording_started_s = monotonic_s()
        self.record_button.configure(text="Stop Recording")
        self.app.coordinator.set_recording("recording")

    # -- layout -----------------------------------------------------------
    def _on_configure(self, event: Any) -> None:
        """Remember a *user* resize; ignore the children reporting their own.

        Debounced through the size comparison rather than a timer: only a
        change in the toplevel's own size is interesting, and children fire
        this event constantly while the preview scales.
        """
        if self._closing or event.widget is not self.root:
            return
        width, height = int(event.width), int(event.height)
        if (width, height) == (self._layout.width, self._layout.height):
            return
        if width < self.MIN_WIDTH or height < self.MIN_HEIGHT:
            return
        self._layout = WindowLayout(width=width, height=height)
        self.message.resize(max(400, width - 60))

    # -- rendering --------------------------------------------------------
    def _render_preview(self) -> None:
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

    def _render_idle_preview(self) -> None:
        """Show the raw frame when no observation exists yet."""
        envelope = self.app.preview.peek()
        if envelope is None or self._diagnostics is None:
            return
        started = monotonic_s()
        if self._diagnostics.render_frame_only(envelope.frame):
            self.app.capture.note_preview_ms((monotonic_s() - started) * 1000.0)

    def _render_setup(self) -> None:
        progress = self.app.coordinator.setup_progress
        self.setup_panel.render(progress)
        self._render_guidance(progress)

    def _render_guidance(self, progress: SetupProgress) -> None:
        """One sentence: what to do next, or what went wrong.

        Failures win over everything else, because a failure is the only state
        in which the user has something to do that is not "wait".
        """
        snapshot = self.app.coordinator.snapshot()
        failure = progress.failure
        if failure is not None and progress.stage is SetupStage.FAILED:
            self.message.set(f"{failure.summary}. {failure.remedy}", BAD)
        elif self.app.authority.release_uncertain:
            self.message.set(
                "A previous release could not be confirmed safe. Press Recover Release "
                "under Advanced before navigating again.",
                BAD,
            )
        elif progress.running:
            self.message.set(f"Setting up: {progress.detail}", GOLD)
        elif snapshot is not None and snapshot.mode is RunMode.LIVE:
            self.message.set("Navigating. Press Stop at any time.", OK)
        elif progress.ok:
            self.message.set(
                "Ready. Focus Roblox and press F1 to let the navigator move your "
                "character; press F2 to stop.",
                OK,
            )
        else:
            self.message.set(
                "Open Roblox in windowed mode with a treasure map equipped, then press "
                "Start Navigator."
            )

        running = progress.running or bool(self.app.coordinator.setup_active)
        self.start_button.configure(
            state="disabled" if running else "normal",
            text="Setting up..." if running else "Start Navigator",
        )
        self.retry_button.configure(state="disabled" if running else "normal")
        blocking = [b for b in self.app.coordinator.blockers() if b.status != "expected"]
        self.arm_button.configure(state="disabled" if blocking else "normal")

    def _render_status(self) -> None:
        snapshot = self.app.coordinator.snapshot()
        if snapshot is not None:
            self._render_badges(snapshot)
        self.analysis.render(self._last_observation)
        self._render_readout(snapshot)
        self._drain_reports()
        self._render_recording_label()

    def _render_badges(self, snapshot: TelemetrySnapshot) -> None:
        text, colour = MODE_BADGES[snapshot.mode]
        armed = snapshot.arm_state not in ("none", "-")
        if snapshot.mode is RunMode.IDLE and armed:
            text, colour = "ARMED - press F1", GOLD
        if self.app.authority.release_uncertain:
            text, colour = "FAULT - input released", BAD
        self.mode_var.set(text)
        self.mode_label.configure(foreground=colour)
        self.observe_button.configure(
            text="Observing - no input" if snapshot.mode is RunMode.SHADOW else "Observe Only",
            state="disabled" if snapshot.mode is RunMode.SHADOW else "normal",
        )
        # Conditional controls keep their cell and change state. Removing a
        # widget from the grid changes the layout, and a layout that changes
        # when a fault appears is a layout that jumps at the worst moment.
        self.recover_button.configure(
            state="normal" if self.app.authority.release_uncertain else "disabled"
        )
        self.return_button.configure(
            state="normal" if snapshot.mode is RunMode.LIVE else "disabled"
        )
        self.guide.set(
            "Navigating - Stop is always available."
            if snapshot.mode is RunMode.LIVE
            else (
                "Armed. Focus Roblox and press F1."
                if armed
                else "Open Roblox windowed with a map equipped, then press Start Navigator."
            ),
            GOLD if armed or snapshot.mode is RunMode.LIVE else MUTED,
        )

    def _render_readout(self, snapshot: TelemetrySnapshot | None) -> None:
        geometry = self.app.guard.geometry
        capabilities = self.app.capabilities
        observation = self._last_observation
        self.readout_vars["viewport"].set(
            f"{geometry.canonical_px[0]}x{geometry.canonical_px[1]} "
            f"{'canonical' if geometry.is_canonical else 'adopted'}"
            if geometry.valid
            else "not connected"
        )
        self.readout_vars["profile"].set(
            f"{self.app.profiles.active_id} rev {self.app.profiles.revision}"
        )
        phase = snapshot.phase.name.lower() if snapshot and snapshot.phase else "idle"
        control = snapshot.control_state.value if snapshot and snapshot.control_state else "-"
        self.readout_vars["state"].set(f"{phase} / {control}")
        direction = observation.direction if observation else None
        self.readout_vars["error"].set(
            f"{direction.error_deg:+.1f} deg"
            if direction is not None and direction.valid and direction.error_deg is not None
            else "no reading"
        )
        response = capabilities.turn_response
        self.readout_vars["turning"].set(
            response.backend.label
            if response is not None and response.usable
            else "not measured"
        )
        metrics = getattr(self, "_last_metrics", None)
        self.readout_vars["rates"].set(
            f"{metrics.unique_fps:.0f} / {metrics.processed_fps:.0f} fps"
            if metrics is not None
            else "-"
        )
        held = self.app.authority.held_targets()
        self.readout_vars["leases"].set(", ".join(held) if held else "none")
        recovery = observation.plain_summary if observation else ""
        self.readout_vars["recovery"].set(recovery[: self.READOUT_VALUE_CHARS] or "-")

    def _render_recording_label(self) -> None:
        if self.recorder is None or self._recording_started_s is None:
            return
        elapsed = int(monotonic_s() - self._recording_started_s)
        self.record_button.configure(text=f"Recording {elapsed // 60:02d}:{elapsed % 60:02d}")
        self.app.coordinator.set_recording(f"recording {elapsed // 60:02d}:{elapsed % 60:02d}")

    def _render_metrics(self) -> None:
        self._last_metrics = self.app.capture.metrics()
        self._render_summaries(self.app.coordinator.snapshot(), self._last_metrics)

    def _render_drawer(self) -> None:
        """Render the drawer only when it is visible and something changed.

        Both halves matter. Rewriting five Text widgets several times a second
        is the single most expensive thing this window can do, and doing it
        while the drawer is folded away is doing it for nobody.
        """
        if not self.advanced.expanded:
            return
        metrics = getattr(self, "_last_metrics", None) or self.app.capture.metrics()
        observation = self._last_observation
        key = (
            id(observation),
            metrics.unique_fps,
            metrics.processed_fps,
            self.app.coordinator.observation_count,
            self.app.guard.revision,
            self.app.coordinator.events.sequence,
        )
        if key == self._last_drawer_key:
            return
        self._last_drawer_key = key
        self.drawer.render(
            self.app.coordinator.snapshot(),
            observation,
            metrics,
            self.app.coordinator.events,
            {
                "recorder": self._recorder_summary(),
                "packets": f"{self.app.coordinator.observation_count} published, "
                f"{self.app.coordinator.stale_packets} refused as stale",
                "profile": f"{self.app.profiles.active_id} rev {self.app.profiles.revision}",
                "geometry": f"revision {self.app.guard.revision}",
                "capabilities": self.app.capabilities.describe(),
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
            "Connected" if connected else "Not found",
            (
                f"{'canonical' if viewport.is_canonical else 'custom'} - "
                f"focus {'yes' if focus else 'no' if focus is False else '?'}"
                if connected
                else fit_progress_text(fit, fit_active)
            ),
        )
        self._set_summary(
            "capture",
            f"{metrics.unique_fps:.0f} unique fps" if metrics.unique_fps else "Waiting",
            f"{metrics.requested_hz} Hz target   "
            f"{0.0 if metrics.frame_age_ms is None else metrics.frame_age_ms:.0f} ms",
        )
        phase = snapshot.phase.name.title() if snapshot and snapshot.phase else "Idle"
        self._set_summary(
            "navigation",
            phase if metrics.processed_fps > 0 else "Idle",
            f"{metrics.processed_fps:.0f} fps   p95 {metrics.end_to_end.p95_ms:.0f} ms",
        )
        if snapshot is None:
            live_head, live_detail = "Waiting", ""
        elif snapshot.mode is RunMode.LIVE:
            live_head, live_detail = "Navigating", "movement is being sent"
        else:
            real = [b for b in snapshot.blockers if b.status != "expected"]
            if real:
                live_head = f"Blocked - {len(real)}"
                live_detail = f"{real[0].code}: {real[0].summary}"
            elif snapshot.arm_state not in ("none", "-"):
                live_head, live_detail = "Armed", "focus Roblox, press F1"
            else:
                live_head, live_detail = "Ready", "press F1 in Roblox to navigate"
        self._set_summary("live", live_head, live_detail)

    def _set_summary(self, key: str, headline: str, detail: str) -> None:
        if self.summary_vars[key].get() != headline:
            self.summary_vars[key].set(headline)
            self.summary_labels[key].configure(foreground=summary_colour(headline))
        if self.summary_details[key].get() != detail:
            self.summary_details[key].set(detail)

    def _drain_reports(self) -> None:
        while True:
            try:
                message = self.app.reports.get_nowait()
            except queue.Empty:
                return
            self.app.coordinator.events.add("pixel-probe", message)

    def on_close(self) -> None:
        self._closing = True
        for ticker in self.tickers.values():
            ticker.stop()
        if self.recorder is not None:
            self.recorder.stop()
        self._layout.save(self._layout_path)
        self.app.shutdown()
        self.root.destroy()


def main() -> int:
    app = build_application()
    try:
        app.deadman.start()
    except Exception as exc:
        print(
            f"[deadman] unavailable: {exc!r} - navigation will refuse to start.",
            file=sys.stderr,
        )
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
