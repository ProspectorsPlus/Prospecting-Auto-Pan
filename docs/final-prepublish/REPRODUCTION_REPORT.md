# Final pre-publish pass — reproduction report

Baseline: branch `fable/prospector-engine`, HEAD `8bb67db`, version `1.0.0-rc.5`,
tracked tree clean. The rc.5 DMG was built from `6c2feeb`; the only delta to
`8bb67db` is one docs file, so every code-path finding below applies equally to
the packaged rc.5 app and the source tree. Reproduction method: direct code
inspection with exact line references, two executable probes against the REAL
code (a Python probe on `prospector_engine.sensing.Sensing.save_pixels`, and a
jsdom probe driving the real `build_html()` page — the same harness style as
`wizard_ui_tests.js`), plus the existing green baselines
(`wizard_ui_tests.py`, `onboarding_trust_tests.py`, `tour_check.py` all PASS at
`8bb67db`). No live-Roblox session was used for this report.

---

## Issue 1 — Explicit Welcome bypasses the wizard when setup is complete

**Steps (probe, real DOM):** boot with `SHOW_WELCOME_EVERY_LAUNCH` off and
onboarding `FINISHED` → app boots straight to the main UI → open the Tutorial
menu item "Welcome, privacy & version" (`#tmwelcome`) → the Welcome gate shows,
`#welGo` is labeled **"Open Prospector Lite"** → click it.

**Observed:** the setup wizard (`#setup`) never opens; the gate closes and the
user is back in the main app. Probe output: `REPRODUCED: the setup wizard
never opens -- Welcome routed straight to the app`.

**Expected:** explicitly choosing Welcome always opens the Welcome/setup
wizard with progress review, regardless of completion state.

**Root cause:** the `#welGo` handler (`prospecting_app.py:11033-11038`) routes
`if(_setupNeeded && window.SETUP){SETUP.open('trust');} else {_startApp();}` —
when setup is finished the wizard branch is unreachable. `openWelcome()`
(`:11002-11005`) only overlays the gate. There is no explicit-vs-automatic
distinction anywhere: the same `#welGo` serves both boot routing and the
user's deliberate menu click. (It never auto-starts the *macro* — `launch()`
is only wired to the Run tab's Start — but it does bypass the wizard.)

**Affected files:** `prospecting_app.py` (JS: `openWelcome`, `#welGo`,
`welActions`, `boot`), `windows/prospecting_app.py` (byte mirror).

**Platform impact:** identical on macOS and Windows (shared embedded JS).

**Packaged vs source:** identical (same embedded page in the rc.5 bundle).

## Issue 2 — No Skip Wizard affordance; "Enter the app anyway" conflates skip with completion

**Steps:** inspect the wizard footer and gate for any skip control (probe
scans `#gate` and `#setup` text).

**Observed:** no "Skip Wizard" button exists anywhere (probe: `REPRODUCED: no
Skip Wizard button exists in gate or wizard`). The nearest affordances are:
Continue buttons that are never blocked (`:11368`, `:11718`), the ready page's
"Enter the app anyway →" (`:11767`) — which **marks onboarding `FINISHED`**
(`:11826-11832`) even though readiness failed — and `SETUP.suspend()`
(`:11816`) used only by the notifications deep-link.

**Expected:** a visible Skip Wizard button opening a confirmation popup with
four honest options (skip this time / mark complete / skip automatically /
cancel), each with distinct persistence.

**Root cause:** no skip system was ever designed; wizard completion
(`onboarding_state.json` `state=FINISHED`) is the only exit, so "skipping"
permanently masquerades as completion. There is no session-only skip and no
auto-skip preference (`grep SKIP_WIZARD` finds nothing).

**Affected files:** `prospecting_app.py`, `lite_onboarding.py`,
`windows/prospecting_app.py`.

**Platform impact / packaged vs source:** identical everywhere (shared code).

