#!/usr/bin/env python3
"""
prospecting_app.py -- a REAL native desktop app (its own window, not a browser
tab) for the Prospecting macro. Built on pywebview, which renders an HTML UI in a
native macOS window using your existing Python -- no Node/Electron needed.

It has tabbed settings, Start/Stop for the macro, and a live trace console, all
in one window. Settings are saved to prospecting_config.json (same file the macro
reads).

First time:
    pip3 install pywebview --break-system-packages
Then run (or double-click "Prospecting Macro.app"):
    python3 prospecting_app.py

If pywebview isn't installed, this falls back to the browser settings UI so it
still works.
"""

import os
import sys
import json
import signal
import threading
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(HERE, "prospecting_config.json")
MACRO_FILE = os.path.join(HERE, "prospecting_old.py")

# reuse the settings schema from the browser UI so they never drift apart
try:
    from prospecting_ui import SECTIONS, DEFAULTS, TYPES, PRESET_V1, PRESET_V2, \
        SECTION_HINT, TAB_ICON
except Exception:
    SECTIONS = []
    DEFAULTS = TYPES = {}
    PRESET_V1 = PRESET_V2 = {}
    SECTION_HINT = TAB_ICON = {}


def load_saved():
    try:
        with open(CONFIG_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError, OSError):
        return {}


# ============================================================================
# JS <-> Python bridge
# ============================================================================
class Api:
    def __init__(self):
        self.proc = None

    def get_state(self):
        saved = load_saved()
        merged = dict(DEFAULTS)
        merged.update({k: v for k, v in saved.items() if k in DEFAULTS})
        return {"values": merged, "running": self.proc is not None,
                "v1": PRESET_V1, "v2": PRESET_V2, "defaults": DEFAULTS}

    def save_config(self, data):
        clean = {}
        for k, t in TYPES.items():
            if k not in data:
                continue
            clean[k] = bool(data[k]) if t == "bool" else int(data[k])
        with open(CONFIG_FILE, "w") as f:
            json.dump(clean, f, indent=2)
        return len(clean)

    def launch(self, data=None):
        if data is not None:
            self.save_config(data)
        if self.proc is not None:
            return "already running"
        py = sys.executable or "python3"
        self.proc = subprocess.Popen(
            [py, MACRO_FILE], cwd=HERE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, bufsize=1)
        threading.Thread(target=self._pump, args=(self.proc,), daemon=True).start()
        return "launched"

    def stop(self):
        if self.proc is not None:
            try:
                self.proc.send_signal(signal.SIGINT)
            except Exception:
                pass
            try:
                self.proc.terminate()
            except Exception:
                pass
            self.proc = None
        return "stopped"

    def _pump(self, proc):
        for line in iter(proc.stdout.readline, ""):
            _emit_log(line.rstrip("\n"))
        rc = proc.wait()
        _emit_log(f"[macro exited, code {rc}]")
        self.proc = None
        _emit_state(False)


_window = None


def _emit_log(text):
    if _window is None:
        return
    try:
        _window.evaluate_js(f"window.addLog && addLog({json.dumps(text)})")
    except Exception:
        pass


def _emit_state(running):
    if _window is None:
        return
    try:
        _window.evaluate_js(f"window.setRunning && setRunning({json.dumps(running)})")
    except Exception:
        pass


# ============================================================================
# HTML (native window content)
# ============================================================================
def build_html():
    navs, panels = [], []
    # Run/console tab first
    navs.append('<button type="button" class="tab active" data-tab="run">'
                '<span class="ti">▶</span><span>Run</span></button>')
    panels.append(
        '<section class="panel active" id="prun">'
        '<div class="phead"><h2>Run</h2>'
        '<p class="chint">Start the macro, then tab into Roblox. Ctrl+K also '
        'starts/stops; Esc quits it.</p></div>'
        '<div class="runbtns">'
        '<button type="button" id="startbtn" class="big go">Start macro</button>'
        '<button type="button" id="stopbtn" class="big stop" disabled>Stop</button>'
        '<span id="rstate" class="rstate">stopped</span></div>'
        '<pre id="log" class="log"></pre></section>')
    for title, items in SECTIONS:
        icon = TAB_ICON.get(title, "•")
        navs.append(f'<button type="button" class="tab" data-tab="{title}">'
                    f'<span class="ti">{icon}</span><span>{title}</span></button>')
        rows = []
        for key, label, typ, default in items:
            if typ == "bool":
                rows.append(
                    f'<label class="row"><span class="lbl">{label}</span>'
                    f'<span class="switch"><input type="checkbox" data-key="{key}" '
                    f'data-type="bool"><span class="track"><span class="knob">'
                    f'</span></span></span></label>')
            else:
                rows.append(
                    f'<label class="row"><span class="lbl">{label}</span>'
                    f'<input type="number" data-key="{key}" data-type="int"></label>')
        hint = SECTION_HINT.get(title, "")
        panels.append(
            f'<section class="panel" id="p_{title}">'
            f'<div class="phead"><h2>{title}</h2><p class="chint">{hint}</p></div>'
            f'<div class="rows">{"".join(rows)}</div></section>')
    return HTML.replace("{{NAV}}", "".join(navs)).replace("{{PANELS}}", "".join(panels))


