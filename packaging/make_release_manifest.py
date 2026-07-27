#!/usr/bin/env python3
"""Build release-manifest.json for a Prospector Lite release directory.

Usage:
    python3 packaging/make_release_manifest.py release/public-candidate

Scans the directory, hashes every artefact, classifies platform/arch from
the filename, and writes release-manifest.json next to them. The website
publishes this file so the download page can show exact versions,
checksums and signing status without hand-editing.

Never uploads anything; purely local.
"""
import hashlib
import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def classify(name):
    n = name.lower()
    plat = ("macos" if "macos" in n else
            "windows" if ("win" in n or n.endswith(".exe")) else "any")
    arch = ("arm64" if "arm64" in n else
            "x64" if ("x64" in n or "win64" in n) else "any")
    kind = ("dmg" if n.endswith(".dmg") else
            "installer" if n.endswith(".exe") else
            "portable" if n.endswith(".zip") and "portable" in n else
            "source" if "source" in n else
            "sbom" if "sbom" in n else
            "checksums" if "sha256" in n else "doc")
    return plat, arch, kind


def main(outdir):
    version = re.search(
        r'VERSION\s*=\s*"([^"]+)"',
        open(os.path.join(ROOT, "prospecting_app.py"),
             encoding="utf-8").read()).group(1)
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT,
                            capture_output=True, text=True).stdout.strip()
    files = []
    for name in sorted(os.listdir(outdir)):
        p = os.path.join(outdir, name)
        if not os.path.isfile(p) or name == "release-manifest.json":
            continue
        plat, arch, kind = classify(name)
        files.append({"name": name, "bytes": os.path.getsize(p),
                      "sha256": sha256(p), "platform": plat,
                      "arch": arch, "kind": kind})
    man = {
        "schema": 1,
        "product": "Prospector Lite",
        "version": version,
        "commit": commit,
        "channel": "release-candidate" if "-rc." in version else "stable",
        "files": files,
        "signing": {
            "macos_signed": False, "macos_notarized": False,
            "windows_authenticode": False,
            "note": "Unsigned builds: verify the SHA-256 checksums before "
                    "opening (see VERIFY_DOWNLOAD.md)."},
        "minimum_os": {"macos": "11.0", "windows": "10 (x64)"},
        "source_repository": os.environ.get("PP_PROJECT_URL", "") or
                             "(publication pending -- see "
                             "LICENSE_CHOICE_REQUIRED.md)",
        "docs": {"privacy": "PRIVACY.md", "security": "SECURITY.md",
                 "permissions": "PERMISSIONS.md",
                 "verify": "VERIFY_DOWNLOAD.md",
                 "changelog": "CHANGELOG.md"},
    }
    out = os.path.join(outdir, "release-manifest.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(man, f, indent=1)
    print("wrote %s (%d files, v%s @ %s)"
          % (out, len(files), version, commit[:12]))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "release/public-candidate")
