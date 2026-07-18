"""PPE1 server side (Phase 04, checkpoint C2).

The engine's --ipc mode: swaps the engine module's EMIT seam for a frame
emitter, runs the version vestibule / instance lock / hello startup order
(protocol section 11.2), a dedicated heartbeat thread, and the control
thread that parses stdin frames.

Command vocabulary served at C2 (the rest NACK UNSUPPORTED until C4/C5):
engine.ping, engine.shutdown, run.pause, run.resume, relic.resetAll,
relic.resetOne, relic.set. Legacy mode never imports this module.
"""
import hashlib
import json
import os
import sys
import threading

from . import protocol
from . import ENGINE_VERSION

HEARTBEAT_S = 2.0


def _lock_dir():
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.join(base, "ProspectorEngine")
    return os.path.expanduser(
        "~/Library/Application Support/ProspectorEngine")


def source_fingerprint(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()[:16]


class InstanceLock(object):
    """Machine-scoped single-live-engine lock (architecture section 3,
    ISS-136/141): one well-known path per OS user, independent of --home.
    Atomic O_CREAT|O_EXCL acquire; stale (dead-pid) locks are reaped by
    unlink-then-retry where the retry is again O_EXCL."""

    def __init__(self, host):
        self.host = host
        self.path = os.path.join(_lock_dir(), "engine.lock")
        self.acquired = False

    def _try_create(self, payload):
        fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        try:
            os.write(fd, payload)
        finally:
            os.close(fd)
        return True

    def acquire(self, started_ts):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        payload = json.dumps({"pid": os.getpid(), "host": self.host,
                              "startedTs": started_ts}).encode("utf-8")
        for _attempt in (1, 2):
            try:
                self._try_create(payload)
                self.acquired = True
                return None
            except FileExistsError:
                owner = self._owner()
                if owner and self._pid_alive(owner.get("pid")):
                    return owner            # live contention -> refuse
                try:
                    os.unlink(self.path)    # stale -> reap, retry O_EXCL
                except OSError:
                    pass
        owner = self._owner()
        return owner or {"pid": None, "host": "unknown"}

    def _owner(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    @staticmethod
    def _pid_alive(pid):
        if not isinstance(pid, int) or pid <= 0:
            return False
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False

    def release(self):
        if self.acquired:
            try:
                os.unlink(self.path)
            except OSError:
                pass
            self.acquired = False


class FrameEmit(object):
    """The ipc-mode EMIT implementation. One process-wide writer lock,
    flush per frame, strictly monotonic seq, engine-monotonic ts.
    Method-for-method mirror of the engine's _LegacyEmit seam."""

    def __init__(self, po, simulated=False):
        self.po = po
        self.simulated = simulated
        self._wlock = threading.Lock()
        self._seq = 0
        self._t0 = po.time.perf_counter()
        self._run_counter = 0
        self.run_id = None

    # -- plumbing ---------------------------------------------------------
    def _ts(self):
        return self.po.time.perf_counter() - self._t0

    def _ev(self, name, data):
        with self._wlock:
            self._seq += 1
            line = protocol.encode_event(self._seq, self._ts(), name, data)
            sys.stdout.write(line)
            sys.stdout.flush()
            return self._seq

    def write_ack(self, line):
        with self._wlock:
            self._seq_noop = None   # acks carry no seq; lock still serializes
            sys.stdout.write(line)
            sys.stdout.flush()

    def _rd(self, data):
        if self.run_id is not None:
            data["runId"] = self.run_id
        return data

    def _mode_label(self):
        po = self.po
        if getattr(po, "TRACKER_MODE", False):
            return "tracker"
        if getattr(po, "SCRIPT_MODE", False):
            return "script"
        if getattr(po, "TREASURE_MODE", False):
            return "treasure"
        if getattr(po, "GEODE_MODE", False):
            return "geode"
        if int(getattr(po, "SHARDS_DIG_CLICKS", 0) or 0) > 0:
            return "shards"
        return "standard"

    # -- lifecycle --------------------------------------------------------
    def hello(self, home):
        po = self.po
        caps = {"findsOcr": sys.platform == "darwin",
                "earningsOcr": sys.platform == "darwin",
                "inputLagProbe": hasattr(po, "_LAG"),
                "fastTravelRecovery": True,
                "simulated": bool(self.simulated)}
        self._ev("engine.hello", {
            "protocol": {"major": protocol.PROTOCOL_MAJOR,
                         "minor": protocol.PROTOCOL_MINOR},
            "engine": {"version": ENGINE_VERSION,
                       "sourceFingerprint": source_fingerprint(po.__file__),
                       "platform": "win" if sys.platform == "win32" else "mac"},
            "pid": os.getpid(), "home": home,
            "capabilities": caps, "state": "idle"})

    def heartbeat(self, state, tick_age_s):
        d = {"state": state, "tickAgeS": round(float(tick_age_s), 1)}
        self._ev("engine.heartbeat", self._rd(d))

    def bye(self, reason, code=None, message=None, data=None):
        d = {"reason": reason}
        if code:
            d["code"] = code
        if message:
            d["message"] = message
        if data is not None:
            d["data"] = data
        self._ev("engine.bye", d)

    # -- EMIT seam methods (same names/signatures as _LegacyEmit) ----------
    def reset(self, origin="hotkey"):
        self._run_counter += 1
        self.run_id = "r%d" % self._run_counter
        po = self.po
        d = {"mode": self._mode_label(), "origin": origin}
        if d["mode"] == "script":
            d["script"] = getattr(po, "SCRIPT_ACTIVE", "")
        self._ev("run.started", self._rd(d))

    def stats(self, flat):
        self._ev("run.stats", self._rd(protocol.split_stats(flat)))

    def event(self, etype, rec):
        po = self.po
        d = {"type": etype, "reason": rec.get("reason", ""),
             "dirty": etype in getattr(po, "_DIRTY_EVENTS", set())}
        for k in ("where", "contents"):
            if rec.get(k):
                d[k] = rec[k]
        self._ev("safety.event", self._rd(d))

    def phase(self, name):
        self._ev("run.phase", self._rd({"phase": name}))

    def find(self, rec):
        self._ev("find.new", self._rd(dict(rec)))

    def find_upd(self, rec):
        d = dict(rec)
        # C2 carries the legacy re-emit 1:1; the departure-final flag is
        # instrumented when FindsWatcher itself is extracted (see handoff)
        d["final"] = False
        self._ev("find.updated", self._rd(d))

    def geode(self, ms, label):
        self._ev("geode.timer", self._rd({"ms": int(ms), "label": label}))

    def script_block(self, payload):
        self._ev("script.block", self._rd(dict(payload)))

    def script_hud(self, payload):
        self._ev("script.hud", self._rd({"text": payload.get("text", "")}))

    def popout(self):
        self._ev("hotkey.popout", {})

    def log_line(self, dt, msg):
        self._ev("engine.log", {"level": "info", "text": str(msg)})

    def toggle_status(self, running):
        if not running:
            # terminal narration arrives via stopped(); the raw toggle line
            # is still useful as a log breadcrumb
            self._ev("engine.log", {"level": "info", "text": "[STOPPED]"})
        else:
            self._ev("engine.log", {"level": "info", "text": "[RUNNING]"})

    def paused(self, origin="hotkey"):
        self._ev("run.paused", self._rd({"origin": origin}))

    def resumed(self, origin="hotkey"):
        self._ev("run.resumed", self._rd({"origin": origin}))

    def stopped(self, reason, final):
        wire = {"manual": "hotkey"}.get(reason, reason)
        if wire not in protocol.STOP_REASONS:
            wire = "hotkey"
        self._ev("run.stopped", self._rd(
            {"reason": wire, "final": protocol.split_stats(final or {})}))
        self.run_id = None

    def quit_(self):
        self._ev("engine.log", {"level": "info", "text": "[QUIT]"})

    def softstop(self):
        self._ev("engine.log", {"level": "info", "text": "[MANUAL SOFT-STOP]"})

    def relic_reset(self, flushed):
        self._ev("relic.changed", {"kind": "resetAll",
                                   "origin": "cmd" if flushed else "hotkey"})

    def relic_one(self, index, origin):
        self._ev("relic.changed", {"kind": "resetOne", "index": int(index),
                                   "origin": origin})

    def relic_set(self, index, seconds, origin):
        self._ev("relic.changed", {"kind": "set", "index": int(index),
                                   "seconds": float(seconds),
                                   "origin": origin})

    def safe_pause(self, msg, reason, attempt, max_retries, retry_s):
        self._ev("safety.safePaused", self._rd(
            {"reason": reason, "attempt": int(attempt),
             "maxAttempts": int(max_retries), "retryInS": int(retry_s)}))

    def hard_stop(self, reason):
        self._ev("safety.hardStopped", self._rd(
            {"reason": reason, "retriesExhausted": True}))

    def sr_recovered(self, reason):
        self._ev("safety.recovery", self._rd(
            {"kind": "sr", "stage": "success", "reason": reason}))

    def fr_recovered(self, reason):
        self._ev("safety.recovery", self._rd(
            {"kind": "fr", "stage": "success", "reason": reason}))

    def interrupted(self):
        self._ev("engine.log", {"level": "warn",
                                "text": "[interrupted -- exiting]"})

    def stopped_final_line(self):
        self._ev("engine.log", {"level": "info",
                                "text": "Stopped, all inputs released."})


class Server(object):
    """Owns the ipc threads. bootstrap() runs the section 11.2 startup
    order and returns the Server, or exits the process on refusal."""

    def __init__(self, po, home, host, simulated):
        self.po = po
        self.home = home
        self.host = host
        self.emit = FrameEmit(po, simulated=simulated)
        self.lock = InstanceLock(host)
        self._shutdown = threading.Event()

    # -- startup ----------------------------------------------------------
    def start(self, requested_protocol):
        po = self.po
        fp = source_fingerprint(po.__file__)
        sys.stderr.write(protocol.vestibule_line(ENGINE_VERSION, fp) + "\n")
        sys.stderr.flush()
        if requested_protocol not in (None, protocol.PROTOCOL_MAJOR):
            self.emit.bye("fatal", code=protocol.BYE_PROTOCOL_UNSUPPORTED,
                          message="engine speaks protocol %d only"
                                  % protocol.PROTOCOL_MAJOR,
                          data={"requested": requested_protocol,
                                "supported": [protocol.PROTOCOL_MAJOR]})
            os._exit(2)
        owner = self.lock.acquire(started_ts=self.emit._t0)
        if owner is not None:
            self.emit.bye("fatal", code=protocol.BYE_ALREADY_RUNNING,
                          message="another engine owns this machine's "
                                  "input session",
                          data={"ownerPid": owner.get("pid"),
                                "ownerHost": owner.get("host")})
            os._exit(2)
        po.State.tick_beat = po.time.perf_counter()
        self.emit.hello(self.home)
        threading.Thread(target=self._heartbeat_loop, daemon=True).start()
        threading.Thread(target=self._control_loop, daemon=True).start()
        return self

    # -- state ------------------------------------------------------------
    def _state(self):
        st = self.po.State
        if getattr(st, "paused", False):
            return "paused"
        if getattr(st, "running", False):
            return "running"
        return "idle"

    def _heartbeat_loop(self):
        while not self._shutdown.is_set():
            age = self.po.time.perf_counter() - getattr(
                self.po.State, "tick_beat", self.emit._t0)
            try:
                self.emit.heartbeat(self._state(), max(0.0, age))
            except Exception:
                pass
            self._shutdown.wait(HEARTBEAT_S)

    # -- shutdown paths ---------------------------------------------------
    def shutdown(self, source):
        """Section 10.1: stop run, release inputs, terminal events, bye,
        exit 0. Runs on the control thread; used by engine.shutdown and by
        stdin EOF (host death)."""
        po = self.po
        was_running = bool(po.State.running or po.State.paused)
        po.State.running = False
        po.State.paused = False
        po.State.alive = False
        try:
            po.release_all()
        except Exception:
            pass
        if was_running and po.State.stats is not None:
            final = po.State.stats.as_dict()
            try:
                final["relics"] = (po.State.relics_ref.remaining()
                                   if po.State.relics_ref else [])
            except Exception:
                final["relics"] = []
            self.emit.stopped("shutdown", final)
        self.emit.bye("shutdown", message=source)
        self.lock.release()
        sys.stdout.flush()
        os._exit(0)

    def main_ended(self):
        """Engine main() returned (quit hotkey / natural end): section 10.1
        epilogue -- terminal run event if one was live, then bye. The
        process exits normally afterwards."""
        po = self.po
        if self.emit.run_id is not None and po.State.stats is not None:
            final = po.State.stats.as_dict()
            try:
                final["relics"] = (po.State.relics_ref.remaining()
                                   if po.State.relics_ref else [])
            except Exception:
                final["relics"] = []
            self.emit.stopped("shutdown", final)
        self._shutdown.set()
        self.emit.bye("shutdown", message="engine main loop ended")
        self.lock.release()

    # -- control thread ----------------------------------------------------
    def _control_loop(self):
        po = self.po
        try:
            for raw in sys.stdin:
                try:
                    kind, obj = protocol.decode_line(raw)
                except protocol.ProtocolError as e:
                    self.emit.log_line(0, "protocol error: %s" % e)
                    continue
                if kind != "frame" or obj.get("t") != "cmd":
                    continue
                self._dispatch(obj)
        except Exception:
            pass
        # stdin EOF or error = the host is gone (section 10.2)
        self.shutdown("host stdin closed")

    def _ack_ok(self, cid, result=None):
        self.emit.write_ack(protocol.encode_ack_ok(cid, result))

    def _ack_err(self, cid, code, message, data=None):
        self.emit.write_ack(protocol.encode_ack_err(cid, code, message, data))

    def _dispatch(self, obj):
        po = self.po
        cid, cmd, params = obj["id"], obj["cmd"], obj["params"]
        st = po.State
        if cmd == "engine.ping":
            r = {"state": self._state(),
                 "uptimeS": round(self.emit._ts(), 1)}
            if self.emit.run_id:
                r["runId"] = self.emit.run_id
            self._ack_ok(cid, r)
        elif cmd == "engine.shutdown":
            self._ack_ok(cid, {})
            self.shutdown("engine.shutdown command")
        elif cmd == "run.pause":
            if not st.running or st.paused:
                self._ack_err(cid, protocol.E_BAD_STATE, "no run to pause",
                              {"state": self._state()})
                return
            po.engine_pause()
            self._ack_ok(cid, {"runId": self.emit.run_id})
        elif cmd == "run.resume":
            if not st.paused:
                self._ack_err(cid, protocol.E_BAD_STATE, "not paused",
                              {"state": self._state()})
                return
            po.engine_resume()
            self._ack_ok(cid, {"runId": self.emit.run_id})
        elif cmd == "relic.resetAll":
            if st.relics_ref is None:
                self._ack_err(cid, protocol.E_BAD_STATE,
                              "relic scheduler not initialized", None)
                return
            st.relics_ref.reset()
            self.emit.relic_reset(True)
            self._ack_ok(cid, {"applied": True})
        elif cmd == "relic.resetOne":
            i = params.get("index")
            if not isinstance(i, int) or not (
                    0 <= i < len(getattr(po, "RELICS", []) or [])):
                self._ack_err(cid, protocol.E_BAD_PARAMS,
                              "bad relic index", {"index": i})
                return
            if st.relics_ref is None:
                self._ack_err(cid, protocol.E_BAD_STATE,
                              "relic scheduler not initialized", None)
                return
            st.relics_ref.reset_one(i)
            self.emit.relic_one(i, "cmd")
            self._ack_ok(cid, {"applied": True})
        elif cmd == "relic.set":
            i = params.get("index")
            s = params.get("seconds")
            if (not isinstance(i, int)
                    or not isinstance(s, (int, float)) or s < 0
                    or not (0 <= i < len(getattr(po, "RELICS", []) or []))):
                self._ack_err(cid, protocol.E_BAD_PARAMS,
                              "bad relic index/seconds",
                              {"index": i, "seconds": s})
                return
            if st.relics_ref is None:
                self._ack_err(cid, protocol.E_BAD_STATE,
                              "relic scheduler not initialized", None)
                return
            st.relics_ref.set_left(i, float(s))
            self.emit.relic_set(i, float(s), "cmd")
            self._ack_ok(cid, {"applied": True})
        elif cmd in protocol.COMMANDS:
            self._ack_err(cid, protocol.E_UNSUPPORTED,
                          "command lands at a later extraction checkpoint",
                          {"pending": cmd})
        else:
            self._ack_err(cid, protocol.E_UNSUPPORTED,
                          "unknown command", {"cmd": cmd})


def bootstrap(po, home, host, requested_protocol, simulated):
    """Called by the engine's main() when --ipc is on, after load_config.
    Swaps EMIT and starts the server threads. Returns the Server."""
    srv = Server(po, home, host, simulated)
    po.EMIT = srv.emit
    return srv.start(requested_protocol)
