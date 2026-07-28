# Packaged acceptance matrix — 1.0.0-rc.3

What was actually executed against the built packages, what passed, and
what remains a human step. Automated probes live in
`packaging/packaged_acceptance.command`; run them against any DMG.

Build under test: `ProspectorLite-1.0.0-rc.3-macos-arm64.dmg`, built from
commit `9f9dcdc` (clean tree, `dirty: false`; the wizard-stabilization
pass, the independent re-verification fixes and the final re-verifier
P3 cleanups), ad-hoc signed (unsigned release), macOS arm64,
2026-07-28. Earlier rc.3 builds from `44e4458` and `38d00d1` passed
the same probes at their respective states.

Correction recorded from the rc.2 matrix: rc.2's probe 3 was **not
actually passable on a true first boot** — nothing wrote
`onboarding_state.json` until a user action, so the criterion the matrix
reported as PASS could only have passed against a seeded home. The
stabilization audit reproduced the failure against the shipped rc.2 DMG.
In rc.3 the onboarding state machine persists its state file eagerly on
first bridge use, which makes the probe's criterion real; the probe also
now cleans up its temp homes on FAIL paths and isolates the
`--capabilities` query from the user's data directory.

## Automated probes (executed this release — all passed)

| # | Probe | Result | Evidence |
|---|-------|--------|----------|
| 1 | DMG mounts read-only; app runs from the mounted image | PASS | `hdiutil attach -readonly`; probes 2–4 all ran from the mount |
| 2 | Self-contained offline probe (`--capabilities`) from the mounted app, isolated data dir | PASS | manifest answered from the mount |
| 3 | Clean first boot with an isolated `PP_DATA_DIR`: app stays alive, JS↔Python bridge comes up (boot() → `welcome_state` → eagerly persisted `onboarding_state.json` observed), nothing written into the read-only bundle | PASS | state file observed in the temp home within the 30 s window |
| 4 | Zero open network sockets during first boot | PASS | `lsof -a -p <pid> -i` = 0 rows |
| 5 | Bundle identity: `build_info.json` carries commit/date/version/dirty/package | PASS | `v1.0.0-rc.3 @ 9f9dcdcd66df dirty=False` |
| 6 | Bundle contents: `trust_manifest.json`, `PERMISSIONS.md`, `PRIVACY.md`, `SECURITY.md`, `TRUST_CENTER.md` present; no personal files (`prospecting_secrets.json`, `coach_history.json`, `run_history.json`, access-code files) | PASS | find over the mounted bundle |
| 7 | Packaged welcome-preference lifecycle (now SCRIPTED as the probe script's step [5]): first boot writes the positive key `SHOW_WELCOME_EVERY_LAUNCH: true` (default ON) and no legacy `WELCOME_SEEN`; a second packaged launch against the same home honours and preserves an OFF preference (`onboarding.log`: `show=False pref=False`) | PASS | two real launches of the built app against one isolated home, driven by `packaged_acceptance.command` |
| 8 | Wizard diagnostics log (`onboarding.log`) written in the isolated home, operation lines with status and no secrets (asserted by the scripted step [5]) | PASS | log inspected after both boots |

Additionally executed on the source tree at the same commit: the full
release gate (`public_release_tests.py` — ALL PASS with the rc.3 pins:
positive welcome key, PP_DATA_DIR-aware secrets path), the extended
onboarding/trust suite (`onboarding_trust_tests.py` — ALL PASS, now
including the welcome-preference lifecycle, the authoritative trust
state model with injected restart states, calibration no-crash guards,
and engine calibration-write semantics; six network-denied runtime
children), `tour_check.py` (JS syntax on every embedded script, tab/tour
resolution, mac↔windows lockstep — ALL PASS), and the engine battery
(contract, parity, characterization, flow, plan, trace, parallel,
pacing, studio suites, selftest — ALL PASS).

## TCC (real permission) coverage — honest scope

This environment cannot flip real macOS privacy permissions for the
newly built app (each ad-hoc rebuild is a new TCC identity, and
modifying TCC programmatically is out of bounds by design). What was
verified instead:

- the dev-terminal identity (granted on this machine): live preflights
  read `granted`, and the real screen-capture test returns a non-blank
  240×150 frame;
- denied / granted-but-restart-pending / stale-entry states: covered by
  dependency-injected preflight snapshots in `child_trust_model`
  (restart inference set, cleared by a passing test, re-set by a failing
  one), not by real TCC flips — stated here so nobody mistakes it for a
  clean-machine TCC journey;
- the packaged rc.3 build carries the honest UI for every one of those
  states ("Not requested yet", "Granted — restart to apply",
  check-again, Restart button, stale-row guidance).

## Human journeys (require eyes/hands; not claimable by automation)

| Journey | Status |
|---------|--------|
| Welcome → Trust screen navigation, Back/Next, step rail | HUMAN-PENDING |
| Checkbox visual check: renders ON on a fresh install, toggling persists across quit/reopen (file-level equivalent PASSED as probe 7) | HUMAN-PENDING |
| Screen-detection explain → Open Settings deep link → grant → card refreshes (poll/focus/check-again) → Test (preview shown once) | HUMAN-PENDING |
| Accessibility explain → grant → sandbox key test (down+up) + pointer wiggle | HUMAN-PENDING |
| Input Monitoring → Safe Stop armed test with countdown (Esc/Ctrl+K heard) | HUMAN-PENDING |
| Permission-denied flow, restart-required flow (Restart button), restart-resume of the wizard | HUMAN-PENDING |
| Guided calibration: item cards → overlay picker → confirm → status flips; denied-capture refusal message | HUMAN-PENDING |
| Readiness Check render, Fix Now, Retest, Copy diagnostic summary, Open wizard log, Finish setup → main app | HUMAN-PENDING |
| Trust Center: every section, View Code local fallback, data actions, double-confirm delete-all | HUMAN-PENDING |
| Real-game short cycle + pause/resume + Safe Stop (live Roblox) | HUMAN-PENDING (owner) |

## Windows

NOT EXECUTED. The Windows package workflow (`build-windows.yml`,
`windows/build.bat`, per-user installer, portable ZIP, content scan,
`--capabilities` smoke, SBOM, checksums, optional Authenticode) is
prepared and YAML-validated locally, but no Windows runtime has run in
this pass. The stabilization pass changed Windows code paths
(SendInput-based input test, GetAncestor frontmost check, ctypes input
release, session-test statuses, elevation-unknown honesty) — these are
statically verified and mirrored via `packaging/sync_windows_app.py`
only. Do not advertise Windows as verified until the workflow has
executed on a real Windows runner and an equivalent journey pass exists.
