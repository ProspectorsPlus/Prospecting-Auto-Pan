PROSPECTORS PLUS  —  Windows
============================

A Roblox "Prospecting" auto-pan macro. Same macro and UI as the macOS version,
ported to Windows.

------------------------------------------------------------
QUICK START
------------------------------------------------------------
1. Double-click   Install.bat
      - Installs Python (if you don't have it) and the needed packages.
      - Wait for it to say "Done!". You only do this ONCE.
      - (If Windows SmartScreen warns, click "More info" -> "Run anyway".)

2. Double-click   "Prospectors Plus.bat"
      - Opens the app window (settings + Start/Stop + live trace).

3. In the app:
      - Set up your settings / pick a preset (v1 or v2).
      - Click "Start macro".
      - Tab into Roblox. Press  Ctrl+K  to start/stop, and  Esc  to quit.

------------------------------------------------------------
IMPORTANT: CALIBRATION (screens differ!)
------------------------------------------------------------
The macro reads specific pixels on the screen (the capacity bar, the prompt
text, the green dig pixel). Those coordinates are set for a particular screen
resolution. If detection seems off on your PC:

  - Open the "Calibrate" tab in the app. Hover the spots it asks about and read
    the PIXEL=(x, y) values.
  - Then update the matching coordinates near the top of  prospecting_old.py
    (CAP_FULL_PIXEL, DEPOSIT_PIX, PAN_PIX, SHAKE_PIX, DIG_TRIGGER_PIXEL, etc.)
    with your values, and save.

Tip: running at the same resolution/full-screen as the person who set it up
means little or no calibration is needed.

------------------------------------------------------------
HOTKEYS
------------------------------------------------------------
  Ctrl + K   start / stop the macro
  Esc        quit the macro

------------------------------------------------------------
TROUBLESHOOTING
------------------------------------------------------------
- "Nothing happens in game": run Roblox in the SAME monitor, and make sure the
  Roblox window is focused. Try running this as Administrator if inputs are
  ignored (some setups need it for the game to accept synthetic input).
- "App didn't open": run Install.bat again; if it still fails, open a terminal
  in this folder and run:  python prospecting_app.py
- "Detection is wrong": see CALIBRATION above.

This is for personal use. Automating gameplay may violate Roblox's Terms of
Service — use at your own risk, ideally on an alt account.
