#!/usr/bin/env python3
"""Drive Prospector Lite's REAL app host against the REAL engine in both
ENGINE_IPC flag states (Phase 04 C3 verification; house style of
studio_tests.py).

What this drives: the actual prospecting_app.py Api -- its real launch()
spawn path, the real pump (legacy _pump / ipc EngineClient), the real
Pause/relic/Stop buttons -- against the real prospecting_old.py engine
process. What it does NOT do: open windows (pywebview render is None-safe
by design), start a run (no input is ever injected; the engine idles), or
touch the user's real config (the app is pointed at a scratch home; the
usage beacon -- a host-only concern outside the engine seam, ISS-121 --
is stubbed for the drive).

Run:  python3 engine_lite_drive.py
"""
import importlib.util
import json
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
FAILS = []


def chk(cond, msg):
    print("  [%s] %s" % ("PASS" if cond else "FAIL", msg))
    if not cond:
        FAILS.append(msg)


def load_app():
    spec = importlib.util.spec_from_file_location(
        "papp_drive", os.path.join(ROOT, "prospecting_app.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["papp_drive"] = mod
    spec.loader.exec_module(mod)
    return mod


def scratch_home(flag_on):
    home = tempfile.mkdtemp(prefix="ppe-lite-drive-")
    cfg = {"ENGINE_IPC": bool(flag_on), "RELICS_ENABLED": False,
           "WEBHOOK_URL": "", "WEBHOOK_SECRET": ""}
    with open(os.path.join(home, "prospecting_config.json"), "w",
              encoding="utf-8") as f:
        json.dump(cfg, f)
    return home


def drive_legacy(app):
    print("[drive] legacy mode (ENGINE_IPC off) -- real spawn, real engine")
    home = scratch_home(False)
    app.DATA_DIR = home
    app.CONFIG_FILE = os.path.join(home, "prospecting_config.json")
    api = app.Api()
    api._report_usage = lambda: None
    r = api.launch()
    chk(r == "launched", "legacy: launch() -> launched")
    chk(api._ipc is False, "legacy: flag off resolves to legacy path")
    chk(api.is_running(), "legacy: is_running() true while engine alive")
    time.sleep(4.0)               # engine reaches its main loop
    chk(api.pause_toggle() == "ok", "legacy: Pause button writes stdin verb")
    chk(api.relic_reset() == "ok", "legacy: relic reset writes stdin verb")
    # the reset ack is the engine's first FLUSHED line (banner prints are
    # unflushed under a pipe -- today's shipped behavior, protocol section 1
    # 'unflushed-status-line class'); it drains the buffered banner with it
    deadline = time.time() + 20
    log = ""
    while time.time() < deadline and "RELIC TIMERS RESET" not in log:
        time.sleep(0.2)
        log = "\n".join(getattr(api, "_run_log", []) or [])
    chk("RELIC TIMERS RESET" in log,
        "legacy: stdin verb round-trip pumped to the app log")
    chk("start/stop" in log,
        "legacy: engine banner drained to the app log with the flush")
    # C8: the calibration surface -- real Api handlers delegating to the
    # ENGINE's sensing module in-process (screen reads run engine-side;
    # a real full-screen grab happens here, zero input is injected)
    r = api.detect_roblox()
    chk(isinstance(r, dict) and "found" in r,
        "legacy: detect_roblox via engine sensing (found=%r)" % r.get("found"))
    r = api.sample_pixels()
    chk(isinstance(r, dict) and ("pixels" in r or "error" in r),
        "legacy: sample_pixels via engine sensing")
    r = api.calibration_health()
    chk(isinstance(r, dict) and "ok" in r, "legacy: calibration_health shape")
    chk("cues" in api.cue_mask_status(), "legacy: cue_mask_status shape")
    r = api.save_pixels({"DEPOSIT_PIX": [10, 20]})
    cfg = json.load(open(app.CONFIG_FILE))
    chk(r == "saved" and cfg.get("DEPOSIT_PIX") == [10, 20]
        and cfg.get("AUTO_CALIBRATE") is False
        and os.path.exists(app.CONFIG_FILE + ".bak"),
        "legacy: save_pixels persists via the engine's atomic writer")
    r = api.start_overlay_calibrate("DEPOSIT_PIX", "Deposit")
    chk(r.get("ok") is True and str(getattr(api, "_shot_b64", ""))
        .startswith("data:image/png"),
        "legacy: overlay capture -> engine session + preview (real grab)")
    r = api.overlay_pick(0.5, 0.5)
    chk(isinstance(r, dict) and ("x" in r or "error" in r),
        "legacy: overlay_pick reads the engine's stored session frame")
    api.overlay_cancel()
    chk(api.stop() == "stopped", "legacy: Stop button stops")
    time.sleep(1.0)
    chk(api.proc is None, "legacy: process cleared after stop")
    return log


def drive_ipc(app):
    print("[drive] ipc mode (ENGINE_IPC on) -- real spawn, PPE1 frames")
    home = scratch_home(True)
    app.DATA_DIR = home
    app.CONFIG_FILE = os.path.join(home, "prospecting_config.json")
    api = app.Api()
    api._report_usage = lambda: None
    r = api.launch()
    chk(r == "launched", "ipc: launch() -> launched")
    chk(api._ipc is True, "ipc: flag on resolves to EngineClient path")
    cli = api.engine
    chk(cli is not None, "ipc: EngineClient attached")
    ready = cli.wait_ready()
    chk(ready, "ipc: engine.hello within budget (real engine, not sim)")
    if ready:
        chk(cli.hello["capabilities"]["simulated"] is False,
            "ipc: real engine declares simulated=false")
        chk(bool(cli.vestibule) and cli.vestibule["major"] == 1,
            "ipc: stderr vestibule parsed")
        a = cli.request("engine.ping")
        chk(a.get("ok") is True and a["result"]["state"] == "idle",
            "ipc: ping acked, engine idle (no run ever started)")
        chk(api.pause_toggle() == "ok",
            "ipc: Pause button routes run.pause command")
        time.sleep(0.5)   # idle engine NACKs BAD_STATE; logged, no crash
        chk(api.relic_reset() == "ok",
            "ipc: relic button routes relic.resetAll command")
        time.sleep(0.5)
        beats = [e for e in cli.recent_events
                 if e["ev"] == "engine.heartbeat"]
        deadline = time.time() + 6
        while time.time() < deadline and len(beats) < 2:
            time.sleep(0.3)
            beats = [e for e in cli.recent_events
                     if e["ev"] == "engine.heartbeat"]
        chk(len(beats) >= 2, "ipc: heartbeats observed from real engine")
        chk(cli.responsive(), "ipc: client sees engine responsive")
    chk(api.stop() == "stopped", "ipc: Stop button returns immediately")
    deadline = time.time() + 10
    while time.time() < deadline and api.proc is not None:
        time.sleep(0.2)
    chk(api.proc is None, "ipc: engine exited after in-band shutdown")
    chk(api.engine is None, "ipc: client cleared by exit handler")
    log = "\n".join(getattr(api, "_run_log", []) or [])
    chk("Dig trigger pixel" in log,
        "ipc: legacy banner arrives as engine.log -> app log surface")
    return log


if __name__ == "__main__":
    app = load_app()
    drive_legacy(app)
    print()
    drive_ipc(app)
    print()
    if FAILS:
        print("LITE DRIVE: %d FAILURES" % len(FAILS))
        sys.exit(1)
    print("LITE DRIVE: ALL PASS (both flag states, real app host + engine)")
