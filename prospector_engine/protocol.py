"""PPE1 protocol library (Phase 04, checkpoint C1).

Pure framing + vocabulary: no engine state, no I/O beyond string encode/
decode. The normative text is phase-02-protocol.md in the Studio repo;
this module is its executable transcription. Both the engine's server
side (C2+) and Lite's EngineClient (C3+) use exactly this module -- one
encoding, two speakers.

Wire form -- one frame per line, ASCII-safe JSON:
    PPE1 {"t":"cmd","id":"c-1","cmd":"engine.ping","params":{}}
    PPE1 {"t":"ack","id":"c-1","ok":true,"result":{...}}
    PPE1 {"t":"ev","seq":7,"ts":12.345,"ev":"run.phase","data":{...}}
"""
import json

MAGIC = "PPE1"
PREFIX = MAGIC + " "
PROTOCOL_MAJOR = 1
PROTOCOL_MINOR = 2   # 1.1: adds the "error" stop reason (fatal-exception path)
                     # 1.2: adds the scriptV3 capability (PPScript v3 accepted
                     #      by script.setActive; PPSCRIPT_V3.md in Studio)

# ---- ack error codes (closed set, protocol section 3.3) ---------------------
E_UNSUPPORTED = "UNSUPPORTED"
E_BAD_PARAMS = "BAD_PARAMS"
E_BAD_STATE = "BAD_STATE"
E_RUN_ACTIVE = "RUN_ACTIVE"
E_VALIDATION_FAILED = "VALIDATION_FAILED"
E_SCHEMA_TOO_NEW = "SCHEMA_TOO_NEW"
E_IO_ERROR = "IO_ERROR"
E_INTERNAL = "INTERNAL"
ERROR_CODES = (E_UNSUPPORTED, E_BAD_PARAMS, E_BAD_STATE, E_RUN_ACTIVE,
               E_VALIDATION_FAILED, E_SCHEMA_TOO_NEW, E_IO_ERROR, E_INTERNAL)
# host-synthesized only -- never sent by the engine (section 3.3)
E_ENGINE_EXITED = "ENGINE_EXITED"
E_ACK_TIMEOUT = "ACK_TIMEOUT"

# ---- engine.bye fatal codes (section 5.1 / 11.2) -----------------------------
BYE_ALREADY_RUNNING = "ENGINE_ALREADY_RUNNING"
BYE_PROTOCOL_UNSUPPORTED = "PROTOCOL_UNSUPPORTED"
BYE_SCHEMA_TOO_NEW = "SCHEMA_TOO_NEW"
BYE_INIT_FAILED = "INIT_FAILED"

# ---- command vocabulary v1.0 (section 4); timeout class per section 8 -------
# class: fast=3s, slow=10s (import 30s), capture=10s
COMMANDS = {
    "engine.describe": "fast",
    "engine.ping": "fast",
    "run.start": "slow",
    "run.stop": "slow",
    "run.pause": "fast",
    "run.resume": "fast",
    "run.softStop": "fast",
    "relic.resetAll": "fast",
    "relic.resetOne": "fast",
    "relic.set": "fast",
    "settings.get": "fast",
    "settings.set": "fast",
    "settings.setOpaque": "fast",
    "settings.validate": "fast",
    "settings.reload": "fast",
    "settings.import": "slow",
    "script.setActive": "fast",
    "engine.shutdown": "fast",
    "calibration.detectWindow": "fast",
    "calibration.capture": "capture",
    "calibration.pick": "fast",
    "calibration.crop": "fast",
    "calibration.sampleSaved": "fast",
    "calibration.detect": "capture",
    "calibration.testRead": "capture",
    "calibration.cueMask": "capture",
    "calibration.health": "fast",
    "calibration.auto": "fast",
    "calibration.savePixels": "fast",
    "recorder.start": "fast",
    "recorder.stop": "slow",
    "recorder.status": "fast",
}

# ---- event vocabulary v1.0 (section 5) ---------------------------------------
EVENTS = (
    "engine.hello", "engine.heartbeat", "engine.bye", "engine.log",
    "run.started", "run.paused", "run.resumed", "run.stopped",
    "run.phase", "run.stats",
    "safety.event", "safety.safePaused", "safety.retrying",
    "safety.recovery", "safety.hardStopped",
    "find.new", "find.updated", "geode.timer",
    "script.block", "script.hud",
    "relic.changed", "hotkey.popout", "settings.changed",
)

