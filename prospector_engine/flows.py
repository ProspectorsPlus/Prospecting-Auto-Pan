"""Background flow manager (canonical-foundation pass, spec C
deliverable 2; docs/CANONICAL_CYCLE_PLAN.md section 2.5).

Generalizes the RelicScheduler pattern: authored flows are PPScript IR
block bodies scheduled at SAFE BOUNDARIES on the engine tick thread --
polled exactly like relics.maybe_fire() before each supervisor/script
tick, plus cycle-boundary detection off the engine's own completed-pan
counter. The single-input-writer invariant is sacred:

  * input: "none" flows are REFUSED at validation if their body contains
    any input-producing node type; they tick ONE block per engine loop
    iteration, interleaved with the supervisor mid-cycle (true concurrent
    sensing/logic at tick granularity).
  * input-exclusive flows run their whole body to completion (or budget)
    at the boundary the way RelicScheduler._fire owns input:
    release_all -> body -> release_all -> the supervisor re-senses next
    tick. Arbitration is priority-ordered with per-flow on_conflict
    policies (queue | skip | cancel_lower | fail).

Safety/recovery always outranks flows: the manager skips any boundary
where a recovery rung acted, and never runs while safe-paused. Engine
pause suspends flows (paused/resumed events); stop cancels in-flight
flows and release_all runs on every stop edge as always.

FLOWS_JSON envelope (exact schema):
    {
      "_flows": 1,
      "flows": [
        {"id": "poller",             # ^[a-z][a-z0-9_]{0,31}$, unique
         "name": "label",            # optional
         "enabled": true,
         "trigger": {"type": ...},   # vocabulary below
         "priority": 0,              # higher runs first (ties: list order)
         "input": "none",            # or {"exclusive":
                                     #     {"on_conflict": "queue" |
                                     #      "skip" | "cancel_lower" |
                                     #      "fail"}}
         "body": [ ...IR blocks ],   # PPScript v2/v3 vocabulary
         "version": 2, "caps": [], "variables": [],
         "max_ms": 10000}            # per-firing budget
      ]
    }

Trigger vocabulary:
    {"type": "run_start"}
    {"type": "cycle_start"} | {"type": "cycle_end"}
    {"type": "interval", "every_s": N}
    {"type": "timer", "at_s": N}          # once, at runtime >= N s
    {"type": "stage", "phase": "dig|water|shake|glide|settle"}
    {"type": "recovery", "rung": RUNG_ID}
    {"type": "image", "asset": ID, "area": [x1,y1,x2,y2]?, "thr": 50-100,
     "poll_ms": N}                        # fires on found (rising edge)
    {"type": "variable", "expr": {...}}   # v2 $expr, rising edge
    {"type": "manual"}                    # flow.trigger only

Lifecycle event: flow.state {id, state, reason?} with state in
started | finished | skipped | canceled | failed | paused | resumed.
stats meta.flows mirrors {id: state}.
"""
import json

TRIGGER_TYPES = ("run_start", "cycle_start", "cycle_end", "interval",
                 "timer", "stage", "recovery", "image", "variable",
                 "manual")
POLICIES = ("queue", "skip", "cancel_lower", "fail")
PHASES = ("dig", "water", "shake", "glide", "settle")

BUDGET_MIN_MS = 100
BUDGET_MAX_MS = 120000
BUDGET_DEFAULT_MS = 10000
_ID_MAX = 32

# Input-producing node types, enumerated from the ScriptRunner handler
# registry semantics (any node that presses/holds/clicks/scrolls/types or
# releases input, or moves the cursor). wait_cue is input-producing ONLY
# when it carries a movement hold -- special-cased in _body_has_input.
INPUT_NODE_TYPES = frozenset((
    "dig", "shake", "hold_key", "tap_key", "click", "relic", "long_press",
    "move_mouse", "drag_mouse",
    "key_press", "key_hold", "key_down", "key_up", "key_combo", "key_seq",
    "type_text", "set_clipboard", "release_keys", "mouse_btn", "scroll",
    "click_image", "move_image",
    "walk_back_x",
    # v4 parallel: its branches can hold/press keys and buttons, so the
    # whole Fork region counts as input-producing for flow arbitration.
    "parallel",
))


