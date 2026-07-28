# Wizard stabilization report — 1.0.0-rc.3

Scope: the release-blocker fix pass over the rc.2 first-run wizard. Companion documents:
`REPRODUCTION_REPORT.md` (what was broken, with evidence),
`CALIBRATION_CRASH_REPORT.md` (the calibration trace incl. refuted hypotheses),
`../trust-and-onboarding/ACCEPTANCE_MATRIX.md` (what was executed against the built package).

Baseline `f1e943c` (rc.2) → stabilized at `44e4458` (rc.3 build commit). All line references
below are post-fix unless marked (baseline).

## What changed, by blocker

**1–3. Permission status stale / does not refresh.**
- `refresh()` now patches card pills, detail text and action buttons in place from a fresh
  `trust_state` snapshot (`updateCards`), preserving test output and skipping cards with an
  armed test; page entries do the full render. Baseline `refresh(false)` fetched and
  discarded (baseline prospecting_app.py:10163).
- Refresh triggers now: page entry; entry-render trailing re-fetch; after every
  request/settings/test action; a manual **Refresh status** button; a bounded 90 s / 2.5 s
  poll armed by request/settings/check-again (stops when the page hides); a
  `document.hasFocus()` transition watcher (pywebview 6.2.1 has no app-activation event —
  verified against `webview.window.Window.events`); the JS window-focus event as fast path.
  Out-of-order snapshots are dropped via a client sequence counter plus the server `seq`.
- The state model separates OS preflight, requested-once, session test result, and
  restart-required: `trust_state` composes `requested` (persisted in the onboarding state),
  `test` (session-scoped real outcomes), `requires_restart` (launch-preflight snapshot vs
  live preflight vs test evidence), `seq`, `checked_at`.
- Honest labels: "Not requested yet" (never asked → no System Settings row exists yet),
  "Granted — restart to apply" (+ one-click **Restart Prospector Lite** via `trust_relaunch`),
  "Works (tested this session)" on Windows, "Test failed", and the stale-System-Settings-row
  guidance for the ad-hoc-signing identity trap (`[PP-TRUST-STALE]`). Opening System Settings
  never marks anything granted.

**4. Input test.** The key-post worker's real result (posted / refused-not-frontmost /
blocked / exception, with error codes) is delivered to the page via `__keyTestResult` and
correlated by request id — a refusal is reported as itself instead of "grant Accessibility".
The observation matches the physical key (`e.code==='KeyT'`) so non-QWERTY layouts pass.
Runs are debounced; listeners are always removed; failure text is platform-branched.

**5. Safe Stop test.** Single-flight (a second arm returns `PP-HOTKEY-BUSY` instead of
spawning a parallel listener whose stale result overwrote the newer one), request ids drop
stale callbacks, a visible countdown runs, and a listener that fails to start (Input
Monitoring blocked) is reported immediately and distinctly from an 8 s timeout
(`LISTENER_START` / `LISTENER_DIED` in lite_trust.await_stop_hotkey).

**6. Calibration.** See `CALIBRATION_CRASH_REPORT.md` for the ten confirmed defects and the
three refuted hypotheses. Highlights: picker buttons surface in-band errors with codes and a
route to the Trust step; Screen-Recording-denied refuses to open the (previously black)
overlay; `_overlay is None` is an error, not a fake success; `bool(detect_window())`
truthiness fixed in both consumers; optional-only saves no longer disable auto-calibration
(engine contract for core saves preserved and pinned by tests); the overlay re-fits to the
current display on open; `launch()` now enforces the calibration gate (`cal:` refusal).

**7. "Show this screen every time" checkbox.** One positive key
(`SHOW_WELCOME_EVERY_LAUNCH`), default ON, one-time migration from the inverse
`WELCOME_SEEN` (which is then removed), rendered from stored state at boot and on menu
reopen (the forced-uncheck is gone), persisted immediately on toggle via
`welcome_set_always_show` with atomic writes, and a failed save is displayed and reverts the
box instead of silently lying. Continue no longer rewrites the preference. Verified across
two real packaged launches (acceptance probe 7).

**8. Professional-flow hardening.** boot() renders an error panel with a code instead of
silently skipping the wizard when the bridge fails; render-generation tokens stop a stale
async page render from overwriting the current page; every wizard operation lands in
`onboarding.log` (operation, status, error code — no secrets, no keystrokes, no images);
JS errors are forwarded to that log; `PP_DEBUG=1` opens the webview inspector; Readiness
gains **Copy diagnostic summary** and **Open wizard log**; diagnostics export includes the
log tail and session test results.

## Cross-cutting fixes

- **Atomicity**: 13 truncate-write `CONFIG_FILE` sites (settings save, relics, HUD position,
  region previews, coach settings/stats, webhook fallback, hotkeys, advanced-cue toggles,
  build load) now route through one atomic tmp+fsync+replace writer; a static test bans the
  pattern. The engine's `.bak` is now a copy, so a live config file exists at all times.
- **Ordering**: `welcome_done` touches the onboarding machine before any config write, and
  `migrate_legacy` is guarded by the at-construction file check — a fresh install can no
  longer be marked FINISHED by the legacy bridge (the reproduced self-trip).
- **Data paths**: the secrets file honours `PP_DATA_DIR`; `--capabilities` runs self-isolate
  into a temp dir (a pure query no longer creates/migrates the real data dir); source runs
  from the `windows/` mirror write to `windows/.devdata/` instead of over the tracked
  sanitized default config.
- **Windows honesty** (static + mirrored only — no Windows runtime ran): SendInput scancode
  path, `GetAncestor(GA_ROOT)` frontmost resolution, ctypes input-release backstop (was a
  guaranteed no-op — pynput is not shipped on Windows), session-test row statuses, readiness
  reflects your own passing/failing test, elevation-check failure reads as unknown.

## Known limitations / owner actions (unchanged or out of scope by rule)

- **Ad-hoc signing**: every rebuild is a new TCC identity, so grants do not survive rebuilds
  and a System Settings row from an older build reads as enabled while the new binary is
  not. The UI now explains and works around this honestly; the real fix is a Developer ID
  certificate + notarization (owner action, tracked in RELEASING.md).
- Calibration example screenshots still ship as labelled placeholders (owner captures owed).
- Windows runtime verification requires a Windows machine/CI run.
- The guided calibration modal starts at its intro step regardless of which card launched it.

## Verification inventory

- Suites (all ALL-PASS at the build commit): onboarding_trust_tests (extended),
  public_release_tests, tour_check, engine contract/parity/characterization/flow/plan/
  trace/parallel/pacing, studio_tests, prospecting_selftest.
- Packaged: acceptance probes 1–8 against the rc.3 DMG from a read-only mount with isolated
  homes (including the first-boot bridge-liveness criterion that rc.2 could not pass, and
  the two-launch welcome-preference lifecycle).
- Independent fresh-context verification: see the verifier section of the final session
  report; P0/P1 findings were required to be fixed before release readiness was claimed.
