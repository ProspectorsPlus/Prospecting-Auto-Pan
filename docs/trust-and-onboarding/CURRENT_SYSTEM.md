# Prospector Lite — Current System Map (Trust & Onboarding Baseline)

Implementation map of the systems the trust-and-onboarding work builds on: boot, first-run,
settings, calibration, OS permissions, network, and packaging. Written for reviewers of the
public repository. Facts are stated as of commit `680f141` on `fable/prospector-engine`
(1.0.0-rc.1). Where the working tree of the current hardening pass has already diverged
(SSL fallbacks, screenshot default), the divergence is called out explicitly.

All paths are repo-relative. `prospecting_app.py` is the macOS/primary host;
`windows/prospecting_app.py` is a near-identical mirror at slightly lower line numbers.

---

## 1. App architecture and boot

**Process model.** A single pywebview host process (`prospecting_app.py`, ~10,525 lines)
owns all UI; the automation engine (`prospector_engine/`) runs as a separate subprocess
speaking stdio-only IPC (`prospector_engine/client.py:81-86`, `prospector_engine/ipc.py:197-205`) —
no sockets anywhere in the IPC path. When frozen, the host re-executes its own bundle binary
with `--run-macro`/`--run-engine` (`prospecting_app.py:4226-4233`, `:10417-10419`), so both
processes share one app identity (relevant for macOS TCC, §5).

**Entry.** `main()` at `prospecting_app.py:10402`. Pre-GUI branches: `--capabilities` prints a
JSON manifest (`:10409`, also the packaging smoke test); frozen engine re-exec (`:10417`);
if pywebview is missing, falls back to the legacy loopback browser UI in `prospecting_ui.py`
(`:10421-10428`). Then `api = Api()` (`:10429`) and a config scrub (`:10430`).

**Windows created** (all hidden except main, all sharing `js_api=api`, all HTML passed through
`_themed()` at `:186-192`): main window from `build_html()` (`:10435-10438`), frameless pill
(`:10453`), HUD (`:10463`), fullscreen calibration overlay (`:10471`), Coach (`:10479`),
Analytics (`:10487`), embedded Studio editor (`:10494-10505`). `webview.start()` at `:10520`;
close funnels through `_quit_everything`.

**Embedded HTML.** The main window is one inline template constant `HTML` at
`prospecting_app.py:6279-9230`: a single `<style>` block (`:6279-7148`), body markup
(`:7149-7284`, including the welcome gate at `:7155-7187`), and a single `<script>` block
(`:7285-9230`). `build_html()` (`:5593-6180`) substitutes `{{NAV}}`/`{{PANELS}}` from the
settings schema. No window is created from a URL, and the embedded pages contain zero
`fetch`/`XMLHttpRequest`/`<script src>`/external CSS/fonts (verified by grep; map §6).

**Api bridge.** Every public method on `class Api` (`prospecting_app.py:2025`) is auto-exposed
to JS as `window.pywebview.api.<method>()` returning a Promise; methods take JSON-serializable
args and return dicts. Python→JS goes through `evaluate_js` with `window.fn && fn(...)` guards
(e.g. log batching `:5339-5353`, `refreshValues` `:2919`). `Api.app_info()` (`:2521-2540`)
surfaces name/version/platform/frozen state/`project_url`/build commit to the UI.

**Settings schema.** `prospecting_ui.py` doubles as (a) the pywebview-less fallback UI and
(b) the canonical schema module: `SECTIONS` (`prospecting_ui.py:31-231`), `DEFAULTS`/`TYPES`,
`HELP`, `PIXEL_FIELDS` (`:1708-1756`), `REGION_FIELDS` — imported by the app at
`prospecting_app.py:306-349`. Settings pages in the sidebar are generated from this schema
(`prospecting_app.py:6043-6098`); `tour_check.py` asserts 146 unique `data-key` settings.

## 2. Current first-run flow

There is **no access code, licence check, or account** — the comment at
`prospecting_app.py:2573-2576` states this explicitly, and `public_release_tests.py:196`
(scan_gate) enforces that the old access-code system stays removed. First run consists of a
splash plus a single welcome gate:

- **JS boot** (`prospecting_app.py:9046-9087`): on `pywebviewready`, `boot()` (`:9075-9078`)
  calls `Api.welcome_state()`, hides the splash, and either shows the gate or goes straight to
  `_startApp()` (idempotent; `:9073-9074`), which runs `init()` and offers the guided tour
  after 900 ms.
