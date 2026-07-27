# Trust & Onboarding Test Matrix

Where every trust/onboarding-relevant behaviour is pinned. Suites are run together by CI
(`.github/workflows/ci.yml`) on macos-14 and windows-latest, Python 3.12. **Honesty note
that applies to every Windows row below: the Windows runtime has not been executed in this
pass. The workflows, scripts and mirrors are prepared, and the suites are written to run
there, but until a Windows runner (or machine) actually executes them their status is
"prepared, not executed" — never "passing".**

## 1. Release gate — `public_release_tests.py` (493 lines)

Static scans over every tracked file plus four network-denied child processes
(socket module disabled, `PP_DATA_DIR`/`PPENGINE_HOME` pointed at throwaway temp dirs).

| Area | Where covered |
|---|---|
| Branding: prohibited legacy names absent (allow-listed context only) | `scan_branding` (:106) |
| Tracking: IP/geo/update/analytics endpoints + fingerprint code banned | `scan_tracking` (:150) — the ban list (:116-121) is also the evidence behind the `location` NOT_REQUIRED entry |
| Roblox safety boundary: no process-injection / memory APIs | `scan_injection` (:166) |
| Access gate removed; welcome API + markup present; legacy keys scrubbed; secrets file in DATA_DIR when frozen | `scan_gate` (:196-222) |
| No credential-shaped strings (webhook URLs with real ids, API keys, private keys) | `scan_secrets` (:236) |
| Subprocess/exec hygiene (no shell=True, no unpickling) | `scan_subprocess` (:252) |
| Shipped default config: webhook off, no URL/secret, no legacy/COACH/WELCOME keys | `scan_default_config` (:275) |
| Version identity: both app copies + installer.iss agree | `scan_version` (:289) |
| Release artifact naming + checksums file (when a candidate exists) | `scan_artifacts` (:451) |

Network-denied children (:309-445):

| Child | What it proves |
|---|---|
| `app_offline` (:335) | DATA_DIR redirection honoured; welcome flow round-trips; webhook test refused without URL; `http://` rejected at save, `https://` accepted; doc-opener traversal blocked; empty PROJECT_URL → external open is a no-op; nothing written outside the temp home; **zero network** |
| `engine_offline` (:369) | A full legacy classic run in the simulated world completes offline **and ends with every input released** (`world.inputs.all_released()`) — the runtime proof behind the input_control card's release claims |
| `engine_webhook_default` (:380) | Engine defaults: `WEBHOOK_ENABLED is False`, `WEBHOOK_URL == ""`; `post_webhook` with defaults is a pure no-op under a denied socket |
| `scrub_and_migration` (:393) | Legacy private keys stripped in memory and on disk; legacy-dir migration is copy-only, key-stripped, idempotent, non-ASCII-path safe |

## 2. Onboarding/trust suite — `onboarding_trust_tests.py`

Being written in parallel with this documentation pass (already wired into CI:
`.github/workflows/ci.yml:56-57` "Onboarding + trust suite"). Its charter, per area:

| Area | What it pins |
|---|---|
| Registry sanity | Every `lite_trust.CAPABILITIES` / `lite_onboarding.CALIBRATION_ITEMS` entry carries the required fields; NOT_REQUIRED entries have empty references; calibration conditions parse to real config flags |
| Manifest resolution | Every source reference resolves via the ast resolver; dead/ambiguous refs fail; empty `project_url` ⇒ every manifest URL empty (never a branch); configured URL ⇒ exact-commit links |
| State machine | Forward-only `mark`; rerun/reset semantics; atomic persistence + tolerant load; one-shot `WELCOME_SEEN` → FINISHED migration; `declined_optional` round-trip |
| TLS policy | Verified-context senders only; certifi fallback; `ssl.SSLError` ⇒ drop; tree-wide scan that `_create_unverified_context` stays at 0 hits |
| Payload preview parity | `Api.webhook_payload_preview` is built by the engine's `_webhook_payload`; secret redacted; limits enforced |
| Readiness probes | `readiness_check` items honest per platform; required failures ⇒ `ok: False`; result recorded into `last_readiness` |
| Data-path isolation | All wizard/trust state stays under DATA_DIR; scoped deletes touch only known files |
| Wording | No "open source" claims; no "100% safe"; three-permission macOS story consistent |

## 3. UI/protocol checks — `tour_check.py` (218 lines)

