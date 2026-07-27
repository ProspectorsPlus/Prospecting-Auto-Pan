# Installing on macOS

Prospector Lite ships as a DMG for macOS 11+ (`ProspectorLite-<version>-macos-<arch>.dmg`). This guide covers the install, the first launch of an unsigned build, the 5-step setup wizard, and uninstalling.

> Releases are not published yet; this describes the process once they are. To run from source instead, see `BUILDING.md`.

## 1. Verify, then install

1. **Verify the checksum first** — see `VERIFY_DOWNLOAD.md`. One command: `shasum -a 256 ProspectorLite-*.dmg`, compared against the release's `SHA256SUMS.txt`.
2. Open the DMG and **drag `Prospector Lite.app` into Applications** (the DMG window contains the app and an Applications shortcut).
3. Eject the DMG.

## 2. First launch of an unsigned build

Current builds are not signed with an Apple Developer certificate (the Trust Center's Build identity screen shows `Signed: no` honestly), so plain double-clicking is blocked by Gatekeeper the first time:

1. In Applications, **right-click (or Control-click) `Prospector Lite.app` → Open**.
2. In the dialog, click **Open** (on newer macOS you may instead need System Settings → Privacy & Security → "Open Anyway", then confirm).

This is a one-time step. Do **not** disable Gatekeeper — the right-click → Open path exists precisely so you never have to.

## 3. The setup wizard (first run)

On first launch a 5-step wizard opens. Progress is saved after every step (atomically, to `onboarding_state.json` in the data folder), so you can quit at any point and resume where you left off. You can re-run it any time from the Tutorial menu → **Re-run setup wizard** or from the Trust Center. If you used an earlier Prospector Lite that only had the single welcome screen, you are never forced back through setup — the wizard just stays available.

1. **Welcome** — what the app is, the exact version and build commit, and the privacy basics.
2. **Trust & Permissions** — one card per capability with live status, a **Request** button (the only thing that ever shows a system prompt), a **Test** button, View code, and honest revoke/decline information. Grant the three permissions here (next section).
3. **Guided Calibration** — walks the calibration items with per-item instructions and status. It drives the *same* sensing engine and the *same* save path as the Calibrate tab, so there is a single calibration store — nothing to keep in sync. Because auto-calibration is on by default (built-in ratio profile), the app is runnable out of the box; manual calibration makes it exact for your setup. Optional items say plainly what skipping them costs. (Example screenshots for some steps may show a clearly labeled "pending" note until reviewed images ship — the text instructions are complete either way.)
4. **Readiness Check** — runs every real probe: the three permissions, Roblox window detection, required calibration, data-folder writability, a settings save/reload round trip, build identity, and network defaults. Anything failing links straight to the screen that fixes it.
5. **The app** — you land in the main interface, ready to run.

## 4. The three permissions

Prospector Lite needs exactly three macOS permissions — and never asks for anything else (no microphone, no camera, no location, no Full Disk Access, no admin). Full details, tests, and source references: `PERMISSIONS.md`.

| Permission | Exact path | Used for |
|---|---|---|
| Screen Recording | System Settings → Privacy & Security → **Screen Recording** (labeled "Screen & System Audio Recording" on recent macOS — this app captures pixels only and has no audio code) | Reading the game's bars and prompts |
| Accessibility | System Settings → Privacy & Security → **Accessibility** | Pressing keys and mouse buttons |
| Input Monitoring | System Settings → Privacy & Security → **Input Monitoring** | Hearing your Safe Stop hotkey (Esc / Ctrl+K) while Roblox has focus |

For each: click **Request** on its wizard/Trust Center card (this triggers the real system prompt and registers the app in the pane), flip the switch for Prospector Lite, then use the card's **Test** button to prove it works.

**Restart after granting:** macOS applies Screen Recording (and sometimes Input Monitoring) on the app's next launch. If a test still fails right after granting, quit Prospector Lite fully (Cmd+Q) and reopen it. macOS may also offer to relaunch the app for you.

If you decline a permission, the app stays fully usable — only **Start Macro** is blocked, with a message naming exactly what is missing.

## 5. Where your data lives

Everything is local JSON under:

```
~/Library/Application Support/Prospector Lite
```

Settings + calibration (`prospecting_config.json`), builds, Studio scripts, run history and logs, wizard progress, and (only if you set a Coach API key) a separate secrets file. The Trust Center → Local data section lists every file live with sizes, and has Open folder / Export / Delete actions. Nothing is stored anywhere else, and nothing is written into the app bundle.

## 6. Uninstalling

1. Quit Prospector Lite.
2. Delete `Prospector Lite.app` from Applications.
3. Optionally delete the data folder above to remove settings, calibration, history, and any secrets. (Keeping it means a reinstall picks up exactly where you left off.)

There are no launch agents, kernel extensions, privileged helpers, or other system files to clean up.

## Troubleshooting

- **Black frames / "not granted" after granting** — quit and reopen the app (see the restart note above).
- **"App is damaged" or repeated Gatekeeper blocks** — re-verify the checksum (`VERIFY_DOWNLOAD.md`); a failing checksum means a bad download, not a setting to bypass.
- **Start Macro is blocked** — the message names the missing permission and opens the Trust Center; grant and test it there.
- More: `SUPPORT.md`.