- **`Api.welcome_state()`** (`:2577-2582`): `show = not bool(cur.get("WELCOME_SEEN"))`;
  forced `show=False` when launched by Studio (`STUDIO_LAUNCH` env, `:172`). Returns
  `{"show", "info": app_info()}`.
- **Gate markup** (`:7155-7187`): `role="dialog"` card with version, build commit, migration
  note, a "show again" checkbox, and three links — View source (`#welSrc`, hidden unless
  `project_url` is non-empty, `:9067`), Privacy (`#welPriv`), Security (`#welSec`) routed
  through the whitelisted `open_doc` (§6). Continue (`#welGo`) calls
  **`Api.welcome_done(always_show)`** (`:2584-2596`), which writes
  `WELCOME_SEEN = not always_show` atomically (tmp + `os.replace`) into the config file.
- The gate can be re-opened any time via `window.openWelcome` (`:9065`). Escape closes it only
  after the app has initialized (`:9086-9087`). The main tour never starts while the gate is
  showing (`maybeStartTour`, `:7474`).

Known limits of this flow (the gap this release addresses, §8): it explains permissions in
one text bullet (`:7173`) but performs no permission preflight, no calibration prompt, and no
capability disclosure beyond static text.

## 3. Settings and data storage

**DATA_DIR rules** (`prospecting_app.py:128-140,149`): when frozen, a per-user app-data
directory (macOS `~/Library/Application Support`-style, Windows `%LOCALAPPDATA%`, generic
`~/.prospector-lite`); in dev mode, the script directory. Overridable via `PP_DATA_DIR`
(honored under test; `public_release_tests.py:335` proves redirection). The Windows uninstaller
deliberately keeps the user data dir (`windows/installer.iss:52-56`).

**Config file.** `CONFIG_FILE = DATA_DIR/prospecting_config.json` (`:150`), a flat JSON dict
holding settings *and* all calibration keys. Loaded by `load_saved()` (`:417-424`). Secrets
(Coach API key) live in a separate gitignored secrets file in the same dir; the gate test
asserts the frozen path is under DATA_DIR (`public_release_tests.py:221-222`). The tracked
default config `windows/prospecting_config.json` is sanitized (webhook disabled, no URLs or
secrets — enforced by `public_release_tests.py:275`) and is shipped by **both** platforms
(mac spec pulls it at `prospector_lite_mac.spec:31`).

**Legacy scrub/migration.** Five private-era keys (`ACCESS_OK`, `ACCESS_HASH`,
`ACCESS_MACHINE`, `MACHINE_SALT`, `SYNC_URL`) exist only inside the scrub tuple
`_PRIVATE_LEGACY_KEYS` (`prospecting_app.py:52-56`); they are stripped on load, on-disk at
startup (`_scrub_config_file`, `:437-443`), and during the copy-only legacy-dir migration
(tested end-to-end by `public_release_tests.py:393`).

**Atomicity is split.** All engine-side writes go through `prospector_engine/settings.py:104-120`
`atomic_write` (tmp + fsync + `os.replace` + rolling `.bak`; corrupt JSON preserved as
`.corrupt.bak`). App-side writes are mixed: `welcome_done` (`:2590-2594`) and
`_scrub_config_file` use tmp+replace, but `save_config` (`:3153-3162`),
`set_advanced_cues` (`:3315-3322`), `save_hotkeys` (`:4466-4468`), and
`_region_preview_save` (`:4600-4607`) use bare `open()+json.dump`. When the IPC engine is
alive it owns scalar writes (single-writer rule, `:3005-3015`). The in-process `FileStore`
never stamps `CONFIG_SCHEMA` (a v0 file stays v0; `prospector_engine/sensing.py:79-99`).

## 4. Calibration system

All calibration capture and math lives in `prospector_engine/sensing.py` (class `Sensing`,
`:110`); the app is a thin wrapper binding it in-process over the config file
(`prospecting_app.py:283-292`). Capture uses `mss`; thresholds come from the bound engine
module so calibration tests and the runtime detector agree by construction.

**Key inventory (summary).** Canonical lists: `PIXEL_KEYS` (12) `sensing.py:36-41`,
`CORE_PIXEL_KEYS` (6) `:44-45`, built-in ratio profile `PIXEL_RATIOS_DEFAULT` `:51-58`.

