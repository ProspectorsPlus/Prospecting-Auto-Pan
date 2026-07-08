#!/usr/bin/env python3
"""Prospectors Plus — Fable  (desktop app)

The UI process: owns the window, spawns the engine subprocess, pumps its
'@@ {json}' protocol into a feed the page polls, and forwards settings
changes / calibration captures to the engine (the ENGINE owns the config
file while alive — single-writer, no races).

Run:  python3 app.py   (or double-click run.command)
"""
from __future__ import annotations

import collections
import json
import os
import subprocess
import sys
import threading
import time

APP_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, APP_DIR)

from engine import __version__ as ENGINE_VERSION            # noqa: E402
from engine import config as configmod                      # noqa: E402
from engine.telemetry import HistoryWriter                  # noqa: E402

PIXEL_FIELDS = ["CAP_LEFT_PIXEL", "CAP_FULL_PIXEL", "DEPOSIT_PIX",
                "PAN_PIX", "SHAKE_PIX", "DIG_TRIGGER_PIXEL"]
SETTINGS_SECTIONS = ["stats", "dig", "water", "shake", "land", "safety",
                     "adapt", "run", "relics", "sense", "keys"]


class EngineProc:
    """The engine subprocess + its protocol pump."""

    def __init__(self):
        self.proc = None
        self.lock = threading.Lock()
        self.seq = 0
        self.feed = collections.deque(maxlen=600)   # (seq, item)
        self.stats = {}
        self.phase = ""
        self.probe = None
        self.running = False
        self.paused = False
        self.pause_reason = ""
        self.hello = {}

    # ---------------------------------------------------------------- spawn --
    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    def spawn(self):
        if self.alive():
            return
        self.proc = subprocess.Popen(
            [sys.executable, os.path.join(APP_DIR, "run_engine.py")],
            cwd=APP_DIR, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, bufsize=1)
        threading.Thread(target=self._pump, daemon=True).start()

    def send(self, line):
        if not self.alive():
            return False
        try:
            self.proc.stdin.write(line.rstrip() + "\n")
            self.proc.stdin.flush()
            return True
        except Exception:
            return False

    def shutdown(self):
        try:
            self.send("quit")
            if self.proc:
                self.proc.wait(timeout=3)
        except Exception:
            try:
                self.proc.terminate()
            except Exception:
                pass

    # ----------------------------------------------------------------- pump --
    def _push(self, item):
        with self.lock:
            self.seq += 1
            self.feed.append((self.seq, item))

    def _pump(self):
        for line in self.proc.stdout:
            line = line.rstrip("\n")
            if not line:
                continue
            if not line.startswith("@@ "):
                if len(line) < 400:
                    self._push({"k": "log", "msg": line})
                continue
            try:
                obj = json.loads(line[3:])
            except Exception:
                continue
            t = obj.get("t")
            if t == "hello":
                self.hello = obj
            elif t == "stats":
                self.stats = obj.get("v") or {}
            elif t == "phase":
                self.phase = obj.get("name", "")
            elif t == "probe":
                self.probe = obj
            elif t == "state":
                self.running = bool(obj.get("running"))
                self.paused = bool(obj.get("paused"))
                self.pause_reason = obj.get("reason", "") if self.paused else ""
                self._push({"k": "state", "running": self.running,
                            "paused": self.paused,
                            "reason": obj.get("reason", "")})
            elif t == "event":
                self._push({"k": "event", "etype": obj.get("etype", ""),
                            "reason": obj.get("reason", "")})
            elif t == "log":
                self._push({"k": "log", "msg": obj.get("msg", "")})
        self._push({"k": "log", "msg": "engine process exited"})
        self.running = False


