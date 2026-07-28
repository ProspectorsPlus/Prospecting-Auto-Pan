#!/usr/bin/env python3
"""PPE1 contract tests (Phase 04). Stdlib-only, house style of
studio_tests.py. Spawns the REAL engine process with --ipc --sim and
asserts the protocol contract from phase-02-contract-tests.md.

Coverage grows with the extraction checkpoints:
  C2 (this file today): framing, hello/heartbeat/bye lifecycle, vestibule,
      engine.log wrapping, seq monotonicity, EOF host-death protection,
      instance lock, ping/shutdown/pause/resume/relic commands, UNSUPPORTED
      NACKs, run.started/run.stats/run.stopped/safety.* event mapping,
      legacy golden replay (via engine_characterization.py).
  C4/C5 add: run.start/stop/softStop, settings.*, calibration.*, script
      commands, full event vocabulary, parity transcripts.
"""
import json
import os
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
from prospector_engine import protocol  # noqa: E402

ENGINE = os.path.join(ROOT, "prospecting_old.py")
FAILS = []


def chk(cond, msg):
    if cond:
        print("  [PASS] %s" % msg)
    else:
        FAILS.append(msg)
        print("  [FAIL] %s" % msg)


def spawn(scenario, extra_args=None, home=None):
    home = home or tempfile.mkdtemp(prefix="ppe-home-")
    args = [sys.executable, ENGINE, "--ipc", "--home", home,
            "--host", "contract-test", "--sim",
            os.path.join(ROOT, "engine_scenarios", scenario + ".json")]
    args += (extra_args or [])
    return subprocess.Popen(args, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", bufsize=1, cwd=ROOT)


def run_batch(scenario, extra_args=None, timeout=180, home=None):
    """Run a self-terminating sim scenario with stdin HELD OPEN (closing
    stdin means 'host died' per protocol section 10.2 and would race the
    scenario). Returns (stdout, stderr, returncode)."""
    p = spawn(scenario, extra_args=extra_args, home=home)
    try:
        out = p.stdout.read()          # engine exits by itself -> EOF
        err = p.stderr.read()
        p.stdin.close()
        rc = p.wait(timeout=timeout)
    finally:
        if p.poll() is None:
            p.kill()
            out, err, rc = out or "", err or "", -1
    return out, err, rc


def parse_stream(text):
    frames, diags, errors = [], [], 0
    for line in text.splitlines():
        try:
            kind, obj = protocol.decode_line(line)
        except protocol.ProtocolError:
            errors += 1
            continue
        (frames if kind == "frame" else diags).append(obj if kind == "frame"
                                                      else line)
    return frames, diags, errors


def events(frames, name=None):
    evs = [f for f in frames if f.get("t") == "ev"]
    return [e for e in evs if name is None or e["ev"] == name]


def acks(frames):
    return {f["id"]: f for f in frames if f.get("t") == "ack"}


# ---------------------------------------------------------------------------
def test_batch_run():
    """A scenario that runs to completion by itself: framing + event
    mapping + bye. (script-bagguard: run starts, 3 passes, bag-full stop,
    scheduled quit.)"""
    print("[contract] batch run (script-bagguard)")
    out, err, rc = run_batch("script-bagguard")
    frames, diags, perr = parse_stream(out)
    chk(rc == 0, "frame.exit-zero: clean exit (rc=%r)" % rc)
    chk(perr == 0, "frame.magic-and-json: zero malformed PPE1 lines")
    legacy = [d for d in diags if d.startswith(("__", "[RUNNING]", "[PAUSED]",
                                                "[STOPPED]"))]
    chk(not legacy, "frame.no-legacy-emits: no legacy markers on stdout "
        "(got %r)" % legacy[:2])
    seqs = [f["seq"] for f in events(frames)]
    chk(seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
        and seqs and seqs[0] == 1,
        "frame.seq-monotonic: gapless from 1 (n=%d)" % len(seqs))
    evs = events(frames)
    chk(evs and evs[0]["ev"] == "engine.hello",
        "ev.hello.first: hello is frame #1")
    hello = evs[0]["data"]
    chk(hello["capabilities"]["simulated"] is True,
        "ev.hello.simulated: sim engine declares simulated=true")
    chk(hello["protocol"]["major"] == 1, "ev.hello.protocol-major=1")
    starts = events(frames, "run.started")
    chk(len(starts) == 1 and starts[0]["data"].get("mode") == "script",
        "ev.run-started: one start, mode=script (got %r)"
        % [s["data"] for s in starts])
    rid = (starts or [{"data": {}}])[0]["data"].get("runId")
    chk(bool(rid), "ev.run-started: carries runId")
    stops = events(frames, "run.stopped")
    chk(len(stops) == 1 and stops[0]["data"]["reason"] == "bag-full",
        "ev.run-stopped: reason bag-full")
    chk(stops[0]["data"].get("runId") == rid,
        "ev.run-stopped: same runId as start")
    fin = stops[0]["data"].get("final") or {}
    chk(fin.get("raw", {}).get("cycles") == 3,
        "ev.run-stopped.final: fresh final stats (raw.cycles=3)")
    chk(len(events(frames, "script.block")) >= 1,
        "ev.script-block: emitted during script run")
    logs = events(frames, "engine.log")
    chk(any("RUNNING" in e["data"]["text"] for e in logs),
        "ev.log.wraps-legacy-prints")
    byes = events(frames, "engine.bye")
    chk(len(byes) == 1 and byes[0]["data"]["reason"] == "shutdown"
        and evs[-1]["ev"] == "engine.bye",
        "ev.bye.on-quit-hotkey: clean quit ends with bye")
    vest = protocol.parse_vestibule((err.splitlines() or [""])[0])
    chk(bool(vest) and vest["major"] == 1,
        "ver.stderr-version-line: vestibule is stderr line 1")


def test_safety_ladder():
    """stuck-ladder scenario in ipc mode: safety.* mapping."""
    print("[contract] safety ladder (stuck-ladder)")
    out, _err, _rc = run_batch("stuck-ladder")
    frames, _d, perr = parse_stream(out)
    chk(perr == 0, "ladder: zero malformed frames")
    sp = events(frames, "safety.safePaused")
    chk(len(sp) == 3 and [e["data"]["attempt"] for e in sp] == [1, 2, 3],
        "ev.safe-paused.attempt-count: attempts 1..3")
    chk(all(e["data"]["maxAttempts"] == 3 and e["data"]["retryInS"] == 60
            for e in sp), "ev.safe-paused: max/retryInS carried")
    hs = events(frames, "safety.hardStopped")
    chk(len(hs) == 1 and hs[0]["data"]["retriesExhausted"] is True,
        "ev.hard-stop.escalation: after retries exhausted")
    se = events(frames, "safety.event")
    chk(any(e["data"]["type"] == "safe_stop" for e in se),
        "ev.safety-event: safe_stop present")
    chk(all(e["data"]["dirty"] is True for e in se
            if e["data"]["type"] == "safe_stop"),
        "ev.safety-event.dirty: matches _DIRTY_EVENTS")
    stops = events(frames, "run.stopped")
    chk(len(stops) == 1 and stops[0]["data"]["reason"] == "safe-stop",
        "ev.run-stopped: reason safe-stop after hard stop")
    stats = events(frames, "run.stats")
    chk(len(stats) >= 10, "ev.stats.cadence: 2s cadence over a long run "
        "(n=%d)" % len(stats))
    st = (stats or [{"data": {}}])[0]["data"]
    chk(set(st.keys()) >= {"raw", "derived", "meta", "runId"},
        "ev.stats.raw-derived-split: structure present")
    chk(set(st.get("raw", {})) == set(protocol.STATS_RAW_KEYS)
        and set(st.get("derived", {})) == set(protocol.STATS_DERIVED_KEYS)
        and set(st.get("meta", {})) == set(protocol.STATS_META_KEYS),
        "ev.stats.key-partition: exact 22/14/5 key split")


def _send(p, cid, cmd, params=None):
    p.stdin.write(protocol.encode_cmd(cid, cmd, params))
    p.stdin.flush()


def _read_until_acks(p, want_ids, timeout_s=20):
    got = {}
    evs = []
    deadline = time.time() + timeout_s
    while time.time() < deadline and set(want_ids) - set(got):
        line = p.stdout.readline()
        if not line:
            break
        try:
            kind, obj = protocol.decode_line(line)
        except protocol.ProtocolError:
            continue
        if kind != "frame":
            continue
        if obj["t"] == "ack":
            got[obj["id"]] = obj
        elif obj["t"] == "ev":
            evs.append(obj)
    return got, evs


def test_commands():
    """Interactive engine: command acks, NACK codes, shutdown."""
    print("[contract] command channel (interactive-run)")
    p = spawn("interactive-run")
    try:
        _send(p, "c-1", "engine.ping")
        a, _ = _read_until_acks(p, ["c-1"])
        chk(a.get("c-1", {}).get("ok") is True,
            "cmd.ping: acked ok")
        # wait for the scheduled toggle to start the run
        deadline = time.time() + 15
        started = False
        while time.time() < deadline and not started:
            line = p.stdout.readline()
            if not line:
                break
            try:
                kind, obj = protocol.decode_line(line)
            except protocol.ProtocolError:
                continue
            started = (kind == "frame" and obj.get("t") == "ev"
                       and obj["ev"] == "run.started")
            if started:
                chk(obj["data"].get("origin") == "hotkey",
                    "ev.run-started.origin-hotkey: sim chord tagged hotkey")
        chk(started, "run started via scheduled sim hotkey")
        _send(p, "c-2", "run.resume")           # not paused -> BAD_STATE
        _send(p, "c-3", "run.pause")
        a, evs = _read_until_acks(p, ["c-2", "c-3"])
        chk(a.get("c-2", {}).get("ok") is False
            and a["c-2"]["error"]["code"] == "BAD_STATE",
            "cmd.resume.bad-state: NACK BAD_STATE when not paused")
        chk(a.get("c-3", {}).get("ok") is True,
            "cmd.pause: acked ok")
        chk(any(e["ev"] == "run.paused" for e in evs),
            "ev.run-paused: emitted on command pause")
        _send(p, "c-4", "run.pause")            # already paused
        _send(p, "c-5", "run.resume")
        _send(p, "c-6", "relic.set", {"index": 99, "seconds": 5})
        _send(p, "c-7", "totally.bogus")
        _send(p, "c-8", "calibration.capture")  # idle-only class, run active
        a, evs = _read_until_acks(p, ["c-4", "c-5", "c-6", "c-7", "c-8"])
        chk(a.get("c-4", {}).get("ok") is False
            and a["c-4"]["error"]["code"] == "BAD_STATE",
            "cmd.pause.bad-state: NACK when already paused")
        chk(a.get("c-5", {}).get("ok") is True, "cmd.resume: acked ok")
        chk(any(e["ev"] == "run.resumed" for e in evs),
            "ev.run-resumed: emitted on command resume")
        chk(a.get("c-6", {}).get("ok") is False
            and a["c-6"]["error"]["code"] == "BAD_PARAMS",
            "cmd.relic.bad-index-nacked: BAD_PARAMS (fixes silent swallow)")
        chk(a.get("c-7", {}).get("ok") is False
            and a["c-7"]["error"]["code"] == "UNSUPPORTED",
            "frame.unknown-command: UNSUPPORTED NACK")
        chk(a.get("c-8", {}).get("ok") is False
            and a["c-8"]["error"]["code"] == "RUN_ACTIVE",
            "cmd.calibration.run-active-rejected: sensing class refused "
            "while a run exists (4.15 idle-only)")
        _send(p, "c-9", "engine.shutdown")
        a, evs = _read_until_acks(p, ["c-9"])
        chk(a.get("c-9", {}).get("ok") is True, "cmd.shutdown.clean: acked")
        rest = p.stdout.read() or ""
        frames, _dg, _pe = parse_stream(rest)
        allev = evs + events(frames)
        chk(any(e["ev"] == "run.stopped"
                and e["data"]["reason"] == "shutdown" for e in allev),
            "cmd.shutdown: active run stopped with reason=shutdown")
        chk(any(e["ev"] == "engine.bye" for e in allev),
            "ev.bye.on-shutdown")
        p.wait(timeout=10)
        chk(p.returncode == 0, "cmd.shutdown: exit 0")
    finally:
        if p.poll() is None:
            p.kill()


def test_heartbeat_and_eof():
    """Heartbeats flow; stdin EOF = host death -> release + bye + exit."""
    print("[contract] heartbeat + host-death EOF")
    # idle-command idles at real pace (no scheduled toggle), so liveness
    # is observable deterministically -- interactive-run races its virtual
    # clock to the duration backstop within seconds once the run starts
    p = spawn("idle-command")
    try:
        deadline = time.time() + 15
        beats = 0
        while time.time() < deadline and beats < 2:
            line = p.stdout.readline()
            if not line:
                break
            try:
                kind, obj = protocol.decode_line(line)
            except protocol.ProtocolError:
                continue
            if kind == "frame" and obj.get("t") == "ev" \
                    and obj["ev"] == "engine.heartbeat":
                beats += 1
                chk("tickAgeS" in obj["data"] and "state" in obj["data"],
                    "ev.heartbeat: shape ok") if beats == 1 else None
        chk(beats >= 2, "ev.heartbeat.cadence: >=2 heartbeats observed")
        p.stdin.close()                      # host dies
        rest = p.stdout.read() or ""
        frames, _d, _e = parse_stream(rest)
        chk(any(f.get("t") == "ev" and f["ev"] == "engine.bye"
                for f in frames),
            "life.host-death-eof: bye emitted on stdin EOF")
        p.wait(timeout=10)
        chk(p.returncode == 0, "life.host-death-eof: engine exited")
    finally:
        if p.poll() is None:
            p.kill()


def test_instance_lock():
    """Second engine refuses with ENGINE_ALREADY_RUNNING, exit 2."""
    print("[contract] machine-scoped instance lock")
    a = spawn("interactive-run")
    try:
        # wait for first engine's hello so its lock is definitely held
        deadline = time.time() + 15
        held = False
        while time.time() < deadline and not held:
            line = a.stdout.readline()
            if not line:
                break
            try:
                kind, obj = protocol.decode_line(line)
            except protocol.ProtocolError:
                continue
            held = kind == "frame" and obj.get("t") == "ev" \
                and obj["ev"] == "engine.hello"
        chk(held, "first engine reached hello (lock held)")
        b = spawn("interactive-run")     # different --home, same machine lock
        out, err = b.communicate(timeout=30)
        frames, _d, _e = parse_stream(out)
        byes = events(frames, "engine.bye")
        chk(len(byes) == 1 and byes[0]["data"]["reason"] == "fatal"
            and byes[0]["data"]["code"] == "ENGINE_ALREADY_RUNNING",
            "life.instance-lock: bye fatal ENGINE_ALREADY_RUNNING")
        chk(byes and byes[0]["data"]["data"].get("ownerHost")
            == "contract-test",
            "life.instance-lock: names the owning host")
        chk(not events(frames, "engine.hello"),
            "life.instance-lock: no hello on refusal")
        chk(b.returncode == 2, "life.instance-lock: exit 2")
        vest = protocol.parse_vestibule((err.splitlines() or [""])[0])
        chk(bool(vest), "ver.stderr-version-line: vestibule present on "
                        "refusal too")
    finally:
        a.stdin.close()
        try:
            a.wait(timeout=10)
        except subprocess.TimeoutExpired:
            a.kill()


def test_protocol_mismatch():
    """--protocol 99 -> bye fatal PROTOCOL_UNSUPPORTED, exit 2, no hello."""
    print("[contract] protocol major mismatch")
    out, _err, rc = run_batch("start-idle-quit",
                              extra_args=["--protocol", "99"])
    frames, _d, _e = parse_stream(out)
    byes = events(frames, "engine.bye")
    chk(len(byes) == 1 and byes[0]["data"]["code"] == "PROTOCOL_UNSUPPORTED",
        "ver.major-mismatch-refused: bye fatal PROTOCOL_UNSUPPORTED")
    chk(not events(frames, "engine.hello"),
        "ver.major-mismatch-refused: no hello")
    chk(rc == 2, "ver.major-mismatch-refused: exit 2")


def _make_client(scenario="interactive-run", allow_simulated=True,
                 on_event=None):
    from prospector_engine.client import EngineClient
    return EngineClient(
        [sys.executable, ENGINE], home=tempfile.mkdtemp(prefix="ppe-home-"),
        host="lite-test", cwd=ROOT, allow_simulated=allow_simulated,
        on_event=on_event,
        extra_args=["--sim", os.path.join(ROOT, "engine_scenarios",
                                          scenario + ".json")])


def test_engine_client():
    """C3: Lite's Python EngineClient drives the engine end to end."""
    print("[contract] EngineClient (C3)")
    evs = []
    cli = _make_client(on_event=evs.append).spawn()
    try:
        chk(cli.wait_ready(), "client.ready: hello within the 10 s budget")
        chk(bool(cli.vestibule) and cli.vestibule["major"] == 1,
            "client.vestibule: stderr version line captured + parsed")
        a = cli.request("engine.ping")
        chk(a.get("ok") is True and "state" in a["result"],
            "client.request: ping acked with state")
        a = cli.request("relic.resetOne", {"index": 99})
        chk(a.get("ok") is False and a["error"]["code"] == "BAD_PARAMS",
            "client.request: engine NACK surfaces verbatim")
        deadline = time.time() + 15
        while time.time() < deadline and not any(
                e["ev"] == "run.started" for e in evs):
            time.sleep(0.1)
        chk(any(e["ev"] == "run.started" for e in evs),
            "client.events: run.started dispatched to on_event")
        a = cli.request("run.pause")
        chk(a.get("ok") is True, "client.request: run.pause ok mid-run")
        a = cli.request("run.resume")
        chk(a.get("ok") is True, "client.request: run.resume ok")
        info = cli.shutdown()
        chk(info is not None and info.clean and info.code == 0,
            "client.shutdown: EOF-with-bye = clean exit, code 0")
        chk(any(e["ev"] == "run.stopped"
                and e["data"]["reason"] == "shutdown" for e in evs),
            "client.shutdown: active run stopped with reason=shutdown")
    finally:
        cli.kill()


def test_client_refuses_simulated():
    """HOST-SIM-1: a shipping host refuses a simulated:true engine."""
    print("[contract] host refuses simulated engine (HOST-SIM-1)")
    cli = _make_client(allow_simulated=False).spawn()
    try:
        ok = cli.wait_ready()
        chk(ok is False, "host-sim-1: wait_ready refuses simulated engine")
        chk((cli.refused or {}).get("code") == "HOST_REFUSED",
            "host-sim-1: refusal recorded")
        cli._ended_evt.wait(10)
        chk(not cli.alive(), "host-sim-1: refused engine is terminated")
    finally:
        cli.kill()


def test_client_crash_taxonomy():
    """Section 9: EOF without bye = crash; in-flight/late commands fail
    ENGINE_EXITED (host-synthesized, never from the wire)."""
    print("[contract] crash taxonomy + ENGINE_EXITED synthesis")
    cli = _make_client().spawn()
    try:
        chk(cli.wait_ready(), "crash: engine ready")
        cli.proc.kill()                       # simulated engine crash
        cli._ended_evt.wait(10)
        chk(cli.exit_info is not None and cli.exit_info.clean is False,
            "life.crash-mid-run: EOF without bye classified as crash")
        a = cli.request("engine.ping")
        chk(a.get("ok") is False
            and a["error"]["code"] == "ENGINE_EXITED",
            "life.crash: post-exit command fails ENGINE_EXITED")
    finally:
        cli.kill()


def test_run_commands():
    """C4: run.start / run.stop / run.softStop -- tick-committed acks,
    origins, safe-pause interruption, run identity."""
    print("[contract] run lifecycle commands (idle-command)")
    evs = []
    cli = _make_client("idle-command", on_event=evs.append).spawn()
    try:
        chk(cli.wait_ready(), "run-cmd: engine ready")
        a = cli.request("run.start", {})
        chk(a.get("ok") is False and a["error"]["code"] == "BAD_PARAMS",
            "cmd.run-start: params exhaustive (missing mode -> BAD_PARAMS)")
        a = cli.request("run.start", {"mode": "auto"})
        chk(a.get("ok") is True and a["result"].get("runId"),
            "cmd.run-start.ack-after-init: tick-committed ok {runId}")
        rid = (a.get("result") or {}).get("runId")
        starts = [e for e in evs if e["ev"] == "run.started"]
        chk(bool(starts) and starts[0]["data"]["origin"] == "cmd"
            and starts[0]["data"].get("runId") == rid,
            "ev.run-started.origin-cmd: origin cmd, same runId as ack")
        chk(starts and starts[0]["data"]["mode"] == "script",
            "cmd.run-start.mode-derivation: SCRIPT_MODE config -> script")
        a = cli.request("run.start", {"mode": "auto"})
        chk(a.get("ok") is False and a["error"]["code"] == "BAD_STATE",
            "cmd.run-start.bad-state: refused while running")
        t0 = time.time()
        a = cli.request("run.softStop")
        chk(a.get("ok") is True and (time.time() - t0) < 3.0,
            "cmd.soft-stop.ack-on-entry: fast-class ack, ladder not awaited")
        deadline = time.time() + 30
        while time.time() < deadline and not any(
                e["ev"] == "safety.event"
                and e["data"].get("type") == "safe_stop" for e in evs):
            time.sleep(0.1)
        chk(any(e["ev"] == "safety.event"
                and e["data"].get("type") == "safe_stop" for e in evs),
            "cmd.soft-stop.ladder: safety.event safe_stop narrated")
        pt0 = time.time()
        p = cli.request("engine.ping")
        chk(p.get("ok") is True and (time.time() - pt0) < 3.0,
            "cmd.ping.during-blocked-tick: serviceable during the ladder")
        a = cli.request("run.stop", {"reason": "user"})
        chk(a.get("ok") is True and a["result"].get("runId") == rid
            and isinstance(a["result"].get("finalSeq"), int),
            "cmd.run-stop: ack {runId, finalSeq} after terminal emit")
        stops = [e for e in evs if e["ev"] == "run.stopped"]
        chk(bool(stops) and stops[-1]["data"]["reason"] == "user",
            "cmd.run-stop.interrupts-safe-pause: reason user, stopped now")
        chk(stops and stops[-1]["seq"] == a["result"]["finalSeq"],
            "cmd.run-stop: finalSeq is the run.stopped seq")
        fin = stops[-1]["data"].get("final") or {}
        chk(set(fin.get("raw", {})) == set(protocol.STATS_RAW_KEYS),
            "ev.run-stopped.final-stats-fresh: full raw partition present")
        a = cli.request("run.stop")
        chk(a.get("ok") is False and a["error"]["code"] == "BAD_STATE",
            "cmd.run-stop.idle-bad-state")
        a = cli.request("run.softStop")
        chk(a.get("ok") is False and a["error"]["code"] == "BAD_STATE",
            "cmd.soft-stop: BAD_STATE while idle")
        a = cli.request("run.start", {"mode": "auto"})
        chk(a.get("ok") is True and a["result"].get("runId")
            and a["result"]["runId"] != rid,
            "run identity: new start -> new runId (section 3.2)")
        a = cli.request("run.pause")
        chk(a.get("ok") is True, "cmd.pause: ok while running")
        pevs = [e for e in evs if e["ev"] == "run.paused"]
        chk(bool(pevs) and pevs[-1]["data"].get("origin") == "cmd",
            "ev.pause-resume.origins: command pause tagged origin=cmd")
        a = cli.request("run.stop")
        chk(a.get("ok") is True, "cmd.run-stop: valid from paused")
        cli.shutdown()
    finally:
        cli.kill()


def test_instance_identity():
    """Protocol 1.5: the durable instance GUID -- persisted at
    <home>/instance_id, surfaced in hello + engine.describe alongside
    exePath/dataDir, carried by run.started and the run.start/run.stop
    acks, and stable across engine restarts of the same home."""
    print("[contract] durable instance identity (1.5)")
    from prospector_engine import ipc as ipc_mod

    # unit: mint once, read thereafter; empty file re-mints
    ud = tempfile.mkdtemp(prefix="ppe-home-")
    g1 = ipc_mod.instance_identity(ud)
    g2 = ipc_mod.instance_identity(ud)
    chk(bool(g1) and g1 == g2 and len(g1) >= 8,
        "id.unit: minted once, read thereafter (%r)" % (g1,))
    with open(os.path.join(ud, "instance_id")) as f:
        chk(f.read().strip() == g1,
            "id.unit: <home>/instance_id holds the GUID")
    with open(os.path.join(ud, "instance_id"), "w") as f:
        f.write("")
    g3 = ipc_mod.instance_identity(ud)
    chk(bool(g3), "id.unit: an empty/unreadable file re-mints")

    # two sequential boots of the SAME home read the same GUID
    home = tempfile.mkdtemp(prefix="ppe-home-")
    out1, _e1, rc1 = run_batch("start-idle-quit", home=home)
    frames1, _d1, _p1 = parse_stream(out1)
    hellos = events(frames1, "engine.hello")
    chk(rc1 == 0 and bool(hellos), "id.boot1: clean batch run with hello")
    h1 = hellos[0]["data"]
    eng1 = h1.get("engine", {})
    inst1 = eng1.get("instance")
    chk(isinstance(inst1, str) and len(inst1) >= 8,
        "id.hello: engine.instance is a GUID (got %r)" % (inst1,))
    chk(h1["protocol"]["minor"] == protocol.PROTOCOL_MINOR
        and protocol.PROTOCOL_MINOR >= 5,
        "id.protocol: 1.5 minor served (hello minor == library minor)")
    chk(eng1.get("dataDir") == home and bool(eng1.get("exePath"))
        and isinstance(eng1.get("frozen"), bool),
        "id.hello: dataDir is the home; exePath + frozen present")
    idf = os.path.join(home, "instance_id")
    on_disk = open(idf).read().strip() if os.path.exists(idf) else None
    chk(on_disk == inst1, "id.persist: <home>/instance_id == hello GUID")
    out2, _e2, _rc2 = run_batch("start-idle-quit", home=home)
    frames2, _d2, _p2 = parse_stream(out2)
    h2 = events(frames2, "engine.hello")[0]["data"]
    chk(h2.get("engine", {}).get("instance") == inst1,
        "id.persist: second boot of the same home reads the same GUID")

    # interactive: describe + the run.start/run.stop acks carry it
    from prospector_engine.client import EngineClient
    evs = []
    cli = EngineClient(
        [sys.executable, ENGINE], home=home, host="lite-test", cwd=ROOT,
        allow_simulated=True, on_event=evs.append,
        extra_args=["--sim", os.path.join(ROOT, "engine_scenarios",
                                          "idle-command.json")]).spawn()
    try:
        chk(cli.wait_ready(), "id.client: engine ready")
        chk((cli.hello or {}).get("engine", {}).get("instance") == inst1,
            "id.persist: third boot (client) still the same GUID")
        a = cli.request("engine.describe")
        de = (a.get("result") or {}).get("engine", {})
        chk(a.get("ok") is True and de.get("instance") == inst1
            and de.get("dataDir") == home and bool(de.get("exePath")),
            "id.describe: engine.instance/dataDir/exePath served")
        a = cli.request("run.start", {"mode": "auto"})
        chk(a.get("ok") is True
            and a["result"].get("instanceId") == inst1,
            "id.run-start.ack: carries instanceId")
        starts = [e for e in evs if e["ev"] == "run.started"]
        chk(bool(starts) and starts[-1]["data"].get("instanceId") == inst1,
            "id.run-started: event carries instanceId")
        a = cli.request("run.stop", {"reason": "user"})
        chk(a.get("ok") is True
            and a["result"].get("instanceId") == inst1,
            "id.run-stop.ack: carries instanceId")
        cli.shutdown()
    finally:
        cli.kill()


def test_mode_derivation():
    """Section 4.3 precedence: tracker > script > treasure > geode >
    shards > standard (unit-level over the emitter's label logic)."""
    print("[contract] run.start mode derivation precedence")
    import time as _t
    from prospector_engine.ipc import FrameEmit

    class _NS(object):
        pass

    def label(**flags):
        ns = _NS()
        ns.time = _t
        ns.__file__ = ENGINE
        for k, v in flags.items():
            setattr(ns, k, v)
        return FrameEmit(ns)._mode_label()

    chk(label(TRACKER_MODE=True, SCRIPT_MODE=True, TREASURE_MODE=True,
              GEODE_MODE=True, SHARDS_DIG_CLICKS=3) == "tracker",
        "cmd.run-start.mode-derivation: tracker wins over all")
    chk(label(SCRIPT_MODE=True, TREASURE_MODE=True, GEODE_MODE=True) ==
        "script", "cmd.run-start.mode-derivation: script > treasure")
    chk(label(TREASURE_MODE=True, GEODE_MODE=True) == "treasure",
        "cmd.run-start.mode-derivation: treasure > geode")
    chk(label(GEODE_MODE=True, SHARDS_DIG_CLICKS=3) == "geode",
        "cmd.run-start.mode-derivation: geode over shards when both set")
    chk(label(SHARDS_DIG_CLICKS=3) == "shards",
        "cmd.run-start.mode-derivation: shards from dig clicks")
    chk(label() == "standard",
        "cmd.run-start.mode-derivation: standard fallback")


def test_relic_hotkey_events():
    """ISS-154: engine-owned relic hotkeys emit relic.changed in ipc mode."""
    print("[contract] relic hotkey chords emit relic.changed (relic-hotkeys)")
    out, _err, rc = run_batch("relic-hotkeys")
    frames, _d, perr = parse_stream(out)
    chk(rc == 0 and perr == 0, "relic-hotkeys: clean framed run")
    ch = events(frames, "relic.changed")
    chk(any(e["data"].get("kind") == "resetAll"
            and e["data"].get("origin") == "hotkey" for e in ch),
        "ev.relic-changed.origins: hotkey reset-all chord emits")
    chk(any(e["data"].get("kind") == "resetOne"
            and e["data"].get("index") == 1
            and e["data"].get("origin") == "hotkey" for e in ch),
        "ev.relic-changed.origins: hotkey reset-one chord emits (ISS-154)")




def test_settings_commands():
    """C5: engine-owned settings over the wire (idle-command)."""
    print("[contract] settings commands (idle-command)")
    evs = []
    cli = _make_client("idle-command", on_event=evs.append).spawn()
    try:
        chk(cli.wait_ready(), "settings: engine ready")
        a = cli.request("engine.describe")
        chk(a.get("ok") is True, "cmd.describe.shape: acked")
        r = a.get("result") or {}
        sch = {s["key"]: s for s in r.get("settingsSchema", [])}
        chk(len(sch) > 100, "cmd.describe: settingsSchema populated (%d keys)"
            % len(sch))
        chk(sch.get("GEODE_DIGS_TO_FILL", {}).get("default") == 1,
            "settings schema: GEODE_DIGS_TO_FILL default is the engine's "
            "acted-on 1 (architecture 6.1)")
        chk("run.start" in r.get("commands", [])
            and "calibration.capture" in r.get("commands", [])
            and sorted(r.get("commands", []))
            == sorted(protocol.COMMANDS),
            "cmd.describe: full v1.0 vocabulary served incl. the 4.15 "
            "calibration class (C8)")
        chk(r.get("injectable", {}).get("keys")
            == list(protocol.INJECTABLE_KEYS),
            "cmd.describe: injectable release vocabulary published")
        a = cli.request("settings.get")
        vals = (a.get("result") or {}).get("values") or {}
        chk(a.get("ok") is True
            and (a["result"].get("schemaVersion") == 1)
            and vals.get("GEODE_DIGS_TO_FILL") == 1,
            "cmd.settings-get.defaults-filled")
        a = cli.request("settings.get", {"keys": ["AUTOSTOP_MINUTES"]})
        chk(a.get("ok") and list((a["result"] or {}).get("values", {}))
            == ["AUTOSTOP_MINUTES"], "cmd.settings-get.subset-keys")
        cfg = os.path.join(cli.home, "prospecting_config.json")
        a = cli.request("settings.validate",
                        {"values": {"NOPE_KEY": 1}})
        chk(a.get("ok") and a["result"]["ok"] is False
            and a["result"]["perKey"]["NOPE_KEY"] == "UNKNOWN_KEY",
            "cmd.settings-validate: unknown key flagged, pure")
        a = cli.request("settings.set",
                        {"values": {"AUTOSTOP_MINUTES": "42"}})
        chk(a.get("ok") is True and a["result"]["effective"] == "now"
            and a["result"]["applied"] == ["AUTOSTOP_MINUTES"],
            "cmd.settings-set: coerced write, effective now while idle")
        a = cli.request("settings.set",
                        {"values": {"AUTOSTOP_MINUTES": 5, "NOPE": 1}})
        chk(a.get("ok") is False
            and a["error"]["code"] == "VALIDATION_FAILED"
            and a["error"]["data"]["perKey"]["NOPE"] == "UNKNOWN_KEY",
            "cmd.settings-set.validation-all-or-nothing")
        a = cli.request("settings.get", {"keys": ["AUTOSTOP_MINUTES"]})
        chk(a.get("ok") and a["result"]["values"]["AUTOSTOP_MINUTES"] == 42,
            "cmd.settings-set: rejected batch wrote nothing (still 42)")
        a = cli.request("settings.setOpaque",
                        {"values": {"ACCESS_TOKEN_TEST": "opaque-v"}})
        chk(a.get("ok") is True, "cmd.settings-opaque: host-only key stored")
        a = cli.request("settings.setOpaque",
                        {"values": {"AUTOSTOP_MINUTES": 9}})
        chk(a.get("ok") is False
            and a["error"]["data"]["perKey"]["AUTOSTOP_MINUTES"]
            == "ENGINE_KEY",
            "cmd.settings-opaque.engine-key-rejected")
        import json as _j
        doc = _j.load(open(cfg))
        chk(doc.get("CONFIG_SCHEMA") == 1 and doc.get("AUTOSTOP_MINUTES") == 42
            and doc.get("ACCESS_TOKEN_TEST") == "opaque-v",
            "settings writer: migrated v1 file holds engine + opaque writes")
        chk(os.path.exists(cfg + ".bak"),
            "cmd.settings-set.persist-atomic: rolling .bak kept")
        chk(any(e["ev"] == "settings.changed"
                and e["data"].get("source") == "cmd" for e in evs),
            "ev.settings-changed.sources: cmd writes emit")
        # external edit + reload
        doc["AUTOSTOP_MINUTES"] = 7
        open(cfg, "w").write(_j.dumps(doc))
        a = cli.request("settings.reload")
        chk(a.get("ok") and "AUTOSTOP_MINUTES" in a["result"]["changedKeys"],
            "cmd.settings-reload.external-edit")
        # import from a foreign v0 home
        import tempfile as _tf
        fdir = _tf.mkdtemp(prefix="ppe-import-")
        open(os.path.join(fdir, "prospecting_config.json"), "w").write(
            _j.dumps({"AUTOSTOP_MINUTES": "15", "MY_UNKNOWN": "kept"}))
        a = cli.request("settings.import", {"fromDir": fdir})
        chk(a.get("ok") and a["result"]["schemaVersion"] == 1,
            "cmd.settings-import.from-macro-dir")
        a = cli.request("settings.get", {"keys": ["AUTOSTOP_MINUTES"]})
        chk(a.get("ok") and a["result"]["values"]["AUTOSTOP_MINUTES"] == 15,
            "cmd.settings-import: migrated value adopted (str '15' -> 15)")
        toonew = _tf.mkdtemp(prefix="ppe-toonew-")
        open(os.path.join(toonew, "prospecting_config.json"), "w").write(
            _j.dumps({"CONFIG_SCHEMA": 99}))
        a = cli.request("settings.import", {"fromDir": toonew})
        chk(a.get("ok") is False
            and a["error"]["code"] == "SCHEMA_TOO_NEW",
            "cmd.settings-import.too-new-refused")
        # script.setActive
        good = {"version": 1, "name": "s2", "blocks": [
            {"id": "b1", "type": "wait", "params": {"ms": 100},
             "children": []}]}
        a = cli.request("script.setActive", {"name": "s2", "script": good})
        chk(a.get("ok") is True and a["result"]["active"] == "s2",
            "cmd.script-activate.validated: good script accepted")
        a = cli.request("script.setActive",
                        {"name": "bad", "script": {"version": 1,
                                                   "blocks": "nope"}})
        chk(a.get("ok") is False
            and a["error"]["code"] == "VALIDATION_FAILED",
            "cmd.script-activate.validated: bad script rejected")
        a = cli.request("script.setActive", {"name": None})
        chk(a.get("ok") is True and a["result"]["active"] is None,
            "cmd.script-activate.null-deactivates")
        a = cli.request("script.setActive", {"name": "s2"})
        chk(a.get("ok") is True,
            "cmd.script-activate: re-activate by name (kept script)")
        # run-active guards + next-run effectivity
        a = cli.request("run.start", {"mode": "auto"})
        chk(a.get("ok") is True, "settings: run started for guards")
        a = cli.request("run.start", {"mode": "auto"}) if False else None
        a = cli.request("settings.set", {"values": {"AUTOSTOP_MINUTES": 3}})
        chk(bool(a) and a.get("ok") is True
            and a["result"]["effective"] == "next-run",
            "cmd.settings-set.next-run-while-running")
        a = cli.request("settings.reload")
        chk(a.get("ok") is False and a["error"]["code"] == "RUN_ACTIVE",
            "cmd.settings-reload.run-active-rejected")
        a = cli.request("script.setActive", {"name": None})
        chk(a.get("ok") is False and a["error"]["code"] == "RUN_ACTIVE",
            "cmd.script-activate.run-active-rejected")
        cli.request("run.stop")
        cli.shutdown()
    finally:
        cli.kill()


def test_calibration_commands():
    """C8: the 4.15 calibration.* sensing class against the sim's
    scripted screen (one cmd.calibration-* per verb, session semantics,
    cueMask op cycle, savePixels derivations)."""
    print("[contract] calibration sensing class (calibration-screen)")
    evs = []
    cli = _make_client("calibration-screen", on_event=evs.append).spawn()
    try:
        chk(cli.wait_ready(), "calibration: engine ready")
        a = cli.request("calibration.pick", {"fx": 0.5, "fy": 0.5})
        chk(a.get("ok") is False and a["error"]["code"] == "BAD_STATE"
            and a["error"].get("data", {}).get("expected")
            == "captureSession",
            "cmd.calibration-pick.no-session: BAD_STATE "
            "{expected:captureSession}")
        a = cli.request("calibration.detectWindow")
        chk(a.get("ok") and a["result"]["found"] is True
            and a["result"]["rect"] == {"x": 100, "y": 50,
                                        "w": 1200, "h": 800}
            and a["result"].get("title") == "Roblox",
            "cmd.calibration-detect-window: scripted rect + title")
        a = cli.request("calibration.capture")
        r = a.get("result") or {}
        chk(a.get("ok") and r.get("fullW") == 1440 and r.get("fullH") == 900
            and r.get("imageW", 9999) <= 1600
            and str(r.get("image", "")).startswith("data:image/png;base64,"),
            "cmd.calibration-capture: session + <=1600px preview")
        fx, fy = 310 / 1440.0, 205 / 900.0
        a = cli.request("calibration.pick", {"fx": fx, "fy": fy})
        chk(a.get("ok") and a["result"]["rgb"] == [200, 10, 30]
            and a["result"]["x"] == 310 and a["result"]["y"] == 205,
            "cmd.calibration-pick: full-res read (red phase)")
        time.sleep(9)          # virtual clock crosses the 5000 ms mutation
        a = cli.request("calibration.pick", {"fx": fx, "fy": fy})
        chk(a.get("ok") and a["result"]["rgb"] == [200, 10, 30],
            "cmd.calibration-pick: STORED frame, not a fresh grab "
            "(screen mutated, pick did not)")
        a = cli.request("calibration.capture")
        chk(a.get("ok"), "calibration: re-capture replaces the session")
        a = cli.request("calibration.pick", {"fx": fx, "fy": fy})
        chk(a.get("ok") and a["result"]["rgb"] == [30, 10, 200],
            "cmd.calibration-capture: session replaced (blue phase)")
        a = cli.request("calibration.crop",
                        {"rect": {"x": 200, "y": 200, "w": 40, "h": 30}})
        chk(a.get("ok") and a["result"]["w"] == 40 and a["result"]["h"] == 30
            and str(a["result"]["image"]).startswith("data:image/png"),
            "cmd.calibration-crop: full-res session crop")
        a = cli.request("calibration.sampleSaved")
        r = a.get("result") or {}
        chk(a.get("ok") and r.get("capFull") is True
            and r.get("whites", {}).get("DEPOSIT_PIX") is True
            and r.get("whites", {}).get("PAN_PIX") is True
            and r.get("whites", {}).get("SHAKE_PIX") is False
            and r.get("pixels", {}).get("DEPOSIT_PIX")
            == {"r": 255, "g": 255, "b": 255},
            "cmd.calibration-sample-saved: 6x6 averages + engine "
            "thresholds (capFull, whites)")
        a = cli.request("calibration.detect", {"target": "capacityBar"})
        r = a.get("result") or {}
        chk(a.get("ok") and r.get("detected") is True
            and r["proposal"]["left"] == [402, 740]
            and r["proposal"]["right"] == [909, 740]
            and r["proposal"]["rgb"] == [255, 200, 60],
            "cmd.calibration-detect: capacityBar ends + solid-gold walk")
        a = cli.request("calibration.detect",
                        {"target": "cuePrompt", "cue": "PAN_PIX"})
        r = a.get("result") or {}
        px = (r.get("proposal") or {}).get("pixel") or [0, 0]
        chk(a.get("ok") and r.get("detected") is True
            and 430 <= px[0] <= 689 and 620 <= px[1] <= 643
            and r["proposal"]["rgb"] == [255, 255, 255],
            "cmd.calibration-detect: cuePrompt proposal inside the "
            "scripted prompt")
        a = cli.request("calibration.detect", {"target": "nope"})
        chk(a.get("ok") is False and a["error"]["code"] == "BAD_PARAMS",
            "cmd.calibration-detect: unknown target BAD_PARAMS")
        a = cli.request("calibration.testRead", {"target": "find"})
        chk(a.get("ok") and a["result"].get("lines")
            == ["Stone Chunk", "Common", "312 kg"],
            "cmd.calibration-test-read: find lines via the engine OCR "
            "path (sim fixtures)")
        a = cli.request("calibration.testRead", {"target": "earnings"})
        chk(a.get("ok") and a["result"].get("money") == "1,204,500"
            and a["result"].get("shards")
            == "no text found -- widen the region",
            "cmd.calibration-test-read: earnings parse + exact legacy "
            "fallback string")
        # cue-mask editor cycle (status/beginCapture/toggle/reset/save/clear)
        a = cli.request("calibration.cueMask", {"op": "status"})
        chk(a.get("ok") and a["result"]["cues"]["PAN"]["has"] is False,
            "cmd.calibration-cue-mask: status shape, PAN absent")
        a = cli.request("calibration.cueMask",
                        {"op": "beginCapture", "cue": "PAN"})
        r = a.get("result") or {}
        chk(a.get("ok") and r.get("cueEdit") is True and r.get("px") == 200
            and str(r.get("image", "")).startswith("data:image/png"),
            "cmd.calibration-cue-mask: beginCapture white-grid at the "
            "saved cue pixel (200 px: PAN + neighbouring DEPOSIT blob)")
        tfx, tfy = 85 / 288.0, 30 / 60.0        # the DEPOSIT blob's centre
        a = cli.request("calibration.cueMask",
                        {"op": "toggle", "fx": tfx, "fy": tfy})
        chk(a.get("ok") and a["result"].get("px") == 100,
            "cmd.calibration-cue-mask: flood-fill toggle removed the "
            "neighbour blob (200 -> 100 px)")
        a = cli.request("calibration.cueMask", {"op": "reset"})
        chk(a.get("ok") and a["result"].get("px") == 200,
            "cmd.calibration-cue-mask: reset restores the grid")
        cli.request("calibration.cueMask",
                    {"op": "toggle", "fx": tfx, "fy": tfy})
        a = cli.request("calibration.cueMask", {"op": "save"})
        chk(a.get("ok") and a["result"].get("px") == 100
            and str(a["result"].get("preview", "")).startswith("data:image"),
            "cmd.calibration-cue-mask: save persists the edited mask")
        chk(any(e["ev"] == "settings.changed"
                and "CUE_MASKS" in e["data"].get("keys", [])
                and "CALIB_WINDOW_RECT" in e["data"].get("keys", [])
                for e in evs),
            "ev.settings-changed.sources: cueMask save narrates the write")
        a = cli.request("calibration.cueMask", {"op": "status"})
        pan = (a.get("result") or {})["cues"]["PAN"]
        chk(a.get("ok") and pan["has"] is True and pan["px"] == 100
            and pan["w"] == 10 and pan["h"] == 10,
            "cmd.calibration-cue-mask: saved mask visible in status")
        a = cli.request("calibration.health")
        chk(a.get("ok") and a["result"] == {"ok": True, "reason": ""},
            "cmd.calibration-health: rect recorded by save matches live")
        a = cli.request("calibration.cueMask", {"op": "clear", "cue": "PAN"})
        chk(a.get("ok") and a["result"] == {"cleared": True},
            "cmd.calibration-cue-mask: clear")
        # auto: baked default profile placement + no-write unless apply.
        # Pins re-derived at rc.3 when the engine's stale profile copy was
        # aligned with the canonical app/shipped profile (0.62333/0.78657
        # capacity-bar row): 848 = 100+0.62333*1200, 679 = 50+0.78657*800,
        # 296 = (0.62333-0.37667)*1200.
        a = cli.request("calibration.auto", {})
        r = a.get("result") or {}
        chk(a.get("ok") and r.get("placed") is True and r.get("count") == 6
            and r["pixels"]["CAP_FULL_PIXEL"] == [848, 679]
            and r["window"] == {"x": 100, "y": 50, "w": 1200, "h": 800},
            "cmd.calibration-auto: default-profile placement math")
        g = cli.request("settings.get", {"keys": ["CAP_BAR_WIDTH"]})
        chk(g["result"]["values"]["CAP_BAR_WIDTH"] == 440,
            "cmd.calibration-auto: apply:false wrote nothing")
        a = cli.request("calibration.auto", {"apply": True})
        g = cli.request("settings.get",
                        {"keys": ["CAP_BAR_WIDTH", "AUTO_CALIBRATE"]})
        chk(a.get("ok") and g["result"]["values"]["CAP_BAR_WIDTH"] == 296
            and g["result"]["values"]["AUTO_CALIBRATE"] is True,
            "cmd.calibration-auto: apply persists derivation, does NOT "
            "flip AUTO_CALIBRATE")
        # savePixels: interactive derivations + the forced flags
        a = cli.request("calibration.savePixels",
                        {"pixels": {"CAP_FULL_PIXEL": [900, 760],
                                    "CAP_LEFT_PIXEL": [500, 760],
                                    "DEPOSIT_PIX": [640, 810]}})
        g = cli.request("settings.get",
                        {"keys": ["CAP_BAR_WIDTH", "AUTO_CALIBRATE",
                                  "WINDOW_RELATIVE", "PIXEL_RATIOS",
                                  "CALIB_WINDOW_RECT"]})
        v = g["result"]["values"]
        chk(a.get("ok") and "CAP_BAR_WIDTH" in a["result"]["saved"]
            and v["CAP_BAR_WIDTH"] == 400
            and v["AUTO_CALIBRATE"] is False
            and v["WINDOW_RELATIVE"] is False
            and v["CALIB_WINDOW_RECT"] == [100, 50, 1200, 800]
            and v["PIXEL_RATIOS"]["CAP_FULL_PIXEL"] == [0.66667, 0.8875],
            "cmd.calibration-save-pixels: bar-width + ratio derivations, "
            "flags forced exactly as the manual save")
        # import path: explicit ratios/windowRect, NO flag forcing
        cli.request("settings.set", {"values": {"AUTO_CALIBRATE": True}})
        a = cli.request("calibration.savePixels",
                        {"pixels": {"PAN_PIX": [700, 810]},
                         "ratios": {"PAN_PIX": [0.5, 0.95]},
                         "windowRect": [1, 2, 3, 4]})
        g = cli.request("settings.get",
                        {"keys": ["AUTO_CALIBRATE", "PIXEL_RATIOS",
                                  "CALIB_WINDOW_RECT"]})
        v = g["result"]["values"]
        chk(a.get("ok") and v["AUTO_CALIBRATE"] is True
            and v["PIXEL_RATIOS"] == {"PAN_PIX": [0.5, 0.95]}
            and v["CALIB_WINDOW_RECT"] == [1, 2, 3, 4],
            "cmd.calibration-save-pixels: explicit ratios/windowRect "
            "(import path) adopted, auto-calibrate flags untouched")
        chk(any(e["ev"] == "settings.changed"
                and "PIXEL_RATIOS" in e["data"].get("keys", [])
                for e in evs),
            "ev.settings-changed.sources: savePixels narrates the write")
        cli.shutdown()
    finally:
        cli.kill()


def test_calibration_capability_gate():
    """C8: testRead where the platform capability is false -> UNSUPPORTED
    {capability}; session dropped by run.start (idle-command)."""
    print("[contract] calibration capability gate + run.start session drop")
    cli = _make_client("calibration-nocap").spawn()
    try:
        chk(cli.wait_ready(), "nocap: engine ready")
        chk(cli.hello["capabilities"]["findsOcr"] is False,
            "nocap: sim scripts findsOcr=false (win parity)")
        a = cli.request("calibration.testRead", {"target": "find"})
        chk(a.get("ok") is False and a["error"]["code"] == "UNSUPPORTED"
            and a["error"].get("data", {}).get("capability") == "findsOcr",
            "cmd.calibration-test-read.unsupported: capability NACK")
        cli.shutdown()
    finally:
        cli.kill()
    cli = _make_client("idle-command").spawn()
    try:
        chk(cli.wait_ready(), "session-drop: engine ready")
        a = cli.request("calibration.capture")
        chk(a.get("ok") is True, "session-drop: capture while idle ok")
        a = cli.request("run.start", {"mode": "auto"})
        chk(a.get("ok") is True, "session-drop: run started")
        cli.request("run.stop")
        a = cli.request("calibration.pick", {"fx": 0.5, "fy": 0.5})
        chk(a.get("ok") is False and a["error"]["code"] == "BAD_STATE"
            and a["error"].get("data", {}).get("expected")
            == "captureSession",
            "cmd.calibration-capture: run.start dropped the session")
        cli.shutdown()
    finally:
        cli.kill()


def test_settings_migration_unit():
    """C5: pure migration semantics over the real engine schema."""
    print("[contract] settings migration (in-process, real schema)")
    import engine_sim
    from prospector_engine import settings as S
    po = engine_sim.load_engine("pmig_unit")
    sch = S.schema(po)
    v0 = {"AUTOSTOP_MINUTES": "15", "WEBHOOK_URL": "",
          "SOME_FUTURE_KEY": {"nested": True}}
    v1 = S.migrate_doc(v0, sch)
    chk(v1["CONFIG_SCHEMA"] == 1, "settings.migrate: stamps CONFIG_SCHEMA 1")
    chk(v1["AUTOSTOP_MINUTES"] == 15,
        "settings.migrate: legacy _coerce semantics (str '15' -> 15)")
    chk(v1["SOME_FUTURE_KEY"] == {"nested": True},
        "settings.migrate: unknown keys preserved verbatim")
    chk(v1["GEODE_DIGS_TO_FILL"] == 1,
        "settings.migrate.v0-fixtures: missing GEODE_DIGS_TO_FILL -> 1")
    missing_zeroed = [k for k, s in sch.items()
                     if k not in v0 and v1.get(k) != s["default"]]
    chk(not missing_zeroed,
        "settings.migrate: no missing key zeroed (all materialized to baked "
        "defaults)")
    chk(S.migrate_doc(v1, sch) == v1, "settings.migrate.idempotent")
    try:
        S.migrate_doc({"CONFIG_SCHEMA": 99}, sch)
        chk(False, "ver.schema-too-new: raised")
    except S.SchemaTooNew:
        chk(True, "ver.schema-too-new: migration refuses a future config")
    # easy-layering: stored values never contain offsets; bind-time math
    # applies stored + offset; rebinding a materialized file cannot creep
    import tempfile, json as _j
    home = tempfile.mkdtemp(prefix="ppe-easy-")
    cfg = os.path.join(home, "prospecting_config.json")
    open(cfg, "w").write(_j.dumps({"EASY_WATER_BACK_MS": 100,
                                   "WATER_EXTRA_BACK_MS": 50}))
    doc, _ = S.migrate_file(cfg, sch)
    chk(doc["WATER_EXTRA_BACK_MS"] == 50 and doc["EASY_WATER_BACK_MS"] == 100,
        "settings.migrate.easy-layering: stored values stay stored")
    po2 = engine_sim.load_engine("pmig_easy")
    po2.CONFIG_FILE = cfg
    po2.load_config()
    chk(po2.WATER_EXTRA_BACK_MS == 150,
        "settings.migrate.easy-layering: bind acts on stored + offset (150)")
    po2.load_config()
    chk(po2.WATER_EXTRA_BACK_MS == 150,
        "settings.migrate.easy-layering: re-bind of a materialized v1 file "
        "does not creep (still 150)")
    # corrupt config -> defaults + preserved original
    open(cfg, "w").write("{corrupt json")
    doc, _ = S.migrate_file(cfg, sch)
    chk(doc["CONFIG_SCHEMA"] == 1 and os.path.exists(cfg + ".corrupt.bak"),
        "settings.migrate: corrupt -> defaults + .corrupt.bak (no silent "
        "reset)")


def test_schema_too_new_refusal():
    """Startup refusal: engine.bye fatal SCHEMA_TOO_NEW, exit 2, no hello."""
    print("[contract] SCHEMA_TOO_NEW startup refusal")
    out, _err, rc = run_batch("schema-too-new")
    frames, _d, _e = parse_stream(out)
    byes = events(frames, "engine.bye")
    chk(len(byes) == 1 and byes[0]["data"].get("code") == "SCHEMA_TOO_NEW"
        and byes[0]["data"]["data"].get("found") == 99,
        "ver.schema-too-new: bye fatal SCHEMA_TOO_NEW at startup")
    chk(not events(frames, "engine.hello"),
        "ver.schema-too-new: no hello on refusal")
    chk(rc == 2, "ver.schema-too-new: exit 2")


def test_egress_and_headless():
    """Engine egress rule + headless constraint (static)."""
    print("[contract] only-webhook egress + headless imports (static)")
    eng = open(os.path.join(ROOT, "prospector_engine", "engine.py"),
               encoding="utf-8").read()
    win = open(os.path.join(ROOT, "windows", "prospecting_old.py"),
               encoding="utf-8").read()
    chk("SYNC_URL" not in eng and "SYNC_URL" not in win,
        "engine.network.only-webhook: baked fallback URL gone (ISS-157)")
    pkg = ""
    pkg_dir = os.path.join(ROOT, "prospector_engine")
    for f in os.listdir(pkg_dir):
        if f.endswith(".py"):
            pkg += open(os.path.join(pkg_dir, f), encoding="utf-8").read()
    bad = [m for m in ("import webview", "import pywebview", "electron",
                       "tkinter") if m in pkg]
    chk(not bad, "engine.headless.no-ui-imports: package imports no UI "
        "modules (%r)" % bad)


def test_legacy_replay():
    """Legacy mode must keep reproducing the C0 goldens byte-for-byte."""
    print("[contract] legacy golden replay (delegates to characterization)")
    r = subprocess.run([sys.executable,
                        os.path.join(ROOT, "engine_characterization.py")],
                       capture_output=True, text=True, timeout=300)
    ok = r.returncode == 0 and "CHARACTERIZATION: ALL PASS" in r.stdout
    chk(ok, "legacy.golden-replay: engine without --ipc byte-identical")


if __name__ == "__main__":
    test_batch_run()
    test_safety_ladder()
    test_commands()
    test_heartbeat_and_eof()
    test_instance_lock()
    test_protocol_mismatch()
    test_run_commands()
    test_instance_identity()
    test_mode_derivation()
    test_relic_hotkey_events()
    test_calibration_commands()
    test_calibration_capability_gate()
    test_settings_commands()
    test_settings_migration_unit()
    test_schema_too_new_refusal()
    test_egress_and_headless()
    test_engine_client()
    test_client_refuses_simulated()
    test_client_crash_taxonomy()
    test_legacy_replay()
    print()
    if FAILS:
        print("CONTRACT TESTS: %d FAILURES" % len(FAILS))
        sys.exit(1)
    print("CONTRACT TESTS: ALL PASS")