- *Core detection pixels* (`[x,y]` physical px): `CAP_FULL_PIXEL` + derived `CAP_BAR_WIDTH`
  (pan-fill bar; `prospector_engine/engine.py:190,196`), `PAN_PIX`/`DEPOSIT_PIX`/`SHAKE_PIX`
  (white cue prompts; `engine.py:214-216`), `DIG_TRIGGER_PIXEL` (`engine.py:170`),
  `TERRAIN_PIXEL` (`engine.py:245`; no UI row).
- *OCR regions* (TL/BR pairs, `[0,0]` = unset): MONEY, SHARDS (min 12×8), FIND (min 20×10) —
  `engine.py:449-454`, validated at `engine.py:1673,2018` / `sensing.py:404-439`.
- *Auto-calibration*: `AUTO_CALIBRATE` defaults **True** (`engine.py:728`);
  `apply_auto_calibrate()` (`engine.py:855-882`) places all pixels from `PIXEL_RATIOS` ×
  live window rect at every run start (`engine.py:9370-9372,9592-9594`). Interactive manual
  save forces `AUTO_CALIBRATE=False` and `WINDOW_RELATIVE=False` (`sensing.py:913-919`).
- *Advanced*: cue masks (`CUE_MASKS`, packbits + preview; placed at `engine.py:1239-1260`
  with drift detection), Fortune/Starfall recovery + AutoPan pixel group
  (`engine.py:740-760,388-391`), display-only `PIXEL_COLORS`, host-only `REGION_PREVIEWS`.
- No general range clamping exists ("legacy no-clamping", `prospector_engine/settings.py:90-93`);
  enforced bounds are only bar width > 20, region minimums, and mask/cue pixel minimums.

**Flows** (Api methods in `prospecting_app.py`, driven by the Calibrate tab —
HTML `:5648-5779`, JS `:8160-8378`):

| Flow | Entry | Persists |
|---|---|---|
| Guided wizard (7 steps: detect window → bar ends → 3 cue pixels) | `#wizbtn`; `wizard_propose` `:4759-4818` → auto-detectors `sensing.py:263-341` → overlay pre-marked | nothing until Confirm |
| Fullscreen overlay pick (the real manual path) | `start_overlay_calibrate` `:4470` → `overlay_pick` `:4555` → `overlay_confirm` `:4609-4667` | via `sensing.save_pixels` `:823-921` (the one semantic write) |
| Region draw-a-box | `start_overlay_region` `:4499` → `overlay_region` `:4508` | TL/BR pair + preview |
| Cue-mask capture | `start_cue_mask_capture` `:4680` → flood-fill `cue_toggle` `:4712` → `cue_save` (`sensing.py:621-663`) | `CUE_MASKS` + window rect |
| Live tests (no writes) | `sample_pixels` `:2956`, `test_find_read` `:2975`, `test_earn_read` `:2985` | — |
| Export / import | `export_calibration` `:2623` / `import_calibration` `:2653` (import does not flip auto-cal flags) | portable JSON |
| Health check | `calibration_health` `:3306` (live rect vs `CALIB_WINDOW_RECT`, ±4 px; `sensing.py:754-783`) | — (advisory banner, polled every 8 s) |

Dead surface: `calibrate_capture` (`:4114`) is a Windows-only legacy method with no remaining
JS caller (errors on macOS). `auto_calibrate` (`:2933`) is still shipped but its button was
removed from the tab.

**Required vs optional.** `launch()` (`:4181`) performs **zero calibration validation** — the
app is runnable out of the box because `AUTO_CALIBRATE=True` + the baked ratio profile place
every pixel at run start. Effectively required for a working classic run (bad values break the
run but never block it): `CAP_FULL_PIXEL`/`CAP_BAR_WIDTH`, the three cue pixels, and window
detection. Conditionally required per feature: `DIG_TRIGGER_PIXEL` (perfect-dig/green
confirms), OCR regions (trackers silently skip or disable when unset, `engine.py:1664-1676`,
`:2018-2020`), FR/SR and AutoPan pixels (recovery/tracker modes log and degrade), cue masks
(fall back to box checks). Purely cosmetic: colors, previews. The only guardrails today are
the advisory health banner and the engine's `recal_reason` signal.

**Persistence.** All sensing saves go through the atomic writer (§3). User-facing guidance is
`CALIBRATION_GUIDE.md` plus in-app tours (`TOUR_DEFAULTS`, calibrate tour at
`prospecting_app.py:645-720`); no calibration screenshots ship.

