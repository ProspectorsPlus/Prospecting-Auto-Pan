#!/usr/bin/env python3
"""Parity tests (Phase 04, Deliverable 4; contract-test plan section 5).

parity.transcript.<scenario>: the engine is driven over the PPE1 protocol
and its full event stream is normalized (wall-clock artifacts removed,
process identity canonicalized) and byte-compared against a checked-in
golden under engine_goldens/parity/. These goldens are the cross-client
contract: Phase 05's TypeScript EngineClient must drive the same pinned
engine through the same scenarios and reproduce these transcripts
byte-identically ("one protocol, two clients, one engine"). The Python
side of parity.settings-defaults and parity.fingerprint runs here today;
their two-client halves join when the TS client exists.

  python3 engine_parity_tests.py            # compare against goldens
  python3 engine_parity_tests.py --update   # regenerate deliberately
  python3 engine_parity_tests.py --selfcheck  # determinism: 2 runs equal
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
from prospector_engine import protocol  # noqa: E402
import engine_contract_tests as C       # noqa: E402  (spawn/run_batch)

GOLD = os.path.join(ROOT, "engine_goldens", "parity")
SCENARIOS = ["start-idle-quit", "quit-during-run", "script-bagguard",
             "script-pause-relic", "script-softstop", "stuck-ladder",
             "tracker-autostop", "relic-hotkeys"]
FAILS = []


def chk(cond, msg):
    print("  [%s] %s" % ("PASS" if cond else "FAIL", msg))
    if not cond:
        FAILS.append(msg)


def normalize(stdout_text):
    """Deterministic view of one engine session's event stream: drop
    wall-clock artifacts (heartbeats, ts), renumber seq, canonicalize
    process identity (pid/home/fingerprint/version/platform)."""
    out = []
    n = 0
    for line in stdout_text.splitlines():
        try:
            kind, obj = protocol.decode_line(line)
        except protocol.ProtocolError:
            continue
        if kind != "frame" or obj.get("t") != "ev":
            continue
        if obj["ev"] == "engine.heartbeat":
            continue
        n += 1
        d = dict(obj["data"])
        if obj["ev"] == "engine.hello":
            d["pid"] = 0
            d["home"] = "<home>"
            eng = dict(d.get("engine", {}))
            eng["sourceFingerprint"] = "<fp>"
            eng["version"] = "<v>"
            eng["platform"] = "<os>"
            d["engine"] = eng
        out.append(json.dumps({"seq": n, "ev": obj["ev"], "data": d},
                              sort_keys=True))
    return "\n".join(out) + "\n"


def _strip_images(v):
    """Canonicalize PNG data-URLs ('<png>') so the golden is stable
    across zlib/encoder versions; pixel truth is contract-test-pinned."""
    if isinstance(v, dict):
        return {k: _strip_images(x) for k, x in sorted(v.items())}
    if isinstance(v, list):
        return [_strip_images(x) for x in v]
    if isinstance(v, str) and v.startswith("data:image/png;base64,"):
        return "<png>"
    return v


CAL_SEQUENCE = [
    ("calibration.detectWindow", {}),
    ("calibration.capture", {}),
    ("calibration.pick", {"fx": 310 / 1440.0, "fy": 205 / 900.0}),
    ("calibration.crop", {"rect": {"x": 200, "y": 200, "w": 40, "h": 30}}),
    ("calibration.sampleSaved", {}),
    ("calibration.detect", {"target": "capacityBar"}),
    ("calibration.detect", {"target": "cuePrompt", "cue": "PAN_PIX"}),
    ("calibration.testRead", {"target": "find"}),
    ("calibration.testRead", {"target": "earnings"}),
    ("calibration.cueMask", {"op": "status"}),
    ("calibration.cueMask", {"op": "beginCapture", "cue": "PAN"}),
    ("calibration.cueMask", {"op": "toggle", "fx": 85 / 288.0, "fy": 0.5}),
    ("calibration.cueMask", {"op": "save"}),
    ("calibration.health", {}),
    ("calibration.auto", {}),
    ("calibration.savePixels", {"pixels": {"CAP_FULL_PIXEL": [900, 760],
                                           "CAP_LEFT_PIXEL": [500, 760]}}),
    ("calibration.pick", {"fx": 0.99, "fy": 0.99}),
]


def calibration_transcript():
    cli = C._make_client("calibration-screen").spawn()
    out = []
    try:
        if not cli.wait_ready():
            return "ENGINE NOT READY\n"
        for cmd, params in CAL_SEQUENCE:
            a = cli.request(cmd, params)
            body = a.get("result") if a.get("ok") else a.get("error")
            out.append(json.dumps({"cmd": cmd, "ok": bool(a.get("ok")),
                                   "r": _strip_images(body)},
                                  sort_keys=True))
        cli.shutdown()
    finally:
        cli.kill()
    return "\n".join(out) + "\n"


def transcript(scenario):
    out, _err, rc = C.run_batch(scenario)
    if scenario == "schema-too-new":
        pass
    return normalize(out), rc


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    os.makedirs(GOLD, exist_ok=True)
    for scen in SCENARIOS:
        print("[parity] %s" % scen)
        t, rc = transcript(scen)
        gp = os.path.join(GOLD, scen + ".jsonl")
        if mode == "--update":
            open(gp, "w", encoding="utf-8").write(t)
            print("  [gen ] %s (%d bytes)" % (gp, len(t)))
            continue
        if mode == "--selfcheck":
            t2, _ = transcript(scen)
            chk(t == t2, "parity.transcript.%s: deterministic" % scen)
            continue
        if not os.path.exists(gp):
            chk(False, "parity.transcript.%s: golden missing" % scen)
            continue
        g = open(gp, encoding="utf-8").read()
        chk(t == g, "parity.transcript.%s: normalized transcript "
                    "byte-identical (%d bytes)" % (scen, len(t)))
        if t != g:
            import difflib
            for d in list(difflib.unified_diff(
                    g.splitlines(), t.splitlines(), "golden", "now",
                    lineterm=""))[:8]:
                print("    ", d[:150])
    # parity.calibration (4.15): both clients drive the same sensing
    # sequence against the sim's scripted screen; normalized ack results
    # (image payloads canonicalized -- pixel truth is pinned by the
    # contract tests) must be byte-identical. Python side today; the TS
    # client reproduces this golden in Phase 05.
    print("[parity] calibration (4.15 sensing sequence)")
    gp = os.path.join(GOLD, "calibration-sequence.jsonl")
    t = calibration_transcript()
    if mode == "--update":
        open(gp, "w", encoding="utf-8").write(t)
        print("  [gen ] %s (%d bytes)" % (gp, len(t)))
    elif mode == "--selfcheck":
        chk(t == calibration_transcript(),
            "parity.calibration: deterministic")
    elif not os.path.exists(gp):
        chk(False, "parity.calibration: golden missing")
    else:
        g = open(gp, encoding="utf-8").read()
        chk(t == g, "parity.calibration: normalized 4.15 sequence "
                    "byte-identical (%d bytes)" % len(t))
        if t != g:
            import difflib
            for d in list(difflib.unified_diff(
                    g.splitlines(), t.splitlines(), "golden", "now",
                    lineterm=""))[:6]:
                print("    ", d[:150])
    if mode == "--update":
        print("PARITY GOLDENS REGENERATED (%d scenarios)" % len(SCENARIOS))
        return
    # parity.settings-defaults (engine side): a fresh home's settings.get
    # equals the schema defaults engine.describe documents
    print("[parity] settings defaults == documented schema")
    cli = C._make_client("idle-command").spawn()
    try:
        ok = cli.wait_ready()
        chk(ok, "parity.settings: engine ready")
        if ok:
            d = cli.request("engine.describe")
            g = cli.request("settings.get")
            sch = {s["key"]: s["default"]
                   for s in d["result"]["settingsSchema"]}
            vals = g["result"]["values"]
            diff = {k for k in sch
                    if k not in ("SCRIPT_MODE", "SCRIPT_ACTIVE",
                                 "SCRIPT_JSON", "RELICS_ENABLED")
                    and vals.get(k) != sch[k]}
            chk(not diff, "parity.settings-defaults: fresh home values == "
                          "schema defaults (diff=%r)" % sorted(diff)[:4])
            fp = cli.hello["engine"]["sourceFingerprint"]
            chk(bool(fp) and fp == d["result"]["engine"]["sourceFingerprint"],
                "parity.fingerprint: hello and describe agree (TS client "
                "must see the same value in Phase 05)")
        cli.shutdown()
    finally:
        cli.kill()
    print()
    if FAILS:
        print("PARITY TESTS: %d FAILURES" % len(FAILS))
        sys.exit(1)
    print("PARITY TESTS: ALL PASS")


if __name__ == "__main__":
    main()
