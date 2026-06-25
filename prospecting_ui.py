#!/usr/bin/env python3
"""
prospecting_ui.py -- a settings panel for the Prospecting macro (like an
auto-clicker UI). Edit timings, pick v1/v2, set how many digs fill the pan, etc.
It writes prospecting_config.json, which prospecting_old.py loads on startup.

Run:
    python3 prospecting_ui.py

Buttons:
    Save              -> write the settings to prospecting_config.json
    Save & Launch     -> save, then open Terminal running the macro
    Load v1 / Load v2 -> fill the dig fields with a preset (then Save)
    Reset defaults    -> restore every field to the built-in default
"""

import os
import json
import subprocess
import tkinter as tk
from tkinter import ttk, messagebox

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(HERE, "prospecting_config.json")
MACRO_FILE = os.path.join(HERE, "prospecting_old.py")

# ---- settings schema: (key, label, type, default) ; type in {int, bool} ------
# Defaults mirror prospecting_old.py. Grouped into sections for the form.
SECTIONS = [
    ("Mode / Dig", [
        ("PERFECT",            "Perfect dig (release on green) — off: timed hold", "bool", False),
        ("DIG_CLICK_MS",       "Dig hold length (ms)",                    "int", 75),
        ("MAX_DIGS_TO_FILL",   "Max digs to fill the pan",                "int", 8),
        ("DIG_FILL_MS",        "Wait for FULL after each dig (ms)",        "int", 250),
        ("PRE_DIG_SETTLE_MS",  "Settle before first dig after shake (ms)", "int", 60),
    ]),
    ("Walk back into water", [
        ("PAN_BACK_MAX_MS",    "Max S walk-back (ms)",                    "int", 200),
        ("WATER_EXTRA_BACK_MS","Extra S after Pan cue / deeper (ms)",     "int", 0),
    ]),
    ("Shake", [
        ("SHAKE_MOMENTUM_W",   "Hold W during shake (glide onto land)",   "bool", True),
        ("SHAKE_CLICK_MS",     "Each shake click length (ms)",            "int", 18),
        ("SHAKE_CLICK_GAP_MS", "Gap between shake clicks (ms)",           "int", 14),
        ("SHAKE_HOLD_MS",      "Shake overall timeout (ms)",              "int", 1500),
        ("SHAKE_BAIL_MS",      "Shake-failed detection (ms)",             "int", 500),
        ("SHAKE_START_DELAY_MS","Delay before shake starts (ms)",         "int", 0),
        ("POST_SHAKE_SETTLE_MS","Settle after pan empties (ms)",          "int", 150),
    ]),
    ("Return to land (dig-probe)", [
        ("DEPOSIT_MAX_MS",     "Max W to find land cue (ms)",             "int", 1200),
        ("LAND_SETTLE_MS",     "Hold W after land cue (ms)",              "int", 45),
        ("DIG_PROBE_MS",       "Wait to detect a probe-dig hit (ms)",     "int", 320),
        ("PROBE_GAP_MS",       "Settle between probe digs (ms)",          "int", 80),
        ("LAND_PROBE_NUDGE_MS","Forward nudge between probe digs (ms)",   "int", 90),
        ("LAND_DIG_TRIES",     "Probe digs before giving up",             "int", 5),
    ]),
    ("Recovery / safety", [
        ("STUCK_TICKS",        "Stuck reads before recovery",             "int", 3),
        ("RECOVER_LIMIT",      "Recoveries before break-out",             "int", 3),
        ("RECOVER_BACK_MS",    "Recovery nudge budget (ms)",              "int", 160),
        ("SHAKE_FAIL_LIMIT",   "Failed shakes before STOP",               "int", 5),
        ("BREAKOUT_LIMIT",     "Break-outs before STOP",                  "int", 2),
        ("BREAKOUT_SHAKE_MS",  "Break-out click-to-finish (ms)",          "int", 700),
        ("BREAKOUT_REPOS_MS",  "Break-out reposition W (ms)",             "int", 160),
    ]),
    ("Recovery movement (jitter taps)", [
        ("BURST_ON_MS",        "Tap hold per pulse (ms)",                 "int", 11),
        ("BURST_OFF_MS",       "Tap release per pulse (ms)",              "int", 1),
    ]),
]

# presets: which fields each "version" sets (the rest stay as-is)
PRESET_V1 = {"PERFECT": False, "DIG_CLICK_MS": 15, "MAX_DIGS_TO_FILL": 1}
PRESET_V2 = {"PERFECT": False, "DIG_CLICK_MS": 75, "MAX_DIGS_TO_FILL": 8}

DEFAULTS = {k: d for _, items in SECTIONS for (k, _l, _t, d) in items}
TYPES = {k: t for _, items in SECTIONS for (k, _l, t, _d) in items}


def load_saved():
    try:
        with open(CONFIG_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError, OSError):
        return {}


