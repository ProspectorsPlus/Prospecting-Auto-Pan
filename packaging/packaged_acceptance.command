#!/bin/bash
# ============================================================================
# Prospector Lite — automated packaged acceptance probes (macOS).
#
# Runs the checks that CAN be automated against the built DMG:
#   1. mount the DMG read-only
#   2. --capabilities probe from the MOUNTED app (self-contained, offline)
#   3. a bounded first-boot probe: launch the mounted app with an isolated
#      PP_DATA_DIR, wait, then verify the JS<->Python bridge came up (the
#      boot sequence calls welcome_state, which creates onboarding_state.json
#      in the isolated home), verify no crash, snapshot open sockets (expect
#      none), then quit it cleanly
#   4. bundle-content checks: trust manifest, permission docs, no personal
#      files, build identity fields
#
# Interactive journeys (clicking through the wizard, granting permissions,
# calibrating against the live game) remain HUMAN steps -- see
# docs/trust-and-onboarding/ACCEPTANCE_MATRIX.md.
#
# Usage: ./packaging/packaged_acceptance.command dist/ProspectorLite-<v>.dmg
# ============================================================================
set -euo pipefail
cd "$(dirname "$0")/.."

DMG="${1:-$(ls -t dist/ProspectorLite-*-macos-*.dmg | head -1)}"
APP="Prospector Lite.app"
BIN="Contents/MacOS/Prospector Lite"
echo "==> Acceptance probes for $DMG"

MNT="$(mktemp -d /tmp/pplite_mnt.XXXXXX)"
HOMEDIR="$(mktemp -d /tmp/pplite_home.XXXXXX)"
hdiutil attach -readonly -nobrowse -mountpoint "$MNT" "$DMG" >/dev/null
trap 'hdiutil detach "$MNT" >/dev/null 2>&1 || true' EXIT

echo "==> [1] mounted read-only at $MNT"

OUT="$("$MNT/$APP/$BIN" --capabilities)"
[ "${#OUT}" -gt 10 ] || { echo "FAIL: --capabilities probe"; exit 1; }
echo "==> [2] --capabilities answered (${#OUT} bytes) from the mounted app"

PP_DATA_DIR="$HOMEDIR" PP_NO_HUD=1 "$MNT/$APP/$BIN" &
PID=$!
# poll up to 30 s: the first launch of a freshly-signed binary from a
# read-only image can take a while (XProtect scan + WebKit first paint)
for _i in $(seq 1 30); do
  [ -f "$HOMEDIR/onboarding_state.json" ] && break
  if ! kill -0 "$PID" 2>/dev/null; then
    echo "FAIL: app exited during first boot"; exit 1
  fi
  sleep 1
done
# -a ANDs the selectors (plain "-p PID -i" would OR them and list every
# socket on the machine). lsof exits 1 when it matches NOTHING -- which is
# the expected zero-sockets case -- so guard it from set -o pipefail.
SOCKS="$( (lsof -a -p "$PID" -i 2>/dev/null || true) | tail -n +2 | wc -l | tr -d ' ')"
if [ -f "$HOMEDIR/onboarding_state.json" ]; then
  echo "==> [3] first boot OK: JS<->Python bridge live (welcome_state ran,"
  echo "        onboarding_state.json created in the isolated home)"
else
  echo "FAIL: onboarding_state.json missing -- boot() never reached the Api"
  kill "$PID" 2>/dev/null || true
  exit 1
fi
echo "==> [3] open network sockets during boot: $SOCKS (expected 0)"
[ "$SOCKS" = "0" ] || { echo "FAIL: unexpected sockets"; kill "$PID"; exit 1; }
# the GUI loop ignores a plain TERM; escalate so the probe never hangs
kill "$PID" 2>/dev/null || true
for _i in 1 2 3 4 5; do
  kill -0 "$PID" 2>/dev/null || break
  sleep 1
done
kill -9 "$PID" 2>/dev/null || true
wait "$PID" 2>/dev/null || true
# nothing may be written into the mounted (read-only) bundle -- guaranteed by
# the read-only mount; the write probe above proves writes went to $HOMEDIR

RES="$MNT/$APP/Contents/Resources"
FRZ="$RES"
[ -d "$RES" ] || FRZ="$MNT/$APP/Contents/Frameworks"
ok=1
for f in trust_manifest.json build_info.json PERMISSIONS.md PRIVACY.md \
         SECURITY.md TRUST_CENTER.md; do
  if ! find "$MNT/$APP" -name "$f" | grep -q .; then
    echo "FAIL: $f missing from the bundle"; ok=0
  fi
done
for bad in prospecting_secrets.json coach_history.json run_history.json \
           ACCESS_CODES_PRIVATE.txt; do
  if find "$MNT/$APP" -name "$bad" | grep -q .; then
    echo "FAIL: personal file $bad inside the bundle"; ok=0
  fi
done
python3 - "$MNT/$APP" <<'PY'
import json, subprocess, sys
app = sys.argv[1]
p = subprocess.run(["find", app, "-name", "build_info.json"],
                   capture_output=True, text=True).stdout.strip().splitlines()
d = json.load(open(p[0]))
missing = [k for k in ("commit", "date", "version", "dirty", "package")
           if k not in d]
assert not missing, "build_info missing fields: %s" % missing
print("==> [4] build identity fields present: v%s @ %s dirty=%s"
      % (d["version"], d["commit"][:12], d["dirty"]))
PY
[ "$ok" = "1" ] || exit 1
echo "==> [4] bundle contents OK"
rm -rf "$HOMEDIR"
echo "==> ACCEPTANCE PROBES: ALL PASS"
