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
Ctrl+N"), and the real hotkeys submit their intents only while Roblox is
positively focused (plan 11.2).

**Stop is always reachable.** *Stop & Release* is pinned in the header at
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
from dataclasses import dataclass
from pathlib import Path
from tkinter import font as tkfont
from tkinter import ttk
from typing import Any

from prospector_engine import __version__
from prospector_engine.application import Application, build_application
from prospector_engine.bindings import BINDINGS, chord_label
from prospector_engine.contracts import (
    CadenceMode,
    CaptureMetrics,
    DiagnosticObservation,
    IntentType,
    RunMode,
    SetupProgress,
    SetupStage,
    TelemetrySnapshot,
    monotonic_s,
)
from prospector_engine.geometry import ViewportGeometry
from prospector_engine.telemetry import (
    EvidenceRecorder,
)
from prospector_engine.trace import PreviewTrace
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


#: Spelled for the running OS - Option on a Mac, Alt on Windows - and read
#: from the one binding registry, so a rebinding cannot leave the window
#: telling people to press a key that no longer does anything.
_OS_NAME = sys.platform
_CHORD_START = chord_label(IntentType.START_LIVE, _OS_NAME)
_CHORD_STOP = chord_label(IntentType.STOP, _OS_NAME)

#: What the armed setup stages are actually doing, in the user's words. These
#: run *inside* Live, after the arm, and none of them is navigation: the
#: character stands still while one bounded probe at a time is pressed and
#: released. A window that said "navigating" through all of them is what made
#: a thirty-second failure indistinguishable from thirty seconds of walking.
_LIVE_STAGE_WORDS: dict[SetupStage, str] = {
    SetupStage.VERIFY_INPUT: "Testing whether Roblox accepts a key",
    SetupStage.VERIFY_CONTROL_MODE: "Checking the camera control mode",
    SetupStage.CHARACTERIZE_TURN: "Measuring how the camera turns",
}


#: Two or three words per binding. The full sentence lives in the registry's
#: ``description`` and in the tooltip; the legend is a reminder, not a manual.
_LEGEND_VERBS: dict[IntentType, str] = {
    IntentType.START_LIVE: "navigate",
    IntentType.START_SHADOW: "observe",
    IntentType.STOP: "stop",
    IntentType.RESET_CHARACTER: "reset",
    IntentType.PAN_SWAP_TEST: "pan",
    IntentType.DIG_LOOP: "dig",
    IntentType.PIXEL_INFO: "pixel",
}


def _hotkey_legend() -> str:
    """The header legend, generated from the registry rather than typed out.

    Kept short on purpose: this label spans the header and a long one widens
    the whole window, which is the geometry rule the dashboard is built on
    (D-041). The chord prefix is written once and the keys listed after it.
    """
    prefix = _CHORD_START.rsplit("+", 1)[0]
    parts = [
        f"{b.chord.key.upper()} {_LEGEND_VERBS.get(b.intent, '')}".strip() for b in BINDINGS
    ]
    return f"{prefix}+  " + " · ".join(parts)


def _hotkey_help() -> str:
    """The long form, for the tooltip, where length costs nothing."""
    lines = [f"{b.label(_OS_NAME)} - {b.description}" for b in BINDINGS]
    lines.append("")
    lines.append("Stop works whether or not Roblox is focused.")
    return "\n".join(lines)