class Api:
    """The JS bridge (window.pywebview.api.*)."""

    def __init__(self, eng):
        self.eng = eng
        self.history = HistoryWriter(os.path.join(APP_DIR, "fable_history.json"))

    # ------------------------------------------------------------------ boot --
    def ui_boot(self):
        cfg = self._read_cfg()
        sections = []
        for sec in SETTINGS_SECTIONS:
            items = []
            for (key, s, typ, default, lo, hi, label, help_) in configmod.SPEC:
                if s != sec or typ == "pix":
                    continue
                items.append({"key": key, "type": typ, "default": default,
                              "lo": lo, "hi": hi, "label": label,
                              "help": help_, "value": cfg.get(key, default)})
            if items:
                sections.append({"id": sec,
                                 "label": configmod.SECTION_LABELS.get(sec, sec),
                                 "items": items})
        pixels = [{"key": k,
                   "label": dict((x[0], x[6]) for x in configmod.SPEC)[k],
                   "help": dict((x[0], x[7]) for x in configmod.SPEC)[k],
                   "value": cfg.get(k, configmod.DEFAULTS[k])}
                  for k in PIXEL_FIELDS]
        legacy = configmod.find_legacy(APP_DIR)
        return {"version": ENGINE_VERSION, "sections": sections,
                "pixels": pixels, "legacy_path": legacy or "",
                "config": cfg}

    def _read_cfg(self):
        try:
            with open(os.path.join(APP_DIR, configmod.CONFIG_NAME)) as f:
                return json.load(f)
        except Exception:
            return dict(configmod.DEFAULTS)

    # ---------------------------------------------------------------- engine --
    def engine_cmd(self, cmd):
        if cmd in ("start", "stop", "probe_on", "probe_off"):
            return {"ok": self.eng.send(cmd)}
        return {"ok": False}

    def engine_restart(self):
        self.eng.shutdown()
        time.sleep(0.4)
        self.eng.spawn()
        return {"ok": True}

    # -------------------------------------------------------------- settings --
    def set_setting(self, key, value):
        try:
            v = configmod._coerce(key, value)
        except Exception as e:
            return {"ok": False, "error": str(e)}
        if self.eng.alive():
            self.eng.send(json.dumps({"cmd": "set", "key": key, "value": v}))
        else:                                  # engine down -> write directly
            cfg = configmod.Config(self._read_cfg(),
                                   os.path.join(APP_DIR, configmod.CONFIG_NAME))
            cfg.set(key, v)
            cfg.save()
        return {"ok": True, "value": v}

    def capture_pixel(self, key, delay_ms):
        ok = self.eng.send(json.dumps({"cmd": "capture_pixel", "key": key,
                                       "delay_ms": int(delay_ms)}))
        return {"ok": ok}

    def import_legacy(self):
        legacy = configmod.find_legacy(APP_DIR)
        if not legacy:
            return {"ok": False, "error": "no legacy config found"}
        cfg = configmod.Config(self._read_cfg(),
                               os.path.join(APP_DIR, configmod.CONFIG_NAME))
        applied = configmod.import_legacy(cfg, legacy)
        cfg.save()
        if self.eng.alive():
            self.eng.send(json.dumps({"cmd": "reload"}))
        return {"ok": True, "applied": applied}

    # ------------------------------------------------------------------ feed --
    def get_feed(self, since):
        eng = self.eng
        with eng.lock:
            items = [{"seq": s, **it} for (s, it) in eng.feed if s > since]
        return {"seq": eng.seq, "items": items[-120:], "stats": eng.stats,
                "phase": eng.phase, "probe": eng.probe,
                "running": eng.running, "paused": eng.paused,
                "pause_reason": eng.pause_reason,
                "engine_alive": eng.alive()}

    def get_history(self):
        runs = self.history.read()
        return {"runs": list(reversed(runs[-60:]))}


# ===========================================================================
#  HTML / CSS / JS
# ===========================================================================
def build_html():
    return r"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
:root{
  --bg:#0e1116; --panel:#161b23; --panel2:#1c2330; --line:#2a3342;
  --tx:#e8ecf3; --dim:#8b96a8; --gold:#f2c14e; --gold2:#c99b2f;
  --green:#4cc98a; --red:#e2635f; --blue:#5aa7e8; --amber:#e8b45a;
  font-size:14px;
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--tx);font:14px -apple-system,'Segoe UI',sans-serif;
     -webkit-user-select:none;user-select:none;overflow:hidden}
#top{display:flex;align-items:center;gap:12px;padding:12px 16px;
     border-bottom:1px solid var(--line);background:var(--panel)}