# run.stats structural split (section 5.4) -- exact key partitions
STATS_RAW_KEYS = (
    "cycles", "clean_cycles", "digs", "dig_clicks", "pauses",
    "finds_count", "finds_stack", "finds_lowconf", "money_earned",
    "shards_earned", "recoveries", "safe_stops", "hard_stops",
    "relics_used", "nudges", "shake_misses", "find_kg", "best_kg",
    "loot_value", "by_rarity", "by_mod", "runtime_s",
)
STATS_DERIVED_KEYS = (
    "clean_pct", "money_per_hr", "shards_per_hr", "money_per_pan",
    "shards_per_pan", "digs_per_pan", "clicks_per_pan", "cyc_mean_s",
    "cyc_p50_s", "cyc_p95_s", "cyc_last_s", "loot_per_hr",
    "total_per_hr", "pans_per_hr",
)
STATS_META_KEYS = ("tracker", "script", "stop_reason", "relics", "input_lag")

SAFETY_EVENT_TYPES = (
    "finds_infer", "finds_fork", "finds_ghost", "finds_resight",
    "safe_stop", "hard_stop", "sr_recover", "fr_recover", "recenter",
    "shake_start_retry", "shake_fail", "nudge", "recover", "break_out",
    "autopan_guard", "autopan_kick", "shake_glitch", "no_progress",
)

STOP_REASONS = ("user", "hotkey", "safe-stop", "auto", "bag-full", "shutdown",
                "error")

# the full injectable vocabulary (section 10.1 release floor). Keycodes are
# platform-specific; these are the platform-neutral names hosts release
# after a force-kill via their own OS layer.
INJECTABLE_KEYS = ("W", "A", "S", "D", "Shift", "Space",
                   "1", "2", "3", "4", "5", "6", "7", "8", "9")
INJECTABLE_BUTTONS = ("left",)


class ProtocolError(Exception):
    """A PPE1-prefixed line that fails JSON parse or shape validation.
    Deliberately NOT a wire event -- callers count it and log locally."""


def encode_frame(obj):
    """One frame = one line. ASCII-safe, no interior newlines."""
    s = json.dumps(obj, ensure_ascii=True, separators=(",", ":"))
    if "\n" in s or "\r" in s:
        raise ProtocolError("frame would contain a newline")
    return PREFIX + s + "\n"


def encode_cmd(cmd_id, cmd, params=None):
    return encode_frame({"t": "cmd", "id": str(cmd_id), "cmd": str(cmd),
                         "params": params if params is not None else {}})


def encode_ack_ok(cmd_id, result=None):
    return encode_frame({"t": "ack", "id": str(cmd_id), "ok": True,
                         "result": result if result is not None else {}})


def encode_ack_err(cmd_id, code, message, data=None):
    err = {"code": str(code), "message": str(message)}
    if data is not None:
        err["data"] = data
    return encode_frame({"t": "ack", "id": str(cmd_id), "ok": False,
                         "error": err})


def encode_event(seq, ts, ev, data):
    return encode_frame({"t": "ev", "seq": int(seq), "ts": round(float(ts), 3),
                         "ev": str(ev), "data": data})


def is_frame_line(line):
    return line.startswith(PREFIX)


def decode_line(line):
    """Decode one stdout/stdin line.

    Returns (kind, obj):
      ("frame", dict)  -- a valid PPE1 frame
      ("diag", str)    -- a non-frame line (forward to logs, never parse)
    Raises ProtocolError for a PPE1-prefixed line that is not a valid frame
    (callers count it, log it, and drop the line -- section 2).
    """
    line = line.rstrip("\r\n")
    if not line.startswith(PREFIX):
        return ("diag", line)
    body = line[len(PREFIX):]
    try:
        obj = json.loads(body)
    except ValueError:
        raise ProtocolError("bad json in frame: %r" % body[:120])
    if not isinstance(obj, dict):
        raise ProtocolError("frame is not an object")
    t = obj.get("t")
    if t == "cmd":
        if not isinstance(obj.get("id"), str) or not isinstance(
                obj.get("cmd"), str) or not isinstance(obj.get("params"), dict):
            raise ProtocolError("bad cmd frame shape")
    elif t == "ack":
        if not isinstance(obj.get("id"), str) or not isinstance(
                obj.get("ok"), bool):
            raise ProtocolError("bad ack frame shape")
        if obj["ok"] and not isinstance(obj.get("result"), dict):
            raise ProtocolError("ok ack missing result")
        if not obj["ok"]:
            err = obj.get("error")
            if (not isinstance(err, dict) or not isinstance(
                    err.get("code"), str) or not isinstance(
                    err.get("message"), str)):
                raise ProtocolError("error ack missing code/message")
    elif t == "ev":
        if (not isinstance(obj.get("seq"), int)
                or not isinstance(obj.get("ts"), (int, float))
                or not isinstance(obj.get("ev"), str)
                or not isinstance(obj.get("data"), dict)):
            raise ProtocolError("bad ev frame shape")
    else:
        raise ProtocolError("unknown frame kind: %r" % (t,))
    return ("frame", obj)