def _id_ok(s):
    return (isinstance(s, str) and 0 < len(s) <= _ID_MAX and s[0].isalpha()
            and s[0].islower()
            and all(c.islower() or c.isdigit() or c == "_" for c in s))


def _pos_int(v, lo=1, hi=10 ** 9):
    return (isinstance(v, int) and not isinstance(v, bool)
            and lo <= v <= hi)


def _body_has_input(blocks):
    """First input-producing node type found in the tree, or None."""
    for b in blocks:
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t in INPUT_NODE_TYPES:
            return t
        if t == "wait_cue":
            hold = (b.get("params") or {}).get("hold")
            if hold not in (None, "", "none"):
                return "wait_cue(hold)"
        for lst in (b.get("children"), b.get("else")):
            if isinstance(lst, list) and lst:
                hit = _body_has_input(lst)
                if hit:
                    return hit
    return None


def _check_trigger(t, idx):
    if not isinstance(t, dict) or t.get("type") not in TRIGGER_TYPES:
        return "flow #%d has an unknown trigger" % idx
    ty = t["type"]
    if ty == "interval" and not _pos_int(t.get("every_s"), 1, 86400):
        return "interval needs every_s"
    if ty == "timer" and not _pos_int(t.get("at_s"), 1, 86400):
        return "timer needs at_s"
    if ty == "stage" and t.get("phase") not in PHASES:
        return "stage needs a phase"
    if ty == "recovery" and not isinstance(t.get("rung"), str):
        return "recovery trigger needs a rung id"
    if ty == "image":
        if not isinstance(t.get("asset"), str) or not t.get("asset"):
            return "image trigger needs an asset id"
        if not _pos_int(t.get("thr", 90), 50, 100):
            return "image thr must be 50..100"
        if not _pos_int(t.get("poll_ms", 250), 50, 60000):
            return "image poll_ms must be 50..60000"
    if ty == "variable" and "expr" not in t:
        return "variable trigger needs expr"
    return None


class Flow(object):
    __slots__ = ("id", "name", "enabled", "trigger", "priority",
                 "exclusive", "on_conflict", "body", "version", "caps",
                 "variables", "max_ms")

    def describe(self):
        return {"id": self.id, "name": self.name, "enabled": self.enabled,
                "trigger": self.trigger, "priority": self.priority,
                "input": ({"exclusive": {"on_conflict": self.on_conflict}}
                          if self.exclusive else "none"),
                "body": {"ir_blocks": len(self.body),
                         "version": self.version, "max_ms": self.max_ms}}


def parse_flows(eng, text):
    """FLOWS_JSON -> ([Flow], error). Empty text -> ([], None).
    input:'none' bodies with any input-producing node are REFUSED here
    (and the IR itself passes the standard ScriptRunner validation --
    limits and caps triple-enforcement intact)."""
    if not text or not str(text).strip():
        return [], None
    try:
        doc = json.loads(text)
    except (TypeError, ValueError):
        return None, "FLOWS_JSON is not valid JSON"
    if not isinstance(doc, dict) or doc.get("_flows") != 1:
        return None, "FLOWS_JSON must be a {'_flows': 1} envelope"
    raw = doc.get("flows", [])
    if not isinstance(raw, list) or len(raw) > 16:
        return None, "flows must be a list of at most 16 flows"
    flows = []
    seen = set()
    for i, f in enumerate(raw):
        if not isinstance(f, dict):
            return None, "flow #%d must be an object" % i
        fid = f.get("id")
        if not _id_ok(fid) or fid in seen:
            return None, "flow #%d needs a unique lowercase id" % i
        seen.add(fid)
        err = _check_trigger(f.get("trigger"), i)
        if err:
            return None, err
        if (f["trigger"]["type"] == "variable"
                and not eng._expr_shape_ok(f["trigger"].get("expr"), {})):
            return None, "flow #%d variable expr is malformed" % i
        body = f.get("body")
        if not isinstance(body, list) or not body:
            return None, "flow %r needs a body block list" % fid
        version = f.get("version", 2)
        if version not in (2, 3):
            return None, "flow %r version must be 2 or 3" % fid
        inp = f.get("input", "none")
        exclusive = False
        policy = "queue"
        if inp == "none":
            hit = _body_has_input(body)
            if hit:
                return None, ("flow %r declares input:none but its body "
                              "contains the input node %r -- declare "
                              "input exclusive or remove the node"
                              % (fid, hit))
        elif isinstance(inp, dict) and isinstance(inp.get("exclusive"),
                                                  dict):
            exclusive = True
            policy = inp["exclusive"].get("on_conflict", "queue")
            if policy not in POLICIES:
                return None, ("flow %r on_conflict must be one of %s"
                              % (fid, ", ".join(POLICIES)))
        else:
            return None, ("flow %r input must be \"none\" or "
                          "{\"exclusive\": {...}}" % fid)
        prio = f.get("priority", 0)
        if not _pos_int(prio, -1000, 1000) and prio != 0:
            return None, "flow %r priority must be an int" % fid
        max_ms = f.get("max_ms", BUDGET_DEFAULT_MS)
        if not _pos_int(max_ms, 1):
            return None, "flow %r max_ms must be positive" % fid
        max_ms = max(BUDGET_MIN_MS, min(BUDGET_MAX_MS, max_ms))
        # standard IR validation (the same validator scripts go through)
        sdoc = {"version": version, "blocks": body}
        if version >= 3:
            sdoc["caps"] = list(f.get("caps") or [])
        if f.get("variables"):
            sdoc["variables"] = f.get("variables")
        runner = eng.ScriptRunner(json.dumps(sdoc), "flow:%s" % fid)
        if runner.dead:
            return None, "flow %r body rejected: %s" % (fid, runner.dead)
        fl = Flow()
        fl.id = fid
        fl.name = str(f.get("name") or fid)[:60]
        fl.enabled = f.get("enabled", True) is not False
        fl.trigger = f["trigger"]
        fl.priority = int(prio)
        fl.exclusive = exclusive
        fl.on_conflict = policy
        fl.body = body
        fl.version = version
        fl.caps = list(f.get("caps") or [])
        fl.variables = list(f.get("variables") or [])
        fl.max_ms = max_ms
        flows.append(fl)
    return flows, None


