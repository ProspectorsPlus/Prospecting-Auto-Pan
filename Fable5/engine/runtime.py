"""Runtime — the engine process.

Owns: config, perception thread, brain, hotkeys, relic timers, the
safe-stop retry policy, and the stdout/stdin protocol the app talks.

Protocol (one JSON per line, prefixed '@@ '):
  out: {"t":"hello"|"log"|"phase"|"event"|"stats"|"probe"|"state"|"stopped"}
  in (stdin, one JSON or bare word per line):
       start | stop | quit | probe_on | probe_off
       {"cmd":"set","key":K,"value":V}   (validated; geometry auto-reloads)
       {"cmd":"reload"}                  (re-read config file)
"""
from __future__ import annotations

import json
import os
import queue
import sys
import threading

from . import config as configmod
from .actions import Actions
from .brain import Brain, SafeStop
from .clock import RealClock
from .inputs import make_input
from .percept import RealPercept
from .telemetry import Telemetry, HistoryWriter

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# pynput vk -> chord code (macOS letters + esc); char fallback for others
MAC_VK_CODE = {0: "KeyA", 1: "KeyS", 2: "KeyD", 3: "KeyF", 4: "KeyH",
               5: "KeyG", 6: "KeyZ", 7: "KeyX", 8: "KeyC", 9: "KeyV",
               11: "KeyB", 12: "KeyQ", 13: "KeyW", 14: "KeyE", 15: "KeyR",
               16: "KeyY", 17: "KeyT", 31: "KeyO", 32: "KeyU", 34: "KeyI",
               35: "KeyP", 37: "KeyL", 38: "KeyJ", 40: "KeyK", 45: "KeyN",
               46: "KeyM", 53: "Escape"}


