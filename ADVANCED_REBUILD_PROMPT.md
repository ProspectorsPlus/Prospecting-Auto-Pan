# PROMPT: Evolve "Prospectors Plus" into a superior, feature-complete v4 — WITHOUT breaking what works

You are an expert automation engineer taking over a mature, working project.
Your job is to make the existing macro **meaningfully better and more
consistent** while keeping it — in behavior, calibration, features, and feel —
**the same macro**. This is an EVOLUTION of a battle-tested codebase, not a
rewrite.

---

## 1. Mission statement (read twice)

Produce the next version of **Prospectors Plus**, an auto-pan macro for the
Roblox game *Prospecting* (macOS primary, Windows kept in lockstep). It must
be:

1. **A strict superset of the old macro.** Every feature, tab, setting,
   behavior, and calibration workflow that exists today must exist and work
   identically, unless the user explicitly approves a change.
2. **More consistent.** Fewer wasted cycles, fewer failed shakes, fewer blind
   nudges, fewer stalls — measured by the run telemetry, not vibes.
3. **Improved only additively.** Every improvement is a new, clearly named
   setting that is opt-in (or a default the user explicitly signs off on),
   with the old behavior always reachable by turning it off.

**The prime directive: parity first, then improve.** You are NOT allowed to
design a new engine, new detection semantics, new defaults, or a new config
format and then "port" the old macro onto it. That was tried (see §3) and it
failed. You start FROM the old code and change it in small, verified steps.

---

## 2. Required reading, in this order (all in the project folder)

1. `Prospectors_Plus_Knowledge_Base.md` — the complete project brain dump:
   game mechanics, glitches, architecture, every setting, war stories, hard
   rules. **Read §11 (Hard rules) and §10 (War stories) before touching any
   code.**
2. `prospecting_old.py` — **the live engine** (despite the name). Input
   layer, Detector, supervisor state machine, do_shake, recovery, Fortune
   River, treasure mode. ~2,500 lines. Read the whole thing.
3. `prospecting_app.py` — the desktop app (pywebview): UI, calibration
   overlay + guided calibrator, saved builds, relic editor, Coach, webhooks,
   pop-out pill, updates, access gate.
4. `prospecting_ui.py` — the settings schema (`SECTIONS`, `HELP`,
   `PIXEL_FIELDS`) and platform launcher.
5. `prospecting_assistant.py` — the Coach brain (offline expert system + API
   mode).
6. `prospecting_config.json` — the user's LIVE config: real calibrated
   pixels, real timings, real stats. Treat as ground truth for what "works
   on this machine" means. **Never copy `WEBHOOK_SECRET`, `COACH_API_KEY`,
   or `SYNC_URL` into new files, logs, or documentation.**
7. `run_history.json` — real telemetry. This defines your improvement
   targets (see §5).
8. `Fable5/` — a failed rebuild attempt. Study it only as a cautionary
   example (§3) and as a parts bin (its simulator/test harness idea is worth
   salvaging). Do not base the new version on it.

---

## 3. Why the last attempt failed (do not repeat this)

A previous model built `Fable5/` — a from-scratch engine with a new config
schema, new perception pipeline, and new defaults. It compiled, its
simulator tests passed, and it still failed the real user immediately:

- **Calibration regressed.** It reinterpreted the calibrated pixels with new
  semantics (fill-fraction over an inset band with new thresholds, a
  combined capture region) instead of reusing the old Detector's proven
  logic. Same pixel coordinates + different interpretation = broken
  detection. It also **ignored `WINDOW_RELATIVE`** (true in the user's
  config): the old engine shifts pixels when the Roblox window moves
  (`apply_window_offset`); the rebuild read raw absolute coords, so if the
  window wasn't exactly where it was at calibration time, every read was
  wrong.
- **The cycle felt wrong.** It silently changed movement defaults
  (walk-back budget 200→350 ms, extra water depth 0→60 ms, momentum-W
  delayed until "start confirmed"). Each was defensible in isolation; the
  sum changed the macro's rhythm on a build that was already tuned.
- **Features were missing.** No saved builds, no guided calibration, no
  manual overlay, no Coach, no easy-tuning knobs, no Fortune River, no
  treasure mode, no webhooks, no pop-out pill. "Core first, rest later" is
  not acceptable to this user — the features ARE the product.
- **Simulator green ≠ works.** Its game model validated its own
  assumptions. Simulation is a useful regression tool but is never evidence
  the real macro works. Only live runs on the user's machine are.

Lessons, distilled: reuse proven code verbatim; keep config keys and
semantics byte-compatible; never change a default without sign-off; ship
parity before improvements; validate every step live with the user.

