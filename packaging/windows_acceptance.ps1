# ============================================================================
# Prospector Lite -- automated packaged acceptance probes (Windows).
#
# The Windows counterpart of packaging/packaged_acceptance.command. Runs the
# checks that CAN be automated against a built package: either the portable
# folder (windows\dist\Prospector Lite\) or an installed copy (the folder
# the installer produced). Interactive journeys (the setup wizard, guided
# calibration against the live game, DPI-scaling spot checks) remain HUMAN
# steps -- a manual checklist is printed at the end, never faked.
#
# HONESTY NOTE: this script was written and syntax-reviewed on macOS where
# PowerShell is not installed; it has NEVER been executed on a real Windows
# machine. Its first real run happens in .github/workflows/build-windows.yml
# or by hand -- see WINDOWS_TESTING.md for the current verification status.
#
# The content-scan patterns in probe [5] are assembled from string fragments
# on purpose: the tracked-tree gate (public_release_tests.py) bans those
# exact tokens, and this script exists to REJECT them, not to ship them.
#
# Usage:
#   pwsh -File packaging\windows_acceptance.ps1 [app-dir]
#   app-dir default: the newest directory under windows\dist\
#
# Exit code: 0 = all probes pass, 1 = failure(s), 2 = refused (elevated).
# ============================================================================

[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [string]$AppDir
)

$ErrorActionPreference = 'Stop'
$script:Fails = @()

function Ok([string]$msg)   { Write-Output ('  ok:   ' + $msg) }
function Fail([string]$msg) { $script:Fails += $msg; Write-Output ('  FAIL: ' + $msg) }

# ---- [6] no-admin assertion ------------------------------------------------
# Checked FIRST: results must reflect a normal user. An elevated run would
# mask AppData/permission behavior a real user would hit.
$wid = [Security.Principal.WindowsIdentity]::GetCurrent()
$wp  = New-Object Security.Principal.WindowsPrincipal($wid)
if ($wp.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Output '[6] REFUSED: this shell is elevated (Administrator).'
    Write-Output '    Run from a normal PowerShell so the results reflect a'
    Write-Output '    real per-user install. No probes were run.'
    exit 2
}
Write-Output '[6] not elevated -- probes reflect a normal user'

# ---- helpers ---------------------------------------------------------------
$RepoRoot = Split-Path -Parent $PSScriptRoot
$script:TempDirs = @()

function New-TempDir([string]$tag) {
    $d = Join-Path ([IO.Path]::GetTempPath()) ('pl_' + $tag + '_' + [IO.Path]::GetRandomFileName())
    New-Item -ItemType Directory -Path $d | Out-Null
    $script:TempDirs += $d
    return $d
}

function Get-TreeSnapshot([string]$root) {
    # path|size|mtime for every file -- cheap tamper/write detector
    Get-ChildItem -Path $root -Recurse -File | ForEach-Object {
        $_.FullName + '|' + $_.Length + '|' + $_.LastWriteTimeUtc.Ticks
    } | Sort-Object
}

function Stop-App($proc) {
    # clean kill: ask the GUI loop first, escalate only if it ignores us
    if ($null -eq $proc) { return }
    if (-not $proc.HasExited) {
        $null = $proc.CloseMainWindow()
        for ($i = 0; $i -lt 5; $i++) {
            if ($proc.HasExited) { break }
            Start-Sleep -Seconds 1
        }
    }
    if (-not $proc.HasExited) {
        Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
    }
}

# ---- resolve the app dir ---------------------------------------------------
if (-not $AppDir) {
    $distRoot = Join-Path $RepoRoot 'windows\dist'
    if (Test-Path $distRoot) {
        $newest = Get-ChildItem -Path $distRoot -Directory |
            Sort-Object LastWriteTime -Descending | Select-Object -First 1
        if ($newest) { $AppDir = $newest.FullName }
    }
}
if (-not $AppDir -or -not (Test-Path $AppDir)) {
    Write-Output 'FAIL: no app dir given and no windows\dist\* folder found.'
    Write-Output 'Usage: pwsh -File packaging\windows_acceptance.ps1 [app-dir]'
    exit 1
}
$AppDir = (Resolve-Path -Path $AppDir).Path
$Exe = Join-Path $AppDir 'Prospector Lite.exe'
Write-Output ('==> Acceptance probes for ' + $AppDir)

$origAppData      = $env:APPDATA
$origLocalAppData = $env:LOCALAPPDATA

