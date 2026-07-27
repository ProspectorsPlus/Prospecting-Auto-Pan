#!/usr/bin/env python3
"""Regenerate windows/prospecting_app.py from the root prospecting_app.py.

The two copies are byte-identical EXCEPT for two platform-variant blocks
(the primary-screen size lookup and its fallback). Root is authoritative;
run this after any root edit, then `python3 tour_check.py` verifies the
lockstep. Aborts loudly if the variant anchors ever drift, so a refactor
cannot silently produce a broken Windows copy.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

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


if __name__ == "__main__":
    main()