---

## 4. The game and macro in one page (full detail in the knowledge base)

Loop: dig on land until the **capacity bar** (gray→gold, bottom center) is
FULL → hold S back into water until the white **"Pan"** prompt → rattle
**rapid mouse clicks** while holding W (the "momentum shake") until the bar
reads EMPTY (momentum glides you back to shore while the pan drains) → dig
again. Prompts: "Collect Deposit" = on land, "Pan" = in water, "Shake" =
shaking.

Non-negotiable physics of this game/platform:
- **Capacity bar is ground truth.** The Shake cue STICKS after shakes end;
  Deposit/Shake cues flicker. Only the Pan cue is trusted for "in water".
- **macOS: synthetic held mouse presses do NOT register in Roblox.** Shaking
  must be a rapid click stream. Keyboard holds work fine.
- **The shake glitch is real:** the game sometimes silently ignores the
  shake (especially near the water's edge). The whole recovery system
  exists because of this.
- Formulas (community calculator, used for estimates only):
  digs-to-fill `= ceil(Capacity / (1.5·DigStrength))`; dig hold
  `= 55000 / DigSpeed%` ms; rolls/s
  `= 4.03266e-9·ss³ − 1.68935e-5·ss² + 0.0255557·ss + 0.206594`;
  cycle `= Cap/(rolls·ShakeStr) + 1.5 + 190·n/DigSpeed% + 0.62`.

User's machine/build (from live config): macOS, pixels calibrated at
CAP 682–1119 @ y878, prompts near (770–848, 981–987); DIG_SPEED 1508
(dig hold 36 ms), Capacity 9448, DigStrength 3521 (2 digs/fill),
ShakeStrength 835, ShakeSpeed 1768 (~0.76 s drain), ~1,200–2,000 pans/hr.
`WINDOW_RELATIVE: true`. Recovery toggles currently OFF in config.
Relics: Solar Magnifier (slot 5, 10 min), Infernal Idol (slot 6, 15 min).

---

## 5. Improvement targets (from `run_history.json`, last 40 runs ≈ 54 min)

| Failure class | Count | Meaning |
|---|---|---|
| `nudge` | 397 | dig-probe missed land → blind forward nudges (landing variance) |
| `shake_fail: never started` | 124 | clicked but the game ignored the shake (edge/glitch) |
| `shake_fail: ran but didn't empty` | 115 | shake started but bar never read empty |
| `no_progress` / `break_out` | 16 | full stalls needing escalation |

"Better" means these numbers drop on THIS user's machine during live runs,
while pans/hr holds or improves, with zero new failure classes introduced.
Add a per-run "clean cycle %" metric (cycles with no retries/recoveries) so
consistency becomes a visible number in the UI and history.

---

## 6. Execution plan — strictly in this order

### Phase 0 — Baseline (no code changes)
- Read everything in §2. Build a feature inventory of the old app (every
  tab, button, setting, behavior). Present it to the user as a parity
  checklist and get confirmation nothing is missing.
- Run the OLD macro's self-checks if present; confirm with the user that the
  old macro currently works, so you have a known-good baseline to diff
  against.

### Phase 1 — Refactor WITHOUT behavior change (parity release)
- Restructure for maintainability if useful (modules instead of one file),
  but **copy the proven logic verbatim**: `Detector` (pixel semantics,
  `is_yellow`, cue white-fraction tests, edge insets), `do_shake` (verbatim
  — it is explicitly protected by project rules), `go_water`,
  `return_and_dig`, the Supervisor tick/watchdogs, `apply_window_offset` /
  `AUTO_CALIBRATE` handling, input layer (Quartz / SendInput).
- Keep `prospecting_config.json` as THE config, same keys, same meanings.
  Same for `prospecting_builds.json` (saved builds MUST load unchanged).
- Full feature parity: guided calibration, manual overlay calibration, live
  test-detection, saved builds (load = merge, keeping pixels/webhook), easy
  tuning, auto-build (conservative — only sets `MAX_DIGS_TO_FILL`,
  `DIG_SPEED`, `DIG_CLICK_MS`, `PERFECT`), relic timers, treasure mode,
  Fortune River recovery, recovery/breakout/safe-stop-retry ladder, Discord
  webhooks + screenshots, keybinds, pop-out pill, run history + telemetry,
  the Coach (offline + API mode), access gate, auto-update. If the cleanest
  path is "the old app with internal cleanups," that is a perfectly good
  Phase 1.
- **Exit gate:** the user runs it live and confirms it behaves identically
  to the old macro (same calibration, same cycle feel, same pans/hr). Do
  not proceed until they say so.

### Phase 2 — Consistency upgrades, ONE at a time, each opt-in
Deliver each as a separate, individually testable change with a settings
toggle, defaulted OFF (or ON only with explicit user sign-off). After each:
the user does a live run; you compare telemetry before/after; keep or
revert. Candidates, in priority order:

1. **Faster shake-fail detection** (targets the 124 "never started"): after
   starting the click stream, confirm the shake actually began (Shake cue
   OR capacity beginning to drop) within a configurable window; on failure,
   retry immediately (optionally with a small extra S tap for depth)
   instead of burning the full bail window. Toggle e.g.
   `SHAKE_START_CONFIRM` + `SHAKE_START_RETRIES`. Do not restructure
   `do_shake`'s click loop to do this — wrap around it or gate a small
   guarded branch inside it.
2. **Cue-confirmed landing assist** (targets the 397 nudges): optional
   short W-hold until "Collect Deposit" confirms before the first probe
   dig. Toggle e.g. `LAND_CUE_ASSIST`.
3. **Continuous background sensing** (perf/latency): a capture thread
   publishing debounced cue/fill state that the EXISTING logic reads in
   place of ad-hoc grabs — same thresholds, same pixel semantics, must be
   bit-identical in its answers on static screens; prove it with a live A/B
   test-detection view before wiring it in. Toggle `FAST_SENSE`.
4. **Drain-stall detection** (targets "ran but didn't empty"): while
   shaking, if fill stops decreasing for N ms, treat as failed early.
5. **Clean-cycle % + per-phase timing telemetry** in History and the pill.
6. Only after all the above are stable: adaptive water-depth trim
   (bounded, decaying, off by default).

### Phase 3 — Windows lockstep + release
Mirror every change into the `windows/` copies (input layer differs;
everything else byte-identical). Update `installer.iss` version,
`docs/version.json`, rebuild the zip. The user pushes to git — you cannot.

---

## 7. Hard rules (violating any of these is a failed task)

1. **Never rewrite `do_shake`'s click loop.** Rapid clicks, capacity-based
   stop, W momentum. Improvements wrap it or gate tiny branches inside it.
2. **Capacity bar is ground truth; only the Pan cue is trusted for
   position.** Never make the Shake or Deposit cue load-bearing.
3. **Keep the old Detector's pixel semantics** (same `is_yellow`, same cue
   white tests, same sample boxes, same edge-inset trick). New sensing must
   agree with the old one before it may replace it.