#top h1{font-size:16px;font-weight:700;letter-spacing:.3px}
#top h1 em{color:var(--gold);font-style:normal}
#ver{color:var(--dim);font-size:11px;margin-top:1px}
#pill{margin-left:auto;padding:4px 12px;border-radius:20px;font-weight:600;
      font-size:12px;background:#2a3342;color:var(--dim)}
#pill.run{background:rgba(76,201,138,.15);color:var(--green)}
#pill.paused{background:rgba(232,180,90,.15);color:var(--amber)}
#bigbtn{padding:8px 22px;border:none;border-radius:8px;font-weight:700;
        font-size:14px;cursor:pointer;background:var(--gold);color:#20180a}
#bigbtn.stop{background:var(--red);color:#fff}
#hint{color:var(--dim);font-size:11px}
#tabs{display:flex;gap:4px;padding:8px 16px 0;background:var(--panel);
      border-bottom:1px solid var(--line)}
.tab{padding:8px 16px;border-radius:8px 8px 0 0;cursor:pointer;color:var(--dim);
     font-weight:600;font-size:13px}
.tab.on{background:var(--bg);color:var(--gold)}
#body{height:calc(100vh - 106px);overflow:auto;padding:16px}
.page{display:none}.page.on{display:block}
/* dashboard */
#cards{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;
      padding:12px 14px}
.card .v{font-size:24px;font-weight:800;margin-top:2px}
.card .l{color:var(--dim);font-size:11px;text-transform:uppercase;letter-spacing:.6px}
.card .s{color:var(--dim);font-size:11px;margin-top:2px}
#viz{display:flex;gap:10px;margin-top:10px}
#livebox{flex:1;background:var(--panel);border:1px solid var(--line);
         border-radius:10px;padding:12px 14px}
#phasebox{width:230px;background:var(--panel);border:1px solid var(--line);
          border-radius:10px;padding:12px 14px}
#capbar{height:22px;background:#242c3a;border-radius:6px;overflow:hidden;margin-top:8px}
#capfill{height:100%;width:0%;background:linear-gradient(90deg,var(--gold2),var(--gold));
         transition:width .12s linear}
#cues{display:flex;gap:14px;margin-top:10px}
.cue{display:flex;align-items:center;gap:6px;color:var(--dim);font-size:12px;font-weight:600}
.dot{width:10px;height:10px;border-radius:50%;background:#39445a}
.cue.on .dot{background:var(--green);box-shadow:0 0 8px var(--green)}
.cue.on{color:var(--tx)}
#phase{font-size:18px;font-weight:800;color:var(--gold);margin-top:6px;min-height:24px}
#pausenote{color:var(--amber);font-size:12px;margin-top:6px;display:none}
#saves{display:flex;gap:16px;margin-top:10px;flex-wrap:wrap}
.sv{color:var(--dim);font-size:12px}.sv b{color:var(--tx)}
#feed{margin-top:12px;background:var(--panel);border:1px solid var(--line);
      border-radius:10px;padding:8px 0;max-height:320px;overflow:auto}
.fi{padding:4px 14px;font-size:12px;color:var(--dim);border-left:3px solid transparent}
.fi b{color:var(--tx);font-weight:600}
.fi.event{border-left-color:var(--blue)}
.fi.warn{border-left-color:var(--amber)}
.fi.bad{border-left-color:var(--red)}
.fi.good{border-left-color:var(--green)}
/* settings */
.sec{background:var(--panel);border:1px solid var(--line);border-radius:10px;
     margin-bottom:10px;overflow:hidden}
.sec>h3{padding:10px 14px;font-size:13px;cursor:pointer;color:var(--gold);
        letter-spacing:.3px}
.sec .rows{display:none;border-top:1px solid var(--line)}
.sec.open .rows{display:block}
.row{display:flex;align-items:center;gap:10px;padding:9px 14px;
     border-bottom:1px solid rgba(42,51,66,.5)}
.row:last-child{border-bottom:none}
.row .lbl{width:290px;flex:none}
.row .lbl .h{color:var(--dim);font-size:11px;margin-top:2px;line-height:1.35}
.row input[type=number],.row input[type=text],.row textarea{
  background:var(--panel2);border:1px solid var(--line);color:var(--tx);
  padding:6px 9px;border-radius:6px;width:130px;font:13px inherit}
