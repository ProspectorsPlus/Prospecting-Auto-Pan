"""PPE1 Python EngineClient (Phase 04, checkpoint C3).

Lite's side of the protocol -- and the reference driver the contract
tests use ("one protocol, two clients, one engine"; the TypeScript
client lands with Phase 05). Owns: spawn with the section 1 argv
contract, the separate stderr pipe + section 11.1 vestibule, hello
gating (10 s), ack correlation by id with section 8 timeout classes,
heartbeat-gap tracking, the section 8 escalation ladder, and the exit
taxonomy of section 9.1 (EOF with a prior bye = clean; without = crash).

The client never interprets diagnostic lines (section 2) -- they are
forwarded verbatim to on_diag. Events are dispatched in arrival order on
the reader thread; handlers must not call request() from that thread
(the ack they wait for could only be read by the thread they block).
"""
import os
import subprocess
import sys
import threading
import time as _time

from . import protocol

HELLO_TIMEOUT_S = 10.0
ACK_FAST_S = 3.0
ACK_SLOW_S = {"run.start": 10.0, "run.stop": 10.0, "settings.import": 30.0}
ACK_CAPTURE_S = 10.0
HEARTBEAT_GAP_S = 6.0          # 3 missed beats -> unresponsive
SHUTDOWN_EXIT_S = 5.0
STDERR_TAIL = 200
EVENT_TAIL = 200


class EngineExit(object):
    """How the engine ended, per section 9.1."""

    def __init__(self, code, clean, bye):
        self.code = code          # process return code (None if killed by us)
        self.clean = clean        # True when a bye preceded EOF
        self.bye = bye            # the bye event data (or None on crash)