class Engine:
    def __init__(self):
        self.clock = RealClock()
        self.cfg = configmod.load(APP_DIR)
        self.telem = Telemetry(self.clock)
        self.history = HistoryWriter(os.path.join(APP_DIR, "fable_history.json"))
        self.running = False
        self.alive = True
        self.probe = False
        self.want_safe_stop = False
        self.safe_retries = 0
        self._resume_at = None
        self._cmds = queue.Queue()

        self.percept = RealPercept(self.cfg, self.clock,
                                   abort_fn=lambda: not self.running,
                                   on_error=lambda e: self.telem.log(
                                       "capture error: %r" % (e,)),
                                   hot_fn=lambda: (self.running or self.probe
                                                   or self._resume_at is not None))
        self.inputs = make_input(self.clock)
        self.actions = Actions(self.cfg, self.percept, self.inputs, self.clock,
                               self.telem, abort_fn=lambda: not self.running)
        self.brain = Brain(self.cfg, self.percept, self.actions, self.telem,
                           self.clock)
        self._relic_next = {}

    # ------------------------------------------------------------- lifecycle --
    def start_run(self):
        if self.running:
            return
        self.running = True
        self.safe_retries = 0
        self._resume_at = None
        self.brain.new_run()
        self.telem.run_started()
        self._relics_reset()
        self.telem._send({"t": "state", "running": True})
        self.telem.log("=== RUNNING ===")

    def stop_run(self, reason="manual"):
        if not self.running and self._resume_at is None:
            return
        self.running = False
        self._resume_at = None
        self.inputs.release_all()
        self.brain.stats.stop_reason = reason
        rec = self.telem.run_record(self.brain.stats.as_dict(), reason)
        if self.brain.stats.cycles > 0 or self.brain.stats.runtime_s() > 30:
            self.history.append(rec)
        self.telem._send({"t": "state", "running": False, "reason": reason})
        self.telem.stats(self.brain.stats.as_dict())
        self.telem.log("=== STOPPED (%s) ===" % reason)

    # ---------------------------------------------------------------- relics --
    def _relics_reset(self):
        now = self.clock.now()
        self._relic_next = {}
        for i, r in enumerate(self.cfg.RELICS or []):
            self._relic_next[i] = now + float(r.get("minutes", 10)) * 60.0

    def _relics_tick(self):
        if not self.cfg.RELICS_ENABLED or not self.running:
            return
        now = self.clock.now()
        for i, r in enumerate(self.cfg.RELICS or []):
            due = self._relic_next.get(i)
            if due is None or now < due:
                continue
            self._relic_next[i] = now + float(r.get("minutes", 10)) * 60.0
            slot = str(r.get("slot", 5))
            clicks = int(r.get("clicks", 2))
            self.telem.event("relic", "using %s (slot %s)"
                             % (r.get("name", "relic"), slot))
            self.inputs.tap_key(slot, 60)
            self.clock.sleep_ms(350)
            for _ in range(max(1, clicks)):
                self.inputs.mouse_tap(30)
                self.clock.sleep_ms(180)
            self.clock.sleep_ms(350)
            self.inputs.tap_key(str(self.cfg.PAN_SLOT), 60)
            self.clock.sleep_ms(350)
            self.brain.stats.relics_used += 1
            # the supervisor re-senses next tick, so this can't corrupt the cycle

    # ---------------------------------------------------------------- hotkeys --
    def _chord_matches(self, spec, code, ctrl, alt, shift):
        return (spec.get("code") == code
                and bool(spec.get("ctrl")) == ctrl
                and bool(spec.get("alt")) == alt
                and bool(spec.get("shift")) == shift)

    def _start_hotkeys(self):
        try:
            from pynput import keyboard
        except Exception as e:
            self.telem.log("hotkeys unavailable: %r" % (e,))
            return
        mods = {"ctrl": False, "alt": False, "shift": False}

        def code_of(key):
            if key == keyboard.Key.esc:
                return "Escape"
            vk = getattr(key, "vk", None)
            if vk is not None and vk in MAC_VK_CODE:
                return MAC_VK_CODE[vk]
            ch = getattr(key, "char", None)
            if ch and ch.isalpha():
                return "Key" + ch.upper()
            return None

        def on_press(key):
            if key in (keyboard.Key.ctrl, keyboard.Key.ctrl_l, keyboard.Key.ctrl_r):
                mods["ctrl"] = True
                return
            if key in (keyboard.Key.alt, keyboard.Key.alt_l, keyboard.Key.alt_r):
                mods["alt"] = True
                return
            if key in (keyboard.Key.shift, keyboard.Key.shift_l, keyboard.Key.shift_r):
                mods["shift"] = True
                return
            code = code_of(key)
            if not code:
                return
            if self._chord_matches(self.cfg.HOTKEY_TOGGLE, code,
                                   mods["ctrl"], mods["alt"], mods["shift"]):
                self._cmds.put("toggle")
            elif self._chord_matches(self.cfg.HOTKEY_SOFTSTOP, code,
                                     mods["ctrl"], mods["alt"], mods["shift"]):
                self._cmds.put("softstop")
            elif self._chord_matches(self.cfg.HOTKEY_QUIT, code,
                                     mods["ctrl"], mods["alt"], mods["shift"]):
                self._cmds.put("quit")

        def on_release(key):
            if key in (keyboard.Key.ctrl, keyboard.Key.ctrl_l, keyboard.Key.ctrl_r):
                mods["ctrl"] = False
            elif key in (keyboard.Key.alt, keyboard.Key.alt_l, keyboard.Key.alt_r):
                mods["alt"] = False
            elif key in (keyboard.Key.shift, keyboard.Key.shift_l, keyboard.Key.shift_r):
                mods["shift"] = False

        keyboard.Listener(on_press=on_press, on_release=on_release,
                          daemon=True).start()

    # ------------------------------------------------------------------ stdin --
    def _start_stdin(self):
        def pump():
            for line in sys.stdin:
                line = line.strip()
                if not line:
                    continue
                self._cmds.put(line)
                if line == "quit":
                    break
        threading.Thread(target=pump, daemon=True).start()

    def _handle_cmd(self, raw):
        if raw == "toggle":
            if self.running or self._resume_at is not None:
                self.stop_run("manual")
            else:
                self.start_run()
            return
        if raw == "softstop":
            self.want_safe_stop = True
            return
        if raw in ("start", "stop", "quit", "probe_on", "probe_off"):
            if raw == "start":
                self.start_run()
            elif raw == "stop":
                self.stop_run("manual")
            elif raw == "quit":
                self.stop_run("quit")
                self.alive = False
            elif raw == "probe_on":
                self.probe = True
            else:
                self.probe = False
            return
        try:
            obj = json.loads(raw)
        except Exception:
            return
        cmd = obj.get("cmd")
        if cmd == "set":
            try:
                self.cfg.set(obj["key"], obj["value"])
                self.cfg.save()
                if obj["key"].endswith("_PIXEL") or obj["key"].endswith("_PIX") \
                        or obj["key"] in ("CAP_BAND_H", "CUE_BOX", "CAP_INSET_PX"):
                    self.percept.reload_geometry()
            except Exception as e:
                self.telem.log("set failed: %r" % (e,))
        elif cmd == "reload":
            self.cfg = configmod.load(APP_DIR)
            self.percept.cfg = self.cfg
            self.actions.cfg = self.cfg
            self.brain.cfg = self.cfg
            self.percept.reload_geometry()
            self.telem.log("config reloaded")
        elif cmd == "capture_pixel":
            threading.Thread(target=self._capture_pixel,
                             args=(obj.get("key"),
                                   int(obj.get("delay_ms", 3000))),
                             daemon=True).start()

    def _screen_scale(self):
        """Physical px / points (Retina). Calibration space is physical px."""
        try:
            import mss
            import Quartz
            with mss.mss() as sct:
                phys = sct.monitors[1]["width"]
            pts = Quartz.CGDisplayBounds(Quartz.CGMainDisplayID()).size.width
            return (phys / pts) if pts else 1.0
        except Exception:
            return 1.0

    def _capture_pixel(self, key, delay_ms):
        """Hover the mouse over the target; after the countdown we save the
        cursor position (in physical px) into the given calibration key."""
        if key not in configmod.TYPES or configmod.TYPES[key] != "pix":
            self.telem.log("capture: bad key %r" % (key,))
            return
        self.clock.sleep_ms(max(500, delay_ms))
        try:
            import Quartz
            p = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
            s = self._screen_scale()
            x, y = int(round(p.x * s)), int(round(p.y * s))
            self.cfg.set(key, [x, y])
            self.cfg.save()
            self.percept.reload_geometry()
            self.telem.event("calib", "%s captured at (%d, %d)" % (key, x, y),
                             key=key, x=x, y=y)
        except Exception as e:
            self.telem.event("calib", "capture failed: %r" % (e,))

    # ------------------------------------------------------------------- main --
    def main(self):
        self.telem._send({"t": "hello", "version": "1.0.0",
                          "platform": sys.platform})
        self.percept.start()
        self._start_hotkeys()
        self._start_stdin()
        last_stats = 0.0
        last_probe = 0.0
        try:
            while self.alive:
                try:
                    raw = self._cmds.get(timeout=0.03)
                    self._handle_cmd(raw)
                    continue
                except queue.Empty:
                    pass

                now = self.clock.now()

                # safe-stop pause window -> auto-resume
                if self._resume_at is not None:
                    if now >= self._resume_at:
                        self._resume_at = None
                        self.brain.reset()
                        self.telem.event("retry", "resuming after safe-stop "
                                                  "pause #%d" % self.safe_retries)
                        self.running = True
                        self.telem._send({"t": "state", "running": True})
                    else:
                        if self.probe and now - last_probe > 0.1:
                            last_probe = now
                            w = self.percept.snapshot()
                            if w:
                                self.telem.world(w)
                        continue

                if self.running:
                    if self.want_safe_stop:
                        self.want_safe_stop = False
                        self._safe_stop(SafeStop("manual soft-stop (test)"))
                        continue
                    try:
                        self.brain.tick()
                    except SafeStop as e:
                        self._safe_stop(e)
                        continue
                    except Exception as e:
                        self.inputs.release_all()
                        self.telem.event("error", "engine error: %r" % (e,))
                        self.stop_run("error: %r" % (e,))
                        continue
                    self._relics_tick()

                    if now - last_stats >= 2.0:
                        last_stats = now
                        self.telem.stats(self.brain.stats.as_dict())
                    if (self.cfg.AUTOSTOP_MINUTES > 0
                            and self.brain.stats.runtime_s()
                            >= self.cfg.AUTOSTOP_MINUTES * 60):
                        self.stop_run("auto-stop after %d min"
                                      % self.cfg.AUTOSTOP_MINUTES)
                    if (self.cfg.STOP_AFTER_PANS > 0
                            and self.brain.stats.cycles
                            >= self.cfg.STOP_AFTER_PANS):
                        self.stop_run("bag-full guard (%d pans)"
                                      % self.cfg.STOP_AFTER_PANS)
                else:
                    if self.probe and now - last_probe > 0.1:
                        last_probe = now
                        w = self.percept.snapshot()
                        if w:
                            self.telem.world(w)
        finally:
            self.inputs.release_all()
            self.percept.stop()
            self.telem._send({"t": "stopped"})

    def _safe_stop(self, e):
        """Safe stop = pause + auto-retry (AFK self-heal), then hard stop."""
        cfg = self.cfg
        self.inputs.release_all()
        self.running = False
        if e.hard or not cfg.SAFE_STOP_RETRY \
                or self.safe_retries >= cfg.SAFE_STOP_MAX_RETRIES:
            self.brain.stats.hard_stops += 1
            self.telem.event("hard_stop", e.reason)
            self.stop_run("hard stop: %s" % e.reason)
            return
        self.safe_retries += 1
        self.brain.stats.safe_stops += 1
        self.telem.event("safe_stop", "%s — pausing %ds then retrying (%d/%d)"
                         % (e.reason, cfg.SAFE_STOP_RETRY_SEC,
                            self.safe_retries, cfg.SAFE_STOP_MAX_RETRIES))
        self.telem._send({"t": "state", "running": False,
                          "paused": True, "reason": e.reason})
        self._resume_at = self.clock.now() + cfg.SAFE_STOP_RETRY_SEC


def main():
    Engine().main()


if __name__ == "__main__":
    main()