HTML = """<!doctype html><html><head><meta charset="utf-8"><style>
 :root{--bg:#0f1115;--panel:#171a21;--head:#1c2029;--line:#262b35;--txt:#e8eaed;
  --mut:#8b94a3;--accent:#3b82f6;--accent2:#10b981;--field:#0c0e12;--nav:#13161c}
 *{box-sizing:border-box} html,body{height:100%;margin:0}
 body{background:var(--bg);color:var(--txt);font:14px/1.45 -apple-system,
  BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;display:flex;flex-direction:column}
 .topbar{flex:0 0 auto;background:#12151b;border-bottom:1px solid var(--line);
  padding:12px 18px;display:flex;align-items:center;gap:12px}
 .brand{font-size:16px;font-weight:700} .brand b{color:var(--accent2)} .grow{flex:1}
 button{font:inherit;font-weight:600;border:0;border-radius:9px;padding:9px 15px;cursor:pointer}
 .btn{background:var(--accent);color:#fff} .btn:hover{filter:brightness(1.08)}
 .body{flex:1;display:flex;min-height:0}
 .side{flex:0 0 196px;background:var(--nav);border-right:1px solid var(--line);
  padding:12px 10px;overflow-y:auto}
 .tab{display:flex;align-items:center;gap:10px;width:100%;text-align:left;
  background:transparent;color:#b9c1cd;border-radius:9px;padding:10px 11px;margin-bottom:3px}
 .tab .ti{width:18px;text-align:center;opacity:.85}
 .tab:hover{background:#1d222b;color:#fff} .tab.active{background:#223049;color:#fff}
 .navsep{height:1px;background:var(--line);margin:10px 4px}
 .pretitle{color:var(--mut);font-size:11px;text-transform:uppercase;letter-spacing:.6px;margin:4px 8px 6px}
 .chip{display:block;width:100%;text-align:left;background:#1b212b;color:#cdd5e0;
  border:1px solid var(--line);border-radius:8px;padding:8px 11px;margin-bottom:6px;font-size:13px}
 .chip:hover{background:#222a35}
 .content{flex:1;overflow-y:auto;padding:18px 22px}
 .panel{display:none} .panel.active{display:block}
 .phead{margin:0 0 12px} .phead h2{margin:0;font-size:18px} .chint{margin:3px 0 0;color:var(--mut)}
 .rows{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:4px 16px;max-width:560px}
 .row{display:flex;align-items:center;gap:14px;padding:12px 0;border-bottom:1px solid #20242d}
 .rows .row:last-child{border-bottom:0} .lbl{flex:1;color:#d7dce4}
 input[type=number]{width:104px;background:var(--field);color:#fff;border:1px solid #2c333f;
  border-radius:8px;padding:9px 11px;text-align:right}
 input[type=number]:focus{outline:0;border-color:var(--accent);box-shadow:0 0 0 3px rgba(59,130,246,.25)}
 .switch{position:relative;display:inline-flex} .switch input{display:none}
 .track{width:46px;height:26px;background:#39414e;border-radius:999px;position:relative;cursor:pointer}
 .knob{position:absolute;top:3px;left:3px;width:20px;height:20px;background:#fff;border-radius:50%;transition:left .15s}
 .switch input:checked + .track{background:var(--accent2)}
 .switch input:checked + .track .knob{left:23px}
 .runbtns{display:flex;align-items:center;gap:10px;margin-bottom:14px}
 .big{padding:11px 20px;font-size:15px} .go{background:var(--accent2);color:#06281c}
 .stop{background:#3a2330;color:#ffb4b4} .stop:disabled,.go:disabled{opacity:.5;cursor:default}
 .rstate{color:var(--mut);margin-left:6px}
 .log{background:#0a0c10;border:1px solid var(--line);border-radius:12px;padding:12px 14px;
  height:calc(100vh - 240px);overflow-y:auto;white-space:pre-wrap;font:12px ui-monospace,
  Menlo,monospace;color:#bfe3d2;margin:0}
 .ok{position:fixed;right:16px;bottom:14px;background:#10301f;color:#7fe6b5;
  border:1px solid #1f6b4a;border-radius:9px;padding:8px 12px;font-size:13px;opacity:0;
  transition:opacity .2s} .ok.show{opacity:1}
</style></head><body>
 <div class="topbar">
   <div class="brand">⛏ Prospecting <b>Macro</b></div>
   <div class="grow"></div>
   <button class="btn" id="savebtn">Save settings</button>
 </div>
 <div class="body">
   <nav class="side">
     {{NAV}}
     <div class="navsep"></div>
     <div class="pretitle">Presets</div>
     <button type="button" class="chip" id="pv1">v1 · fast 1-dig</button>
     <button type="button" class="chip" id="pv2">v2 · multi-dig</button>
     <button type="button" class="chip" id="pdef">Reset defaults</button>
   </nav>
   <div class="content">{{PANELS}}</div>
 </div>
 <div class="ok" id="toast"></div>
<script>
 let DEF={},V1={},V2={};
 const $=s=>document.querySelector(s), $$=s=>document.querySelectorAll(s);
 function fields(){return $$('[data-key]');}
 function setVals(v){fields().forEach(el=>{const k=el.dataset.key;
   if(el.dataset.type==='bool')el.checked=!!v[k]; else el.value=(v[k]??'');});}
 function collect(){const o={};fields().forEach(el=>{const k=el.dataset.key;
   o[k]=el.dataset.type==='bool'?el.checked:parseInt(el.value||'0',10);});return o;}
 function preset(p){fields().forEach(el=>{const k=el.dataset.key; if(!(k in p))return;
   if(el.dataset.type==='bool')el.checked=!!p[k]; else el.value=p[k];});}
 function toast(t){const e=$('#toast');e.textContent=t;e.classList.add('show');
   clearTimeout(window._tt);window._tt=setTimeout(()=>e.classList.remove('show'),1800);}
 window.addLog=function(t){const l=$('#log');l.textContent+=t+"\\n";l.scrollTop=l.scrollHeight;};
 window.setRunning=function(r){$('#startbtn').disabled=r;$('#stopbtn').disabled=!r;
   $('#rstate').textContent=r?'running':'stopped';};
 $$('.tab').forEach(b=>b.onclick=()=>{$$('.tab').forEach(x=>x.classList.remove('active'));
   $$('.panel').forEach(x=>x.classList.remove('active'));b.classList.add('active');
   document.getElementById('p'+(b.dataset.tab==='run'?'run':'_'+b.dataset.tab)).classList.add('active');});
 $('#pv1').onclick=()=>preset(V1); $('#pv2').onclick=()=>preset(V2); $('#pdef').onclick=()=>preset(DEF);
 $('#savebtn').onclick=async()=>{const n=await window.pywebview.api.save_config(collect());toast('Saved '+n+' settings');};
 $('#startbtn').onclick=async()=>{await window.pywebview.api.launch(collect());setRunning(true);toast('Macro launched — Ctrl+K to start');};
 $('#stopbtn').onclick=async()=>{await window.pywebview.api.stop();setRunning(false);};
 async function init(){const s=await window.pywebview.api.get_state();
   DEF=s.defaults;V1=s.v1;V2=s.v2;setVals(s.values);setRunning(s.running);}
 window.addEventListener('pywebviewready',init);
 if(window.pywebview&&window.pywebview.api)init();
</script></body></html>"""


def main():
    global _window
    try:
        import webview
    except ImportError:
        # pywebview not installed -> fall back to the browser settings UI
        print("pywebview not installed; opening the browser settings UI instead.\n"
              "For the native app:  pip3 install pywebview --break-system-packages")
        import runpy
        runpy.run_path(os.path.join(HERE, "prospecting_ui.py"), run_name="__main__")
        return
    api = Api()
    _window = webview.create_window("Prospecting Macro", html=build_html(),
                                    js_api=api, width=940, height=860,
                                    min_size=(820, 640))
    webview.start()


if __name__ == "__main__":
    main()