.row textarea{width:100%;min-height:60px;font-family:ui-monospace,monospace;font-size:12px}
.row input[type=checkbox]{width:18px;height:18px;accent-color:var(--gold)}
.saved{color:var(--green);font-size:11px;opacity:0;transition:opacity .3s}
.saved.show{opacity:1}
button.mini{background:var(--panel2);border:1px solid var(--line);color:var(--tx);
     padding:6px 12px;border-radius:6px;cursor:pointer;font-size:12px;font-weight:600}
button.mini:hover{border-color:var(--gold);color:var(--gold)}
/* calibrate */
#livecal{display:flex;gap:10px;margin-bottom:12px}
#detbox{flex:1;background:var(--panel);border:1px solid var(--line);
        border-radius:10px;padding:12px 14px}
.pxrow{display:flex;align-items:center;gap:10px;padding:9px 14px;
       border-bottom:1px solid rgba(42,51,66,.5)}
.pxrow .lbl{width:290px;flex:none}
.pxv{font-family:ui-monospace,monospace;color:var(--gold);width:110px}
.note{color:var(--dim);font-size:12px;line-height:1.5;margin:10px 2px}
/* history */
table{width:100%;border-collapse:collapse;background:var(--panel);
      border:1px solid var(--line);border-radius:10px;overflow:hidden}
th,td{padding:8px 12px;text-align:left;font-size:12px;border-bottom:1px solid rgba(42,51,66,.5)}
th{color:var(--dim);text-transform:uppercase;font-size:10px;letter-spacing:.6px}
td b{color:var(--gold)}
tr.det td{color:var(--dim);font-size:11px;background:var(--panel2)}
</style></head><body>
<div id="top">
  <div><h1>Prospectors <em>Plus</em> — Fable</h1><div id="ver"></div></div>
  <span id="pill">idle</span>
  <button id="bigbtn" onclick="toggleRun()">Start</button>
  <span id="hint"></span>
</div>
<div id="tabs">
  <div class="tab on" data-p="dash" onclick="tab('dash')">Dashboard</div>
  <div class="tab" data-p="set" onclick="tab('set')">Settings</div>
  <div class="tab" data-p="cal" onclick="tab('cal')">Calibrate</div>
  <div class="tab" data-p="hist" onclick="tab('hist')">History</div>
</div>
<div id="body">
  <div class="page on" id="p_dash">
    <div id="cards">
      <div class="card"><div class="l">Pans</div><div class="v" id="c_pans">0</div>
        <div class="s" id="c_digs">0 digs</div></div>
      <div class="card"><div class="l">Pans / hr</div><div class="v" id="c_phr">0</div>
        <div class="s" id="c_rt">0:00</div></div>
      <div class="card"><div class="l">Clean cycles</div><div class="v" id="c_clean">—</div>
        <div class="s">no retries / recoveries</div></div>
      <div class="card"><div class="l">Saves</div><div class="v" id="c_saves">0</div>
        <div class="s">micro-retries that saved a cycle</div></div>
    </div>
    <div id="viz">
      <div id="livebox">
        <div class="l" style="color:var(--dim);font-size:11px;text-transform:uppercase;
             letter-spacing:.6px">Live detection</div>
        <div id="capbar"><div id="capfill"></div></div>
        <div id="cues">
          <div class="cue" id="cue_dep"><div class="dot"></div>LAND</div>
          <div class="cue" id="cue_pan"><div class="dot"></div>WATER</div>
          <div class="cue" id="cue_shk"><div class="dot"></div>SHAKE</div>
          <div class="cue" id="cue_full"><div class="dot"></div>FULL</div>
          <span style="margin-left:auto;color:var(--dim);font-size:12px" id="fillpct"></span>
        </div>
      </div>
      <div id="phasebox">
        <div class="l" style="color:var(--dim);font-size:11px;text-transform:uppercase;
             letter-spacing:.6px">Phase</div>
        <div id="phase">—</div>
        <div id="pausenote"></div>
      </div>
    </div>
    <div id="saves">
      <span class="sv">shake retries <b id="s_ret">0</b></span>
      <span class="sv">shake fails <b id="s_sf">0</b></span>
      <span class="sv">nudges <b id="s_nud">0</b></span>
      <span class="sv">recoveries <b id="s_rec">0</b></span>
      <span class="sv">break-outs <b id="s_bo">0</b></span>
      <span class="sv">safe stops <b id="s_ss">0</b></span>
    </div>
    <div id="feed"></div>
  </div>
  <div class="page" id="p_set"></div>
  <div class="page" id="p_cal">
    <div id="livecal">
      <div id="detbox">
        <div class="l" style="color:var(--dim);font-size:11px;text-transform:uppercase;
             letter-spacing:.6px">Live test — stand in game so each prompt shows, watch the lights</div>
        <div id="capbar2" style="height:22px;background:#242c3a;border-radius:6px;
             overflow:hidden;margin-top:8px"><div id="capfill2" style="height:100%;width:0%;
             background:linear-gradient(90deg,var(--gold2),var(--gold))"></div></div>
        <div id="cues" style="display:flex;gap:14px;margin-top:10px">
          <div class="cue" id="cal_dep"><div class="dot"></div>LAND</div>
          <div class="cue" id="cal_pan"><div class="dot"></div>WATER</div>
          <div class="cue" id="cal_shk"><div class="dot"></div>SHAKE</div>
          <span style="margin-left:auto;color:var(--dim);font-size:12px" id="fillpct2"></span>
        </div>
      </div>
    </div>
    <div class="note">Pixels are <b>absolute screen coordinates</b>. To capture one:
      click <b>Capture</b>, then move your mouse over the target on screen —
      it saves after a 3-second countdown. Calibrate with Roblox open:
      the capacity bar tips with the bar <b>full</b> (gold), each prompt while
      it is <b>showing</b>.</div>
    <div class="sec open"><div class="rows" id="pxrows"></div></div>
    <div style="margin-top:10px"><button class="mini" onclick="importLegacy()">
      Import old calibration + settings</button>
      <span id="implabel" class="saved"></span></div>
  </div>
  <div class="page" id="p_hist"><div id="histbox"></div></div>