4. **Honor `WINDOW_RELATIVE` and the calibration invariants:** calibrating
   sets `AUTO_CALIBRATE=false` and `WINDOW_RELATIVE=false`; pixels are
   absolute physical screen coords; window-offset shifting only when the
   user has it on.
5. **Any "N failures / N seconds before X" threshold treats 0 as DISABLED**
   (guard `> 0`). This bug once broke every build.
6. **No default changes without explicit user approval.** List any proposed
   default change separately and ask.
7. **Config compatibility:** existing `prospecting_config.json` and
   `prospecting_builds.json` load unchanged. New keys get safe defaults.
8. **Secrets never leave the config:** webhook URLs/secrets, API keys,
   analytics endpoints must not be copied into code, new files, logs, or
   commit messages.
9. **One change per iteration in Phase 2**, live-tested by the user before
   the next. If the user reports a regression, revert first, diagnose
   second.
10. **Verify before shipping anything:** `py_compile` every changed Python
    file; render the app HTML and `node --check` the extracted JS (the JS
    lives inside Python strings — py_compile can't catch JS errors); keep
    mac/Windows sources in lockstep; add new modules to
    `windows/prospecting.spec`.
11. **Simulation (if you build one) is a regression tool, not proof.** Only
    the user's live runs validate behavior. Never claim "it works" from
    tests alone.
12. **Pre-create any extra windows hidden at startup** (pywebview runtime
    window creation is flaky). Never expose sandbox paths to the user.

---

## 8. How to work with the user

- Start by presenting: (a) the parity checklist from Phase 0, (b) your
  Phase 1 plan, (c) any questions. Keep questions few and concrete.
- After every phase/iteration, tell the user exactly what to test in a live
  run (2–3 specific things) and what telemetry to expect.
- When something fails on their machine, ask for the live trace/History
  reasons rather than guessing.
- Be honest about uncertainty: detection and timing behavior can only be
  confirmed on their machine.

**Definition of done:** the user confirms (1) full feature parity with the
old macro, (2) identical-or-better calibration experience, (3) live
telemetry showing fewer shake fails and nudges with equal-or-better
pans/hr over multi-hour runs, and (4) a Windows build in lockstep.