## 5. OS capabilities actually used

Conclusion table (macOS-centric; evidence is exhaustive — greps for pyautogui, event taps,
notification APIs, mic/camera/location all come back empty):

| Capability | Implementing API | macOS permission | Evidence |
|---|---|---|---|
| Screen capture (detection, calibration, webhook screenshots) | `mss` (`sct.grab`) | **Screen Recording** — engine *and* host (calibration sensing runs in-process) | `prospector_engine/platform_mac.py:36`; `engine.py:1299,2984-2998`; `sensing.py:135-141`; `prospecting_app.py:283-295` |
| Roblox window lookup | `Quartz.CGWindowListCopyWindowInfo` reading owner name + bounds only (never window titles) | None | `platform_mac.py:86-165` |
| Key/mouse/scroll synthesis, cursor warp | `Quartz.CGEventPost(kCGHIDEventTap, …)`, `CGWarpMouseCursorPosition` | **Accessibility** | `platform_mac.py:169-230,262-340` |
| Stop/toggle hotkeys (engine) | `pynput.keyboard.Listener` (internally a listen-only event tap) | **Input Monitoring** | `platform_mac.py:29-33,348-416`; `engine.py:9378-9379` |
| Studio input recorder | `pynput` keyboard + mouse listeners | **Input Monitoring** | `prospector_engine/recorder.py:34-39,108-136` |
| Host force-kill input release | `pynput` Controllers (OS up-events for the whole key vocabulary) | Accessibility (host) | `prospecting_app.py:5229-5253` |
| Secure-input detection | Carbon `IsSecureEventInputEnabled` | None | `recorder.py:46-59` |
| Alert sound / clipboard / screen geometry | `afplay` subprocess; `pbcopy`/`clip.exe`; `CGDisplayBounds` | None | `engine.py:341,3425-3434`; `platform_mac.py:313-317`; `prospecting_app.py:3203-3205` |
| Windows capture / input / hotkeys / DPI | `mss` (GDI); `SendInput` scancodes + `SetCursorPos`; `GetAsyncKeyState` polling; `SetProcessDpiAwareness(2)` | n/a — no admin (installer `PrivilegesRequired=lowest`, `windows/installer.iss:25-28`) | `platform_win.py:15-403,410-484,28-36` |
| Mic / camera / location / Apple Events | — none anywhere | — | comment only, `prospector_lite_mac.spec:107` |

**Net macOS requirement: Screen Recording + Accessibility + Input Monitoring** for
`Prospector Lite.app`; because the engine is a re-exec of the same bundle binary
(`prospecting_app.py:4226-4233,10419`), one TCC grant set covers both processes. Dev mode
(`python3 prospecting_old.py`) attributes grants to the terminal instead.

**No permission preflight or request code exists** — no `AXIsProcessTrusted`,
`CGPreflightScreenCaptureAccess`, `CGRequestScreenCaptureAccess`, or `IOHIDCheckAccess`
anywhere; TCC prompts fire implicitly on first API use. Permissions are mentioned only in
prose (`prospecting_app.py:7173`, `engine.py:27-30`, spec comments). Note the released docs
claim "exactly two" permissions (`docs/public-release/ROBLOX_SAFETY_BOUNDARY.md:53`) and omit
Input Monitoring — a wording gap this release fixes (§8).

## 6. Network paths

Network primitives exist in exactly five runtime files; `prospecting_assistant.py`,
`prospecting_core.py`, both `prospecting_old.py` copies, and every engine module except
`engine.py` contain zero network code. `requests`/`http.client` are unused. **Nothing fires
at startup** — every egress path requires an explicit user action plus opt-in configuration
(traced in full; matches `docs/public-release/NETWORK_BEHAVIOR.md` and enforced by the
network-denied child tests, `public_release_tests.py:310-319`).

Complete inventory:

| Site | Location | Contacts | Trigger |
|---|---|---|---|
| Webhook test | `prospecting_app.py:2497-2515` | user's saved `WEBHOOK_URL` (https enforced `:2478`) | user clicks "send a test" |
| Run-event webhook | `engine.py:3047-3089` | user's `WEBHOOK_URL` (https re-checked at send, `engine.py:3070-3074`) | run events, only when `WEBHOOK_ENABLED` (default **False**) and a URL is set |
| Coach LLM API | `prospecting_app.py:2824-2850` | Anthropic endpoint, or user-set OpenAI-compatible base URL | user sends a chat message in API mode with a stored key; otherwise a fully offline brain answers |
| Browser open | `prospecting_app.py:2547` | OS browser, user-clicked URL | user click; empty `PROJECT_URL` (`:35`) makes source links a hidden no-op |
| Local doc open | `prospecting_app.py:2567` | `file://` only, whitelist of 4 bundled .md names (`:2558-2560`), traversal-blocked | user click |
| Legacy fallback UI | `prospecting_ui.py:2496-2511` | loopback `127.0.0.1` HTTP server only | only without pywebview |

**Defaults.** `WEBHOOK_ENABLED=False`, `WEBHOOK_URL=""`, `WEBHOOK_SECRET=""`
(`engine.py:660-664`); the tracked default config ships all of them empty/off
(`windows/prospecting_config.json`, gated by `public_release_tests.py:275-280`). No update
check, no telemetry, no version feed, no machine fingerprint transmission (the local
`_engine_fingerprint` sha256 never leaves the machine).

**Webhook mechanics.** Fire-and-forget daemon thread, timeout 8 s, no retry/backoff on HTTP
errors; JSON payload (`engine.py:3004-3029`) with app username, event, capped text fields, and
a stats dict; optional `x-macro-secret` header only if the user hand-edits a secret into the
config (there is no UI field; never logged). Screenshot attachment: base64 PNG of the full
primary monitor downscaled to ~1280 px (`engine.py:2984-3001`) — as of `680f141` gated by
`NOTIFY_SCREENSHOT` defaulting **True**; **in the current working tree this default is now
`False`** (`engine.py:673`), making screenshots opt-in.

**SSL verification.** As of `680f141` there were five `ssl._create_unverified_context`
fallback sites (retry-after-cert-failure, rationale: bundled Python without certs):
`prospecting_app.py:2509,2850`, `engine.py:3054`, `windows/prospecting_app.py:2388,2729`.
**In the current working tree the root-app and engine sites are removed** (webhook and Coach
traffic is verified-TLS-only, with a bundled/certifi trust store replacing the bypass); the
`windows/` mirror sites are being synced in the same pass. The stale disclosures in
README/SECURITY/PRIVACY/NETWORK_BEHAVIOR are updated in lockstep (§8).

Remote content in webviews: none by default. The only vector is a conditional YouTube iframe
for tutorial cards with an owner-set video id (`prospecting_app.py:7348-7349`) — the shipped
tour content sets none. One dangling owner-only reference exists: `_tutorial_remote()` is
called at `prospecting_app.py:2458` but never defined (dead export path; no pull mechanism
exists anywhere).

## 7. Packaging and build identity

**Version flow.** Single source of truth `VERSION = "1.0.0-rc.1"` at `prospecting_app.py:34`;
extracted by regex into the mac spec (`prospector_lite_mac.spec:12-13` → Info.plist
`CFBundleShortVersionString`/`CFBundleVersion`) and into `build_dmg.command:24` (DMG name).
Two hand-maintained copies must match: `windows/prospecting_app.py:34` and
`windows/installer.iss:6` — enforced by the release gate (`public_release_tests.py:289-303`).
Build identity is `build/build_info.json` = `{commit, date}` only (no version field), stamped
by `build_dmg.command:46-53` / `windows/build.bat:18` / CI from `GITHUB_SHA`.

**macOS.** `prospector_lite_mac.spec`: bundle id `org.prospectorlite.app` (`:95`),
`LSMinimumSystemVersion 11.0`, category utilities, and only one usage-description key
(`NSAppleEventsUsageDescription`, defensive text — the app sends no Apple Events); no
entitlements file exists. Ships the sanitized default config, the engine package as hidden
imports, and the public docs. `build_dmg.command`: venv or `$PYTHON`, build-info stamp,
`SOURCE_DATE_EPOCH` from the commit for reproducibility, PyInstaller, codesign (real identity
via `CODESIGN_ID`, else **ad-hoc**; no `--entitlements` either way; notarization manual per
`RELEASING.md`), a `--capabilities` smoke test of the frozen binary, then `hdiutil` DMG +
SHA-256. DMG checksums are non-reproducible by design (container timestamps;
`docs/public-release/REPRODUCIBLE_BUILDS.md:20-23`) though the .app tree is ~bit-identical.

