#!/usr/bin/env python3
"""
prospecting_ui.py -- settings panel for the Prospecting macro, served in your
BROWSER (no tkinter needed -- works on any Python 3). Edit timings, pick v1/v2,
set how many digs fill the pan, etc. Writes prospecting_config.json, which
prospecting_old.py loads on startup.

Run:
    python3 prospecting_ui.py          (or:  python3 prospecting_macro.py ui)

It starts a tiny local web server and opens the page automatically. Edit fields,
click Save (the macro reads them next time it starts). "Save & Launch" also opens
Terminal running the macro. Close the terminal (Ctrl+C) when done.
"""

import os
import json
import shlex
import socket
import subprocess
import threading
import webbrowser
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(HERE, "prospecting_config.json")
MACRO_FILE = os.path.join(HERE, "prospecting_old.py")

# ---- settings schema: (key, label, type, default) ; type in {int, bool} ------
SECTIONS = [
    ("Mode / Dig", [
        ("PERFECT",            "Perfect dig (release on green) — off = timed hold", "bool", False),
        ("DIG_CLICK_MS",       "Dig hold length (ms)",                    "int", 75),
        ("MAX_DIGS_TO_FILL",   "Max digs to fill the pan",                "int", 8),
        ("DIG_FILL_MS",        "Wait for FULL after each dig (ms)",        "int", 250),
        ("PRE_DIG_SETTLE_MS",  "Settle before first dig after shake (ms)", "int", 60),
    ]),
    ("Walk back into water", [
        ("PAN_BACK_MAX_MS",    "Max S walk-back (ms)",                    "int", 200),
        ("WATER_EXTRA_BACK_MS","Extra S after Pan cue / go deeper (ms)",  "int", 0),
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
        ("LAND_PROBE_NUDGE_MS","Forward W nudge between probe digs (ms)", "int", 90),
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
        ("BURST_OFF_MS",       "Tap release per pulse (ms)",             "int", 1),
    ]),
]

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


SECTION_HINT = {
    "Mode / Dig": "How each dig works and how many it takes to fill the pan.",
    "Walk back into water": "Getting from land into the water to shake.",
    "Shake": "Emptying the pan; momentum carries you back to land.",
    "Return to land (dig-probe)": "Finding land after a shake by test-digging.",
    "Recovery / safety": "What happens when something goes wrong.",
    "Recovery movement (jitter taps)": "Tiny tap timing used only during recovery.",
}


def render(msg=""):
    saved = load_saved()
    cards = []
    for title, items in SECTIONS:
        rows = []
        for key, label, typ, default in items:
            val = saved.get(key, default)
            if typ == "bool":
                checked = "checked" if val else ""
                control = (f'<span class="switch"><input type="checkbox" name="{key}" '
                           f'data-type="bool" {checked}>'
                           f'<span class="track"><span class="knob"></span></span></span>')
            else:
                control = (f'<input type="number" name="{key}" data-type="int" '
                           f'value="{val}">')
            rows.append(f'<label class="row"><span class="lbl">{label}</span>'
                        f'{control}</label>')
        hint = SECTION_HINT.get(title, "")
        cards.append(
            f'<section class="card"><div class="chead"><h2>{title}</h2>'
            f'<p class="chint">{hint}</p></div>'
            f'<div class="rows">{"".join(rows)}</div></section>')
    banner = f'<div class="ok">{msg}</div>' if msg else ""
    return PAGE.replace("{{CARDS}}", "".join(cards)).replace("{{MSG}}", banner) \
        .replace("{{DEFAULTS}}", json.dumps(DEFAULTS)) \
        .replace("{{V1}}", json.dumps(PRESET_V1)) \
        .replace("{{V2}}", json.dumps(PRESET_V2))


PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Prospecting Macro — Settings</title>
<style>
 :root{--bg:#0f1115;--card:#171a21;--card2:#1c2029;--line:#262b35;--txt:#e8eaed;
   --mut:#8b94a3;--accent:#3b82f6;--accent2:#10b981;--field:#0c0e12}
 *{box-sizing:border-box}
 body{background:var(--bg);color:var(--txt);font:14px/1.45 -apple-system,BlinkMacSystemFont,
   "Segoe UI",Helvetica,Arial,sans-serif;margin:0}
 .topbar{position:sticky;top:0;z-index:10;background:rgba(15,17,21,.92);
   backdrop-filter:blur(8px);border-bottom:1px solid var(--line);
   padding:14px 22px;display:flex;align-items:center;gap:14px}
 .brand{font-size:17px;font-weight:700;letter-spacing:.2px}
 .brand b{color:var(--accent2)} .grow{flex:1}
 button{font:inherit;font-weight:600;border:0;border-radius:9px;padding:9px 15px;
   cursor:pointer;transition:transform .04s,filter .15s}
 button:active{transform:translateY(1px)}
 .btn{background:var(--accent);color:#fff} .btn:hover{filter:brightness(1.08)}
 .btn2{background:#2a3340;color:#dfe5ee} .btn2:hover{background:#33404f}
 .chip{background:#20262f;color:#cdd5e0;border:1px solid var(--line);
   border-radius:999px;padding:7px 13px;font-size:13px}
 .chip:hover{border-color:#3a4757;background:#262d38}
 .wrap{max-width:660px;margin:0 auto;padding:18px 22px 90px}
 .lead{color:var(--mut);margin:2px 0 16px}
 .presets{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:0 0 18px}
 .presets .plabel{color:var(--mut);margin-right:2px}
 .card{background:var(--card);border:1px solid var(--line);border-radius:14px;
   margin:0 0 16px;overflow:hidden}
 .chead{padding:13px 16px 11px;background:var(--card2);border-bottom:1px solid var(--line)}
 .chead h2{margin:0;font-size:14.5px;color:#cfe9df;letter-spacing:.2px}
 .chint{margin:3px 0 0;color:var(--mut);font-size:12.5px}
 .rows{padding:4px 16px}
 .row{display:flex;align-items:center;gap:14px;padding:9px 0;
   border-bottom:1px solid #20242d}
 .rows .row:last-child{border-bottom:0}
 .lbl{flex:1;color:#d7dce4}
 input[type=number]{width:96px;background:var(--field);color:#fff;
   border:1px solid #2c333f;border-radius:8px;padding:8px 10px;text-align:right;
   font-variant-numeric:tabular-nums}
 input[type=number]:focus{outline:0;border-color:var(--accent);
   box-shadow:0 0 0 3px rgba(59,130,246,.25)}
 .switch{position:relative;display:inline-flex} .switch input{display:none}
 .track{width:46px;height:26px;background:#39414e;border-radius:999px;position:relative;
   transition:background .15s;cursor:pointer}
 .knob{position:absolute;top:3px;left:3px;width:20px;height:20px;background:#fff;
   border-radius:50%;transition:left .15s}
 .switch input:checked + .track{background:var(--accent2)}
 .switch input:checked + .track .knob{left:23px}
 .ok{background:#10301f;color:#7fe6b5;border:1px solid #1f6b4a;border-radius:10px;
   padding:10px 13px;margin:0 0 16px;font-size:13px}
 .foot{color:var(--mut);font-size:12.5px;text-align:center;margin-top:6px}
</style></head><body>
<form method="POST" action="/save" id="f">
 <div class="topbar">
   <div class="brand">⛏ Prospecting <b>Macro</b></div>
   <div class="grow"></div>
   <button class="btn2" type="submit" formaction="/launch">Save &amp; Launch</button>
   <button class="btn" type="submit" formaction="/save">Save</button>
 </div>
 <div class="wrap">
   <p class="lead">Tune timings and behaviour. Click <b>Save</b>; the macro loads these
     each time it starts (Ctrl+K). No file editing needed.</p>
   {{MSG}}
   <div class="presets">
     <span class="plabel">Preset:</span>
     <button type="button" class="chip" onclick="preset(V1)">v1 · fast 1-dig</button>
     <button type="button" class="chip" onclick="preset(V2)">v2 · multi-dig</button>
     <button type="button" class="chip" onclick="preset(DEF)">Reset defaults</button>
   </div>
   {{CARDS}}
   <p class="foot">Changes apply on the macro's next start.</p>
 </div>
</form>
<script>
 const DEF={{DEFAULTS}},V1={{V1}},V2={{V2}};
 function preset(p){for(const k in p){const el=document.querySelector('[name="'+k+'"]');
   if(!el)continue; if(el.dataset.type==='bool')el.checked=!!p[k]; else el.value=p[k];}}
</script>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, html, code=200):
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html.encode("utf-8"))

    def log_message(self, *a):
        pass  # quiet

    def do_GET(self):
        self._send(render())

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(n).decode("utf-8")
        form = urllib.parse.parse_qs(raw, keep_blank_values=True)
        data = {}
        for key, typ in TYPES.items():
            if typ == "bool":
                data[key] = key in form          # checkbox only sent when checked
            else:
                try:
                    data[key] = int(form.get(key, [DEFAULTS[key]])[0])
                except (ValueError, IndexError):
                    data[key] = DEFAULTS[key]
        with open(CONFIG_FILE, "w") as f:
            json.dump(data, f, indent=2)
        msg = f"Saved {len(data)} settings ✓"
        if self.path == "/launch":
            try:
                launch_macro()
                msg += " — launched in Terminal (press Ctrl+K to start)"
            except Exception as e:
                msg += f" — couldn't auto-launch ({e}); run python3 prospecting_old.py"
        self._send(render(msg))


def launch_macro():
    """Open the macro in Terminal WITHOUT needing AppleScript automation perms:
    write a .command file and `open` it (double-click-equivalent)."""
    launcher = os.path.join(HERE, "_run_macro.command")
    with open(launcher, "w") as f:
        f.write("#!/bin/bash\n"
                f"cd {shlex.quote(HERE)}\n"
                f"exec python3 {shlex.quote(MACRO_FILE)}\n")
    os.chmod(launcher, 0o755)
    subprocess.run(["open", launcher], check=True)


# Chromium-family browsers can open a chromeless "app" window with --app=URL,
# so the settings panel looks like a standalone popup, not a browser tab.
APP_BROWSERS = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
]


def open_window(url):
    """Open as an app-style popup window if a Chromium browser exists; else fall
    back to a normal browser tab."""
    for path in APP_BROWSERS:
        if os.path.exists(path):
            try:
                subprocess.Popen([path, f"--app={url}", "--window-size=780,940"])
                return "app window"
            except Exception:
                pass
    webbrowser.open(url)
    return "browser tab"


def free_port(start=8765):
    for p in range(start, start + 20):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", p)); return p
            except OSError:
                continue
    return start


if __name__ == "__main__":
    port = free_port()
    url = f"http://127.0.0.1:{port}/"
    srv = HTTPServer(("127.0.0.1", port), Handler)
    threading.Timer(0.5, lambda: print(f"Opened as {open_window(url)}.")).start()
    print(f"Prospecting settings UI -> {url}\nClose this window / Ctrl+C when done.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nUI closed.")
