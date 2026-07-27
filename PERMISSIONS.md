# Permissions

Everything Prospector Lite can ask the operating system for, why, and how to check it yourself. This document is bundled into the app and opens from the **Trust Center** (the shield tab) and from Step 2 of the setup wizard. The machine-readable version of this document is the capability registry in `lite_trust.py` (`CAPABILITIES`) — every claim below maps to a named function in this repository, and the build fails if a reference stops resolving (`lite_trust.py`, `generate_manifest`).

Ground rules, enforced by the code and its tests:

- **Checking status never triggers an OS prompt.** On macOS the app reads permission state with the read-only preflight APIs (`lite_trust.py`, `_mac_preflights`). A system prompt can appear only when *you* click a clearly labeled **Request** button (`lite_trust.py`, `request_permission`).
- **The app never touches the TCC databases** or any other permission store, and it never asks you to disable Gatekeeper, SmartScreen, SIP, or your antivirus. Any instruction to do so — from anyone — is a red flag.
- **A missing permission only ever blocks Start Macro**, never the app itself. Settings, calibration viewing, documentation, and the Trust Center always work (`prospecting_app.py`, `Api.launch` — the trust gate returns `perm:<ids>` and the UI opens the Trust Center).

---

## macOS: the three permissions

Prospector Lite needs exactly **three** macOS permissions, all under System Settings → Privacy & Security. Each is detected with the real system API (no guessing, no prompt on detection), requested only from an explicit button, and testable in-app.

| Permission | macOS pane | Detected by | Requested by |
|---|---|---|---|
| Screen Recording | Privacy & Security → Screen Recording | `CGPreflightScreenCaptureAccess` | `CGRequestScreenCaptureAccess` |
| Accessibility | Privacy & Security → Accessibility | `AXIsProcessTrusted` | `AXIsProcessTrustedWithOptions` (with prompt) |
| Input Monitoring | Privacy & Security → Input Monitoring | `CGPreflightListenEventAccess` | `CGRequestListenEventAccess` |

The in-app **Open Settings** buttons deep-link to the exact pane (`x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture` / `?Privacy_Accessibility` / `?Privacy_ListenEvent`, see `lite_trust.py`, `_MAC_SETTINGS_LINKS`), with the manual path always shown as a fallback.

**Restart note:** macOS applies Screen Recording (and sometimes Input Monitoring) grants on the app's next launch. If a test still fails right after granting, quit and reopen Prospector Lite — macOS may also offer to relaunch it for you.

**Running from source?** macOS attributes these permissions to your terminal or IDE, not to a "Prospector Lite" app bundle. The packaged app appears under its own name because the engine subprocess is a re-execution of the same bundle binary, so one grant covers both processes.

### 1. Screen Recording — "Screen Detection"

- **Why needed:** the macro works by looking at the screen the way you do — it samples a handful of calibrated pixels (the pan-fill bar, the dig bar, the Pan / Collect Deposit / Shake prompts) and, if you enable trackers, the small money/shards/finds regions you drew.
- **Which features:** every macro mode, calibration and the live detection test, and the optional earnings/finds trackers.
- **Required or optional:** **required for the core macro** (`REQUIRED_FOR_CORE`). A macro that cannot see the game would misclick blindly, so Start is blocked without it.
- **Data accessed:** visible screen pixels of the display Roblox is on, at specific calibrated points and small user-drawn regions.
- **Retained:** none by default. Calibration stores coordinates and reference colors (numbers), not images. The only screenshot that can ever be written or sent is the optional Discord notification screenshot — a separate opt-in that is **off by default** (see Network features below).
- **Transmitted:** nothing. Detection is entirely local.
- **How to enable:** System Settings → Privacy & Security → Screen Recording → switch on Prospector Lite. Or click **Request** on the capability card (wizard Step 2 or Trust Center) — that runs `CGRequestScreenCaptureAccess`, which shows the system prompt and registers the app in the pane.
- **How to test in-app:** the **Test** button captures a small patch at the center of your screen, reports its size and whether it is non-blank, shows it once inside the app, and discards the frame. Nothing is saved (`lite_trust.py`, `test_screen_capture`; `prospecting_app.py`, `Api.trust_test_screen`).
- **How to revoke:** same pane, switch off Prospector Lite. macOS may relaunch the app.
- **If declined:** the app opens and everything except detection works. Starting a macro is blocked with a clear message; calibration tests report the denied capture honestly (black frames).
- **Source:** `prospector_engine/sensing.py` (`Sensing._grab_full`, `Sensing.sample_saved`), `prospector_engine/engine.py` (`Detector`, and `_grab_screenshot_b64` — the *only* screenshot encoder, used solely for the opt-in Discord screenshot), `lite_trust.py` (`test_screen_capture`).

**About the "Screen & System Audio Recording" label.** Recent macOS versions label this permission category "Screen & System Audio Recording." That label belongs to the OS category, not to what this app does: Prospector Lite reads **pixels only** and contains **no audio-capture code at all** — there is no microphone or system-audio API anywhere in the source, and the onboarding/trust suite scans the source to keep it that way. Granting the permission does not cause any audio to be recorded by this app.

