#!/usr/bin/env bash
# Clean macOS build. Host-guarded, isolated environment, hashed lock, verified.
#
# NATIVE STATUS: this script has not been executed. No macOS bundle was
# produced during implementation (STATUS.md, phase 6).
set -euo pipefail

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "build_macos.sh must run on macOS (found $(uname -s))." >&2
  exit 2
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD="${ROOT}/packaging/build"
DIST="${ROOT}/packaging/dist"

# An explicit, verified interpreter. Never a bare `python3`: the ambient
# interpreter on this machine is 3.8 and is unsupported (plan 17).
PYTHON="${TREASURE_PYTHON:-python3.13}"
if ! command -v "${PYTHON}" >/dev/null 2>&1; then
  echo "Interpreter ${PYTHON} not found. Set TREASURE_PYTHON to a verified 3.13+." >&2
  exit 2
fi

echo "==> Clean"
rm -rf "${BUILD}" "${DIST}"
mkdir -p "${BUILD}" "${DIST}"

echo "==> Isolated build environment"
"${PYTHON}" -m venv "${BUILD}/venv"
VENV_PY="${BUILD}/venv/bin/python"

echo "==> Install the hashed platform lock"
if [[ -f "${ROOT}/requirements-macos.lock" ]]; then
  "${VENV_PY}" -m pip install --quiet --require-hashes -r "${ROOT}/requirements-macos.lock"
else
  echo "requirements-macos.lock is missing; refusing to build from unpinned deps." >&2
  exit 2
fi
"${VENV_PY}" -m pip install --quiet --no-deps -e "${ROOT}"
"${VENV_PY}" -m pip install --quiet pyinstaller

echo "==> Probe the native stack before freezing"
"${VENV_PY}" - <<'PY'
import tkinter, _tkinter, cv2, mss, numpy, PIL, Quartz
print(f"  tk {tkinter.TkVersion}  cv2 {cv2.__version__}  numpy {numpy.__version__}")
PY

echo "==> Local gates"
(cd "${ROOT}" && "${VENV_PY}" -m pytest -q)

echo "==> Freeze"
(cd "${ROOT}" && "${VENV_PY}" -m PyInstaller \
  --noconfirm --clean \
  --distpath "${DIST}" --workpath "${BUILD}/work" \
  packaging/treasure.spec)

echo "==> Verify the bundle"
"${VENV_PY}" "${ROOT}/packaging/verify_bundle.py" \
  "${DIST}/Treasure Navigator.app" \
  --report "${DIST}/verification.json"

echo
echo "Build complete: ${DIST}"
echo "Signing and notarization are NOT performed here and remain PENDING."
echo "Native game gates remain PENDING; see STATUS.md."
