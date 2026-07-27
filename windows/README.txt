PROSPECTOR LITE  —  Windows
===========================

A free auto-pan macro for the Roblox game "Prospecting". Its full source
is available for inspection (no open-source license has been chosen yet).
It works from the OUTSIDE only: it reads pixels on your screen and presses
ordinary keys and mouse buttons. It never injects into Roblox, never touches
game files or memory, and makes no network requests unless you set up
optional Discord notifications yourself. Full details: PRIVACY.md and
SECURITY.md in the app folder / source repository.

------------------------------------------------------------
INSTALL (pick one)
------------------------------------------------------------
A) Installer:  run ProspectorLiteSetup.exe
     - Installs per-user (no administrator prompt), adds Start Menu and
       optional desktop shortcuts. Uninstall from Windows Settings as usual;
       your settings in %APPDATA%\Prospector Lite are kept.

B) Portable ZIP:  extract the zip anywhere and run "Prospector Lite.exe"
     - Nothing else to install. Python is NOT required — everything the app
       needs is inside the folder.

Your data (settings, calibration, builds, run history) lives in
%APPDATA%\Prospector Lite. Delete that folder to remove every trace.

------------------------------------------------------------
IF WINDOWS BLOCKS IT  (SmartScreen / Smart App Control)
------------------------------------------------------------
Windows flags unsigned files that came from the internet; these builds are
not code-signed yet. This is expected:

  1. BEFORE extracting a zip: right-click it -> Properties -> tick
     "Unblock" -> OK, THEN extract (clears the mark for every file at once).
  2. If SmartScreen warns on the installer/exe: "More info" -> "Run anyway".
  3. You can verify the download first: compare its SHA-256 hash against
     SHA256SUMS.txt from the same release (PowerShell:
     Get-FileHash "ProspectorLiteSetup.exe").

------------------------------------------------------------
QUICK START
------------------------------------------------------------
1. Open Prospector Lite. On first run a short setup wizard walks you
   through what the app does and stores, the capabilities it uses (no
   special Windows permissions are needed), guided calibration, and a
   readiness check. You can re-run it any time from the Tutorial menu
   ("Re-run setup wizard").
2. Open Roblox Prospecting with the game HUD visible for the wizard's
   calibration step (or the Calibrate tab -> "Guided calibration" later).
   Redo calibration after resolution/window changes.
3. Pick a build/preset, press Start, and click into Roblox so the game has
   focus.

------------------------------------------------------------
HOTKEYS
------------------------------------------------------------
  Ctrl + K   start / stop the macro
  Esc        Safe Stop: stops instantly and releases every key and button

------------------------------------------------------------
TROUBLESHOOTING
------------------------------------------------------------
- "Nothing happens in game": keep Roblox on the same monitor and focused.
- "Detection is wrong": re-run Guided calibration, then "Test detection
  (live)" on the Calibrate tab.
- "App didn't open": launch "Prospector Lite.exe" from the install folder
  to see any error text, and check that antivirus didn't quarantine it.

Prospector Lite is not affiliated with or endorsed by Roblox or the makers
of Prospecting. Automating gameplay may violate the game's Terms of
Service — use at your own risk.
