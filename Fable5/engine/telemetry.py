"""Telemetry — structured events, phase timings, and run history.

Everything notable the engine does is emitted as one JSON line on stdout
prefixed with '@@ ' (the app pumps these). Every event carries a human
REASON — 'why did it do that' must always be answerable from the record.

Phase timing aggregation is the consistency lens: for each phase (dig,
to_water, shake, land) we track count / mean / p95 duration, so variance
shows up as a number instead of a feeling.
"""
from __future__ import annotations

import json
import os
import threading
import time


def _p95(sorted_vals):
    if not sorted_vals:
        return 0.0
    i = min(len(sorted_vals) - 1, int(round(0.95 * (len(sorted_vals) - 1))))
    return sorted_vals[i]


class PhaseTimer:
    KEEP = 400                      # samples kept per phase (enough for p95)

    def __init__(self, clock):
        self.clock = clock
        self.samples = {}           # name -> [ms, ...]
        self._cur = None            # (name, t0)

    def enter(self, name):
        now = self.clock.now()
        if self._cur:
            pname, t0 = self._cur
            arr = self.samples.setdefault(pname, [])
            arr.append((now - t0) * 1000.0)
            if len(arr) > self.KEEP:
                del arr[: len(arr) - self.KEEP]
        self._cur = (name, now)

    def close(self):
        self.enter("__end__")
        self._cur = None
        self.samples.pop("__end__", None)

    def summary(self):
        out = {}
        for name, arr in self.samples.items():
            if not arr:
                continue
            s = sorted(arr)
            out[name] = {"n": len(arr),
                         "mean_ms": round(sum(arr) / len(arr)),
                         "p95_ms": round(_p95(s))}
        return out


class Telemetry:
    def __init__(self, clock, sink=None):
        self.clock = clock
        self._sink_lock = threading.Lock()
        raw = sink or (lambda line: print(line, flush=True))

        def locked(line):
            with self._sink_lock:            # percept thread can emit too
                raw(line)
        self.sink = locked
        self.events = []            # this run's events (capped)
        self.counts = {}            # etype -> n
        self.reasons = {}           # "etype: reason" -> n
        self.phases = PhaseTimer(clock)
        self._run_t0 = None

    def _send(self, obj):
        try:
            self.sink("@@ " + json.dumps(obj))
        except Exception:
            pass

    def log(self, msg):
        self._send({"t": "log", "msg": str(msg)})

    def phase(self, name):
        self.phases.enter(name)
        self._send({"t": "phase", "name": name})

    def event(self, etype, reason="", **extra):
        ev = {"t": "event", "etype": etype, "reason": reason,
              "ts": round(self.clock.now(), 3)}
        ev.update(extra)
        self.counts[etype] = self.counts.get(etype, 0) + 1
        key = "%s: %s" % (etype, reason) if reason else etype
        self.reasons[key] = self.reasons.get(key, 0) + 1
        if len(self.events) < 400:
            self.events.append({k: ev[k] for k in ("etype", "reason", "ts")})
        self._send(ev)

    def stats(self, d):
        self._send({"t": "stats", "v": d})

    def world(self, w):
        """Probe stream for the calibration/dashboard live view."""
        self._send({"t": "probe", "fill": round(w.fill, 3),
                    "rate": round(w.rate, 3), "full": w.full, "empty": w.empty,
                    "dep": w.dep, "pan": w.pan, "shk": w.shk,
                    "raw": [round(x, 3) for x in w.raw], "where": w.where})

    # -- run lifecycle ---------------------------------------------------------
    def run_started(self):
        self.events = []
        self.counts = {}
        self.reasons = {}
        self.phases = PhaseTimer(self.clock)
        self._run_t0 = self.clock.now()

    def run_record(self, stats_dict, stop_reason):
        self.phases.close()
        return {
            "ended": time.strftime("%Y-%m-%d %H:%M"),
            "stop_reason": stop_reason,
            "event_counts": dict(self.counts),
            "reason_counts": dict(self.reasons),
            "phase_timings": self.phases.summary(),
            "events": self.events[-200:],
            **stats_dict,
        }


class HistoryWriter:
    MAX_RUNS = 200

    def __init__(self, path):
        self.path = path

    def append(self, record):
        runs = []
        try:
            if os.path.exists(self.path):
                with open(self.path) as f:
                    runs = json.load(f)
        except Exception:
            runs = []
        runs.append(record)
        runs = runs[-self.MAX_RUNS:]
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(runs, f, indent=1)
            os.replace(tmp, self.path)
        except Exception:
            pass

    def read(self):
        try:
            with open(self.path) as f:
                return json.load(f)
        except Exception:
            return []
