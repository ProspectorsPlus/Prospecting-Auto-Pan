"""Data-driven recovery program (canonical-foundation pass, spec C
deliverable 1; docs/CANONICAL_CYCLE_PLAN.md section 2.5).

The engine's five-rung ladder (Supervisor.tick R1-R3, the per-situation
guards R4, safe_stop R5) consults a RecoveryProgram. The DEFAULT program
is the built-in ladder as data with every threshold bound to the LIVE
module globals -- default behavior is byte-identical (the ladder sites
read their knobs through eng._rv(), which returns the module global when
nothing overrides it).

A pushed RECOVERY_JSON (`_recovery:1` envelope) may:
  (a) override rung parameters -- the SAME vocabulary the settings keys
      already offer (per-rung whitelists below);
  (b) disable a rung (R1-R4; the safe-stop rung R5 is the safety funnel
      and cannot be disabled);
  (c) add AUTHORED rungs: a trigger + a PPScript IR action block list
      executed by a bounded sub-runner at the tick boundary, with
      resume/cooldown/limit semantics.

Envelope (exact schema):
    {
      "_recovery": 1,
      "rungs": {                      # optional builtin overrides by id
        "R1": {"enabled": true, "params": {"SHAKE_GLITCH_LIMIT": 3}},
        ...
      },
      "authored": [
        {"id": "my_rung",             # ^[a-z][a-z0-9_]{0,31}$, unique
         "name": "label",             # optional
         "enabled": true,
         "trigger": {"type": ...},    # vocabulary below
         "actions": [ ...IR blocks ], # PPScript v2/v3 vocabulary
         "version": 2,                # 2 | 3 (IR vocabulary of actions)
         "caps": [],                  # v3 capability declaration
         "variables": [],             # optional v2 variable declarations
         "max_ms": 15000,             # action budget (clamped 100..120000)
         "resume": "retry_tick",      # retry_tick | restart_stage |
                                      # restart_cycle | safe_stop | hard_stop
         "leg": "dig",                # restart_stage only (advisory: the
                                      # Supervisor re-senses; the leg is
                                      # recorded, not forced)
         "cooldown_s": 0,
         "limit": 0}                  # 0 = unlimited fires per run
      ]
    }

Trigger vocabulary (authored rungs):
    {"type": "stage_timeout", "leg": "dig|water|shake|glide|settle", "ms": N}
    {"type": "missing_cue", "cue": "pan|deposit|shake", "ms": N}
    {"type": "capacity_timeout", "ms": N}      # capacity never read FULL
    {"type": "no_progress", "s": N}
    {"type": "shake_fail", "count": N}
    {"type": "image", "asset": ID, "area": [x1,y1,x2,y2]?, "thr": 50-100,
     "ms": N}                                  # template visible >= ms
    {"type": "window_lost"}
    {"type": "custom_expr", "expr": {...}}     # v2 $expr over engine reads
    {"type": "manual"}                         # recovery.trigger only
    {"type": "flow_signal", "flow": FLOW_ID}   # the named flow FAILED

Emission: safety.event type "recovery_rung" {id, authored, action} fires
for AUTHORED rung firings and MANUAL triggers only -- the default ladder's
automatic firings emit exactly what they always emitted (byte-pinned
characterization goldens stay meaningful).
"""
import json

# Builtin rung ids, in ladder order.
BUILTIN_RUNGS = ("R1", "R2", "R3", "R4", "R5")

# Per-rung override vocabulary: exactly the settings keys the ladder site
# reads for that rung (limits / budgets / enables).
RUNG_PARAMS = {
    "R1": ("SHAKE_GLITCH_LIMIT", "BREAKOUT_ENABLED", "BREAKOUT_LIMIT",
           "BREAKOUT_SHAKE_MS", "BREAKOUT_REPOS_MS"),
    "R2": ("NO_PROGRESS_SEC", "BREAKOUT_ENABLED", "BREAKOUT_LIMIT"),
    "R3": ("STUCK_TICKS", "RECOVER_ENABLED", "RECOVER_LIMIT",
           "RECOVER_BACK_MS", "SHAKE_RETRY_ENABLED"),
    "R4": ("STUCK_LIMIT", "NO_FULL_LIMIT"),
    "R5": ("SR_RECOVERY", "FR_RECOVERY", "SAFE_STOP_RETRY",
           "SAFE_STOP_RETRY_SEC", "SAFE_STOP_MAX_RETRIES"),
}