## Issue 3 — Tutorial never reopens after being viewed/dismissed once

**Steps (probe, real DOM):** `tutorial_state.json` has `main='DISMISSED'`
(user skipped the tour once); boot into the main app; `maybeStartTour` fires.

**Observed:** the tutorial does not open (probe: `REPRODUCED: tutorial does
NOT open on main-app entry (main=DISMISSED suppresses forever)`); no state
transition occurs.

**Expected (new requirement):** the tutorial opens on every main-app entry by
default, even if previously viewed/completed/dismissed, closable via X, with a
clear preference to disable auto-open.

**Root cause:** `maybeStartTour` (`prospecting_app.py:9358-9376`) requires
`st.main === 'NOT_STARTED'`; any other lifecycle value — COMPLETED, DISMISSED,
even a crash-orphaned ACTIVE — suppresses auto-start permanently (design
comment `:559-566` says once-only was intentional; the product decision has
changed). There is no `tutorial_auto_open` preference, no per-session state
(`:9195-9196` in-memory guards only), and no X control (exits are "Skip tour"
`#tourskip`, Esc, or Finish — `:9283-9298`).

**Affected files:** `prospecting_app.py` (Python lifecycle `:559-590`,
`Api.tutorial_state/mark :2620-2658`; JS tour engine `:9188-9381`),
`windows/prospecting_app.py`.

**Platform impact / packaged vs source:** identical everywhere.

## Issue 4 — Warning indicators name broad categories and deep-link nowhere

**Steps (probe, real DOM):** set the red calibration badge exactly as the app
does (`setNavBadge('cal','red', 'Hard stops happened: the Capacity bar RIGHT
end pixel is probably mis-set. Re-run Calibrate.')`), then click it.

**Observed:** the click only switches to the Calibrate tab (the badge sits
inside the tab `<button>` and has no handler of its own — `:10047-10054`); no
diagnostic drawer, detail surface, or `[data-diagnostic]` element exists in
the DOM; banners (`#calbanner`, `#calbanner2`, `#cycbanner`) are not clickable.

**Expected:** clicking a red/yellow indicator opens a diagnostic detail
surface that explains what was observed, the most likely cause with evidence
and confidence, and deep-links to the exact setting/calibration/permission.

**Root cause:** the indicator system is two hardcoded badges — red `cal`
(window-size drift via `Sensing.health` ±4 px, `sensing.py:824-851`, or
`hard_stops>0`, `:9917-9921`) and yellow `cycle` (per-cycle thresholds on
`nudges/shake_misses/recoveries`, `:9917-9932`) — with tooltip-only text.
There is no diagnostic event model, no recommendation model, no setting
metadata registry (bounds exist only in `prospecting_assistant.RANGES` for
Cycle sliders), no per-setting deep-link API beyond the internal
`cygJump`/`_pvGoto` primitives, no FAQ, no dedupe/suppression store. The
"Fix now" flow exists only on the wizard Readiness page (`:11752-11757`).

**Affected files:** `prospecting_app.py`, `prospecting_ui.py` (registry
metadata), `prospecting_assistant.py` (RANGES), `prospector_engine/protocol.py`
(closed event vocabularies to build evidence from),
`windows/prospecting_app.py`, `windows/prospecting_ui.py`.

**Platform impact / packaged vs source:** identical everywhere.

## Issue 5 — Pan capacity RIGHT-end calibration looks successful but fails afterward (release blocker)

**Steps (probe, real code):** with a previously valid calibration
(`CAP_FULL_PIXEL=[1122,894]`, `CAP_LEFT_PIXEL=[678,895]`, `CAP_BAR_WIDTH=444`),
re-run right-end calibration and confirm a bad point `[400,894]` (left of the
left tip). Probe: `python3 repro_capacity.py` against the real
`Sensing.save_pixels`.

