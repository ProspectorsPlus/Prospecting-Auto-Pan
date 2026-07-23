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
from . import sensing as sensing_mod
from . import settings as settings_mod
from . import ENGINE_VERSION

HEARTBEAT_S = 2.0


def capabilities(po, simulated):
    """The one capability truth (hello + describe). A sim scenario may
    override flags via po._SIM_CAPS (simulated engines only -- the world
    scripts platform capabilities the test host can't reach, e.g. the
    windows no-Vision testRead refusal on a mac test machine)."""
    from prospector_engine import recorder as recorder_mod
    caps = {"findsOcr": sys.platform == "darwin",
            "earningsOcr": sys.platform == "darwin",
            "inputLagProbe": hasattr(po, "_LAG"),
            "fastTravelRecovery": True,
            "scriptV3": hasattr(po, "_SCRIPT_HANDLERS_V3"),
            "vision": True,
            "recorder": recorder_mod.available(),
            "simulated": bool(simulated)}
    if simulated:
        try:
            caps.update(getattr(po, "_SIM_CAPS", None) or {})
        except Exception:
            pass
    return caps


def _lock_dir():
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        return os.path.join(base, "ProspectorEngine")
    return os.path.expanduser(
        "~/Library/Application Support/ProspectorEngine")


def source_fingerprint(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()[:16]


def instance_identity(home):
    """The engine's durable instance GUID (protocol 1.5): minted once into
    <home>/instance_id and read on every later boot, so hosts can tell
    "the same runner install answered again" from "a different runner owns
    this home". An absent/unreadable/empty file mints a fresh GUID; a
    failed persist still returns the minted value (identity beats
    durability -- consumers treat the field as optional either way)."""
    import uuid
    path = os.path.join(home, "instance_id")
    try:
        with open(path, encoding="utf-8") as f:
            val = f.read().strip()
        if val:
            return val
    except OSError:
        pass
    val = str(uuid.uuid4())
    try:
        os.makedirs(home, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(val + "\n")
        os.replace(tmp, path)
    except OSError:
        pass
    return val


def engine_identity(po, home, instance):
    """The engine object served in hello AND engine.describe (1.5): the
    pre-1.5 version/fingerprint/platform triple plus the durable instance
    GUID, the executable actually running, and the data directory (the
    engine home). One builder so the two surfaces can never drift."""
    return {"version": ENGINE_VERSION,
            "sourceFingerprint": source_fingerprint(po.__file__),
            "platform": "win" if sys.platform == "win32" else "mac",
            "instance": instance,
            "exePath": sys.executable or "",
            "frozen": bool(getattr(sys, "frozen", False)),
            "dataDir": home}


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
        self.server = None            # backref for tick-committed acks (C4)
        self.instance_id = None       # durable GUID (1.5; Server sets it)
        self._next_origin = None      # origin for the next run.started
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
        caps = capabilities(po, self.simulated)
        self._ev("engine.hello", {
            "protocol": {"major": protocol.PROTOCOL_MAJOR,
                         "minor": protocol.PROTOCOL_MINOR},
            "engine": engine_identity(po, home, self.instance_id),
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
    def reset(self, origin=None):
        self._run_counter += 1
        po = self.po
        # the persistent run id is minted by the tick thread at fresh
        # start (engine._mint_run_id -> State.run_id); the per-process
        # counter remains only as a fallback for exotic call orders
        self.run_id = (getattr(po.State, "run_id", "")
                       or "r%d" % self._run_counter)
        if origin is None:
            origin = self._next_origin or "hotkey"
        self._next_origin = None
        d = {"mode": self._mode_label(), "origin": origin}
        if d["mode"] == "script":
            d["script"] = getattr(po, "SCRIPT_ACTIVE", "")
        if self.instance_id:
            # 1.5: WHICH runner install this run executes on
            d["instanceId"] = self.instance_id
        self._ev("run.started", self._rd(d))

    def run_init_committed(self):
        # the tick thread's run.start commit point (protocol section 2):
        # fresh-init is complete and the run substate is running
        if self.server is not None:
            self.server.on_run_init_committed()

    def stats(self, flat):
        self._ev("run.stats", self._rd(protocol.split_stats(flat)))

    def event(self, etype, rec):
        po = self.po
        d = {"type": etype, "reason": rec.get("reason", ""),
             "dirty": etype in getattr(po, "_DIRTY_EVENTS", set())}
        for k in ("where", "contents"):
            if rec.get(k):
                d[k] = rec[k]
        for k in ("id", "authored", "action"):
            if k in rec:               # recovery_rung structured fields
                d[k] = rec[k]          # (authored may be False -- carry it)
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

    def flow_state(self, payload):
        self._ev("flow.state", self._rd(dict(payload)))

    def popout(self):
        self._ev("hotkey.popout", {})

    def settings_changed(self, keys, source):
        self._ev("settings.changed", {"keys": sorted(keys),
                                       "source": source,
                                       "schemaVersion":
                                       settings_mod.SCHEMA_VERSION})

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
        rid = self.run_id
        seq = self._ev("run.stopped", self._rd(
            {"reason": wire, "final": protocol.split_stats(final or {})}))
        self.run_id = None
        if self.server is not None:
            # run.stop's tick-committed ack: terminal event emitted and
            # inputs released is the commit point (protocol section 4.4)
            self.server.on_run_stopped(rid, seq)

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


class _ServerStore(object):
    """Sensing's settings access in ipc mode: reads/writes the server's
    settings document under its lock through the single atomic writer,
    and narrates every calibration write with settings.changed."""

    def __init__(self, server):
        self.server = server

    def get(self):
        with self.server._settings_lock:
            return dict(self.server.settings_doc)

    def write(self, doc, changed_keys, source):
        srv = self.server
        with srv._settings_lock:
            srv.settings_doc = dict(doc)
            settings_mod.atomic_write(srv.po.CONFIG_FILE, srv.settings_doc)
        if changed_keys:
            srv.emit.settings_changed(sorted(changed_keys), source)


class Server(object):
    """Owns the ipc threads. bootstrap() runs the section 11.2 startup
    order and returns the Server, or exits the process on refusal."""

    def __init__(self, po, home, host, simulated):
        self.po = po
        self.home = home
        self.host = host
        self.emit = FrameEmit(po, simulated=simulated)
        self.emit.server = self
        self.lock = InstanceLock(host)
        # 1.5 durable identity: one GUID per engine home, persisted forever
        self.instance_id = instance_identity(home)
        self.emit.instance_id = self.instance_id
        self._shutdown = threading.Event()
        # C4 transition serialization (protocol section 2): every run-state
        # mutation happens under this one mutex with its source-state guard
        # re-checked at execution -- commands, hotkeys, and the tick hook.
        self.state_lock = threading.Lock()
        self._pending_start = None    # {"cid": str|None, "origin": str}
        self._start_inflight = None   # awaiting run_init_committed()
        self._stop_inflight = None    # awaiting on_run_stopped()
        # C5: engine-owned settings. The schema snapshots the BAKED
        # module globals (bootstrap runs before load_config mutates
        # them), so defaults are the engine's acted-on values (6.1).
        self.settings_schema = settings_mod.schema(po)
        self.settings_doc = {}
        self._settings_lock = threading.Lock()
        self.served = sorted(protocol.COMMANDS)
        # C8: the calibration sensing class (protocol 4.15) -- the one
        # implementation, bound to the server's settings document.
        self.sensing = sensing_mod.Sensing(po, _ServerStore(self))
        self._recorder = None      # input-capture session (recorder.*)

    # -- startup ----------------------------------------------------------
    # Section 11.2 order: stderr vestibule -> --protocol check -> instance
    # lock -> config load + migration -> hello. The lock comes BEFORE any
    # config work (ISS-156), and go() runs only after the engine module has
    # bound its config (main() calls load_config between the two halves).
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
        try:
            self.settings_doc, _chg = settings_mod.migrate_file(
                po.CONFIG_FILE, self.settings_schema)
        except settings_mod.SchemaTooNew as e:
            self.emit.bye("fatal", code=protocol.BYE_SCHEMA_TOO_NEW,
                          message="config file is from a newer engine",
                          data={"found": e.found, "supported": e.supported})
            self.lock.release()
            os._exit(2)
        return self

    def go(self):
        po = self.po
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
            if getattr(st, "safe_paused", False):
                return "safePaused"
            return "running"
        return "idle"

    # -- C4: serialized run-state transitions ------------------------------
    # Hotkeys (listener/poller thread) and the sim world's scheduled
    # operator actions land here in ipc mode instead of flipping state
    # cross-thread; commands land here from the control thread. Guards are
    # re-checked under the mutex, so an ack can never misreport committed
    # state and every transition emits exactly one lifecycle event.

    def req_toggle(self, origin="hotkey"):
        po = self.po
        with self.state_lock:
            st = po.State
            if st.paused:
                po.engine_resume(origin)
                return
            if st.running:
                self._begin_stop_locked(None, None, origin)
                return
            if self._pending_start is None and self._start_inflight is None:
                self._pending_start = {"cid": None, "origin": origin}

    def req_pause_toggle(self, origin="hotkey"):
        po = self.po
        with self.state_lock:
            st = po.State
            if st.paused:
                po.engine_resume(origin)
            elif st.running and not getattr(st, "safe_paused", False):
                po.engine_pause(origin)
            else:
                self.emit.log_line(0, "[ipc] pause hotkey dropped: state=%s"
                                   % self._state())

    def req_soft(self, origin="hotkey"):
        po = self.po
        with self.state_lock:
            st = po.State
            if st.running and not st.paused \
                    and not getattr(st, "safe_paused", False):
                st.want_safe_stop = True
                self.emit.softstop()
            else:
                self.emit.log_line(0, "[ipc] soft-stop hotkey dropped: "
                                      "state=%s" % self._state())

    def req_quit(self, origin="hotkey"):
        po = self.po
        with self.state_lock:
            po.EMIT.quit_()
            po.State.running = False
            po.State.paused = False
            po.State.alive = False
            po.release_all()

    def _begin_stop_locked(self, cid, reason, origin):
        """The abort primitive (section 7): flip the running flag so every
        hold/wait unwinds; the tick thread then reaches its stop edge,
        emits run.stopped, and the ack (if any) completes there."""
        po = self.po
        st = po.State
        if reason and st.stats is not None:
            st.stats.stop_reason = reason
        if reason:
            st.stop_reason = reason
        st.want_reset = False           # no stale supervisor reset (4.4)
        st.safe_wait_skip = True        # interrupt a safe-pause wait now
        st.paused = False
        st.running = False
        po.EMIT.toggle_status(False)    # legacy [STOPPED] breadcrumb
        po.release_all()
        if cid is not None:
            self._stop_inflight = {"cid": cid}

    def tick_hook(self):
        """Runs on the tick thread at each loop top: consumes a queued
        start request at its commit boundary (fresh-init runs in this same
        iteration; run_init_committed() writes the ack)."""
        with self.state_lock:
            req = self._pending_start
            if req is None:
                return
            self._pending_start = None
            st = self.po.State
            if st.running or st.paused:
                # CAS source state gone by consumption time (section 2)
                if req["cid"] is not None:
                    self._ack_err(req["cid"], protocol.E_BAD_STATE,
                                  "not idle at commit",
                                  {"state": self._state()})
                else:
                    self.emit.log_line(0, "[ipc] start hotkey dropped: "
                                          "state=%s" % self._state())
                return
            self.emit._next_origin = req["origin"]
            self._start_inflight = req
            self.sensing.drop_session()   # protocol 4.15: run.start drops it
            if self._recorder is not None and self._recorder.recording:
                # a run must never be recorded (nor fight the listeners)
                self._recorder.stop("run-start")
            try:
                # re-bind config per run start (ipc only): settings.set
                # while idle/running becomes effective at the next run
                # without a respawn (fixes bake-at-spawn; legacy mode
                # keeps its single process-start bind untouched)
                self.po.load_config()
            except Exception:
                pass
            st.running = True
            self.po.EMIT.toggle_status(True)   # legacy [RUNNING] breadcrumb

    def on_run_init_committed(self):
        with self.state_lock:
            req, self._start_inflight = self._start_inflight, None
        if req is not None and req.get("cid") is not None:
            self._ack_ok(req["cid"], {"runId": self.emit.run_id,
                                      "instanceId": self.instance_id})

    def on_run_stopped(self, run_id, seq):
        with self.state_lock:
            req, self._stop_inflight = self._stop_inflight, None
        if req is not None and req.get("cid") is not None:
            self._ack_ok(req["cid"], {"runId": run_id, "finalSeq": seq,
                                      "instanceId": self.instance_id})

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

    def fatal_error(self, exc):
        """An unhandled exception is ending the engine (section 9). Emit a
        structured terminal for any live run (run.stopped reason=error) and
        an error bye BEFORE the traceback kills the process, so hosts get a
        machine-readable stop reason instead of a bare crash. Every step is
        best-effort: nothing here may raise over the original error."""
        po = self.po
        try:
            po.release_all()
        except Exception:
            pass
        try:
            if self.emit.run_id is not None:
                final = (po.State.stats.as_dict()
                         if po.State.stats is not None else {})
                self.emit.stopped("error", final)
        except Exception:
            pass
        try:
            self.emit.bye("error", code="EXCEPTION",
                          message="%s: %s" % (type(exc).__name__, exc))
        except Exception:
            pass
        try:
            self._shutdown.set()
            self.lock.release()
            sys.stdout.flush()
        except Exception:
            pass

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
        elif cmd == "run.start":
            if params.get("mode") != "auto":
                self._ack_err(cid, protocol.E_BAD_PARAMS,
                              'run.start requires {"mode":"auto"} in v1.0',
                              {"mode": params.get("mode")})
                return
            with self.state_lock:
                if st.running or st.paused or self._pending_start is not None \
                        or self._start_inflight is not None:
                    self._ack_err(cid, protocol.E_BAD_STATE, "not idle",
                                  {"state": self._state()})
                    return
                # tick-committed (section 4.3): the tick thread consumes the
                # request, runs fresh-init, and acks at init commit
                self._pending_start = {"cid": cid, "origin": "cmd"}
        elif cmd == "run.stop":
            with self.state_lock:
                if not (st.running or st.paused):
                    self._ack_err(cid, protocol.E_BAD_STATE, "no run active",
                                  {"state": self._state()})
                    return
                self._begin_stop_locked(cid, str(params.get("reason")
                                                 or "user"), "cmd")
        elif cmd == "run.softStop":
            with self.state_lock:
                if not st.running or st.paused \
                        or getattr(st, "safe_paused", False):
                    self._ack_err(cid, protocol.E_BAD_STATE,
                                  "no running run to soft-stop",
                                  {"state": self._state()})
                    return
                # ack commits LADDER ENTRY, not the ladder's outcome -- the
                # one section 3.3 exception (4.6); outcome arrives as
                # safety.* events on the tick thread
                st.want_safe_stop = True
                self.emit.softstop()
                self._ack_ok(cid, {"runId": self.emit.run_id})
        elif cmd == "run.pause":
            with self.state_lock:
                if not st.running or st.paused \
                        or getattr(st, "safe_paused", False):
                    self._ack_err(cid, protocol.E_BAD_STATE,
                                  "no run to pause",
                                  {"state": self._state()})
                    return
                po.engine_pause("cmd")
                self._ack_ok(cid, {"runId": self.emit.run_id})
        elif cmd == "run.resume":
            with self.state_lock:
                if st.paused:
                    po.engine_resume("cmd")
                    self._ack_ok(cid, {"runId": self.emit.run_id})
                elif getattr(st, "safe_paused", False):
                    # legal in safePaused: skip the rest of the wait (4.5)
                    st.safe_wait_skip = True
                    self._ack_ok(cid, {"runId": self.emit.run_id})
                else:
                    self._ack_err(cid, protocol.E_BAD_STATE, "not paused",
                                  {"state": self._state()})
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
        elif cmd == "engine.describe":
            caps = capabilities(po, self.emit.simulated)
            sch = [{"key": k, "type": s["type"], "default": s["default"],
                    "applies": s["applies"]}
                   for k, s in sorted(self.settings_schema.items())]
            self._ack_ok(cid, {
                "protocol": {"major": protocol.PROTOCOL_MAJOR,
                             "minor": protocol.PROTOCOL_MINOR},
                "engine": engine_identity(po, self.home, self.instance_id),
                "capabilities": caps,
                "settingsSchema": sch,
                "commands": list(self.served),
                "events": list(protocol.EVENTS),
                "injectable": {"keys": list(protocol.INJECTABLE_KEYS),
                               "buttons": list(protocol.INJECTABLE_BUTTONS)}})
        elif cmd == "settings.get":
            keys = params.get("keys")
            with self._settings_lock:
                if keys is None:
                    vals = {k: self.settings_doc.get(k)
                            for k in self.settings_schema}
                else:
                    vals = {k: self.settings_doc.get(k) for k in keys
                            if k in self.settings_schema}
            self._ack_ok(cid, {"values": vals,
                               "schemaVersion": settings_mod.SCHEMA_VERSION})
        elif cmd in ("settings.set", "settings.setOpaque"):
            values = params.get("values")
            if not isinstance(values, dict) or not values:
                self._ack_err(cid, protocol.E_BAD_PARAMS,
                              "values must be a non-empty object", None)
                return
            opaque = cmd == "settings.setOpaque"
            per_key = settings_mod.validate(self.settings_schema, values,
                                            opaque=opaque)
            if per_key:
                self._ack_err(cid, protocol.E_VALIDATION_FAILED,
                              "invalid settings write (nothing written)",
                              {"perKey": per_key})
                return
            with self._settings_lock:
                for k, v in values.items():
                    if opaque:
                        self.settings_doc[k] = v
                    else:
                        t = self.settings_schema[k]["type"]
                        self.settings_doc[k] = settings_mod.coerce(t, v)
                try:
                    settings_mod.atomic_write(po.CONFIG_FILE,
                                              self.settings_doc)
                except OSError as e:
                    self._ack_err(cid, protocol.E_IO_ERROR, str(e), None)
                    return
            applied = sorted(values)
            self.emit.settings_changed(applied, "cmd")
            if opaque:
                self._ack_ok(cid, {"applied": applied})
            else:
                run_active = bool(st.running or st.paused)
                self._ack_ok(cid, {
                    "applied": applied,
                    "effective": "next-run" if run_active else "now",
                    "schemaVersion": settings_mod.SCHEMA_VERSION})
        elif cmd == "settings.validate":
            values = params.get("values")
            if not isinstance(values, dict):
                self._ack_err(cid, protocol.E_BAD_PARAMS,
                              "values must be an object", None)
                return
            per_key = settings_mod.validate(self.settings_schema, values)
            r = {"ok": not per_key}
            if per_key:
                r["perKey"] = per_key
            self._ack_ok(cid, r)
        elif cmd == "settings.reload":
            if st.running or st.paused:
                self._ack_err(cid, protocol.E_RUN_ACTIVE,
                              "idle-only while a run exists",
                              {"state": self._state()})
                return
            with self._settings_lock:
                old = dict(self.settings_doc)
                try:
                    self.settings_doc, _c = settings_mod.migrate_file(
                        po.CONFIG_FILE, self.settings_schema)
                except settings_mod.SchemaTooNew as e:
                    self._ack_err(cid, protocol.E_SCHEMA_TOO_NEW,
                                  "config file is from a newer engine",
                                  {"found": e.found,
                                   "supported": e.supported})
                    return
                changed = sorted(k for k in
                                 set(old) | set(self.settings_doc)
                                 if old.get(k) != self.settings_doc.get(k))
            if changed:
                self.emit.settings_changed(changed, "reload")
            self._ack_ok(cid, {"schemaVersion": settings_mod.SCHEMA_VERSION,
                               "changedKeys": changed})
        elif cmd == "settings.import":
            if st.running or st.paused:
                self._ack_err(cid, protocol.E_RUN_ACTIVE,
                              "idle-only while a run exists",
                              {"state": self._state()})
                return
            from_dir = params.get("fromDir")
            if not isinstance(from_dir, str) or not from_dir:
                self._ack_err(cid, protocol.E_BAD_PARAMS,
                              "fromDir required", None)
                return
            fpath = os.path.join(from_dir, "prospecting_config.json")
            if not os.path.exists(fpath):
                self._ack_err(cid, protocol.E_IO_ERROR,
                              "no config at %s" % fpath, None)
                return
            foreign, corrupt = settings_mod.read_doc(fpath)
            if corrupt:
                self._ack_err(cid, protocol.E_IO_ERROR,
                              "foreign config unreadable", None)
                return
            try:
                merged = settings_mod.migrate_doc(foreign,
                                                  self.settings_schema)
            except settings_mod.SchemaTooNew as e:
                self._ack_err(cid, protocol.E_SCHEMA_TOO_NEW,
                              "foreign config is from a newer engine",
                              {"found": e.found, "supported": e.supported})
                return
            with self._settings_lock:
                old = dict(self.settings_doc)
                self.settings_doc = merged
                try:
                    settings_mod.atomic_write(po.CONFIG_FILE,
                                              self.settings_doc)
                except OSError as e:
                    self.settings_doc = old
                    self._ack_err(cid, protocol.E_IO_ERROR, str(e), None)
                    return
                changed = sorted(k for k in
                                 set(old) | set(self.settings_doc)
                                 if old.get(k) != self.settings_doc.get(k))
            self.emit.settings_changed(changed, "import")
            self._ack_ok(cid, {"schemaVersion": settings_mod.SCHEMA_VERSION,
                               "changedKeys": changed})
        elif cmd == "script.setActive":
            if st.running or st.paused:
                self._ack_err(cid, protocol.E_RUN_ACTIVE,
                              "idle-only while a run exists",
                              {"state": self._state()})
                return
            name = params.get("name")
            script = params.get("script")
            if name is not None and not isinstance(name, str):
                self._ack_err(cid, protocol.E_BAD_PARAMS,
                              "name must be a string or null", None)
                return
            writes = {}
            if name is None:
                writes["SCRIPT_MODE"] = False
            else:
                if script is not None:
                    raw = json.dumps(script)
                    runner = po.ScriptRunner(raw, name)
                    if runner.dead:
                        self._ack_err(cid, protocol.E_VALIDATION_FAILED,
                                      "script rejected: %s" % runner.dead,
                                      {"reason": runner.dead})
                        return
                    writes["SCRIPT_JSON"] = raw
                writes["SCRIPT_MODE"] = True
                writes["SCRIPT_ACTIVE"] = name
            with self._settings_lock:
                self.settings_doc.update(writes)
                try:
                    settings_mod.atomic_write(po.CONFIG_FILE,
                                              self.settings_doc)
                except OSError as e:
                    self._ack_err(cid, protocol.E_IO_ERROR, str(e), None)
                    return
            self.emit.settings_changed(sorted(writes), "cmd")
            self._ack_ok(cid, {"active": name})
        elif cmd == "recorder.start":
            from prospector_engine import recorder as recorder_mod
            if st.running or st.paused:
                self._ack_err(cid, protocol.E_RUN_ACTIVE,
                              "idle-only while a run exists",
                              {"state": self._state()})
                return
            if not recorder_mod.available():
                self._ack_err(cid, protocol.E_UNSUPPORTED,
                              "input capture is unavailable on this install",
                              None)
                return
            if self._recorder is None:
                scale = 1.0
                try:
                    with po._MSS() as sct:
                        scale = po.get_scale(sct)
                except Exception:
                    pass
                self._recorder = recorder_mod.Recorder(
                    po.V3_KEYCODES, scale=scale)
            r = self._recorder.start()
            if not r.get("ok"):
                self._ack_err(cid, protocol.E_BAD_STATE, r.get("error", ""),
                              None)
                return
            self._ack_ok(cid, {"recording": True})
        elif cmd == "recorder.stop":
            if self._recorder is None or not self._recorder.recording:
                # a capture that hit its cap or Secure Input still has events
                if self._recorder is not None and self._recorder.events:
                    r = {"ok": True, "events": list(self._recorder.events),
                         "truncated": self._recorder.truncated,
                         "durationMs": 0,
                         "reason": self._recorder.stop_reason or "stopped"}
                    self._recorder.events = []
                    self._ack_ok(cid, {k: v for k, v in r.items()
                                       if k != "ok"})
                    return
                self._ack_err(cid, protocol.E_BAD_STATE, "not recording",
                              None)
                return
            r = self._recorder.stop()
            self._ack_ok(cid, {k: v for k, v in r.items() if k != "ok"})
        elif cmd == "recorder.status":
            if self._recorder is None:
                self._ack_ok(cid, {"recording": False, "count": 0,
                                   "secureInput": False, "truncated": False})
            else:
                self._ack_ok(cid, self._recorder.status())
        elif cmd == "vision.assetStat":
            ids = params.get("ids")
            if not isinstance(ids, list):
                self._ack_err(cid, protocol.E_BAD_PARAMS,
                              "ids must be a list", None)
                return
            try:
                self._ack_ok(cid, self.sensing.vision_asset_stat(ids))
            except Exception as e:
                self._ack_err(cid, protocol.E_INTERNAL,
                              "vision failure: %r" % (e,), None)
        elif cmd == "vision.testMatch":
            # idle-only like the calibration verbs: it owns the capture
            # session and reads the screen (protocol 4.15 semantics)
            if st.running or st.paused:
                self._ack_err(cid, protocol.E_RUN_ACTIVE,
                              "idle-only while a run exists",
                              {"state": self._state()})
                return
            try:
                self._ack_ok(cid, self.sensing.vision_test_match(
                    params.get("png"), params.get("threshold"),
                    params.get("rect")))
            except sensing_mod.SensingError as e:
                self._ack_err(cid, e.code, e.message, e.data)
            except Exception as e:
                self._ack_err(cid, protocol.E_INTERNAL,
                              "vision failure: %r" % (e,), None)
        elif cmd == "plan.describe":
            # 1.4: the canonical Cycle Plan. Legal idle or running -- it
            # only READS globals. While idle it first re-binds the config
            # exactly the way tick_hook does at run start, so the plan
            # always describes what the NEXT run will execute with
            # (settings.set applies at run start; the plan must agree).
            # While a run exists the live (already-bound) globals are the
            # truth and are read as-is -- never re-bind mid-run.
            from . import cycleplan
            with self.state_lock:
                if not (st.running or st.paused):
                    try:
                        po.load_config()
                    except Exception:
                        pass
                try:
                    plan = cycleplan.resolve_cycle_plan(po)
                except Exception as e:
                    self._ack_err(cid, protocol.E_INTERNAL,
                                  "plan resolution failed: %r" % (e,), None)
                    return
            self._ack_ok(cid, {"plan": plan})
        elif cmd in ("recovery.trigger", "flow.trigger"):
            # 1.4 additive verbs: queue a manual recovery-rung / flow
            # firing for the tick thread (running only -- the boundary
            # consumes it before the next supervisor/script tick)
            tid = params.get("id")
            if not isinstance(tid, str) or not tid:
                self._ack_err(cid, protocol.E_BAD_PARAMS, "id required",
                              None)
                return
            if not st.running or st.paused \
                    or getattr(st, "safe_paused", False):
                self._ack_err(cid, protocol.E_BAD_STATE,
                              "running only", {"state": self._state()})
                return
            if cmd == "recovery.trigger":
                ok = bool(po.recovery_manual_trigger(tid))
                what = "rung"
            else:
                ok = bool(po.flow_manual_trigger(tid))
                what = "flow"
            if not ok:
                self._ack_err(cid, protocol.E_BAD_PARAMS,
                              "unknown %s id" % what, {"id": tid})
                return
            self._ack_ok(cid, {"queued": True, "id": tid})
        elif cmd.startswith("calibration.") and cmd in protocol.COMMANDS:
            self._dispatch_calibration(cid, cmd, params)
        else:
            self._ack_err(cid, protocol.E_UNSUPPORTED,
                          "unknown command", {"cmd": cmd})

    def _dispatch_calibration(self, cid, cmd, params):
        """The 4.15 sensing class: idle-only (RUN_ACTIVE while any run
        substate exists), control-thread execution, never touches the run
        state machine. Session ops without a live capture session NACK
        BAD_STATE {expected:"captureSession"}."""
        po = self.po
        st = po.State
        if st.running or st.paused:
            self._ack_err(cid, protocol.E_RUN_ACTIVE,
                          "idle-only while a run exists",
                          {"state": self._state()})
            return
        s = self.sensing
        try:
            if cmd == "calibration.detectWindow":
                r = s.detect_window()
                out = {"found": bool(r.get("found"))}
                if r.get("found"):
                    out["rect"] = {"x": r["x"], "y": r["y"],
                                   "w": r["w"], "h": r["h"]}
                    if r.get("title"):
                        out["title"] = r["title"]
                self._ack_ok(cid, out)
            elif cmd == "calibration.capture":
                self._ack_ok(cid, s.capture())
            elif cmd == "calibration.pick":
                fx, fy = params.get("fx"), params.get("fy")
                if not isinstance(fx, (int, float)) \
                        or not isinstance(fy, (int, float)):
                    self._ack_err(cid, protocol.E_BAD_PARAMS,
                                  "fx and fy fractions required", None)
                    return
                self._ack_ok(cid, s.pick(fx, fy))
            elif cmd == "calibration.crop":
                rect = params.get("rect")
                if not (isinstance(rect, dict)
                        and all(isinstance(rect.get(k), (int, float))
                                for k in ("x", "y", "w", "h"))):
                    self._ack_err(cid, protocol.E_BAD_PARAMS,
                                  "rect {x,y,w,h} required", None)
                    return
                self._ack_ok(cid, s.crop(rect, params.get("zoom")))
            elif cmd == "calibration.sampleSaved":
                r = s.sample_saved()
                if "error" in r:
                    self._ack_err(cid, protocol.E_INTERNAL, r["error"], None)
                    return
                self._ack_ok(cid, r)
            elif cmd == "calibration.detect":
                target = params.get("target")
                if target not in ("capacityBar", "cuePrompt"):
                    self._ack_err(cid, protocol.E_BAD_PARAMS,
                                  'target must be "capacityBar" or '
                                  '"cuePrompt"', {"target": target})
                    return
                self._ack_ok(cid, s.detect(target, params.get("cue")))
            elif cmd == "calibration.testRead":
                target = params.get("target")
                if target not in ("find", "earnings"):
                    self._ack_err(cid, protocol.E_BAD_PARAMS,
                                  'target must be "find" or "earnings"',
                                  {"target": target})
                    return
                caps = capabilities(po, self.emit.simulated)
                cap_key = ("findsOcr" if target == "find"
                           else "earningsOcr")
                if not caps.get(cap_key):
                    self._ack_err(cid, protocol.E_UNSUPPORTED,
                                  "OCR not available on this platform",
                                  {"capability": cap_key})
                    return
                self._ack_ok(cid, s.test_read(target))
            elif cmd == "calibration.cueMask":
                self._dispatch_cue_mask(cid, params)
            elif cmd == "calibration.health":
                self._ack_ok(cid, s.health())
            elif cmd == "calibration.auto":
                self._ack_ok(cid, s.auto(apply=bool(params.get("apply"))))
            elif cmd == "calibration.savePixels":
                pixels = params.get("pixels")
                if not isinstance(pixels, dict):
                    self._ack_err(cid, protocol.E_BAD_PARAMS,
                                  "pixels object required", None)
                    return
                try:
                    r = s.save_pixels(pixels, colors=params.get("colors"),
                                      fr=params.get("fr"),
                                      ratios=params.get("ratios"),
                                      window_rect=params.get("windowRect"))
                except OSError as e:
                    self._ack_err(cid, protocol.E_IO_ERROR, str(e), None)
                    return
                self._ack_ok(cid, r)
        except sensing_mod.SensingError as e:
            self._ack_err(cid, e.code, e.message, e.data)
        except Exception as e:
            self._ack_err(cid, protocol.E_INTERNAL,
                          "sensing failure: %r" % (e,), None)

    def _dispatch_cue_mask(self, cid, params):
        s = self.sensing
        op = params.get("op")
        if op == "status":
            self._ack_ok(cid, s.cue_status())
        elif op == "beginCapture":
            cue = params.get("cue")
            if cue not in sensing_mod.CUE_PIXEL_KEY:
                self._ack_err(cid, protocol.E_BAD_PARAMS,
                              "cue must be PAN, SHAKE or DEPOSIT",
                              {"cue": cue})
                return
            self._ack_ok(cid, s.cue_begin(cue, params.get("thresh")))
        elif op == "toggle":
            fx, fy = params.get("fx"), params.get("fy")
            if not isinstance(fx, (int, float)) \
                    or not isinstance(fy, (int, float)):
                self._ack_err(cid, protocol.E_BAD_PARAMS,
                              "fx and fy fractions required", None)
                return
            r = s.cue_toggle(fx, fy)
            if "error" in r:
                self._ack_err(cid, protocol.E_BAD_STATE, r["error"],
                              {"expected": "captureSession"})
                return
            self._ack_ok(cid, r)
        elif op == "reset":
            r = s.cue_reset()
            if "error" in r:
                self._ack_err(cid, protocol.E_BAD_STATE, r["error"],
                              {"expected": "captureSession"})
                return
            self._ack_ok(cid, r)
        elif op == "save":
            r = s.cue_save()
            if "error" in r:
                data = ({"expected": "captureSession"}
                        if r["error"] == "not editing" else None)
                self._ack_err(cid, protocol.E_BAD_STATE, r["error"], data)
                return
            self._ack_ok(cid, r)
        elif op == "clear":
            cue = params.get("cue")
            if not isinstance(cue, str) or not cue:
                self._ack_err(cid, protocol.E_BAD_PARAMS,
                              "cue required", None)
                return
            r = s.cue_clear(cue)
            if not r.get("ok"):
                self._ack_err(cid, protocol.E_IO_ERROR,
                              r.get("error", "write failed"), None)
                return
            self._ack_ok(cid, {"cleared": True})
        else:
            self._ack_err(cid, protocol.E_BAD_PARAMS,
                          "unknown cueMask op", {"op": op})


def bootstrap(po, home, host, requested_protocol, simulated):
    """Called by the engine's main() when --ipc is on, BEFORE load_config
    (section 11.2 startup order; the lock precedes config work -- ISS-156).
    Swaps EMIT, runs vestibule/protocol/lock/migration. main() then binds
    config (load_config) and calls .go() for hello + threads."""
    srv = Server(po, home, host, simulated)
    po.EMIT = srv.emit
    return srv.start(requested_protocol)