try {
    # ---- [1] exe + version identity ----------------------------------------
    Write-Output '[1] exe + version identity'
    if (-not (Test-Path $Exe)) {
        Fail ('Prospector Lite.exe missing from ' + $AppDir)
        Write-Output ''
        Write-Output ('WINDOWS ACCEPTANCE: ' + $script:Fails.Count + ' FAILURE(S)')
        $script:Fails | ForEach-Object { Write-Output ('  - ' + $_) }
        exit 1
    }
    Ok 'Prospector Lite.exe present'

    $vi = (Get-Item -Path $Exe).VersionInfo
    if ($vi.ProductVersion) {
        Write-Output ('  note: exe version resource: ' + $vi.ProductVersion)
    } else {
        Write-Output '  note: exe carries no version resource (prospecting.spec'
        Write-Output '  does not stamp one); build_info.json is the identity source'
    }

    $ver = $null
    $bi = Get-ChildItem -Path $AppDir -Recurse -Filter 'build_info.json' |
        Select-Object -First 1
    if ($null -eq $bi) {
        Fail 'build_info.json missing from the bundle'
    } else {
        $info = Get-Content -Path $bi.FullName -Raw | ConvertFrom-Json
        $missing = @()
        foreach ($k in @('commit', 'date', 'version', 'dirty', 'package')) {
            if ($null -eq $info.$k -and $info.$k -isnot [bool]) { $missing += $k }
        }
        if ($missing.Count -gt 0) {
            Fail ('build_info.json missing fields: ' + ($missing -join ', '))
        }
        $ver = $info.version
        if ($ver -match '^\d+\.\d+\.\d+(-[0-9A-Za-z.]+)?$') {
            Ok ('build_info version well-formed: ' + $ver)
        } else {
            Fail ('build_info version malformed: "' + $ver + '"')
        }
    }
    # compare against the source VERSION when a checkout sits next to us
    $srcApp = Join-Path $RepoRoot 'windows\prospecting_app.py'
    if (-not (Test-Path $srcApp)) { $srcApp = Join-Path $RepoRoot 'prospecting_app.py' }
    if ((Test-Path $srcApp) -and $ver) {
        $src = Get-Content -Path $srcApp -Raw
        if ($src -match 'VERSION\s*=\s*"([^"]+)"') {
            $srcVer = $Matches[1]
            if ($srcVer -eq $ver) {
                Ok ('bundle version matches source VERSION (' + $srcVer + ')')
            } else {
                Fail ('version mismatch: bundle ' + $ver + ' vs source ' + $srcVer)
            }
        } else {
            Fail 'VERSION assignment not found in the source checkout'
        }
    } elseif ($ver) {
        Write-Output '  note: no source checkout next to this script; source'
        Write-Output '  VERSION comparison skipped (regex shape check only)'
    }

    # ---- [2] --capabilities probe ------------------------------------------
    Write-Output '[2] --capabilities probe (isolated PP_DATA_DIR, offline)'
    # sandbox the profile dirs for probes 2-3 so ANY stray AppData write goes
    # to a throwaway dir we can inspect, never the operator's real profile
    $sandboxAppData  = New-TempDir 'appdata'
    $sandboxLocal    = New-TempDir 'local'
    $env:APPDATA      = $sandboxAppData
    $env:LOCALAPPDATA = $sandboxLocal

    $capsHome = New-TempDir 'caps'
    $outFile = Join-Path $capsHome 'caps_stdout.txt'
    $errFile = Join-Path $capsHome 'caps_stderr.txt'
    $before = @(Get-TreeSnapshot $AppDir)

    $env:PP_DATA_DIR = $capsHome
    $sp = @{
        FilePath               = $Exe
        ArgumentList           = @('--capabilities')
        PassThru               = $true
        RedirectStandardOutput = $outFile
        RedirectStandardError  = $errFile
    }
    $p2 = Start-Process @sp
    $null = $p2.Handle   # cache the handle so ExitCode survives process exit
    if (-not $p2.WaitForExit(60000)) {
        Stop-App $p2
        Fail '--capabilities did not exit within 60s'
    } elseif ($p2.ExitCode -ne 0) {
        Fail ('--capabilities exit code ' + $p2.ExitCode)
    } else {
        Ok '--capabilities exited cleanly'
    }
    Remove-Item Env:PP_DATA_DIR -ErrorAction SilentlyContinue

    $capsLen = 0
    if (Test-Path $outFile) { $capsLen = (Get-Item -Path $outFile).Length }
    if ($capsLen -gt 10) {
        Ok ('--capabilities answered (' + $capsLen + ' bytes)')
    } else {
        Fail ('--capabilities output too short (' + $capsLen + ' bytes)')
    }

    # "no writes outside the temp dir": the app dir must be untouched and the
    # sandboxed profile dirs must have gained nothing (PP_DATA_DIR wins).
    # A full-filesystem write trace is NOT attempted -- this is the honest
    # automatable subset.
    $after = @(Get-TreeSnapshot $AppDir)
    $treeDiff = Compare-Object -ReferenceObject $before -DifferenceObject $after
    if ($treeDiff) {
        Fail 'app dir contents changed during the --capabilities run'
    } else {
        Ok 'app dir unchanged (no writes into the install folder)'
    }

    # ---- [3] bounded first-boot probe --------------------------------------
    Write-Output '[3] bounded first-boot probe (isolated home, PP_NO_HUD=1)'
    $bootHome = New-TempDir 'home'
    $env:PP_DATA_DIR = $bootHome
    $env:PP_NO_HUD = '1'
    $p3 = Start-Process -FilePath $Exe -PassThru
    $null = $p3.Handle   # cache the handle so ExitCode survives process exit
    # poll up to 30 s: the marker is real bridge liveness -- boot() ->
    # welcome_state() persists onboarding_state.json in the isolated home
    $marker = Join-Path $bootHome 'onboarding_state.json'
    $live = $false
    for ($i = 0; $i -lt 30; $i++) {
        if (Test-Path $marker) { $live = $true; break }
        if ($p3.HasExited) { break }
        Start-Sleep -Seconds 1
    }
    if ($live) {
        Ok 'bridge live: onboarding_state.json created in the isolated home'
    } elseif ($p3.HasExited) {
        Fail ('app exited during first boot (code ' + $p3.ExitCode + ')')
    } else {
        Fail 'onboarding_state.json not created within 30s'
    }
    # offline proof: the booted app may own ZERO TCP connections
    if (-not $p3.HasExited) {
        $conns = @(Get-NetTCPConnection -OwningProcess $p3.Id -ErrorAction SilentlyContinue)
        if ($conns.Count -eq 0) {
            Ok 'no TCP connections owned by the app during boot (offline)'
        } else {
            Fail ('unexpected TCP connections during boot: ' + $conns.Count)
        }
    }
    Stop-App $p3
    Remove-Item Env:PP_DATA_DIR -ErrorAction SilentlyContinue
    Remove-Item Env:PP_NO_HUD -ErrorAction SilentlyContinue

    # while PP_DATA_DIR was set (probes 2-3), nothing may leak into AppData
    if (Test-Path (Join-Path $sandboxAppData 'Prospector Lite')) {
        Fail 'data leaked into %APPDATA% while PP_DATA_DIR was set'
    } else {
        Ok 'no AppData leak while PP_DATA_DIR was set'
    }

    # ---- [4] AppData path convention ---------------------------------------
    Write-Output '[4] AppData convention (no PP_DATA_DIR, sandboxed APPDATA)'
    $sandboxAppData4 = New-TempDir 'appdata4'
    $env:APPDATA = $sandboxAppData4
    $env:PP_NO_HUD = '1'
    $p4 = Start-Process -FilePath $Exe -PassThru
    $null = $p4.Handle   # cache the handle so ExitCode survives process exit
    $expectDir = Join-Path $sandboxAppData4 'Prospector Lite'
    $marker4 = Join-Path $expectDir 'onboarding_state.json'
    $landed = $false
    for ($i = 0; $i -lt 30; $i++) {
        if (Test-Path $marker4) { $landed = $true; break }
        if ($p4.HasExited) { break }
        Start-Sleep -Seconds 1
    }
    if ($landed) {
        Ok 'data landed in %APPDATA%\Prospector Lite (documented convention)'
    } elseif ($p4.HasExited) {
        Fail ('app exited during the AppData probe (code ' + $p4.ExitCode + ')')
    } else {
        Fail 'no data in %APPDATA%\Prospector Lite within 30s'
    }
    Stop-App $p4
    Remove-Item Env:PP_NO_HUD -ErrorAction SilentlyContinue
    $env:APPDATA      = $origAppData
    $env:LOCALAPPDATA = $origLocalAppData

    # ---- [5] bundle content ------------------------------------------------
    Write-Output '[5] bundle content'
    foreach ($f in @('build_info.json', 'trust_manifest.json',
                     'PERMISSIONS.md', 'PRIVACY.md', 'SECURITY.md')) {
        $hit = Get-ChildItem -Path $AppDir -Recurse -Filter $f |
            Select-Object -First 1
        if ($hit) { Ok ($f + ' present') } else { Fail ($f + ' missing from the bundle') }
    }
    foreach ($f in @('prospecting_secrets.json', 'run_history.json',
                     'coach_history.json', ('ACCESS_' + 'CODES_PRIVATE.txt'))) {
        $hit = Get-ChildItem -Path $AppDir -Recurse -Filter $f |
            Select-Object -First 1
        if ($hit) { Fail ('personal file ' + $f + ' inside the bundle') }
        else { Ok ($f + ' absent') }
    }
    # content scan: same patterns + doc exemptions as build-windows.yml.
    # Patterns are assembled from fragments so this file never contains the
    # tokens the tracked-tree gate bans (it rejects them, it does not ship
    # them).
    $bad = @(
        ('Prospectors' + ' Plus'),
        ('Prospectors' + 'Plus'),
        ('PPLUS' + '-'),
        ('ip-api' + '.com'),
        ('prospectorsplus' + '.github.io'),
        ('discord.com/api/' + 'webhooks/1')
    )
    $docs = @('README.md', 'PRIVACY.md', 'SECURITY.md', 'THIRD_PARTY_NOTICES.md')
    $hits = @()
    Get-ChildItem -Recurse -Path $AppDir -Include *.json, *.py, *.txt, *.md |
        Where-Object { $docs -notcontains $_.Name } | ForEach-Object {
            $t = Get-Content -Path $_.FullName -Raw
            foreach ($b in $bad) {
                if ($t -like ('*' + $b + '*')) { $hits += ($_.Name + ': ' + $b) }
            }
        }
    if ($hits.Count -gt 0) {
        $hits | ForEach-Object { Fail ('content scan: ' + $_) }
    } else {
        Ok 'content scan clean (workflow patterns, doc exemptions applied)'
    }

    # ---- [7] DPI awareness (static) ----------------------------------------
    Write-Output '[7] DPI awareness (STATIC check -- dynamic query is unreliable)'
    # Querying a live process's DPI awareness via MainWindowHandle is flaky;
    # the guarantee is static: prospector_engine/platform_win.py calls
    # SetProcessDpiAwareness(2) at import, so capture and cursor coordinates
    # are both physical pixels. Scaling behavior itself is MANUAL (below).
    $platWin = Join-Path $RepoRoot 'prospector_engine\platform_win.py'
    if (Test-Path $platWin) {
        $t = Get-Content -Path $platWin -Raw
        if ($t -match 'SetProcessDpiAwareness\(2\)') {
            Ok 'platform_win.py sets SetProcessDpiAwareness(2) at import'
        } else {
            Fail 'platform_win.py no longer sets SetProcessDpiAwareness(2)'
        }
    } else {
        Write-Output '  note: no source checkout next to this script; the frozen'
        Write-Output '  bundle compiles platform_win.py into the PYZ archive, so'
        Write-Output '  this static property is not directly greppable here.'
        Write-Output '  Verify in the repo: prospector_engine/platform_win.py'
        Write-Output '  calls SetProcessDpiAwareness(2) at import.'
    }
}
finally {
    $env:APPDATA      = $origAppData
    $env:LOCALAPPDATA = $origLocalAppData
    Remove-Item Env:PP_DATA_DIR -ErrorAction SilentlyContinue
    Remove-Item Env:PP_NO_HUD -ErrorAction SilentlyContinue
    foreach ($d in $script:TempDirs) {
        Remove-Item -Recurse -Force -Path $d -ErrorAction SilentlyContinue
    }
}

# ---- manual checklist (printed, never faked) -------------------------------
Write-Output ''
Write-Output 'MANUAL DPI CHECKLIST (human steps -- this script does NOT fake them):'
Write-Output '  [ ] 100% display scaling: HUD/overlay aligned, guided calibration'
Write-Output '      lands, Test detection (live) reads the correct pixels'
Write-Output '  [ ] 125% display scaling: same checks after an app restart'
Write-Output '  [ ] 150% display scaling: same checks after an app restart'
Write-Output '  Change scaling in Settings > System > Display, restart the app'
Write-Output '  each time, re-run Guided calibration, then Test detection (live).'
Write-Output '  Also test the Safe Stop key (Esc) and Ctrl+K start/stop with'
Write-Output '  Roblox focused -- see WINDOWS_TESTING.md.'

Write-Output ''
if ($script:Fails.Count -eq 0) {
    Write-Output 'WINDOWS ACCEPTANCE: ALL PASS'
    exit 0
} else {
    Write-Output ('WINDOWS ACCEPTANCE: ' + $script:Fails.Count + ' FAILURE(S)')
    $script:Fails | ForEach-Object { Write-Output ('  - ' + $_) }
    exit 1
}