</div>
<script>
"use strict";
var seq = 0, running = false, booted = false, curTab = "dash";
var $ = function(id){ return document.getElementById(id); };

function api(){ return window.pywebview.api; }

function tab(p){
  curTab = p;
  var tabs = document.querySelectorAll(".tab");
  for (var i=0;i<tabs.length;i++) tabs[i].classList.toggle("on", tabs[i].dataset.p===p);
  var pages = document.querySelectorAll(".page");
  for (var j=0;j<pages.length;j++) pages[j].classList.remove("on");
  $("p_"+p).classList.add("on");
  if (p === "hist") loadHistory();
}

function toggleRun(){
  api().engine_cmd(running ? "stop" : "start");
}

/* ---------------- settings rendering ---------------- */
function inputFor(item){
  if (item.type === "bool"){
    return '<input type="checkbox" '+(item.value?"checked":"")+
      ' onchange="setVal(\''+item.key+'\', this.checked, this)">';
  }
  if (item.type === "int" || item.type === "float"){
    var step = item.type === "float" ? ' step="0.01"' : '';
    return '<input type="number" value="'+item.value+'"'+step+
      ' onchange="setVal(\''+item.key+'\', this.value, this)">';
  }
  var v = JSON.stringify(item.value);
  v = v.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/"/g,"&quot;");
  return '<textarea onchange="setJson(\''+item.key+'\', this)">'+v+'</textarea>';
}

function renderSettings(sections){
  var h = "";
  for (var i=0;i<sections.length;i++){
    var sec = sections[i];
    h += '<div class="sec'+(i<5?" open":"")+'"><h3 onclick="this.parentNode.classList.toggle(\'open\')">'+
         sec.label+'</h3><div class="rows">';
    for (var k=0;k<sec.items.length;k++){
      var it = sec.items[k];
      h += '<div class="row"><div class="lbl"><div>'+it.label+
           '</div><div class="h">'+it.help+'</div></div>'+inputFor(it)+
           '<span class="saved">saved</span></div>';
    }
    h += '</div></div>';
  }
  $("p_set").innerHTML = h;
}

function flashSaved(el){
  var s = el.parentNode.querySelector(".saved");
  if (!s) return;
  s.classList.add("show");
  setTimeout(function(){ s.classList.remove("show"); }, 1200);
}