class _FlowRun(object):
    """One flow's per-run scheduling + execution state."""

    __slots__ = ("flow", "runner", "state", "error", "next_due", "fired_at",
                 "inflight", "deadline", "manual_pending", "img_prev",
                 "expr_prev", "img_next_poll", "started_emitted")

    def __init__(self, flow):
        self.flow = flow
        self.runner = None
        self.state = "idle"
        self.error = ""
        self.next_due = None
        self.fired_at = None
        self.inflight = False
        self.deadline = None
        self.manual_pending = False
        self.img_prev = False
        self.expr_prev = False
        self.img_next_poll = 0.0
        self.started_emitted = False


class FlowManager(object):
    """Polled on the tick thread exactly like relics.maybe_fire(). One
    manager per run (built at fresh start from FLOWS_JSON)."""

    def __init__(self, eng, flows):
        self.eng = eng
        self.runs = [_FlowRun(f) for f in flows]
        self._t0 = eng.time.perf_counter()
        self._last_cycles = 0
        self._ran_run_start = False
        self._phase_seq_seen = eng.State.phase_seq
        self._new_phases = ()
        self._signals = []            # failed-flow ids (recovery feed)
        self._queue = []              # exclusive firings queued to a
                                      # later boundary (queue policy)
        self._preempt = None          # cancel_lower arrival mid-execution

    # ---- host surface ----------------------------------------------------
    def states(self):
        """meta.flows: {id: state} for every configured flow."""
        return {r.flow.id: r.state for r in self.runs}

    def manual(self, fid):
        """flow.trigger {id}: queue a manual firing. False = unknown."""
        for r in self.runs:
            if r.flow.id == fid:
                r.manual_pending = True
                return True
        return False

    def consume_signal(self, fid):
        """RecoveryRuntime flow_signal trigger: the named flow failed."""
        if fid in self._signals:
            self._signals.remove(fid)
            return True
        return False

    def on_pause(self):
        for r in self.runs:
            if r.inflight:
                self._emit(r, "paused")

    def on_resume(self):
        for r in self.runs:
            if r.inflight:
                self._emit(r, "resumed")

    def on_stop(self):
        """Stop cancels in-flight flows; the stop edge's release_all
        already owns the input floor."""
        for r in self.runs:
            if r.inflight:
                r.inflight = False
                r.runner = None
                self._emit(r, "canceled", "run stopped")
        del self._queue[:]

    # ---- plumbing ---------------------------------------------------------
    def _now(self):
        return self.eng.time.perf_counter()

    def _emit(self, run, state, reason=""):
        run.state = state
        payload = {"id": run.flow.id, "state": state}
        if reason:
            payload["reason"] = reason
        try:
            self.eng.EMIT.flow_state(payload)
        except Exception:
            pass
        if state == "failed":
            self._signals.append(run.flow.id)
            del self._signals[:-16]

    def _fresh_runner(self, run):
        eng = self.eng
        f = run.flow
        doc = {"version": f.version, "blocks": f.body}
        if f.version >= 3:
            doc["caps"] = f.caps
        if f.variables:
            doc["variables"] = f.variables
        runner = eng.ScriptRunner(json.dumps(doc), "flow:%s" % f.id)
        runner.last_emit_t = float("inf")    # no script.block noise
        runner.stack = [[runner.blocks, 0, 1, "hook", None]]
        return runner

    # ---- triggers ---------------------------------------------------------
    def _due(self, run, det, cycles_ended):
        eng = self.eng
        st = eng.State
        f = run.flow
        t = f.trigger
        ty = t["type"]
        now = self._now()
        if run.manual_pending:
            run.manual_pending = False
            return True
        if ty == "manual":
            return False
        if ty == "run_start":
            return not self._ran_run_start
        if ty == "cycle_end":
            return cycles_ended > 0
        if ty == "cycle_start":
            # a cycle starts at run start and after every completed pan
            return (not self._ran_run_start) or cycles_ended > 0
        if ty == "interval":
            if run.next_due is None:
                run.next_due = self._t0 + t["every_s"]
            if now >= run.next_due:
                run.next_due = now + t["every_s"]
                return True
            return False
        if ty == "timer":
            if run.fired_at is not None:
                return False
            if now - self._t0 >= t["at_s"]:
                run.fired_at = now
                return True
            return False
        if ty == "stage":
            return t["phase"] in self._new_phases
        if ty == "recovery":
            rt = getattr(st, "recovery", None)
            return bool(rt and rt.consume_fired(t["rung"]))
        if ty == "variable":
            try:
                stub = _stub(eng)
                cur = bool(eng._sv_truthy(
                    eng._script_eval(t["expr"], stub, det)))
            except Exception:
                cur = False
            prev, run.expr_prev = run.expr_prev, cur
            return cur and not prev
        if ty == "image":
            if now < run.img_next_poll:
                return False
            run.img_next_poll = now + t.get("poll_ms", 250) / 1000.0
            cur = self._image_found(t, det)
            prev, run.img_prev = run.img_prev, cur
            return cur and not prev
        return False

    def _image_found(self, t, det):
        eng = self.eng
        try:
            from . import vision as vision_mod
            import numpy as np
            path = vision_mod.asset_path(eng._HOME_DIR, t["asset"])
            tpl = vision_mod.load_png_rgb(path)
            area = t.get("area")
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
            r = vision_mod.match_template(img, tpl,
                                          t.get("thr", 90) / 100.0)
            return bool(r["found"])
        except Exception:
            return False

    # ---- execution ---------------------------------------------------------
    def _start(self, run, det):
        run.runner = self._fresh_runner(run)
        run.runner._det = det
        run.inflight = True
        run.deadline = self._now() + run.flow.max_ms / 1000.0
        self._emit(run, "started")

    def _step_readonly(self, run, det):
        """One block per engine loop iteration -- interleaved concurrency."""
        eng = self.eng
        if self._now() > run.deadline:
            run.inflight = False
            run.runner = None
            self._emit(run, "failed", "budget exceeded")
            return
        try:
            run.runner._step(det)
        except Exception as e:
            run.inflight = False
            run.runner = None
            self._emit(run, "failed", "error: %s" % e)
            return
        if not run.runner.stack:
            run.inflight = False
            run.runner = None
            self._emit(run, "finished")

    def _run_exclusive(self, run, det):
        """Whole body at the boundary, the RelicScheduler._fire way:
        release_all -> body -> release_all; the supervisor re-senses on
        the next tick. Between blocks, a due cancel_lower flow of higher
        priority preempts (the canceled flow reports canceled)."""
        eng = self.eng
        self._start(run, det)
        eng.release_all()
        failed = ""
        canceled = False
        try:
            while (run.runner.stack and eng.State.running
                   and eng.State.alive):
                if self._now() > run.deadline:
                    failed = "budget exceeded"
                    break
                try:
                    run.runner._step(det)
                except Exception as e:
                    failed = "error: %s" % e
                    break
                pre = self._check_preempt(run, det)
                if pre is not None:
                    canceled = True
                    self._preempt = pre
                    break
        finally:
            eng.release_all()
        run.inflight = False
        run.runner = None
        if canceled:
            self._emit(run, "canceled", "preempted by higher priority")
        elif failed:
            self._emit(run, "failed", failed)
        elif not eng.State.running:
            self._emit(run, "canceled", "run stopped")
        else:
            self._emit(run, "finished")

    def _check_preempt(self, running, det):
        """Mid-execution: does a higher-priority cancel_lower exclusive
        flow become due? Returns that run (to fire next) or None."""
        for r in self.runs:
            f = r.flow
            if (not f.enabled or not f.exclusive or r is running
                    or r.state == "error" or f.on_conflict != "cancel_lower"
                    or f.priority <= running.flow.priority):
                continue
            try:
                if self._due(r, det, 0):
                    return r
            except Exception:
                pass
        return None

    def poll(self, det):
        """The tick-boundary hook. No flows configured -> None manager;
        this method is never on the default path."""
        eng = self.eng
        st = eng.State
        if not st.running or st.safe_paused:
            return
        rt = getattr(st, "recovery", None)
        if rt is not None and (rt.active_id is not None
                               or rt.fired_this_poll):
            return                     # recovery outranks flows
        now = self._now()
        cycles = st.stats.cycles if st.stats else 0
        cycles_ended = max(0, cycles - self._last_cycles)
        self._last_cycles = cycles
        # phases emitted since the last boundary (stage triggers see every
        # transition, even ones inside a single supervisor tick)
        self._new_phases = tuple(n for (q, n) in st.phase_ring
                                 if q > self._phase_seq_seen)
        self._phase_seq_seen = st.phase_seq
        # 1) advance in-flight read-only flows: one block each, every
        #    loop iteration (interleaved with the supervisor mid-cycle)
        for r in self.runs:
            if r.inflight and not r.flow.exclusive:
                self._step_readonly(r, det)
        # 2) collect due firings
        due = []
        for r in self.runs:
            f = r.flow
            if not f.enabled or r.state == "error" or r.inflight:
                # a re-trigger while in-flight is ignored (documented)
                if r.inflight:
                    try:
                        self._due(r, det, cycles_ended)   # consume edges
                    except Exception:
                        pass
                continue
            try:
                if self._due(r, det, cycles_ended):
                    due.append(r)
            except Exception:
                pass
        self._ran_run_start = True
        self._new_phases = ()          # consumed; preempt checks must not
                                       # re-fire stage triggers
        # 3) start read-only firings (all of them -- no input, no conflict)
        for r in [r for r in due if not r.flow.exclusive]:
            self._start(r, det)
            due.remove(r)
        # 4) exclusive arbitration: priority order; conflicts per policy
        due.extend(q for q in self._queue if q not in due)
        del self._queue[:]
        due.sort(key=lambda r: -r.flow.priority)
        while due:
            if not st.running or st.safe_paused:
                break
            r = due.pop(0)
            # every other due exclusive is in conflict with r right now
            keep = []
            for other in due:
                pol = other.flow.on_conflict
                if pol == "skip":
                    self._emit(other, "skipped", "conflict")
                elif pol == "fail":
                    self._emit(other, "failed", "conflict")
                    other.state = "error"
                else:                  # queue / cancel_lower wait
                    keep.append(other)
            due = keep
            self._run_exclusive(r, det)
            if self._preempt is not None:
                pre, self._preempt = self._preempt, None
                if pre not in due:
                    due.insert(0, pre)
        self._queue.extend(due)


def _stub(eng):
    class _S(object):
        version = 2
        vars = {}
        var_decls = {}
        passes = 0
    s = _S()
    s._t0 = eng.time.perf_counter()
    s._rand = eng._mulberry32(1)
    return s


def describe_effective(eng, text):
    """plan.describe helper: authored flows section, or None when no
    flows are configured."""
    if not text or not str(text).strip():
        return None
    flows, err = parse_flows(eng, text)
    if err:
        return {"error": err}
    return [f.describe() for f in flows]