| Area | Where covered |
|---|---|
| Python compiles (both app copies + engine + UI modules) | section 1 (:33-45) |
| **JS syntax**: `node --check` on every `<script>` from all six HTML surfaces, both copies — this is what catches a broken wizard/Trust Center script block | `check_copy` (:47-78) |
| Settings schema: exact unique `data-key` count pinned | `check_copy` keys step (the suite enforces the exact number; see its output) |
| **Tour/tab resolution**: every tour step's `sel`/`tab`/`open` resolves in the rendered HTML — covers the wizard's tab-handoff targets (Calibrate tab, Notifications page) | `check_copy` targets step (:90+) |
| **Lockstep**: shared blocks byte-identical between `prospecting_app.py` and `windows/prospecting_app.py` | slice comparison (:158-189) — how the Windows mirror of the trust/wizard code is kept in sync without running Windows |

## 4. Engine suites (CI "Engine suites" step, `ci.yml:69-79`)

`engine_contract_tests.py`, `engine_parity_tests.py`, `engine_characterization.py`,
`engine_plan_tests.py`, `engine_trace_tests.py`, `engine_flow_tests.py`,
`engine_pacing_tests.py`, `engine_parallel_tests.py`, `engine_lite_drive.py` — all driven
through `engine_sim.py`'s display-free virtual world.

| Trust-relevant behaviour | Where covered |
|---|---|
| **Input release** on every stop path (safe stop, quit, host death) | `engine_offline` child (above) + the contract suite's release vocabulary check (`engine_contract_tests.py:682`) and stdin-EOF ⇒ release+bye behaviour (:279) |
| Absence of the legacy `SYNC_URL` phone-home | `engine_contract_tests.py` negative check (allow-listed in the gate, `public_release_tests.py:124-125`) |
| Webhook toggles honoured in the run loop | flow/pacing suites exercising notify paths in the simulated world |

`studio_conformance.py` **skips with exit 0** when the private Studio goldens directory is
absent (:432-438), so public clones and public CI pass; the goldens live in a private
sibling repository and their absence is a SKIP, not a failure.

Also in CI: `finds_sim.py`, `studio_tests.py`, `prospecting_selftest.py` (`ci.yml:59-67`) —
not trust-specific, listed for completeness of the gate.

## 5. Packaged acceptance matrix

What a release build must demonstrate, per platform. macOS rows were executed on this pass's
builds; **every Windows row is prepared, not executed**.

| Check | macOS | Windows |
|---|---|---|
| Build stamps `build_info.json` (8 fields) + emits `trust_manifest.json` from the exact checkout | Executed — `build_dmg.command:50-69` | Prepared, not executed — `windows/build.bat:17-19`, `build-windows.yml:37` |
| Dead code reference fails the build (`--emit` non-zero) | Executed (dev + build path) | Prepared, not executed |
| Frozen binary answers `--capabilities` offline (smoke) | Executed — `build_dmg.command:106-109`, `build-macos.yml:47-49` | Prepared, not executed (workflow step exists) |
| Package-content scan: no brand/endpoint/secret strings in shipped resources | Executed — `build-macos.yml:34` | Prepared, not executed — `build-windows.yml` scan step |
| Signing: ad-hoc by default; `CODESIGN_ID` ⇒ hardened runtime + `packaging/entitlements.plist` + codesign/spctl verify; unsigned builds labelled in-app (`signed: false`) | Executed (ad-hoc path); real-identity + notarization are manual owner steps (`RELEASING.md`) | Prepared, not executed — Authenticode steps gated on repo secrets (`build-windows.yml:71-83`), no cert in repo |
| Installer is per-user, no admin (`PrivilegesRequired=lowest`); uninstall keeps user data | n/a (drag-install DMG) | Prepared, not executed — `windows/installer.iss` |
| First-run wizard end-to-end on a packaged build (permissions prompt only from Request buttons; resume after force-quit; Trust Center delete-all returns to fresh first run) | Executed manually on the rc build | **Not executed** — owner/tester action before any Windows release |
| Example calibration screenshots present + approved | **Blocked — owner action.** The manifest pipeline ships placeholders until the owner captures/approves assets (`assets/onboarding/calibration/manifest.json`); final public packaging waits on this | Same blocker |

## 6. Known gaps (kept honest)

- `onboarding_trust_tests.py` is landing in parallel; until it is merged the areas in §2 are
  design-pinned but not yet machine-pinned. CI already references it, so a missing/failing
  suite fails the pipeline rather than being silently skipped.
- Windows: everything (§5 plus every windows-latest CI row) is prepared, not executed.
- Historical git-history secrets remain un-revoked until the owner acts
  (`docs/public-release/SECRET_AUDIT.md`); no test can fix that — it is a release
  precondition (`RELEASING.md`).
