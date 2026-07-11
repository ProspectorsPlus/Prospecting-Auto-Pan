# PROMPT — Investigate and fix the Prospectors Plus macro's cycle smoothness (macOS only)

You are an expert real‑time systems + macOS automation engineer. A mature,
working desktop macro has developed a **performance/consistency regression**,
and you are being asked to **investigate it from first principles, form your own
hypothesis, prove it, and fix it.** Do not take anyone else's theory of the
cause as given — including any you may find in notes or other prompt files in
this repo. Reach your own conclusion from evidence.

**Scope: macOS only this round.** Ignore the `windows/` copies; the user will
port later. Work in the repo folder you have access to
(`/…/Roblox Macro/Claude`).

---

## 1. The system (context so you don't break it)

**Prospectors Plus** automates the Roblox game *Prospecting*. It is screen‑
reading + synthetic OS input (not an injector). Two processes:

- **Engine** — `prospecting_old.py` (legacy name; the current engine). A
  tick‑based supervisor senses the screen (`mss` + `numpy`) and sends synthetic
  input via macOS **Quartz `CGEvent`**. The core loop: dig to fill the pan →
  walk back into water → **shake** (a rapid synthetic‑click stream) to empty it
  → walk onto land → dig again. It also spawns background daemon threads and
  prints protocol lines on stdout. Launched as a subprocess by the app.
- **App** — `prospecting_app.py` (pywebview): the window/UI, settings,
  calibration, Coach, the Analytics window, and a thread that reads the engine's
  stdout.

Other files: `prospecting_ui.py` (settings schema), `prospecting_assistant.py`
(Coach), `prospecting_config.json` (active settings + calibrated pixels),
`prospecting_prices.json` (analytics value table), `run_history.json` (per‑run
stats + telemetry), and `Prospectors_Plus_Knowledge_Base.md` — the full project
brain dump. **Read the knowledge base first**, especially its "Hard rules /
invariants" and "War stories" sections; they encode expensive lessons.

**Invariants you must not violate (from the knowledge base):**
1. **Never rewrite `do_shake`'s click loop.** The shake is a rapid *click
   stream* because on macOS a synthetic *held* mouse press does nothing in
   Roblox — only rapid clicks register. Tune around it; don't restructure it.
2. Capacity bar is ground truth; only the "Pan" cue is trusted for "in water".
3. Any "N‑before‑action" threshold treats 0 as disabled.
4. Prefer additive, reversible changes; don't refactor the supervisor or the
   movement verbs to chase this.
