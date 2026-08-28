# Clean Windows build. Host-guarded, isolated environment, hashed lock, verified.
#
# NATIVE STATUS: this script has not been executed. No Windows bundle was
# produced during implementation, and no code in prospector_engine/platform_win.py
# has ever run on Windows (STATUS.md, phase 6; DECISIONS.md D-009).
$ErrorActionPreference = "Stop"

if (-not $IsWindows) {
    Write-Error "build_windows.ps1 must run on Windows."
    exit 2
}

$Root  = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Build = Join-Path $Root "packaging\build"
$Dist  = Join-Path $Root "packaging\dist"

# An explicit, verified interpreter - never whatever `python` happens to be.
$Python = if ($env:TREASURE_PYTHON) { $env:TREASURE_PYTHON } else { "py -3.13" }

Write-Host "==> Clean"
Remove-Item -Recurse -Force $Build, $Dist -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $Build, $Dist | Out-Null

Write-Host "==> Isolated build environment"
Invoke-Expression "$Python -m venv `"$Build\venv`""
$VenvPy = Join-Path $Build "venv\Scripts\python.exe"

Write-Host "==> Install the hashed platform lock"
$Lock = Join-Path $Root "requirements-windows.lock"
if (-not (Test-Path $Lock)) {
    Write-Error "requirements-windows.lock is missing; refusing to build from unpinned deps."
    exit 2
}
# The committed file is a documented placeholder until it is generated ON
# Windows (DECISIONS.md D-009). A lock with no pinned requirement is not a lock.
$Pinned = Select-String -Path $Lock -Pattern '^\S+==' -Quiet
if (-not $Pinned) {
    Write-Error "requirements-windows.lock contains no pinned requirements. Generate it on Windows first - see the header of that file."
    exit 2
}
& $VenvPy -m pip install --quiet --require-hashes -r $Lock
& $VenvPy -m pip install --quiet --no-deps -e $Root
& $VenvPy -m pip install --quiet pyinstaller

Write-Host "==> Probe the native stack before freezing"
& $VenvPy -c "import tkinter, _tkinter, cv2, mss, numpy, PIL; print('  tk', tkinter.TkVersion, 'cv2', cv2.__version__)"

Write-Host "==> Local gates"
Push-Location $Root
& $VenvPy -m pytest -q
Pop-Location

Write-Host "==> Freeze"
Push-Location $Root
& $VenvPy -m PyInstaller --noconfirm --clean --distpath $Dist --workpath (Join-Path $Build "work") "packaging\treasure.spec"
Pop-Location

Write-Host "==> Verify the bundle"
& $VenvPy (Join-Path $Root "packaging\verify_bundle.py") (Join-Path $Dist "TreasureNavigator") --report (Join-Path $Dist "verification.json")

Write-Host ""
Write-Host "Build complete: $Dist"
Write-Host "Authenticode signing is NOT performed here and remains PENDING."
Write-Host "Native game gates remain PENDING; see STATUS.md."