**Observed:** `save_pixels` returned `{"saved": ["CAP_FULL_PIXEL"]}` — no
error channel at all; the inverted endpoint was stored; `CAP_BAR_WIDTH`
silently kept the stale `444`. The runtime `cap_fill` band
(`engine.py:1400-1410`: columns `[right.x−WIDTH, right.x−1]`) would read
`[−44, 399]` — entirely outside the real bar.

**Expected:** the right endpoint is validated (ordering, width, row
alignment, in-window bounds, colour plausibility) before anything is saved;
an invalid capture is rejected with the exact reason and the previous valid
value is retained; a Test action verifies the live reading.

**Root causes (all confirmed in code):**

1. **Manual clicks are unguarded at the anti-aliased edge.** The auto-detector
   deliberately walks both tips ~6 px inward to *solid* gold because "the
   literal edge pixel is a pale anti-aliased blend" that fails `is_yellow`
   (`sensing.py:296-312`). A manual overlay click gets no walk-in and no
   colour check (`overlay_confirm` capacity branch,
   `prospecting_app.py:6141-6148` → `save_pixels`), so a click on the literal
   tip saves fine and `capacity_full()` (`is_yellow` over a 6×6 box biased
   left/top of the point, `engine.py:1296-1319`) never fires afterwards —
   producing hard stops. The shipped badge text at `:9920` ("RIGHT end pixel
   is probably mis-set") is the after-the-fact symptom of exactly this.
2. **No save-time validation.** `save_pixels` (`sensing.py:891-1004`) stores
   endpoints unconditionally as ints; nothing checks right>left, row
   alignment, window bounds, or colour. Reproduced above.
3. **Silent stale width.** `CAP_BAR_WIDTH` is only re-derived when
   `right.x − left.x > 20` (`sensing.py:920-927`); otherwise the *old* width
   silently persists against *new* endpoints, and the return value has no
   error channel. Reproduced above.
4. **The paired left tip is discarded in the staged flow.** The guided/WIZ
   `CAP_RIGHT` confirm saves only `CAP_FULL_PIXEL`; the auto-detected left tip
   from the same frame is kept only for the legacy `CAP` key
   (`prospecting_app.py:6141-6148`). `CAP_LEFT` is a separate later capture on
   a NEW frame (rc.5 staged flow never auto-chains), so bar drain or a window
   move between stages produces a mismatched pair — and each confirm
   re-derives ALL `PIXEL_RATIOS` + `CALIB_WINDOW_RECT` from the live window at
   that instant (`sensing.py:934-952`).
5. **Runtime asymmetries amplify small errors.** Runtime never reads
   `CAP_LEFT_PIXEL` (not an engine global — `engine.py:133-136`); `cap_fill`
   uses only the right tip's Y for its 20-px band and excludes the right-tip
   column; width rounding differs between interactive save (`int()`
   truncation) and auto placement (`int(round())`).

Not the cause (ruled out): Retina/DPI transform mismatch. With the pinned
mss 10.2.0, macOS grabs at nominal (1×) resolution so `get_scale ≡ 1.0` and
calibration frames, saved coords, runtime grabs and window rects share one
coordinate space (`platform_mac.py:73-140`; verified against the installed
mss `darwin.py`); on Windows `SetProcessDpiAwareness(2)` makes everything
physical (`platform_win.py:28-76`). The bundled mss version must stay pinned
(an older mss would grab 2× while reporting points — a systemic hazard worth
a guard, but not today's defect).

**Affected files:** `prospector_engine/sensing.py` (validation + save),
`prospecting_app.py` (overlay confirm, Test action, migration surface),
`lite_onboarding.py` (`calibration_status` needs-review for suspicious pairs),
`prospector_engine/engine.py` (no change expected to runtime math),
`windows/prospecting_app.py`.

**Platform impact:** identical logic on both platforms; both affected.

**Packaged vs source:** identical (engine ships inside both bundles).

## Issue 6 — Fortune River Recovery appears in the onboarding wizard

**Steps:** inspect the wizard calibration registry and the guided flow.

**Observed:** `fortune_river` is a registry item (`lite_onboarding.py:368-385`)
rendered in the wizard checklist as an OPTIONAL step with wizard instruction
pages (`:800-830`), a saved-state summary ("N of 5 points saved",
`:1095-1099`), and — despite its registry `action:'tab'` — a full 5-stage
staged capture plan inside the wizard (`GD_STEPS.fortune_river`,
`prospecting_app.py:11409-11414`; the `anykey:true` flag there is dead code).
It does NOT block readiness (optional items never block —
`calibration_ready`, `lite_onboarding.py:1161-1173`) and has no dedicated
readiness row.

**Expected:** absent from the wizard sequence, progress counts, and setup
copy entirely; available only as an optional/advanced feature on the normal
Calibrate tab (which already has a dedicated section, `:7409-7451`); user
data preserved.

**Root cause:** the registry has no wizard-presence dimension — every item in
`CALIBRATION_ITEMS` is composed into the wizard payload
(`compose_registry`, `lite_onboarding.py:1109-1158`).

**Affected files:** `lite_onboarding.py`, `prospecting_app.py` (GD_STEPS,
wizard copy), `windows/prospecting_app.py`. Calibrate-tab FR section stays.

**Platform impact / packaged vs source:** identical everywhere.

## Issue 7 — Windows runtime verification incomplete + one real mirror divergence

**Steps:** diff the mirrors; inspect the sync tool and CI; check for an
acceptance script.

**Observed:**

- `windows/prospecting_app.py` is in perfect lockstep with root (only the two
  intended platform hunks at `13253-13257`).
- **`windows/prospecting_ui.py` has silently diverged**: root got the atomic
  config-write fix in `38d00d1` (`prospecting_ui.py:2427-2430`,
  tmp + `os.replace`) but the Windows copy still writes `CONFIG_FILE`
  directly. `packaging/sync_windows_app.py` only regenerates
  `prospecting_app.py`, and `tour_check.py` lockstep-checks only the
  studio-schema region of the ui pair — nothing catches this class of drift.
- `packaging/windows_acceptance.ps1` does not exist; the only Windows checks
  are two never-executed steps inside `build-windows.yml`.
- The Windows runtime has never been executed anywhere (honest status in
  `PUBLIC_RELEASE_READINESS.md:23`, `PUBLIC_RELEASE_STATUS.md:75-76`); all
  Windows verification is static (py_compile, tour_check lockstep, YAML).
- Cosmetic rot: `windows/Install.bat` still launches the long-gone
  `Prospectors Plus.bat` (line 64) with pre-rename branding — source-checkout
  path only, not bundled.

**Expected:** all new behavior lands in shared code or the synced mirror; the
ui divergence is healed and guarded; a `windows_acceptance.ps1` exists; the
runtime status stays honestly "pending real Windows".

**Affected files:** `windows/prospecting_ui.py`, `packaging/sync_windows_app.py`
(or a new drift guard), `packaging/windows_acceptance.ps1` (new),
`windows/installer.iss` (version bump), `windows/Install.bat`.

**Platform impact:** Windows only. **Packaged vs source:** the frozen Windows
package would ship the non-atomic write; macOS unaffected.

---

## Reproduction probes (runnable)

- `repro_capacity.py` — Python probe on the real `Sensing.save_pixels`
  (inverted pair saved, stale width kept, no error channel). Output captured
  above; kept out of the tracked tree (session artifact).
- `repro_ui.js` — jsdom probe over the real `build_html()` page (explicit
  Welcome bypass, missing skip button, tutorial suppression, badge-click
  behavior). All checks reported `REPRODUCED`.

Baseline suites at `8bb67db`: `wizard_ui_tests.py` ALL PASS,
`onboarding_trust_tests.py` ALL PASS, `tour_check.py` ALL CHECKS PASS.
