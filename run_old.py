#!/usr/bin/env python3
"""
run_old.py — launch the OLD, proven v1.0.0 macro on macOS for local testing.

This is the Mac equivalent of your friend's Windows "Prospectors Plus.bat"
(which just runs `pythonw prospecting_app.py`). It launches the isolated
v1.0.0 app in ./Prospectors_Old_v1.0.0/ WITHOUT touching your latest version.

    python3 run_old.py           # or double-click run_old.command

Your current calibrated pixels were copied into the old config, so it should
see your screen right away. Hotkeys are the old app's own (shown in its window).
"""
import os
import sys
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
OLD_DIR = os.path.join(HERE, "Prospectors_Old_v1.0.0")
APP = os.path.join(OLD_DIR, "prospecting_app.py")

if not os.path.exists(APP):
    sys.exit("Old version not found at %s\n"
             "Re-extract it with:  git archive v1.0.0 | tar -x -C Prospectors_Old_v1.0.0"
             % APP)

print("Launching the OLD v1.0.0 macro from:\n  %s\n(this does NOT affect your latest version)\n" % OLD_DIR)
# run in the old folder so it reads ITS own config/engine, exactly like the .bat
raise SystemExit(subprocess.call([sys.executable, APP], cwd=OLD_DIR))