### 2. Accessibility — "Keyboard & Mouse Control"

- **Why needed:** the macro plays by pressing the same ordinary keys and clicks you would (W/A/S/D, Space, the left mouse button). On macOS, posting synthetic input uses the `CGEventPost` API, which macOS files under Accessibility because it is an assistive-technology interface.
- **Which features:** every macro mode (movement, digging, panning, shaking), the in-app control test, and input release on stop (a safety feature).
- **Required or optional:** **required for the core macro** (`REQUIRED_FOR_CORE`).
- **Data accessed:** none. This capability is **output only** — it does not observe your input, and it does not let the app read your screen or your keystrokes (those are separate permissions).
- **Retained / transmitted:** nothing.
- **How to enable:** System Settings → Privacy & Security → Accessibility → switch on Prospector Lite, or click **Request** on the capability card (`AXIsProcessTrustedWithOptions` with the prompt option).
- **How to test in-app:** the sandbox test posts **one** harmless keystroke into the app's own test field and verifies that both the key-down **and** the key-up arrive — proving synthetic input works and releases are clean — plus a 2-pixel pointer wiggle that is verified by reading the cursor position back. No clicks, and nothing is sent to Roblox or any other application (`lite_trust.py`, `post_test_key`, `test_pointer_wiggle`; `prospecting_app.py`, `Api.trust_test_key`, `Api.trust_test_pointer`).
- **How to revoke:** same pane, switch off Prospector Lite.
- **If declined:** the app opens; settings and calibration viewing work; starting a macro is blocked with a clear message (it could not press anything).
- **Source:** `prospector_engine/platform_mac.py` (`key_down`, `key_up`), `prospector_engine/platform_win.py` (`key_down`), `prospector_engine/engine.py` (`release_all` — the release-everything safety floor on every stop path), `prospecting_app.py` (`_host_release_inputs` — the host-side release backstop), `lite_trust.py` (`test_input_control`).

### 3. Input Monitoring — "Safe Stop & Global Hotkeys"

- **Why needed:** while the macro runs, a listener watches for the few control chords you configured — by default **Esc** (quit), **Ctrl+K** (start/stop), **Ctrl+J** (soft stop), **Ctrl+L** (pause) — even while Roblox has focus. That is what makes Safe Stop reliable: you never have to find the app window to stop the macro. This is the price of a global panic key.
- **Which features:** Safe Stop from anywhere, the start/stop/pause/soft-stop/relic hotkeys, and the opt-in Studio input recorder (which records only during an explicit recording you start and stop).
- **Required or optional:** **required for the core macro** (`REQUIRED_FOR_CORE`). Prospector Lite treats the panic key as required for your safety — running a macro you cannot instantly stop is not safe.
- **Data accessed:** global key events, matched against your configured control chords via a listen-only `pynput` tap. It does **not** log keystrokes, build any buffer of what you type, or transmit anything.
- **Retained:** none. No keystroke log exists anywhere.
- **Transmitted:** nothing.
- **How to enable:** System Settings → Privacy & Security → Input Monitoring → switch on Prospector Lite, or click **Request** on the capability card (`CGRequestListenEventAccess`).
- **How to test in-app:** the Safe Stop test arms a one-shot listener for **8 seconds** that reacts to **Esc or Ctrl+K only** and reports whether it heard your press. Nothing else is captured (`lite_trust.py`, `await_stop_hotkey`; `prospecting_app.py`, `Api.trust_test_hotkey`).
- **How to revoke:** same pane, switch off Prospector Lite.
- **If declined:** starting a macro is blocked, because without the listener the Safe Stop hotkey cannot work. The in-app Stop button would still work, but it requires reaching the window.
- **Source:** `prospector_engine/platform_mac.py` (`make_listener` — matches configured chords only), `prospector_engine/platform_win.py` (`make_listener`), `prospector_engine/recorder.py` (`Recorder` — the opt-in Studio recorder), `lite_trust.py` (`await_stop_hotkey`).

---

## Windows: no permission prompts

Windows has no permission model for screen capture or synthetic input from desktop apps, so **there is nothing to grant and no prompt will appear**. The app does not pretend otherwise — instead of inventing a "granted" state, the Windows capability cards say "no permission prompt" and give you **the same real in-app tests** (screen grab, sandbox keystroke + pointer wiggle, Safe Stop listen) so you can prove each capability works before a run.

Facts that matter on Windows:

- **No administrator rights** are required or recommended. The installer is per-user (`PrivilegesRequired=lowest` in `windows/installer.iss`) and the app runs as a normal user process. Never run Prospector Lite elevated.
- **Integrity-level mismatch:** if Roblox itself runs as administrator, Windows silently drops synthetic input from normal-level processes. The fix is to run **both** programs at the normal level — elevating the macro is never the answer. The Trust Center's Windows tab shows whether the app is currently elevated (it should not be).
- **Hotkeys** use a 30 ms `GetAsyncKeyState` poll of the specific configured keys (`prospector_engine/platform_win.py`, `make_listener`) — no keyboard hook is installed.
- **No firewall prompt:** the app has no inbound listener, so Windows Firewall has nothing to ask about.
- **SmartScreen** may warn about an unsigned or low-reputation download. SmartScreen is a *reputation* check, not a malware verdict — but do not bypass it blind. Verify the SHA-256 checksum against the release's `SHA256SUMS.txt` first (see `VERIFY_DOWNLOAD.md`), and only then use "More info → Run anyway." **Never disable SmartScreen**, and never trust software that tells you to.

**Honesty note:** the Windows build and CI workflows for this release are prepared but have **not yet been executed on a real Windows machine by the developers**. The capability tests above exist precisely so you can verify the Windows build on your own hardware; treat the first Windows releases as needing that verification.

---

## Network features (these are NOT recording permissions)

Two optional features can reach the network. Both are **off by default**, both point at endpoints *you* choose, and neither has anything to do with the microphone or any recording permission — they are network features. Prospector Lite contains no audio-capture code. Normal startup and macro use make **zero network requests** (enforced by the network-denied child-process tests in `public_release_tests.py`).

### Discord notifications (optional, off by default)

Paste your own Discord webhook URL and the macro can post you run events (start, stop, Safe Stop, bag full, periodic stats). Facts:

- Off by default, empty URL by default; there is no developer endpoint anywhere.
- `https://` is enforced when you save the URL **and again at send time**; TLS certificate verification is **mandatory** — the old unverified-SSL fallback has been removed everywhere, and if the interpreter ships without a trust store the bundled `certifi` CA bundle is used instead (`prospector_engine/engine.py`, `_webhook_tls_context`, `_webhook_send`).
- One attempt per event, 8-second timeout, no retry storm.
- The payload is exactly: your optional display name, the event, a message, and run stats. You can see the **exact** payload before enabling anything — the in-app preview is built by the same engine function that sends (`prospector_engine/engine.py`, `_webhook_payload`; `prospecting_app.py`, `Api.webhook_payload_preview`).
- The screenshot attachment is a **separate, second opt-in** (`NOTIFY_SCREENSHOT`), whose default is **off** as of this release. A screenshot shows whatever is on your screen at that moment — enable it knowingly.
- The optional `x-macro-secret` header (for self-hosted receivers) comes from your config, is never logged, and is redacted in previews.
- **No microphone, no audio, ever.** This feature posts JSON over HTTPS; it cannot hear anything.

### Coach cloud AI (optional, offline by default)

The built-in Coach answers setup questions **offline, for free** — a local rule engine with no network access. If you opt into API mode with **your own** provider key, your chat message (plus current settings values and summary stats so the Coach can advise) goes to the provider you chose — never to the developer. The key is stored in a separate local secrets file, is never shown back to the interface, and is never included in exports (`prospecting_app.py`, `Api._coach_api`, `_save_coach_key`, `Api.coach_settings`). Also a network feature: no microphone, no audio.

### Sound alerts (no permission involved)

Alerts play a short system sound (macOS: `afplay` of a built-in sound; Windows: `MessageBeep`) — audio **output** only, unrelated to any recording permission and needing none (`prospector_engine/engine.py`, `_beep`).

---

## What Prospector Lite never requests

The capability registry carries explicit `NOT_REQUIRED` entries for these, so the app itself can show you that it knows about them and does not want them:

| Never requested | Notes |
|---|---|
| **Microphone** | No audio-capture code exists in the app. If macOS ever shows a microphone prompt naming this app, treat it as a red flag for a tampered download and verify your copy's checksum (`VERIFY_DOWNLOAD.md`). |
| **Camera** | No code path touches the camera. |
| **Location** | No location API, no IP-based geolocation, no lookup services. (Older private builds phoned a location service; that code was removed before the public release and `public_release_tests.py` bans those endpoints.) |
| **Administrator / root** | Per-user install, normal-user process on both platforms. No privileged helper, no `sudo`, no UAC prompt in normal use. |
| **Full Disk Access** (macOS) | The app reads and writes only its own data folder plus files you explicitly pick in open/save dialogs. |

## Verify it yourself

- **Trust Center → Permissions & capability tests**: live status from the real OS APIs, plus a Test button per capability.
- **Trust Center → Source code**: the trust manifest lists file, symbol, and exact line range for every capability above, generated at build time from the exact source of the build; **View Code** opens the file at the exact commit when a public repository URL is configured, or shows the local file + symbol + commit otherwise.
- **From a checkout:** `python3 lite_trust.py` prints the live capability statuses; `python3 lite_trust.py --emit` regenerates the trust manifest and fails on any dead reference.

Prospector Lite is source-available for inspection (no open-source license has been chosen yet — see `LICENSE_CHOICE_REQUIRED.md`), and no macro can honestly be called risk-free; see `README.md` and `docs/public-release/ROBLOX_SAFETY_BOUNDARY.md` for the boundary it keeps.