class App:
    def __init__(self, root):
        self.root = root
        root.title("Prospecting Macro — Settings")
        self.vars = {}
        saved = load_saved()

        # ---- scrollable body ----
        outer = ttk.Frame(root, padding=8)
        outer.pack(fill="both", expand=True)
        canvas = tk.Canvas(outer, highlightthickness=0, width=520, height=560)
        sb = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        body = ttk.Frame(canvas)
        body.bind("<Configure>",
                  lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=body, anchor="nw")
        canvas.configure(yscrollcommand=sb.set)
        canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        # trackpad / wheel scroll
        canvas.bind_all("<MouseWheel>",
                        lambda e: canvas.yview_scroll(int(-1 * (e.delta or 0) / 3), "units"))

        ttk.Label(body, text="Prospecting auto-pan settings",
                  font=("Helvetica", 15, "bold")).pack(anchor="w", pady=(0, 2))
        ttk.Label(body, foreground="#888",
                  text="Edit values, then Save. The macro reads these on start "
                       "(Ctrl+K).").pack(anchor="w", pady=(0, 8))

        # preset row
        pr = ttk.Frame(body); pr.pack(fill="x", pady=(0, 8))
        ttk.Label(pr, text="Preset:").pack(side="left")
        ttk.Button(pr, text="Load v1 (fast 1-dig)",
                   command=lambda: self.apply_preset(PRESET_V1)).pack(side="left", padx=4)
        ttk.Button(pr, text="Load v2 (multi-dig)",
                   command=lambda: self.apply_preset(PRESET_V2)).pack(side="left", padx=4)

        for title, items in SECTIONS:
            lf = ttk.LabelFrame(body, text=title, padding=8)
            lf.pack(fill="x", pady=5)
            for i, (key, label, typ, default) in enumerate(items):
                val = saved.get(key, default)
                if typ == "bool":
                    var = tk.BooleanVar(value=bool(val))
                    ttk.Checkbutton(lf, text=label, variable=var).grid(
                        row=i, column=0, columnspan=2, sticky="w", pady=2)
                else:
                    var = tk.StringVar(value=str(val))
                    ttk.Label(lf, text=label).grid(row=i, column=0, sticky="w", pady=2)
                    ttk.Entry(lf, textvariable=var, width=8).grid(
                        row=i, column=1, sticky="e", padx=(8, 0))
                lf.columnconfigure(0, weight=1)
                self.vars[key] = var

        # ---- buttons + status (fixed at bottom) ----
        bar = ttk.Frame(root, padding=(8, 6))
        bar.pack(fill="x")
        ttk.Button(bar, text="Save", command=self.save).pack(side="left")
        ttk.Button(bar, text="Save & Launch", command=self.save_launch).pack(side="left", padx=6)
        ttk.Button(bar, text="Reset defaults", command=self.reset).pack(side="left")
        self.status = ttk.Label(bar, text="", foreground="#2a8")
        self.status.pack(side="right")

    def apply_preset(self, preset):
        for k, v in preset.items():
            if k in self.vars:
                if TYPES[k] == "bool":
                    self.vars[k].set(bool(v))
                else:
                    self.vars[k].set(str(v))
        self.status.config(text="preset loaded — remember to Save")

    def reset(self):
        for k, var in self.vars.items():
            if TYPES[k] == "bool":
                var.set(bool(DEFAULTS[k]))
            else:
                var.set(str(DEFAULTS[k]))
        self.status.config(text="defaults restored — remember to Save")

    def collect(self):
        out = {}
        for k, var in self.vars.items():
            if TYPES[k] == "bool":
                out[k] = bool(var.get())
            else:
                raw = var.get().strip()
                try:
                    out[k] = int(raw)
                except ValueError:
                    raise ValueError(f"'{k}' must be a whole number (got '{raw}')")
        return out

    def save(self):
        try:
            data = self.collect()
        except ValueError as e:
            messagebox.showerror("Invalid value", str(e))
            return None
        with open(CONFIG_FILE, "w") as f:
            json.dump(data, f, indent=2)
        self.status.config(text=f"saved {len(data)} settings ✓")
        return data

    def save_launch(self):
        if self.save() is None:
            return
        # open Terminal running the macro (Terminal has the accessibility perms)
        cmd = f'cd {json.dumps(HERE)} && python3 {json.dumps(MACRO_FILE)}'
        script = f'tell application "Terminal" to do script "{cmd}"\n' \
                 'tell application "Terminal" to activate'
        try:
            subprocess.run(["osascript", "-e", script], check=True)
            self.status.config(text="launched in Terminal ✓  (Ctrl+K to start)")
        except Exception as e:
            messagebox.showinfo(
                "Saved",
                "Settings saved. Couldn't auto-launch Terminal "
                f"({e}).\nRun it yourself:\n  python3 prospecting_old.py")


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