# Disabling a rung is expressed as the value that makes its trigger
# unreachable at the ladder site (R5 refuses to disable).
DISABLE_VALUES = {
    "R1": {"SHAKE_GLITCH_LIMIT": 0},
    "R2": {"NO_PROGRESS_SEC": 0},
    "R3": {"RECOVER_ENABLED": False},
    "R4": {"STUCK_LIMIT": 10 ** 9, "NO_FULL_LIMIT": 10 ** 9},
}

TRIGGER_TYPES = ("stage_timeout", "missing_cue", "capacity_timeout",
                 "no_progress", "shake_fail", "image", "window_lost",
                 "custom_expr", "manual", "flow_signal")
RESUMES = ("retry_tick", "restart_stage", "restart_cycle", "safe_stop",
           "hard_stop")
PHASES = ("dig", "water", "shake", "glide", "settle")
CUES = ("pan", "deposit", "shake")

_ID_MAX = 32
ACTION_BUDGET_MIN_MS = 100
ACTION_BUDGET_MAX_MS = 120000
ACTION_BUDGET_DEFAULT_MS = 15000


def _id_ok(s):
    return (isinstance(s, str) and 0 < len(s) <= _ID_MAX and s[0].isalpha()
            and s[0].islower()
            and all(c.islower() or c.isdigit() or c == "_" for c in s))


def _pos_int(v, lo=1, hi=10 ** 9):
    return (isinstance(v, int) and not isinstance(v, bool)
            and lo <= v <= hi)


class AuthoredRung(object):
    __slots__ = ("id", "name", "enabled", "trigger", "actions", "version",
                 "caps", "variables", "max_ms", "resume", "leg",
                 "cooldown_s", "limit")

    def describe(self):
        return {"id": self.id, "name": self.name, "authored": True,
                "enabled": self.enabled, "trigger": self.trigger,
                "actions": {"ir_blocks": len(self.actions),
                            "version": self.version,
                            "max_ms": self.max_ms},
                "resume": (self.resume if self.resume != "restart_stage"
                           else {"restart_stage": self.leg}),
                "cooldown_s": self.cooldown_s, "limit": self.limit}


class RecoveryProgram(object):
    """Parsed program: builtin overrides + disabled set + authored rungs."""

    def __init__(self):
        self.overrides = {}      # settings key -> overridden value
        self.disabled = set()    # builtin rung ids disabled
        self.authored = []       # [AuthoredRung]
        self.raw = None          # the parsed envelope (plan.describe)

    def value_for(self, key):
        """Effective ladder value for a settings key, or None = use the
        live module global (the default program returns None for every
        key -- byte-identical behavior)."""
        for rid in self.disabled:
            dv = DISABLE_VALUES.get(rid, {})
            if key in dv:
                return dv[key]
        if key in self.overrides:
            return self.overrides[key]
        return None

    def rung_ids(self):
        return list(BUILTIN_RUNGS) + [a.id for a in self.authored]


def _check_trigger(t, idx):
    if not isinstance(t, dict) or t.get("type") not in TRIGGER_TYPES:
        return "authored rung #%d has an unknown trigger" % idx
    ty = t["type"]
    if ty == "stage_timeout":
        if t.get("leg") not in PHASES or not _pos_int(t.get("ms")):
            return "stage_timeout needs leg + ms"
    elif ty == "missing_cue":
        if t.get("cue") not in CUES or not _pos_int(t.get("ms")):
            return "missing_cue needs cue + ms"
    elif ty == "capacity_timeout":
        if not _pos_int(t.get("ms")):
            return "capacity_timeout needs ms"
    elif ty == "no_progress":
        if not _pos_int(t.get("s"), 1, 86400):
            return "no_progress needs s"
    elif ty == "shake_fail":
        if not _pos_int(t.get("count"), 1, 1000):
            return "shake_fail needs count"
    elif ty == "image":
        if not isinstance(t.get("asset"), str) or not t.get("asset"):
            return "image trigger needs an asset id"
        if not _pos_int(t.get("thr", 90), 50, 100):
            return "image thr must be 50..100"
        if not _pos_int(t.get("ms", 1), 1):
            return "image ms must be positive"
        area = t.get("area")
        if area is not None and not (
                isinstance(area, list) and len(area) == 4
                and all(isinstance(v, int) and not isinstance(v, bool)
                        for v in area)):
            return "image area must be [x1,y1,x2,y2]"
    elif ty == "custom_expr":
        if "expr" not in t:
            return "custom_expr needs expr"
    elif ty == "flow_signal":
        if not _id_ok(t.get("flow")):
            return "flow_signal needs a flow id"
    return None