**Windows.** `windows/build.bat` → PyInstaller (`windows/prospecting.spec`; note `upx=True`
unlike mac, and no VERSION resource on the exe — only the installer carries version metadata)
→ Inno Setup `windows/installer.iss`: **per-user install, no admin**
(`PrivilegesRequired=lowest`, override dialog allowed, `:27-28`); uninstall preserves user
data. Portable ZIP also produced in CI.

**CI** (`.github/workflows/`): `ci.yml` runs py_compile plus the full suite battery
(release gate, `tour_check.py`, `finds_sim.py`, `studio_tests.py`, self-test, and ten engine
suites driven inside `engine_sim.py`'s virtual display-free world) on macos-14 + windows-latest,
Python 3.12, no artifacts. `build-macos.yml`/`build-windows.yml` build, grep-scan the packaged
Resources for forbidden brand/endpoint strings, smoke-test `--capabilities`, and emit
DMG/installer + SBOM (CycloneDX) + SHA256SUMS artifacts. `release.yml` on `v*` tags chains
tests → both builds → a **draft** GitHub release with merged checksums. Actions are not yet
SHA-pinned (self-noted). One portability defect: `studio_conformance.py:19` resolves goldens
from a **private sibling repository** and exits 1 when absent (`:431-433`), so `ci.yml:73`
cannot pass on a public clone until guarded.

**Release gate** (`public_release_tests.py`, 494 lines): static scans (branding, tracking
endpoints, injection APIs, gate removal, secret shapes, subprocess hygiene, default config,
version agreement, artifact naming) plus four network-denied child processes that run the real
app and engine offline in a temp data dir, proving DATA_DIR redirection, welcome flow, https
enforcement, doc-opener traversal blocking, engine offline operation, webhook-off defaults,
and legacy-key scrub/migration.

## 8. Known gaps this release addresses

The trust-and-onboarding work exists because the rc.1 baseline above, while clean on network
and data defaults, still lacks a trust *surface*:

1. **No permission preflights.** Zero preflight/request/status code (§5); macOS TCC prompts
   fire mid-flow on first API use, attributed to whichever process touched the API first, with
   no in-app explanation, ordering, or recovery guidance beyond one welcome-screen sentence
   (`prospecting_app.py:7173`).
2. **Undisclosed Input Monitoring.** The pynput hotkey listener and recorder require Input
   Monitoring, but every shipped doc and the welcome screen claim only Screen Recording +
   Accessibility (`docs/public-release/ROBLOX_SAFETY_BOUNDARY.md:53`, `README.md:40-45`).
3. **No first-run trust surface.** The welcome gate is a single static card: no capability
   registry, no per-permission rationale, no calibration hand-off, no network-features
   disclosure at the moment of consent. `launch()` has no calibration or readiness gate (§4).
4. **SSL-unverified fallbacks.** Five bypass sites at `680f141` could send webhook payloads,
   secrets, and Coach API keys over unverified TLS after a cert failure (§6). Removed in the
   working tree of this pass (root app + engine done; `windows/` mirror in the same pass),
   with the corresponding disclosure rows in README/SECURITY/PRIVACY/NETWORK_BEHAVIOR/
   THREAT_MODEL to be rewritten in lockstep.
5. **Screenshot-by-default.** `NOTIFY_SCREENSHOT` defaulted True at `680f141`, so enabling
   the webhook implicitly opted into full-screen captures leaving the machine; the working
   tree flips the default to False (opt-in).
6. **Wording debt.** "Open source" claims in six app/doc locations while no LICENSE file
   exists (`LICENSE_CHOICE_REQUIRED.md:3`); `pynput` (LGPL-3.0) missing from
   `THIRD_PARTY_NOTICES.md` and `BUILDING.md` install commands; stale "being reworked" notes
   in `BUILDING.md`; internal status docs and the private-sibling goldens path
   (`studio_conformance.py:19`) unfit for a public clone.
7. **Write-path inconsistency.** App-side non-atomic config writes coexist with the engine's
   atomic writer (§3), a corruption risk during the exact flows (calibration, hotkeys) a
   first-run wizard exercises.

Together these define the scope: a capability/permission preflight surface, an honest
three-permission story, a consent-aware first-run flow that hands off to calibration, TLS
hardening with opt-in screenshots, and documentation brought into lockstep.