def _font_family() -> str:
    candidates = ["SF Pro Text", "Helvetica Neue", "Segoe UI", "DejaVu Sans", "TkDefaultFont"]
    available = set(tkfont.families())
    for name in candidates:
        if name in available:
            return name
    return "TkDefaultFont"


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
            text=f"Stop & Release All Input  ({_CHORD_STOP})",
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
            f"begin movement: after arming, focus Roblox and press {_CHORD_START}. "
            "Stop & Release "
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
        self._preflight_cache: Any = None
        self._preflight_at_s = 0.0
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

        legend = tk.Label(
            body,
            text=_hotkey_legend(),
            bg=SURFACE,
            fg=MUTED,
            font=self.f_small,
            anchor="w",
        )
        # Requested width fixed in characters: the legend is generated, and a
        # generated string must never be able to decide how wide the window is.
        legend.configure(width=68)
        legend.grid(row=2, column=0, columnspan=4, sticky="ew", padx=4, pady=(0, 4))
        Tooltip(legend, _hotkey_help())

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
        elif progress.running and progress.stage.emits_input:
            # Armed, and *not* navigating. Saying "movement is being sent" here
            # would be the single most misleading thing this window could do:
            # these stages press one bounded probe at a time with the character
            # standing still, and the last one has not run yet.
            self.message.set(
                f"{_LIVE_STAGE_WORDS[progress.stage]}. Press {_CHORD_STOP} to stop.", GOLD
            )
        elif progress.running:
            self.message.set(f"Setting up: {progress.detail}", GOLD)
        elif snapshot is not None and snapshot.mode is RunMode.LIVE:
            self.message.set(
                f"Navigating - your character is moving. Press {_CHORD_STOP} to stop.", OK
            )
        elif progress.ok:
            self.message.set(
                f"Ready. Focus Roblox and press {_CHORD_START} to let the navigator move "
                f"your character; press {_CHORD_STOP} to stop.",
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
            text, colour = f"ARMED - press {_CHORD_START}", GOLD
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
        setup = self.app.coordinator.setup_progress
        if snapshot.mode is RunMode.LIVE and setup.running and setup.stage.emits_input:
            guide = f"{_LIVE_STAGE_WORDS[setup.stage]} - Stop is always available."
        elif snapshot.mode is RunMode.LIVE:
            guide = "Navigating - Stop is always available."
        elif armed:
            guide = f"Armed. Focus Roblox and press {_CHORD_START}."
        else:
            guide = "Open Roblox windowed with a map equipped, then press Start Navigator."
        self.guide.set(guide, GOLD if armed or snapshot.mode is RunMode.LIVE else MUTED)

    def _render_readout(self, snapshot: TelemetrySnapshot | None) -> None:
        geometry = self.app.guard.geometry
        capabilities = self.app.capabilities
        observation = self._last_observation
        self.readout_vars["viewport"].set(self._viewport_text(geometry, snapshot))
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

    def _viewport_text(self, geometry: ViewportGeometry, snapshot: Any) -> str:
        """Requested and achieved, because a clamp is an answer worth reading.

        "1280x720 adopted" hides the interesting case: the window was asked for
        the canonical size and gave a different one. Both numbers, when they
        differ.
        """
        if not geometry.valid:
            return "not connected"
        client = geometry.client_logical
        achieved = (
            f"{client.width:g}x{client.height:g}"
            if client is not None
            else f"{geometry.canonical_px[0]}x{geometry.canonical_px[1]}"
        )
        setup = getattr(snapshot, "setup", None) if snapshot is not None else None
        requested = getattr(setup, "requested_client_logical", None) if setup else None
        if requested is not None and client is not None:
            want = f"{requested[0]:g}x{requested[1]:g}"
            if want != achieved:
                return f"{want} -> {achieved} clamped"
        return f"{achieved} {'canonical' if geometry.is_canonical else 'adopted'}"

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
        preflight = self._preflight()
        if snapshot is not None and snapshot.mode is RunMode.LIVE:
            live_head, live_detail = "Navigating", "movement is being sent"
        elif not preflight.ok:
            # A denied permission or a dead listener outranks every other
            # explanation: nothing downstream can work, and the fix is a
            # specific settings pane rather than anything in this window. Only
            # *faults* reach here - "not armed" and "Roblox is not focused" are
            # normal preconditions and are reported below in their own words.
            first = preflight.faults[0]
            live_head = f"Blocked - {len(preflight.faults)}"
            live_detail = f"{first.label}: {first.detail}"
        elif snapshot is None:
            live_head, live_detail = "Waiting", ""
        else:
            real = [b for b in snapshot.blockers if b.status != "expected"]
            if real:
                live_head = f"Blocked - {len(real)}"
                live_detail = f"{real[0].code}: {real[0].summary}"
            elif snapshot.arm_state not in ("none", "-"):
                live_head, live_detail = "Armed", f"focus Roblox, press {_CHORD_START}"
            else:
                live_head, live_detail = "Ready", f"press {_CHORD_START} in Roblox to navigate"
        self._set_summary("live", live_head, live_detail)
        Tooltip.retarget(self.summary_labels["live"], self._preflight_tooltip(preflight))

    #: The permission probes are cheap but not free, and nothing they report
    #: changes between two frames. Read at most this often.
    PREFLIGHT_INTERVAL_S = 1.0

    def _preflight(self) -> Any:
        now = monotonic_s()
        cached = self._preflight_cache
        if cached is None or now - self._preflight_at_s >= self.PREFLIGHT_INTERVAL_S:
            self._preflight_cache = self.app.preflight()
            self._preflight_at_s = now
        return self._preflight_cache

    @staticmethod
    def _preflight_tooltip(preflight: Any) -> str:
        """Every check and, for anything denied, the exact pane that fixes it."""
        lines = []
        for capability in preflight.capabilities:
            mark = {"ok": "OK", "denied": "NO", "unknown": "??", "n/a": "--"}[
                capability.state.value
            ]
            lines.append(f"[{mark}] {capability.label}: {capability.detail}")
            if capability.remedy:
                lines.append(f"      {capability.remedy}")
            if capability.settings_pane:
                lines.append(f"      {capability.settings_pane}")
        return "\n".join(lines)

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

    hotkeys = app.port.create_hotkey_source(
        submit_from_hotkey, on_edge=app.coordinator.note_hotkey_edge
    )
    # Held on the application so the dashboard can show whether it is running.
    # A listener that silently is not - the usual cause being Input Monitoring
    # not granted to whichever app launched this - is indistinguishable from a
    # chord that does nothing, and that is the whole confusion.
    app.hotkeys = hotkeys
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
