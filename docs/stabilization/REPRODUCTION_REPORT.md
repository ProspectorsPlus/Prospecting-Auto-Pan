# Wizard stabilization — reproduction report

Baseline under test: branch `fable/prospector-engine`, HEAD `f1e943cb39edb75bc041129e098133ab6b9e3c35`,
version `1.0.0-rc.2`, clean tracked tree. Line numbers below refer to that commit.

Reproduction environments:
- **dev**: `python3` source run on this Mac (Darwin 25.4.0, arm64, Python 3.13.1, pywebview 6.2.1),
  isolated `PP_DATA_DIR` under the session scratchpad for every probe.
- **packaged**: `dist/Prospector Lite.app` / `ProspectorLite-1.0.0-rc.2-macos-arm64.dmg`
  (ad-hoc signed, bundle id `org.prospectorlite.app`), launched with isolated `PP_DATA_DIR`.

Statuses: every user-reported blocker was either reproduced with tool evidence or root-caused
to a code path whose failure is deterministic from the source. No speculative causes below —
each item lists the exact evidence.

---

## H/I. "Show this screen every time" checkbox wrong default + lost preference (blocker 7)

**Steps (dev, isolated data dir, three simulated launches driving the real `Api`):**

1. Launch 1 (fresh): `welcome_state()` → `show=True`. The checkbox `#welAgain`
   (prospecting_app.py:7994) has no `checked` attribute and no JS ever initialises it
   → renders **unchecked**. Expected: **checked** (default ON).
2. User checks it, clicks Continue → `welcome_done(always_show=True)` stores the inverse key
   `WELCOME_SEEN=False` (prospecting_app.py:2692).
3. Launch 2: `show=True` (correct so far) but the box renders **unchecked again** — nothing reads
   the stored preference back into the DOM.
4. User clicks Continue without touching the (wrong) box → `welcome_done(False)` →
   `WELCOME_SEEN=True`.
5. Launch 3: `show=False`. **The ON preference was silently lost.**

Observed tool output:

```
LAUNCH 1 (fresh): show=True setup_needed=True
after welcome_done(always_show=True): WELCOME_SEEN = False
LAUNCH 2: show=True  -> checkbox renders UNCHECKED again
after Continue with box unchecked: WELCOME_SEEN = True
LAUNCH 3: show=False  -> the ON preference was silently lost
```

**Additional causes:**
- `window.openWelcome` (prospecting_app.py:9912) force-sets `checked=false` every time the
  welcome screen is reopened from the menu.
- The preference persists only via Continue; toggling the box saves nothing.
- `welcome_done`'s config write swallows `OSError` (prospecting_app.py:2698-2699): a failed save
  silently reverts with no UI feedback.
- The stored key is the inverse (`WELCOME_SEEN`) of what the checkbox expresses, inviting exactly
  this class of sign bug.

**Root cause:** no code path ever renders the stored preference; the default is the HTML default
(unchecked); the key is inverted; saving happens only as a side effect of Continue.
**Affected:** prospecting_app.py:2672-2704 (Api), :7994 (HTML), :9908-9939 (JS).

---

## A/B/C. Stale "Not granted" after enabling; refresh unreliable (blockers 1-3)

**Cause 1 — post-action refreshes never re-render (all modes).**
Every action handler on a capability card ends with `refresh(false)`
(prospecting_app.py:10007, 10015, 10030, 10034). `refresh(render)` re-renders only when
`render!==false` (prospecting_app.py:10163). So after *Request access*, *Test*, or returning from
System Settings, the fresh `trust_state()` is fetched and **discarded from the DOM**: the status
pill keeps whatever it showed at page render. The only recovery paths are re-opening the page or
a JS `window` focus event.

**Cause 2 — no reliable focus/activation refresh.**
The only automatic refresh is `window.addEventListener('focus', ...)`
(prospecting_app.py:10186-10188). pywebview 6.2.1 exposes **no app-activation event**
(verified against the installed `webview.window.Window.events`: closed/closing/loaded/
before_load/before_show/initialized/shown/minimized/maximized/restored/resized/moved/
request_sent/response_received only), and WKWebView does not reliably fire a JS window focus
event on app re-activation when the webview's internal focus never changed. There is no
`document.hasFocus()` polling fallback, no visibilitychange hook, no manual **Refresh** button,
and no "I've enabled it — check again" affordance.

**Ruled out — a missing `ApplicationServices` module in the bundle.** The `Frameworks/`
directory of the rc.2 bundle lists no `ApplicationServices` entry, which initially looked like
a broken Accessibility preflight; the independent packaged audit then verified with
`pyi-archive_viewer` that `ApplicationServices`, `HIServices`, and every other wizard import
ARE present in the frozen PYZ (modulegraph pulled them in through lite_trust's function-level
imports). Not a root cause. As hardening, both modules are now named explicitly in the spec's
hiddenimports and `lite_trust` falls back to `HIServices` if `ApplicationServices` ever fails
to import (`from HIServices import AXIsProcessTrusted` verified working on this machine).