def _validate_actions(eng, rung_id, blocks, version, caps, variables):
    """Triple-enforcement: the authored IR runs through the engine's OWN
    ScriptRunner validator (shape, limits, caps, expressions)."""
    doc = {"version": version, "blocks": blocks}
    if version >= 3:
        doc["caps"] = list(caps or [])
    if variables:
        doc["variables"] = variables
    runner = eng.ScriptRunner(json.dumps(doc), "recovery:%s" % rung_id)
    if runner.dead:
        return None, "rung %r actions rejected: %s" % (rung_id, runner.dead)
    return runner, None


def parse_program(eng, text):
    """RECOVERY_JSON -> (RecoveryProgram, error). Empty text -> the default
    program (no overrides, no authored rungs). Never raises."""
    prog = RecoveryProgram()
    if not text or not str(text).strip():
        return prog, None
    try:
        doc = json.loads(text)
    except (TypeError, ValueError):
        return None, "RECOVERY_JSON is not valid JSON"
    if not isinstance(doc, dict) or doc.get("_recovery") != 1:
        return None, "RECOVERY_JSON must be a {'_recovery': 1} envelope"
    prog.raw = doc
    rungs = doc.get("rungs", {})
    if rungs is None:
        rungs = {}
    if not isinstance(rungs, dict):
        return None, "rungs must be an object keyed by rung id"
    for rid, spec in rungs.items():
        if rid not in BUILTIN_RUNGS:
            return None, "unknown builtin rung %r" % (rid,)
        if not isinstance(spec, dict):
            return None, "rung %s override must be an object" % rid
        if spec.get("enabled") is False:
            if rid == "R5":
                return None, "the safe-stop rung R5 cannot be disabled"
            prog.disabled.add(rid)
        params = spec.get("params", {})
        if params is None:
            params = {}
        if not isinstance(params, dict):
            return None, "rung %s params must be an object" % rid
        for k, v in params.items():
            if k not in RUNG_PARAMS[rid]:
                return None, ("rung %s cannot override %r (allowed: %s)"
                              % (rid, k, ", ".join(RUNG_PARAMS[rid])))
            cur = getattr(eng, k, None)
            if isinstance(cur, bool):
                if not isinstance(v, bool):
                    return None, "rung %s param %s must be a bool" % (rid, k)
            elif not _pos_int(v, 0):
                return None, ("rung %s param %s must be a non-negative int"
                              % (rid, k))
            if k in prog.overrides and prog.overrides[k] != v:
                return None, ("conflicting overrides for %s across rungs"
                              % k)
            prog.overrides[k] = v
    authored = doc.get("authored", [])
    if authored is None:
        authored = []
    if not isinstance(authored, list) or len(authored) > 16:
        return None, "authored must be a list of at most 16 rungs"
    seen = set(BUILTIN_RUNGS)
    for i, a in enumerate(authored):
        if not isinstance(a, dict):
            return None, "authored rung #%d must be an object" % i
        rid = a.get("id")
        if not _id_ok(rid) or rid in seen:
            return None, ("authored rung #%d needs a unique lowercase id"
                          % i)
        seen.add(rid)
        err = _check_trigger(a.get("trigger"), i)
        if err:
            return None, err
        if (a["trigger"]["type"] == "custom_expr"
                and not eng._expr_shape_ok(a["trigger"].get("expr"), {})):
            return None, ("authored rung #%d custom_expr is malformed" % i)
        actions = a.get("actions")
        if not isinstance(actions, list) or not actions:
            return None, "authored rung %r needs an actions block list" % rid
        version = a.get("version", 2)
        if version not in (2, 3):
            return None, "authored rung %r version must be 2 or 3" % rid
        resume = a.get("resume", "retry_tick")
        if resume not in RESUMES:
            return None, ("authored rung %r resume must be one of %s"
                          % (rid, ", ".join(RESUMES)))
        leg = a.get("leg", "")
        if resume == "restart_stage" and leg not in PHASES:
            return None, "authored rung %r restart_stage needs a leg" % rid
        cooldown = a.get("cooldown_s", 0)
        if not _pos_int(cooldown, 0, 86400):
            return None, "authored rung %r cooldown_s must be >= 0" % rid
        limit = a.get("limit", 0)
        if not _pos_int(limit, 0, 100000):
            return None, "authored rung %r limit must be >= 0" % rid
        max_ms = a.get("max_ms", ACTION_BUDGET_DEFAULT_MS)
        if not _pos_int(max_ms, 1):
            return None, "authored rung %r max_ms must be positive" % rid
        max_ms = max(ACTION_BUDGET_MIN_MS,
                     min(ACTION_BUDGET_MAX_MS, max_ms))
        _runner, err = _validate_actions(eng, rid, actions, version,
                                         a.get("caps"), a.get("variables"))
        if err:
            return None, err
        r = AuthoredRung()
        r.id = rid
        r.name = str(a.get("name") or rid)[:60]
        r.enabled = a.get("enabled", True) is not False
        r.trigger = a["trigger"]
        r.actions = actions
        r.version = version
        r.caps = list(a.get("caps") or [])
        r.variables = list(a.get("variables") or [])
        r.max_ms = max_ms
        r.resume = resume
        r.leg = leg
        r.cooldown_s = int(cooldown)
        r.limit = int(limit)
        prog.authored.append(r)
    return prog, None


