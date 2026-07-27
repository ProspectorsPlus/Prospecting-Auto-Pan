# Packaged acceptance matrix — 1.0.0-rc.2

What was actually executed against the built packages, what passed, and
what remains a human step. Automated probes live in
`packaging/packaged_acceptance.command`; run them against any DMG.

Build under test: `ProspectorLite-1.0.0-rc.2-macos-arm64.dmg`, built from
commit `0b2a536` (clean tree, `dirty: false`), ad-hoc signed (unsigned
release), macOS arm64, 2026-07-27.

## Automated probes (executed this release — all passed)

| # | Probe | Result | Evidence |
|---|-------|--------|----------|
| 1 | DMG mounts read-only; app runs from the mounted image | PASS | `hdiutil attach -readonly`; probes 2–4 all ran from the mount |
| 2 | Self-contained offline probe (`--capabilities`) from the mounted app, no system Python | PASS | 3,846-byte manifest answered |
| 3 | Clean first boot with an isolated `PP_DATA_DIR`: app stays alive, JS↔Python bridge comes up (boot() → `welcome_state` → `onboarding_state.json` created in the isolated home), nothing written into the read-only bundle | PASS | state file observed in the temp home |
| 4 | Zero open network sockets during first boot | PASS | `lsof -a -p <pid> -i` = 0 rows |
| 5 | Bundle identity: `build_info.json` carries commit/date/version/dirty/package | PASS | `v1.0.0-rc.2 @ 0b2a5367f909 dirty=False` |
| 6 | Bundle contents: `trust_manifest.json`, `PERMISSIONS.md`, `PRIVACY.md`, `SECURITY.md`, `TRUST_CENTER.md` present; no personal files (`prospecting_secrets.json`, `coach_history.json`, `run_history.json`, access-code files) | PASS | find over the mounted bundle |

Additionally executed on the source tree at the same commit: the full
release gate (`public_release_tests.py` — ALL PASS, including the new
TLS-bypass and licence-wording scans), the onboarding/trust suite
(`onboarding_trust_tests.py` — ALL PASS, network-denied children
included), `tour_check.py` (JS syntax on every embedded script, tab/tour
resolution, mac↔windows lockstep — ALL PASS), and the complete engine
battery (contract, flow, parity, plan, pacing, parallel, trace,
characterization, lite-drive, studio suites, finds sim, selftest,
conformance — ALL PASS).

## Human journeys (require eyes/hands; not claimable by automation)

These follow the numbered journey list in the task specification. Status
HUMAN-PENDING means the code paths are implemented and unit/probe-covered,
but nobody has clicked through them in this pass:

| Journey | Status |
|---------|--------|
| Welcome → Trust screen navigation, Back/Next, step rail | HUMAN-PENDING |
| macOS tab / Windows tab switching, keyboard access | HUMAN-PENDING |
| Screen-detection explain → Open Settings deep link → grant → Test (preview shown once) | HUMAN-PENDING |
| Accessibility explain → grant → sandbox key test (down+up) + pointer wiggle | HUMAN-PENDING |
| Input Monitoring → Safe Stop armed test (Esc/Ctrl+K heard) | HUMAN-PENDING |
| Permission-denied flow + restart-resume of the wizard | HUMAN-PENDING |
| Guided calibration: item cards → overlay picker → confirm → status flips | HUMAN-PENDING |
| Readiness Check render, Fix Now, Retest, Finish setup → main app | HUMAN-PENDING |
| Trust Center: every section, View Code local fallback, data actions, double-confirm delete-all | HUMAN-PENDING |
| Real-game short cycle + pause/resume + Safe Stop (live Roblox) | HUMAN-PENDING (owner) |

## Windows

NOT EXECUTED. The Windows package workflow (`build-windows.yml`,
`windows/build.bat`, per-user installer, portable ZIP, content scan,
`--capabilities` smoke, SBOM, checksums, optional Authenticode) is
prepared and YAML-validated locally, but no Windows runtime has run in
this pass. Do not advertise Windows as verified until the workflow has
executed on a real Windows runner and an equivalent journey pass exists.