function setVal(key, value, el){
  api().set_setting(key, value).then(function(r){
    if (r.ok){ if (el.type==="number") el.value = r.value; flashSaved(el); }
  });
}

function setJson(key, el){
  var v;
  try { v = JSON.parse(el.value); }
  catch(e){ el.style.borderColor = "var(--red)"; return; }
  el.style.borderColor = "var(--line)";
  api().set_setting(key, v).then(function(r){ if (r.ok) flashSaved(el); });
}

/* ---------------- calibrate ---------------- */
function renderPixels(pixels){
  var h = "";
  for (var i=0;i<pixels.length;i++){
    var p = pixels[i];
    h += '<div class="pxrow"><div class="lbl"><div>'+p.label+
         '</div><div class="h">'+p.help+'</div></div>'+
         '<span class="pxv" id="px_'+p.key+'">('+p.value[0]+', '+p.value[1]+')</span>'+
         '<button class="mini" onclick="capture(\''+p.key+'\', this)">Capture (3s)</button>'+
         '<span class="saved">saved</span></div>';
  }
  $("pxrows").innerHTML = h;
}

function capture(key, btn){
  btn.disabled = true;
  var n = 3;
  var tick = setInterval(function(){
    btn.textContent = "hover target… " + n;
    n -= 1;
    if (n < 0){
      clearInterval(tick);
      btn.textContent = "Capture (3s)";
      btn.disabled = false;
    }
  }, 1000);
  api().capture_pixel(key, 3000);
}

function importLegacy(){
  api().import_legacy().then(function(r){
    var el = $("implabel");
    el.textContent = r.ok ? ("imported " + r.applied.length + " values") :
                            ("failed: " + r.error);
    el.classList.add("show");
    setTimeout(function(){ el.classList.remove("show"); }, 4000);
    boot(true);
  });
}

/* ---------------- history ---------------- */
function fmtDur(s){
  s = Math.max(0, s|0);
  var m = (s/60)|0, h = (m/60)|0;
  if (h) return h + "h " + (m%60) + "m";
  return m + "m " + (s%60) + "s";
}

function loadHistory(){
  api().get_history().then(function(r){
    if (!r.runs.length){
      $("histbox").innerHTML = '<div class="note">No runs recorded yet.</div>';
      return;
    }
    var h = "<table><tr><th>Ended</th><th>Runtime</th><th>Pans</th>"+
            "<th>Pans/hr</th><th>Clean %</th><th>Stop reason</th></tr>";
    for (var i=0;i<r.runs.length;i++){
      var u = r.runs[i];
      var ev = [];
      var rc = u.reason_counts || {};
      for (var k in rc) ev.push(rc[k] + "× " + k);
      h += "<tr><td>"+(u.ended||"?")+"</td><td>"+fmtDur(u.runtime_s||0)+
           "</td><td><b>"+(u.cycles||0)+"</b></td><td>"+(u.pans_per_hr||0)+
           "</td><td>"+(u.clean_pct!=null?u.clean_pct:"—")+
           "</td><td>"+(u.stop_reason||"")+"</td></tr>";
      if (ev.length){
        h += '<tr class="det"><td colspan="6">'+ev.join(" &nbsp;·&nbsp; ")+"</td></tr>";
      }
    }
    $("histbox").innerHTML = h + "</table>";
  });
}

/* ---------------- live feed ---------------- */
function cue(id, on){ $(id).classList.toggle("on", !!on); }

function feedClass(it){
  if (it.k !== "event") return "";
  var e = it.etype;
  if (e === "hard_stop" || e === "error") return " bad";
  if (e === "safe_stop" || e === "shake_fail" || e === "no_progress" ||
      e === "shake_glitch" || e === "break_out") return " warn";
  if (e === "relic" || e === "calib" || e === "retry") return " good";
  return " event";
}