class EngineClient(object):
    def __init__(self, argv_base, home, host="lite", cwd=None,
                 protocol_major=None, on_event=None, on_diag=None,
                 on_exit=None, allow_simulated=False, extra_args=None):
        self.argv_base = list(argv_base)
        self.home = os.path.abspath(home)
        self.host = host
        self.cwd = cwd
        self.protocol_major = protocol_major
        self.on_event = on_event
        self.on_diag = on_diag
        self.on_exit = on_exit
        self.allow_simulated = allow_simulated
        self.extra_args = list(extra_args or [])
        self.proc = None
        self.hello = None                 # engine.hello data once received
        self.vestibule = None             # parsed section 11.1 stderr line
        self.stderr_tail = []
        self.recent_events = []           # last EVENT_TAIL events (crash report)
        self.exit_info = None             # EngineExit once ended
        self.refused = None               # fatal bye data on startup refusal
        self.last_heartbeat = None
        self._bye_seen = None
        self._hello_evt = threading.Event()
        self._ended_evt = threading.Event()
        self._pending = {}                # id -> {evt, ack, deadline}
        self._plock = threading.Lock()
        self._wlock = threading.Lock()
        self._cid = 0
        self.protocol_errors = 0

    # -- spawn -------------------------------------------------------------
    def spawn(self):
        args = self.argv_base + ["--ipc", "--home", self.home,
                                 "--host", self.host]
        if self.protocol_major is not None:
            args += ["--protocol", str(self.protocol_major)]
        args += self.extra_args
        self.proc = subprocess.Popen(
            args, cwd=self.cwd, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()
        threading.Thread(target=self._watch_timeouts, daemon=True).start()
        return self

    def wait_ready(self, timeout=HELLO_TIMEOUT_S):
        """Block until hello (True) or startup failure (False). On failure
        the process is dead and stderr_tail/refused explain why."""
        ok = self._hello_evt.wait(timeout)
        if ok and self.hello is not None:
            h = self.hello
            if h.get("protocol", {}).get("major") != protocol.PROTOCOL_MAJOR:
                self._refuse_local("engine/app protocol mismatch "
                                   "(engine %r, app %d)"
                                   % (h.get("protocol"),
                                      protocol.PROTOCOL_MAJOR))
                return False
            if (h.get("capabilities", {}).get("simulated")
                    and not self.allow_simulated):
                self._refuse_local("refusing a simulated engine "
                                   "(shipping host, HOST-SIM-1)")
                return False
            return True
        if self._ended_evt.is_set():
            return False                  # refused (bye) or died pre-hello
        self._refuse_local("engine failed to start (no hello in %ds)"
                           % int(timeout))
        return False

    def _refuse_local(self, why):
        self._diag("[engine-client] %s" % why)
        if self.refused is None:
            self.refused = {"reason": "fatal", "code": "HOST_REFUSED",
                            "message": why}
        self.kill()

    # -- plumbing ----------------------------------------------------------
    def _diag(self, line):
        if self.on_diag:
            try:
                self.on_diag(line)
            except Exception:
                pass

    def _read_stdout(self):
        p = self.proc
        try:
            for raw in iter(p.stdout.readline, ""):
                try:
                    kind, obj = protocol.decode_line(raw)
                except protocol.ProtocolError as e:
                    self.protocol_errors += 1
                    self._diag("[engine-client] protocol error: %s" % e)
                    continue
                if kind == "diag":
                    self._diag(obj)
                    continue
                t = obj.get("t")
                if t == "ack":
                    self._resolve(obj["id"], obj)
                elif t == "ev":
                    self._on_ev(obj)
        except Exception:
            pass
        self._on_eof()

    def _on_ev(self, obj):
        ev = obj["ev"]
        if ev == "engine.hello":
            self.hello = obj["data"]
            self.last_heartbeat = _time.monotonic()
            self._hello_evt.set()
        elif ev == "engine.heartbeat":
            self.last_heartbeat = _time.monotonic()
        elif ev == "engine.bye":
            self._bye_seen = obj["data"]
            if obj["data"].get("reason") == "fatal":
                self.refused = obj["data"]
        self.recent_events.append(obj)
        if len(self.recent_events) > EVENT_TAIL:
            del self.recent_events[:len(self.recent_events) - EVENT_TAIL]
        if self.on_event:
            try:
                self.on_event(obj)
            except Exception:
                pass

    def _read_stderr(self):
        p = self.proc
        try:
            for raw in iter(p.stderr.readline, ""):
                line = raw.rstrip("\r\n")
                if self.vestibule is None:
                    v = protocol.parse_vestibule(line)
                    if v is not None:
                        self.vestibule = v
                        continue
                self.stderr_tail.append(line)
                if len(self.stderr_tail) > STDERR_TAIL:
                    del self.stderr_tail[:len(self.stderr_tail) - STDERR_TAIL]
        except Exception:
            pass

    def _on_eof(self):
        try:
            code = self.proc.wait(timeout=5)
        except Exception:
            code = None
        self.exit_info = EngineExit(code, self._bye_seen is not None,
                                    self._bye_seen)
        self._ended_evt.set()
        self._hello_evt.set()
        with self._plock:
            pending, self._pending = dict(self._pending), {}
        for cid, rec in pending.items():
            rec["ack"] = {"t": "ack", "id": cid, "ok": False,
                          "error": {"code": protocol.E_ENGINE_EXITED,
                                    "message": "engine exited before ack"}}
            rec["evt"].set()
        if self.on_exit:
            try:
                self.on_exit(self.exit_info)
            except Exception:
                pass

    def _resolve(self, cid, ack):
        with self._plock:
            rec = self._pending.pop(cid, None)
        if rec is not None:
            rec["ack"] = ack
            rec["evt"].set()

    def _watch_timeouts(self):
        while not self._ended_evt.is_set():
            now = _time.monotonic()
            overdue = []
            with self._plock:
                for cid, rec in list(self._pending.items()):
                    if now > rec["deadline"]:
                        overdue.append((cid, self._pending.pop(cid)))
            for cid, rec in overdue:
                rec["ack"] = {"t": "ack", "id": cid, "ok": False,
                              "error": {"code": protocol.E_ACK_TIMEOUT,
                                        "message": "no ack within budget"}}
                rec["evt"].set()
            self._ended_evt.wait(0.25)

    # -- liveness ----------------------------------------------------------
    def responsive(self):
        if self.last_heartbeat is None:
            return self.alive()
        return (_time.monotonic() - self.last_heartbeat) <= HEARTBEAT_GAP_S

    def alive(self):
        return self.proc is not None and self.proc.poll() is None

    # -- commands ----------------------------------------------------------
    def _budget(self, cmd):
        cls = protocol.COMMANDS.get(cmd, "fast")
        if cmd in ACK_SLOW_S:
            return ACK_SLOW_S[cmd]
        if cls == "capture":
            return ACK_CAPTURE_S
        if cls == "slow":
            return 10.0
        return ACK_FAST_S

    def request(self, cmd, params=None, timeout=None):
        """Send one command, block for its ack (or a synthesized
        ACK_TIMEOUT/ENGINE_EXITED error). Returns the ack frame dict."""
        if not self.alive():
            return {"t": "ack", "id": "-", "ok": False,
                    "error": {"code": protocol.E_ENGINE_EXITED,
                              "message": "engine not running"}}
        with self._plock:
            self._cid += 1
            cid = "c-%d" % self._cid
            budget = timeout if timeout is not None else self._budget(cmd)
            rec = {"evt": threading.Event(), "ack": None,
                   "deadline": _time.monotonic() + budget}
            self._pending[cid] = rec
        line = protocol.encode_cmd(cid, cmd, params)
        try:
            with self._wlock:
                self.proc.stdin.write(line)
                self.proc.stdin.flush()
        except Exception:
            self._resolve(cid, {"t": "ack", "id": cid, "ok": False,
                                "error": {"code": protocol.E_ENGINE_EXITED,
                                          "message": "engine stdin closed"}})
        rec["evt"].wait(budget + 1.0)
        return rec["ack"] or {"t": "ack", "id": cid, "ok": False,
                              "error": {"code": protocol.E_ACK_TIMEOUT,
                                        "message": "no ack within budget"}}

    def fire(self, cmd, params=None, on_ack=None):
        """Fire-and-correlate from a UI thread: request() on a worker so
        the caller never blocks; late/error acks go to on_ack (or diag)."""
        def _run():
            ack = self.request(cmd, params)
            if on_ack:
                try:
                    on_ack(ack)
                except Exception:
                    pass
            elif not ack.get("ok"):
                err = ack.get("error", {})
                self._diag("[engine-client] %s failed: %s %s"
                           % (cmd, err.get("code"), err.get("message")))
        threading.Thread(target=_run, daemon=True).start()

    # -- shutdown ladder (section 8) --------------------------------------
    def shutdown(self, on_force_kill=None):
        """Clean stop: engine.shutdown -> wait exit 5 s -> terminate 2 s ->
        kill. Returns the EngineExit. on_force_kill runs only if we had to
        force-kill (host must then release the injectable vocabulary)."""
        forced = False
        if self.alive():
            self.request("engine.shutdown")
            try:
                self.proc.wait(timeout=SHUTDOWN_EXIT_S)
            except subprocess.TimeoutExpired:
                forced = True
                try:
                    self.proc.terminate()
                    self.proc.wait(timeout=2)
                except Exception:
                    try:
                        self.proc.kill()
                        self.proc.wait(timeout=2)
                    except Exception:
                        pass
        self._ended_evt.wait(2)
        if forced and on_force_kill:
            try:
                on_force_kill(self.injectable())
            except Exception:
                pass
        return self.exit_info

    def kill(self):
        if self.alive():
            try:
                self.proc.kill()
            except Exception:
                pass

    def injectable(self):
        """The release-floor vocabulary (section 10.1). engine.describe's
        result overrides once served (C5); until then the library floor --
        the same values by construction."""
        return {"keys": list(protocol.INJECTABLE_KEYS),
                "buttons": list(protocol.INJECTABLE_BUTTONS)}
