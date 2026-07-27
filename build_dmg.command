#!/bin/bash
# ============================================================================
# Prospector Lite — macOS package builder.
#
# Produces a SELF-CONTAINED "Prospector Lite.app" (PyInstaller bundles Python
# and every library; users install nothing) and wraps it in a drag-to-install
# DMG:   dist/ProspectorLite-<version>-macos-<arch>.dmg
#
# Usage:      ./build_dmg.command
# CI usage:   PYTHON=python ./build_dmg.command   (uses that interpreter's
#             site-packages instead of creating a local build venv)
# Signing:    unsigned by default (ad-hoc signature only). Set CODESIGN_ID to
#             a "Developer ID Application: ..." identity to really sign;
#             notarization is a separate manual step (see RELEASING.md).
#
# No secrets, webhooks or personal configuration are ever bundled: the app
# ships the tracked, sanitized windows/prospecting_config.json as its default
# config (see prospector_lite_mac.spec).
# ============================================================================
set -euo pipefail
cd "$(dirname "$0")"

APPNAME="Prospector Lite"
VERSION="$(python3 -c "import re;print(re.search(r'VERSION\s*=\s*\"([^\"]+)\"',open('prospecting_app.py').read()).group(1))")"
ARCH="$(uname -m)"
DMG="dist/ProspectorLite-${VERSION}-macos-${ARCH}.dmg"

echo "==> Building $APPNAME $VERSION ($ARCH)"
rm -rf "build/$APPNAME" "build/dmgroot" "dist/$APPNAME" "dist/$APPNAME.app" "$DMG"
mkdir -p build dist

# 1) interpreter: an isolated build venv by default, or $PYTHON when set (CI)
if [ -n "${PYTHON:-}" ]; then
  PYB="$PYTHON"
else
  if [ ! -x build/venv/bin/python ]; then
    echo "==> Creating build venv"
    python3 -m venv build/venv
  fi
  build/venv/bin/pip -q install --upgrade pip
  build/venv/bin/pip -q install pyinstaller pywebview pyobjc mss numpy pillow pynput certifi
  PYB=build/venv/bin/python
fi

# 2) build identity stamp (embedded; shown in About, the welcome screen and
#    the Trust Center) + the trust manifest (exact per-capability source
#    references, generated from THIS checkout so code links can never point
#    at a different revision). PP_PROJECT_URL optionally stamps the public
#    repository URL once the owner has published it.
"$PYB" - <<'PY'
import json, subprocess, datetime, os, re
c = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                   text=True).stdout.strip()
dirty = bool(subprocess.run(["git", "status", "--porcelain",
                             "--untracked-files=no"], capture_output=True,
                            text=True).stdout.strip())
ver = re.search(r'VERSION\s*=\s*"([^"]+)"',
                open("prospecting_app.py", encoding="utf-8").read()).group(1)
os.makedirs("build", exist_ok=True)
json.dump({"commit": c, "date": datetime.date.today().isoformat(),
           "version": ver, "dirty": dirty, "package": "dmg",
           "project_url": os.environ.get("PP_PROJECT_URL", ""),
           "signed": bool(os.environ.get("CODESIGN_ID")),
           "notarized": False},
          open("build/build_info.json", "w"))
if dirty:
    print("   NOTE: tree is dirty -- this build is marked development")
PY
"$PYB" lite_trust.py --emit build/trust_manifest.json

# 3) icon: icon.icns from icon.png
if [ -f icon.png ] && [ ! -f build/icon.icns ]; then
  rm -rf build/icon.iconset && mkdir -p build/icon.iconset
  for s in 16 32 64 128 256 512; do
    sips -z $s $s icon.png --out "build/icon.iconset/icon_${s}x${s}.png" >/dev/null 2>&1 || true
    d=$((s*2)); sips -z $d $d icon.png --out "build/icon.iconset/icon_${s}x${s}@2x.png" >/dev/null 2>&1 || true
  done
  iconutil -c icns build/icon.iconset -o build/icon.icns 2>/dev/null || true
fi

# 4) the self-contained .app. SOURCE_DATE_EPOCH pins the commit time into
#    every archive timestamp PyInstaller writes, so two builds of the same
#    commit produce byte-identical bundle content (see REPRODUCIBLE_BUILDS).
export SOURCE_DATE_EPOCH="$(git log -1 --format=%ct 2>/dev/null || echo 0)"
"$PYB" -m PyInstaller --noconfirm prospector_lite_mac.spec

# 5) sign: real identity when provided, ad-hoc otherwise (arm64 requires a
#    signature to run at all; ad-hoc still means right-click > Open once).
#    Real signing uses the hardened runtime + the minimal tracked
#    entitlements (packaging/entitlements.plist -- no permission
#    entitlements). Notarization + stapling are separate manual steps, see
#    RELEASING.md; credentials are never read from the repository.
if [ -n "${CODESIGN_ID:-}" ]; then
  echo "==> Signing with $CODESIGN_ID (hardened runtime)"
  codesign --force --deep --options runtime \
    --entitlements packaging/entitlements.plist \
    --sign "$CODESIGN_ID" "dist/$APPNAME.app"
  codesign --verify --deep --strict "dist/$APPNAME.app"
  spctl --assess --type execute "dist/$APPNAME.app" \
    || echo "   (spctl assessment pending notarization -- expected before notarize/staple)"
else
  codesign --force --deep --sign - "dist/$APPNAME.app" 2>/dev/null || \
    echo "   (ad-hoc codesign skipped)"
fi

# 6) smoke check: the frozen binary must answer the offline capability probe
OUT="$("dist/$APPNAME.app/Contents/MacOS/$APPNAME" --capabilities)"
[ "${#OUT}" -gt 10 ] || { echo "smoke test FAILED"; exit 1; }
echo "==> Frozen smoke test ok (--capabilities answered)"

# 7) drag-install DMG
mkdir -p build/dmgroot
cp -R "dist/$APPNAME.app" build/dmgroot/
ln -sf /Applications build/dmgroot/Applications
hdiutil create -volname "$APPNAME $VERSION" -srcfolder build/dmgroot \
  -ov -format UDZO "$DMG" >/dev/null

shasum -a 256 "$DMG"
echo "==> Done: $DMG"