**Cause 3 (packaged) — ad-hoc signing makes TCC identity unstable.**
`codesign -dv` on the built app: `Signature=adhoc`, `TeamIdentifier=not set`,
`flags=0x2(adhoc)`. Every rebuild yields a new ad-hoc identity, so a System Settings entry
created by an earlier build shows **enabled** while the current binary's preflight honestly
returns **false**. This exactly reproduces "access is already enabled but the app says Not
granted". Without a Developer ID certificate this cannot be fully fixed in code; the app must
(a) say this state clearly, (b) guide the user to remove the stale entry / re-toggle for the
running copy, (c) expose a restart affordance. (Signing remains an owner action.)

**Cause 4 — no restart-required state exists.**
macOS applies Screen Recording / Input Monitoring grants to a *running* process only after
relaunch. The UI has no `restart_required` state (the status map at prospecting_app.py:9954-9958
has only granted/untested/configured/not_granted/disabled/not_requested/info/unknown), so the
truthful "granted, but restart Prospector Lite to apply" case renders as a stale
"Not granted"/failed test with no explanation.

---

## D. Input test does nothing / fails (blocker 4)

`trust_test_key` (prospecting_app.py:2765-2771) arms a daemon thread running
`lite_trust.post_test_key` and immediately returns `{armed: true}`. The thread's actual result —
`{skipped: "not frontmost"}`, `{error: ...}`, or `{posted: true}` — is **discarded**; nothing
delivers it to the page. The JS (prospecting_app.py:10019-10030) only watches for the synthetic
keystroke for 2.4 s and, when nothing arrives, reports "no keystroke arrived (grant
Accessibility...)" even when the true cause is the frontmost guard refusing to post, a missing
module in the bundle, or an exception. The pointer half (`trust_test_pointer`) is synchronous and
does report, but the combined UI cannot distinguish *refused to post* from *posted but blocked by
macOS* from *posted and swallowed*.

## E. Safe Stop test unreliability (blocker 5)

`trust_test_hotkey` (prospecting_app.py:2778-2792) has no single-flight guard (double-click arms
two listeners; both later call `window.__hotkeyResult`, the second overwriting the first's DOM
output), no request id (a late result from an earlier arm can overwrite a newer run), and no
page-navigation cancellation (navigating away leaves `window.__hotkeyResult` installed; the
callback fires into a card that no longer exists). The 8 s pynput listener itself stops on
timeout (lite_trust.py:962-969). On macOS the listener needs Input Monitoring; when the packaged
app's TCC identity is stale (Cause 4 above) the test times out with a message that does not
mention the stale-entry case.

## F/G. Calibration crashes / does not run (blocker 6)

Traced from the wizard's calibration cards (prospecting_app.py:10096-10116 at baseline). The
full evidence trail, including hypotheses that were checked and ruled out (the
`SETUP.suspend()`/tab-click race does NOT exist — tab and `#wizbtn` handlers bind at script
parse; pywebview 6.2.1's `show()`/`evaluate_js` marshal to the main thread via
`AppHelper.callAfter`, so overlay threading is safe), lives in `CALIBRATION_CRASH_REPORT.md`.
Confirmed causes:

- **Silent no-op pickers** — the `pixel`/`region` handlers ignore the API's in-band
  `{error: ...}` result entirely, and `start_overlay_calibrate` returns `{ok: True}` when the
  pre-created overlay window does not exist (baseline prospecting_app.py:5226-5232): every
  capture/overlay failure looked like "the button does nothing".
- **Always-"found" Roblox window** — `bool(s.detect_window())` is truthy even for
  `{found: False, error: ...}` (a non-empty dict), so the wizard card and the Readiness Check
  claimed "Roblox window found" with Roblox closed (baseline :2899-2900, :3052-3053).
- **Optional save kills auto-calibration** — every interactive save forces
  `AUTO_CALIBRATE=False` (sensing.py:909-915), so confirming a single optional pixel (dig
  green, a tracker box) instantly turned all four required items into blockers.
- **Black-overlay trap** — no Screen Recording preflight before opening the full-screen,
  frameless, on-top overlay: with capture denied the user got a black screen with no
  explanation.
- **Non-atomic config write** — `_region_preview_save` truncate-writes `CONFIG_FILE`
  (baseline :5339-5340); 12 further truncate-write sites existed across the app. The engine's
  `atomic_write` additionally renamed the live config away to `.bak` before landing the new
  file, leaving a no-file window for concurrent readers.
- **Cosmetics that read as breakage** — "Found at \"\"" (a `rect` key that never existed),
  raw empty-JSON test output on fresh auto-calibrated installs.

---

## J. False success/failure reporting

- Opening System Settings prints "flip the switch ... then Test" but the card itself keeps the
  stale status indefinitely (Cause 1 above) — reads as false failure.
- `request` handler treats `r.granted` as the only success signal (prospecting_app.py:10005);
  for `screen_detection`/`stop_hotkeys` macOS frequently registers the app and returns granted
  only after relaunch, so the card says "not granted" forever without the restart explanation —
  false failure.
- The input test reports "no keystroke arrived (grant Accessibility…)" when the real cause was
  the frontmost-guard refusal or a bundle import error — false failure with a wrong remedy.

---

Full per-agent audit evidence (six independent audits: bridge/UI, calibration, persistence,
packaged bundle, Windows parity, refresh model) is summarised in the sections above and in
`CALIBRATION_CRASH_REPORT.md`. The fixes are recorded in `STABILIZATION_REPORT.md`.