def vestibule_line(engine_version, fingerprint,
                   major=PROTOCOL_MAJOR, minor=PROTOCOL_MINOR):
    """The engine's first stderr line, format frozen across ALL majors
    (section 11.1)."""
    return ("PROSPECTOR-ENGINE proto=%d.%d engine=%s fp=%s"
            % (int(major), int(minor), engine_version, fingerprint))


def parse_vestibule(line):
    """Parse a vestibule line -> dict or None. Frozen format, never extend
    incompatibly."""
    line = line.strip()
    if not line.startswith("PROSPECTOR-ENGINE "):
        return None
    out = {}
    for part in line.split()[1:]:
        if "=" in part:
            k, v = part.split("=", 1)
            out[k] = v
    try:
        maj, mnr = out.get("proto", "").split(".", 1)
        out["major"], out["minor"] = int(maj), int(mnr)
    except ValueError:
        return None
    if "engine" not in out or "fp" not in out:
        return None
    return out


def split_stats(flat):
    """Reshape the legacy flat SessionStats.as_dict() payload (plus the
    wire-added relics/input_lag) into the section 5.4 {raw, derived, meta}
    structure. Pure re-encoding: same values, no computation."""
    raw = {}
    derived = {}
    meta = {}
    for k, v in flat.items():
        if k in STATS_RAW_KEYS:
            raw[k] = v
        elif k in STATS_DERIVED_KEYS:
            derived[k] = v
        elif k in STATS_META_KEYS:
            meta[k] = v
        # unknown keys are dropped from the structured wire form by design:
        # additions must be classified here first (fabrication guard)
    return {"raw": raw, "derived": derived, "meta": meta}


def _selftest():
    line = encode_cmd("c-1", "engine.ping")
    kind, obj = decode_line(line)
    assert kind == "frame" and obj["cmd"] == "engine.ping"
    kind, obj = decode_line(encode_ack_ok("c-1", {"state": "idle"}))
    assert obj["ok"] is True
    kind, obj = decode_line(encode_ack_err("c-2", E_BAD_STATE, "no run",
                                           {"state": "idle"}))
    assert obj["error"]["code"] == "BAD_STATE"
    kind, obj = decode_line(encode_event(1, 0.0015, "engine.hello", {}))
    assert obj["seq"] == 1 and obj["ts"] == 0.002
    assert decode_line("plain diagnostic")[0] == "diag"
    try:
        decode_line('PPE1 {"t":"nope"}')
        raise AssertionError("bad frame accepted")
    except ProtocolError:
        pass
    v = vestibule_line("0.4.0", "abc123")
    p = parse_vestibule(v)
    assert p["major"] == PROTOCOL_MAJOR and p["fp"] == "abc123"
    flat = {"cycles": 3, "clean_pct": 100.0, "tracker": False, "relics": [],
            "input_lag": None, "runtime_s": 12, "money_per_hr": 0}
    s = split_stats(flat)
    assert s["raw"] == {"cycles": 3, "runtime_s": 12}
    assert s["derived"] == {"clean_pct": 100.0, "money_per_hr": 0}
    assert s["meta"] == {"tracker": False, "relics": [], "input_lag": None}
    assert len(STATS_RAW_KEYS) == 22 and len(STATS_DERIVED_KEYS) == 14
    assert len(STATS_META_KEYS) == 5 and len(SAFETY_EVENT_TYPES) == 18
    print("protocol selftest: ALL PASS")


if __name__ == "__main__":
    _selftest()