function applyFeed(r){
  running = r.running;
  var pill = $("pill"), btn = $("bigbtn");
  if (r.paused){
    pill.textContent = "paused — retrying";
    pill.className = "paused";
    btn.textContent = "Stop"; btn.className = "stop";
    $("pausenote").style.display = "block";
    $("pausenote").textContent = r.pause_reason;
  } else if (running){
    pill.textContent = "running";
    pill.className = "run";
    btn.textContent = "Stop"; btn.className = "stop";
    $("pausenote").style.display = "none";
  } else {
    pill.textContent = r.engine_alive ? "idle" : "engine down";
    pill.className = "";
    btn.textContent = "Start"; btn.className = "";
    $("pausenote").style.display = "none";
  }
  var st = r.stats || {};
  $("c_pans").textContent = st.cycles || 0;
  $("c_digs").textContent = (st.digs || 0) + " digs";
  $("c_phr").textContent = st.pans_per_hr || 0;
  $("c_rt").textContent = fmtDur(st.runtime_s || 0);
  $("c_clean").textContent = st.cycles ? (st.clean_pct + "%") : "—";
  $("c_saves").textContent = st.shake_retries || 0;
  $("s_ret").textContent = st.shake_retries || 0;
  $("s_sf").textContent = st.shake_fails || 0;
  $("s_nud").textContent = st.nudges || 0;
  $("s_rec").textContent = st.recoveries || 0;
  $("s_bo").textContent = st.breakouts || 0;
  $("s_ss").textContent = st.safe_stops || 0;
  $("phase").textContent = r.phase ? r.phase.replace("_", " ") : "—";
  var p = r.probe;
  if (p){
    var pct = Math.round(p.fill * 100) + "%";
    $("capfill").style.width = pct;  $("fillpct").textContent = pct;
    $("capfill2").style.width = pct; $("fillpct2").textContent = pct;
    cue("cue_dep", p.dep); cue("cue_pan", p.pan); cue("cue_shk", p.shk);
    cue("cue_full", p.full);
    cue("cal_dep", p.dep); cue("cal_pan", p.pan); cue("cal_shk", p.shk);
  }
  if (r.items.length){
    var fd = $("feed");
    for (var i=0;i<r.items.length;i++){
      var it = r.items[i];
      var d = document.createElement("div");
      d.className = "fi" + feedClass(it);
      if (it.k === "event"){
        d.innerHTML = "<b>" + it.etype + "</b> — " + (it.reason || "");
      } else if (it.k === "state"){
        d.innerHTML = "<b>" + (it.running ? "RUNNING" : (it.paused ?
          "PAUSED (auto-retry)" : "STOPPED")) + "</b>" +
          (it.reason ? " — " + it.reason : "");
      } else {
        d.textContent = it.msg;
      }
      fd.insertBefore(d, fd.firstChild);
    }
    while (fd.children.length > 250) fd.removeChild(fd.lastChild);
  }
}

function poll(){
  if (!window.pywebview){ setTimeout(poll, 200); return; }
  api().get_feed(seq).then(function(r){
    seq = r.seq;
    applyFeed(r);
    setTimeout(poll, 400);
  }).catch(function(){ setTimeout(poll, 800); });
}

/* ---------------- boot ---------------- */
function chord(c){
  if (!c) return "";
  var s = [];
  if (c.ctrl) s.push("Ctrl");
  if (c.alt) s.push("Alt");
  if (c.shift) s.push("Shift");
  s.push((c.code || "").replace("Key", ""));
  return s.join("+");
}

function boot(again){
  if (!window.pywebview){ setTimeout(function(){ boot(again); }, 150); return; }
  api().ui_boot().then(function(r){
    $("ver").textContent = "v" + r.version;
    renderSettings(r.sections);
    renderPixels(r.pixels);
    $("hint").textContent = chord(r.config.HOTKEY_TOGGLE) + " start/stop · " +
                            chord(r.config.HOTKEY_SOFTSTOP) + " soft-stop";
    if (!booted){
      booted = true;
      api().engine_cmd("probe_on");
      poll();
    }
  });
}
window.addEventListener("pywebviewready", function(){ boot(false); });
setTimeout(function(){ boot(false); }, 700);
</script></body></html>"""


def main():
    import webview
    eng = EngineProc()
    eng.spawn()
    api = Api(eng)
    win = webview.create_window(
        "Prospectors Plus — Fable", html=build_html(), js_api=api,
        width=1060, height=760, min_size=(880, 600),
        background_color="#0e1116")

    def on_closed():
        eng.shutdown()

    win.events.closed += on_closed
    webview.start()


if __name__ == "__main__":
    main()
