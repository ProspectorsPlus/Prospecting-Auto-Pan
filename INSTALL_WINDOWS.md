# Installing on Windows

Prospector Lite ships for Windows as a per-user installer (`ProspectorLiteSetup.exe`) and a portable ZIP (`ProspectorLite-portable-win64.zip`). This guide covers both, SmartScreen, the setup wizard, and uninstalling.

> **Honest status:** the Windows build and its CI workflows are prepared, but this release has **not yet been runtime-verified by the developers on a real Windows machine**. The packaging follows the same code and tests as macOS, and the in-app capability tests exist so you can verify everything on your own hardware — but treat the first Windows releases accordingly. Releases are not published yet; this describes the process once they are.

## 1. Verify, then install

1. **Verify the checksum first** — see `VERIFY_DOWNLOAD.md`. In PowerShell: `Get-FileHash .\ProspectorLiteSetup.exe -Algorithm SHA256`, compared against the release's `SHA256SUMS.txt`.
2. Pick one:
   - **Installer** — run `ProspectorLiteSetup.exe`. It installs **per-user** (`PrivilegesRequired=lowest` in `windows/installer.iss`): no administrator rights, no UAC prompt in the normal flow, nothing system-wide. You get Start-menu and optional desktop shortcuts and an uninstaller.
   - **Portable ZIP** — unzip `ProspectorLite-portable-win64.zip` anywhere (Desktop, a USB stick) and run `Prospector Lite.exe` from the folder. No installation, no registry changes.

Never run Prospector Lite "as administrator" — it is not required, not recommended, and never the fix for anything (see step 4).

## 2. About SmartScreen

Until the Windows build is Authenticode-signed, SmartScreen will likely show "Windows protected your PC" on first run. SmartScreen is a **reputation** warning — it means "this file is unsigned/uncommon", not "this file is malicious". The correct response:

1. Verify the SHA-256 checksum (step 1). This is your actual evidence about the file.
2. Only after it matches: click **More info → Run anyway**.

**Never disable SmartScreen**, and never follow instructions that tell you to — from this project or anyone else.

**Windows Defender false positives:** unsigned Python-packaged apps occasionally trip generic heuristics. If Defender flags a file whose checksum matches the release: don't disable your antivirus — verify the checksum, report the detection as a false positive to Microsoft (and to this project's issue tracker), and wait for the definitions to update or build from source. A flagged file whose checksum does **not** match is simply a bad download: delete it.

## 3. The setup wizard (first run)

First launch opens a 5-step wizard. Progress is saved after every step (atomically, to `onboarding_state.json` in the data folder) and resumes if you close the app; re-run it any time from the Tutorial menu → **Re-run setup wizard** or from the Trust Center.

1. **Welcome** — what the app is, the exact version and build commit, and the privacy basics.
2. **Trust & Permissions** — capability cards on a Windows tab. **There are no permission prompts on Windows**: the OS has no grant model for screen capture or synthetic input from desktop apps, and the app says so instead of inventing a "granted" state. Each card instead offers a real **Test** — a small one-shot screen grab (shown once, then discarded), a sandbox keystroke whose press *and* release are verified plus a 2-pixel pointer wiggle, and an 8-second Safe Stop listen for Esc / Ctrl+K only — plus View code and decline info. The two optional network features (your own Discord webhook, your own Coach AI key) are shown here too, off by default. Details: `PERMISSIONS.md`.
3. **Guided Calibration** — walks the calibration items with per-item instructions and live status, driving the same engine and save path as the Calibrate tab (one calibration store). Auto-calibration is on by default, so the app is runnable out of the box; manual calibration makes it exact.
4. **Readiness Check** — real probes: capability tests, Roblox window detection, required calibration, data-folder writability, a settings save/reload round trip, build identity, and network defaults.
5. **The app.**

## 4. Roblox running as administrator

If Roblox itself runs elevated, Windows silently drops synthetic input from normal-level processes, so the macro presses keys and nothing happens. The fix is to run **both** programs at the normal level — never to elevate the macro. The Trust Center's Windows view shows whether Prospector Lite is currently elevated (it should say it is not).

Also worth knowing:

- Hotkeys poll specific keys via `GetAsyncKeyState` (`prospector_engine/platform_win.py`, `make_listener`) — no keyboard hook is installed.
- The app has no inbound network listener, so Windows Firewall has no rule to prompt about.
- Normal startup and macro use make zero network requests (`docs/public-release/NETWORK_BEHAVIOR.md`).

## 5. Where your data lives

Everything is local JSON under:

```
%APPDATA%\Prospector Lite
```

(Type that into the Explorer address bar; the app falls back to `%LOCALAPPDATA%` only if `%APPDATA%` is undefined.) Settings + calibration, builds, Studio scripts, run history and logs, wizard progress, and (only if you set a Coach API key) a separate secrets file. The Trust Center → Local data section lists every file live with sizes, and has Open folder / Export / Delete actions.

## 6. Uninstalling

- **Installer version:** Settings → Apps → Prospector Lite → Uninstall (or the Start-menu "Uninstall Prospector Lite" shortcut). The uninstaller deliberately **keeps the data folder** so your settings and calibration survive a reinstall — delete `%APPDATA%\Prospector Lite` yourself for complete removal.
- **Portable version:** delete the unzipped folder, and optionally `%APPDATA%\Prospector Lite`.

No services, drivers, scheduled tasks, or system-wide files are ever installed.

## Troubleshooting

- **SmartScreen or Defender warning** — see step 2: checksum first, never disable protection.
- **Keys pressed but nothing happens in Roblox** — integrity-level mismatch; see step 4.
- **Capture test fails** — remote-desktop sessions and protected-content setups can block GDI capture; run locally.
- **Start Macro does nothing** — run the Readiness Check (Trust Center → Re-run setup wizard, or the Readiness step) and fix what it names.
- More: `SUPPORT.md`.
