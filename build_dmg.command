#!/bin/bash
# ============================================================================
# Prospectors Plus - macOS DMG builder.  Double-click this file (or run it in
# Terminal) ON YOUR MAC to produce a drag-to-install DMG.  It bundles the app
# source INSIDE the .app (Contents/Resources/app), so the installed app works
# from /Applications with no source files lying around.
#
#   Output:  dist/ProspectorsPlus-<version>.dmg
#
# Requires: macOS (hdiutil, sips, iconutil - all built in).  No Xcode needed.
# The end user just needs Python 3 installed; the app installs its Python libs
# on first launch.
# ============================================================================
set -e
cd "$(dirname "$0")"
ROOT="$(pwd)"
VERSION="$(grep -m1 'VERSION *= *"' prospecting_app.py | sed -E 's/.*"([0-9.]+)".*/\1/')"
APPNAME="Prospectors Plus"
STAGE="build/dmg"
APPDIR="$STAGE/$APPNAME.app"

echo "==> Building $APPNAME $VERSION DMG"
rm -rf build/dmg "dist/ProspectorsPlus-$VERSION.dmg"
mkdir -p "$STAGE" dist

# 1) copy the .app template, then bundle the live source into Resources/app
cp -R "$APPNAME.app" "$APPDIR"
mkdir -p "$APPDIR/Contents/Resources/app"
# (prospecting_builds.json is deliberately NOT shipped — it's your personal
#  builds file; Windows builds don't ship it either)
for f in prospecting_app.py prospecting_old.py prospecting_ui.py \
         prospecting_assistant.py prospecting_prices.json icon.png; do
  [ -e "$f" ] && cp "$f" "$APPDIR/Contents/Resources/app/" || true
done
# ship a CLEAN default config (never your personal one with unlock/webhook)
python3 - "$APPDIR/Contents/Resources/app/prospecting_config.json" <<'PY'
import json, sys
# start from the sanitized public build config
base = json.load(open("windows/prospecting_config.json"))
json.dump(base, open(sys.argv[1], "w"), indent=2)
PY
# optional: inject your private analytics webhook from your local secrets file
if [ -f prospecting_secrets.json ]; then
  python3 - "$APPDIR/Contents/Resources/app/prospecting_config.json" <<'PY'
import json, sys
try:
    sec = json.load(open("prospecting_secrets.json"))
    url = sec.get("SYNC_URL", "")
    if url:
        p = sys.argv[1]; d = json.load(open(p)); d["SYNC_URL"] = url
        json.dump(d, open(p, "w"), indent=2)
        print("   (injected SYNC_URL from prospecting_secrets.json)")
except Exception:
    pass
PY
fi

# 2) icon: make Resources/icon.icns from icon.png if it's missing/stale
if [ -f icon.png ]; then
  TMPSET="build/icon.iconset"; rm -rf "$TMPSET"; mkdir -p "$TMPSET"
  for s in 16 32 64 128 256 512; do
    sips -z $s $s icon.png --out "$TMPSET/icon_${s}x${s}.png" >/dev/null 2>&1 || true
    d=$((s*2)); sips -z $d $d icon.png --out "$TMPSET/icon_${s}x${s}@2x.png" >/dev/null 2>&1 || true
  done
  iconutil -c icns "$TMPSET" -o "$APPDIR/Contents/Resources/icon.icns" 2>/dev/null || true
fi

chmod +x "$APPDIR/Contents/MacOS/launch"

# 3) ad-hoc code-sign so Gatekeeper is less aggressive (unsigned still needs a
#    right-click > Open the first time; that's normal for indie apps).
codesign --force --deep --sign - "$APPDIR" 2>/dev/null || \
  echo "   (codesign skipped - app still runs via right-click > Open)"

# 4) lay out the DMG folder with an /Applications shortcut for drag-install
mkdir -p "$STAGE/root"
cp -R "$APPDIR" "$STAGE/root/"
ln -sf /Applications "$STAGE/root/Applications"

# 5) build the compressed DMG
hdiutil create -volname "$APPNAME $VERSION" \
  -srcfolder "$STAGE/root" -ov -format UDZO \
  "dist/ProspectorsPlus-$VERSION.dmg" >/dev/null

echo "==> Done:  dist/ProspectorsPlus-$VERSION.dmg"
echo "    Drag it to test, or attach it to the GitHub Release next to the .exe."
open dist 2>/dev/null || true