def build_default_program(eng):
    """The EXACT built-in ladder as data: no overrides, no authored rungs;
    every threshold stays bound to the live globals (value_for -> None)."""
    return RecoveryProgram()


class RecoveryRuntime(object):
    """Per-run runtime over a RecoveryProgram, consulted on the tick
    thread only. For the default program poll() is a no-op (no screen
    reads, no state changes) -- default behavior is byte-identical."""

    def __init__(self, eng, program):
        self.eng = eng
        self.program = program
        self.active_id = None          # currently executing rung (meta)
        self.fired = []                # rung ids fired (flow trigger feed)
        self.fired_this_poll = False   # flows skip a boundary we acted in
        self._pending_manual = []      # rung ids queued by recovery.trigger
        self._fires = {}               # authored id -> count this run
        self._cooldown_until = {}      # authored id -> perf_counter
        self._cue_seen = {}            # cue -> last-seen perf_counter
        self._cap_full_seen = None     # last capacity-FULL perf_counter
        self._img_since = {}           # rung id -> first-matched time
        self._expr_prev = {}           # rung id -> previous truthiness
        self._t0 = eng.time.perf_counter()

    # ---- consultation (the eng._rv seam) --------------------------------
    def value_for(self, key):
        return self.program.value_for(key)

    def note_builtin_fired(self, rid):
        """Called from the ladder sites when a builtin rung fires (state
        only -- no emission; feeds flow recovery{rung} triggers)."""
        self.fired.append(rid)
        del self.fired[:-16]

    def manual(self, rid):
        """recovery.trigger {id}: queue a manual firing (tick thread
        consumes it at the next boundary). Returns False for unknown id."""
        if rid not in self.program.rung_ids():
            return False
        self._pending_manual.append(rid)
        return True

    def consume_fired(self, rid):
        """FlowManager: pop-and-report whether the rung fired since last
        asked."""
        if rid in self.fired:
            self.fired.remove(rid)
            return True
        return False

    # ---- trigger evaluation ---------------------------------------------
    def _now(self):
        return self.eng.time.perf_counter()

    def _needs_sensing(self):
        for r in self.program.authored:
            if r.trigger["type"] in ("missing_cue", "capacity_timeout",
                                     "image"):
                return True
        return False

    def _sense_track(self, det):
        now = self._now()
        cues = set()
        for r in self.program.authored:
            t = r.trigger
            if t["type"] == "missing_cue":
                cues.add(t["cue"])
            elif t["type"] == "capacity_timeout":
                cues.add("_cap")
        for cue in cues:
            try:
                if cue == "_cap":
                    if det.capacity_full():
                        self._cap_full_seen = now
                    elif self._cap_full_seen is None:
                        self._cap_full_seen = self._t0
                else:
                    fn = {"pan": det.on_pan, "deposit": det.on_deposit,
                          "shake": det.on_shake}[cue]
                    if fn():
                        self._cue_seen[cue] = now
                    else:
                        self._cue_seen.setdefault(cue, self._t0)
            except Exception:
                pass

    def _image_state(self, rung, det):
        t = rung.trigger
        try:
            from . import vision as vision_mod
            path = vision_mod.asset_path(self.eng._HOME_DIR, t["asset"])
            tpl = vision_mod.load_png_rgb(path)
        except Exception:
            return False
        area = t.get("area")
        try:
            import numpy as np
            if area:
                x1, y1, x2, y2 = area
                gx, gy = min(x1, x2), min(y1, y2)
                gw, gh = max(1, abs(x2 - x1)), max(1, abs(y2 - y1))
            else:
                mon = det.sct.monitors[1]
                gx, gy = int(mon["left"]), int(mon["top"])
                gw, gh = int(mon["width"]), int(mon["height"])
            img = np.asarray(det.sct.grab(
                {"left": gx, "top": gy, "width": gw,
                 "height": gh}))[:, :, :3]
            img = np.ascontiguousarray(img[:, :, ::-1])
            from . import vision as vision_mod
            r = vision_mod.match_template(img, tpl,
                                          t.get("thr", 90) / 100.0)
            return bool(r["found"])
        except Exception:
            return False

    def _due(self, rung, det):
        eng = self.eng
        st = eng.State
        t = rung.trigger
        ty = t["type"]
        now = self._now()
        if ty == "manual":
            return False                       # only via recovery.trigger
        if ty == "no_progress":
            return (st.last_progress
                    and now - st.last_progress > t["s"])
        if ty == "shake_fail":
            return st.shake_fails >= t["count"]
        if ty == "stage_timeout":
            return (st.phase_name == t["leg"] and st.phase_t
                    and (now - st.phase_t) * 1000.0 > t["ms"])
        if ty == "missing_cue":
            seen = self._cue_seen.get(t["cue"])
            return seen is not None and (now - seen) * 1000.0 > t["ms"]
        if ty == "capacity_timeout":
            seen = self._cap_full_seen
            return seen is not None and (now - seen) * 1000.0 > t["ms"]
        if ty == "window_lost":
            try:
                return eng.find_roblox_rect() is None
            except Exception:
                return False
        if ty == "custom_expr":
            try:
                stub = _ExprStub(eng)
                val = eng._script_eval(t["expr"], stub, det)
                cur = bool(eng._sv_truthy(val))
            except Exception:
                cur = False
            prev = self._expr_prev.get(rung.id, False)
            self._expr_prev[rung.id] = cur
            return cur and not prev            # rising edge
        if ty == "image":
            if self._image_state(rung, det):
                since = self._img_since.setdefault(rung.id, now)
                return (now - since) * 1000.0 >= t.get("ms", 1)
            self._img_since.pop(rung.id, None)
            return False
        if ty == "flow_signal":
            mgr = getattr(eng.State, "flows_mgr", None)
            return bool(mgr and mgr.consume_signal(t["flow"]))
        return False

    # ---- execution -------------------------------------------------------
    def _emit_rung(self, rid, authored, action):
        self.eng.emit_event("recovery_rung", "recovery rung %s" % rid,
                            extra={"id": rid, "authored": bool(authored),
                                   "action": action})

    def _run_actions(self, rung, det):
        """Bounded sub-runner: release_all -> actions (single pass, budget
        checked between blocks) -> release_all. Input allowed -- recovery
        outranks everything."""
        eng = self.eng
        doc = {"version": rung.version, "blocks": rung.actions}
        if rung.version >= 3:
            doc["caps"] = rung.caps
        if rung.variables:
            doc["variables"] = rung.variables
        runner = eng.ScriptRunner(json.dumps(doc), "recovery:%s" % rung.id)
        if runner.dead:                        # can't happen post-parse
            return False
        runner.last_emit_t = float("inf")      # no script.block noise
        runner._det = det
        runner.stack = [[runner.blocks, 0, 1, "hook", None]]
        eng.release_all()
        deadline = self._now() + rung.max_ms / 1000.0
        ok = True
        try:
            while (runner.stack and eng.State.running and eng.State.alive
                   and self._now() < deadline):
                runner._step(det)
        except Exception as e:
            eng.log("[recovery] rung %s action error: %s" % (rung.id, e))
            ok = False
        finally:
            eng.release_all()
        if runner.stack and self._now() >= deadline:
            eng.log("[recovery] rung %s hit its %d ms budget"
                    % (rung.id, rung.max_ms))
        return ok

    def _apply_resume(self, rung):
        eng = self.eng
        if rung.resume == "retry_tick":
            return                             # next tick re-senses
        if rung.resume in ("restart_stage", "restart_cycle"):
            eng.State.want_reset = True        # Supervisor/ScriptRunner
            return                             # reset on the next tick
        if rung.resume == "safe_stop":
            eng.safe_stop("recovery rung %s" % rung.id)
            return
        if rung.resume == "hard_stop":
            eng.safe_stop("recovery rung %s" % rung.id, hard=True)

    def _fire_authored(self, rung, det):
        now = self._now()
        if rung.limit and self._fires.get(rung.id, 0) >= rung.limit:
            return False
        if now < self._cooldown_until.get(rung.id, 0.0):
            return False
        self._fires[rung.id] = self._fires.get(rung.id, 0) + 1
        self._cooldown_until[rung.id] = now + rung.cooldown_s
        self.active_id = rung.id
        self.fired.append(rung.id)
        del self.fired[:-16]
        self.fired_this_poll = True
        self._emit_rung(rung.id, True, "ir_actions")
        try:
            self._run_actions(rung, det)
            self._apply_resume(rung)
        finally:
            self.active_id = None
        return True

    def _fire_builtin_manual(self, rid, det):
        eng = self.eng
        self.active_id = rid
        self.fired_this_poll = True
        try:
            s = eng.sense_stable(det)
            if rid in ("R1", "R2"):
                self._emit_rung(rid, False, "break_out")
                eng.break_out(det, s)
            elif rid == "R3":
                self._emit_rung(rid, False, "recover")
                eng.recover(det, s)
            elif rid == "R4":
                self._emit_rung(rid, False, "safe_stop")
                eng.safe_stop("manual recovery trigger (R4)")
            else:
                self._emit_rung(rid, False, "safe_stop")
                eng.safe_stop("manual recovery trigger (R5)")
        finally:
            self.active_id = None

    def poll(self, det):
        """Tick-boundary consultation (before each supervisor/script
        tick). Default program: returns immediately -- no reads, no state
        changes."""
        self.fired_this_poll = False
        eng = self.eng
        st = eng.State
        if not st.running or st.safe_paused:
            return
        pending, self._pending_manual = self._pending_manual, []
        for rid in pending:
            if not st.running:
                return
            if rid in BUILTIN_RUNGS:
                self._fire_builtin_manual(rid, det)
            else:
                for rung in self.program.authored:
                    if rung.id == rid:
                        self._fire_authored(rung, det)
                        break
        if not self.program.authored:
            return
        if self._needs_sensing():
            self._sense_track(det)
        for rung in self.program.authored:
            if not st.running:
                return
            if not rung.enabled:
                continue
            if self.active_id is not None:
                break
            try:
                due = self._due(rung, det)
            except Exception:
                due = False
            if due:
                self._fire_authored(rung, det)


class _ExprStub(object):
    """Minimal ScriptRunner stand-in for evaluating a custom_expr trigger
    over the v2 engine reads (no variables, pass 0)."""

    def __init__(self, eng):
        self.version = 2
        self.vars = {}
        self.var_decls = {}
        self.passes = 0
        self._t0 = eng.time.perf_counter()
        self._rand = eng._mulberry32(1)


def describe_effective(eng, text):
    """plan.describe helper: parse RECOVERY_JSON -> {overrides, disabled,
    authored[...describe()], error?} or None when no program is
    configured. Deterministic."""
    if not text or not str(text).strip():
        return None
    prog, err = parse_program(eng, text)
    if err:
        return {"error": err}
    return {
        "overrides": {k: prog.overrides[k]
                      for k in sorted(prog.overrides)},
        "disabled": sorted(prog.disabled),
        "authored": [r.describe() for r in prog.authored],
    }
