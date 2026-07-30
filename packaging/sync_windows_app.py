#!/usr/bin/env python3
"""Regenerate the windows/ copies from their root counterparts.

Two kinds of sync, both with root as the single source of truth:

1. windows/prospecting_app.py -- byte-identical to root EXCEPT for two
   platform-variant blocks (the primary-screen size lookup and its
   fallback), which this script substitutes. Aborts loudly if the variant
   anchors ever drift, so a refactor cannot silently produce a broken
   Windows copy.
2. The hand-synced twins (prospecting_ui.py, prospecting_assistant.py,
   prospecting_old.py, prospecting_prices.json) -- plain verbatim byte
   copies, no platform transforms. Copying them here means one command
   syncs everything; historically they were synced by hand and
   windows/prospecting_ui.py silently missed the atomic config-write fix.

Run after any root edit, then `python3 tour_check.py` verifies the
lockstep (including full-file byte parity for the twins).
"""
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Verbatim byte copies: no platform differences exist in these files.
VERBATIM_TWINS = [
    "prospecting_ui.py",
    "prospecting_assistant.py",
    "prospecting_old.py",
    "prospecting_prices.json",
]

MAC_BLOCK = """        import Quartz as _Q
        _b = _Q.CGDisplayBounds(_Q.CGMainDisplayID())
        _sw, _sh = int(_b.size.width), int(_b.size.height)"""
WIN_BLOCK = """        import ctypes as _ct
        _sw = int(_ct.windll.user32.GetSystemMetrics(0))
        _sh = int(_ct.windll.user32.GetSystemMetrics(1))"""
MAC_DEFAULT = "        _sw, _sh = 1440, 900"
WIN_DEFAULT = "        _sw, _sh = 1920, 1080"


def main():
    src_path = os.path.join(ROOT, "prospecting_app.py")
    dst_path = os.path.join(ROOT, "windows", "prospecting_app.py")
    src = open(src_path, encoding="utf-8").read()
    if src.count(MAC_BLOCK) != 1:
        sys.exit("sync_windows_app: screen-size anchor not found exactly "
                 "once in prospecting_app.py -- update this script's "
                 "MAC_BLOCK to match the refactor")
    if src.count(MAC_DEFAULT) != 1:
        sys.exit("sync_windows_app: fallback-size anchor not found exactly "
                 "once -- update MAC_DEFAULT")
    out = src.replace(MAC_BLOCK, WIN_BLOCK).replace(MAC_DEFAULT, WIN_DEFAULT)
    open(dst_path, "w", encoding="utf-8").write(out)
    print("windows/prospecting_app.py regenerated (%d chars)" % len(out))
    for name in VERBATIM_TWINS:
        src_twin = os.path.join(ROOT, name)
        dst_twin = os.path.join(ROOT, "windows", name)
        if not os.path.isfile(src_twin):
            sys.exit("sync_windows_app: root twin missing: %s" % name)
        shutil.copyfile(src_twin, dst_twin)
        print("windows/%s copied verbatim (%d bytes)"
              % (name, os.path.getsize(dst_twin)))


if __name__ == "__main__":
    main()