5. Verify before shipping: `py_compile` every changed file; render the app HTML
   (`build_html()`, `ANALYTICS_HTML`, `COACH_HTML`, `PILL_HTML`) and
   `node --check` the extracted `<script>` blocks (the JS lives inside Python
   strings, so `py_compile` alone won't catch JS errors).
6. You cannot `git push` — give the user the commands. Never expose sandbox
   paths.

---

## 2. The symptom (all that is actually established)

On the user's machine — an **M3 Pro MacBook Pro** — the pan cycle used to run
**smooth and consistent**. It is now **jittery / laggy and inconsistent**: the
shake stutters, movement hitches, cycle times vary, and the game sometimes
appears to lag. The user reports it was **noticeably smoother earlier in the
project, before a large block of recent work** (on‑screen money OCR and an
"exotic / Dinosaur‑Skull finds" detector were added during that period). That
timing correlation is a lead, not a proven cause — the same period also changed
other things. Treat the symptom as the ground truth and the timeline as a hint.

**The goal:** restore **buttery‑smooth, consistent cycles**, while keeping the
analytics features (money + finds detection) functional. Assume this is a
timing/performance problem to be diagnosed, not a request to rip features out.

---

## 3. Your mandate: investigate, prove, then fix

Do the thinking yourself. A rigorous path (adapt as you learn):

1. **Reconstruct what changed.** Use `git log`/`git diff` on this repo to see
   the history. NOTE: much of the recent work may be **uncommitted in the
   working tree** — compare the working tree against the last relevant commit,
   and read the commit messages for the arc. Identify every change since the
   "smoother" era that could plausibly affect cycle timing (engine loop, input
   timing, added threads, config defaults, logging, the app's read side).
2. **Form hypotheses and rank them.** Consider the full space: CPU/scheduling
   contention from added background work; how synthetic input timing is
   produced and whether anything can delay a press/release; blocking or
   back‑pressure on the engine's output; changes to sleep/wait behavior; config
   value changes; per‑tick overhead; the app process load feeding back to the
   engine. Don't anchor on the first idea.
3. **Prove the cause with evidence — do not guess.** You can't see the game, but
   you can instrument and measure: add temporary timing/telemetry to the engine
   (e.g. measure whether specific timed operations overrun their intended
   duration, and when), run controlled A/Bs (e.g. toggle suspect subsystems and
   have the user report the felt difference and the run telemetry), and
   correlate. Ground your conclusion in a measurement or a clean A/B, not a
   plausible story. Report what you measured.
4. **Fix minimally and reversibly.** Once the cause is proven, implement the
   smallest change that restores smoothness without regressing the analytics
   features or touching the proven dig/shake/movement logic. If the right fix is
   structural, propose it with the evidence before committing to it.
5. **Prove the fix.** Show the regression is gone by the same measurement that
   demonstrated it, and give the user an exact live test to confirm the feel.

You own the diagnosis and the design. The user does not want a pre‑baked answer
applied blindly; they want the real cause found and fixed.

---

## 4. What "fixed" means (acceptance)

The user validates live on the M3 Pro; tell them precisely what to check:
- With the analytics features **ON**, the shake no longer stutters and cycles
  are consistent — the pre‑regression feel is back. Corroborate with the run
  telemetry: shake‑fail / no‑progress / nudge counts fall back toward their
  earlier levels and per‑phase timing (shake/dig/water) tightens (p95 → mean).
- Toggling analytics off vs on no longer changes the cycle feel.
- No regression to dig/shake/movement, relic timers, pause/resume, Ctrl+C exit,
  or the Analytics window.
- Before/after evidence from your own instrumentation backs the claim.

---

## 5. Operating guidance (you are Claude Fable — read this)

- **Investigate before you act, but once the evidence is in, act.** Don't loop
  on analysis after you've proven the cause; implement and verify.
- **Ground every claim in a tool result or a user‑confirmed A/B.** If you
  haven't measured it, say so. Never report a smoothness win you can't
  demonstrate. Report failures and dead‑ends plainly — a disproven hypothesis is
  progress, state it and move on.
- **Keep it simple; don't over‑engineer.** No refactors of the supervisor,
  movement, or the shake; no speculative abstractions or new config beyond what
  the fix needs. Match the surrounding code style. Do the simplest thing that
  restores smoothness.
- **Stay in scope.** macOS only; leave `windows/` alone. Don't change the finds‑
  detector's *accuracy* logic except where it directly bears on the performance
  cause. Keep the analytics features working.
- **Pause only when genuinely blocked** — e.g. you need the user to run an A/B
  (feature on vs off) on the real game and report the feel + telemetry, or to
  capture a short clip. Ask for exactly that, then end the turn. Don't end on a
  plan or a promise ("I'll…") when you can do the work now.
- You do **not** need to narrate or reproduce your internal reasoning in your
  responses — give the user a plain, outcome‑first summary. Run at high effort;
  this is a real‑time timing problem where the details matter.

---

## 6. First moves

1. Read `Prospectors_Plus_Knowledge_Base.md` (hard rules + war stories).
2. `git log` / `git diff` (working tree vs last commit) to map what changed
   across the "smoother → jittery" transition; read the code paths for input
   timing, the tick loop, any background threads, logging/output, and config.
3. Instrument to measure the regression, form and test your hypothesis, prove
   the cause, then implement and prove the minimal fix. Tell the user the exact
   live A/B to confirm buttery‑smooth cycles.
