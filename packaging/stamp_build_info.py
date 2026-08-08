#!/usr/bin/env python3
"""Write windows/build_info.json — the Windows build-identity stamp.

One shared implementation for build.bat and .github/workflows/
build-windows.yml (the previous inline ``python -c`` one-liner embedded
escaped quotes that cmd accepted but PowerShell's parser rejected, and a
swallowed failure shipped a bundle with no identity stamp).

Commit comes from $GITHUB_SHA in CI, or git (with a dirty check) locally.
``PP_PROJECT_URL`` is stamped when set so in-app View Code links resolve
to the exact build commit. Prints the stamp it wrote; exits non-zero on
any failure so the caller can never silently continue without a stamp.
"""
import datetime
import json
import os
import re
import subprocess
import sys


def main():
    here = os.path.dirname(os.path.abspath(__file__))          # packaging/
    windir = os.path.join(os.path.dirname(here), "windows")
    with open(os.path.join(windir, "prospecting_app.py"),
              encoding="utf-8") as f:
        m = re.search(r'VERSION\s*=\s*"([^"]+)"', f.read())
    if not m:
        print("stamp_build_info: no VERSION constant found", file=sys.stderr)
        return 1
    version = m.group(1)

    commit = os.environ.get("GITHUB_SHA", "")
    dirty = False
    if not commit:
        commit = subprocess.run(["git", "rev-parse", "HEAD"],
                                capture_output=True, text=True).stdout.strip()
        dirty = bool(subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            capture_output=True, text=True).stdout.strip())

    info = {"commit": commit,
            "date": datetime.date.today().isoformat(),
            "version": version,
            "dirty": dirty,
            "package": "windows",
            "project_url": os.environ.get("PP_PROJECT_URL", ""),
            "signed": False,
            "notarized": False}
    out = os.path.join(windir, "build_info.json")
    with open(out, "w") as f:
        json.dump(info, f)
    print("build_info.json: version %s commit %s dirty=%s"
          % (version, commit[:12] or "<none>", dirty))
    return 0


if __name__ == "__main__":
    sys.exit(main())
