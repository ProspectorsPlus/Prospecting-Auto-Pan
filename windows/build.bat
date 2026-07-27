@echo off
REM ===================================================================
REM  Build "ProspectorLiteSetup.exe" + the portable folder on Windows.
REM  Requires: Python 3.11+ (py launcher) and Inno Setup 6.
REM  Run from this folder:  build.bat
REM ===================================================================
setlocal
cd /d "%~dp0"

echo [1/5] Installing build dependencies...
py -m pip install --upgrade pip                                  >nul
py -m pip install pyinstaller pywebview pythonnet mss numpy pillow || goto :err

echo [2/5] Making icon.ico from icon.png (if needed)...
if not exist icon.ico py -c "from PIL import Image;Image.open('icon.png').convert('RGBA').save('icon.ico',sizes=[(16,16),(32,32),(48,48),(64,64),(128,128),(256,256)])"

echo [3/5] Stamping build identity (commit + date)...
py -c "import json,subprocess,datetime;c=subprocess.run(['git','rev-parse','HEAD'],capture_output=True,text=True).stdout.strip();json.dump({'commit':c,'date':datetime.date.today().isoformat()},open('build_info.json','w'))"

echo [4/5] Building the app with PyInstaller...
rmdir /s /q build dist 2>nul
py -m PyInstaller --noconfirm prospecting.spec || goto :err

echo [5/5] Building the installer with Inno Setup...
set ISCC="C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
if not exist %ISCC% set ISCC="C:\Program Files\Inno Setup 6\ISCC.exe"
if exist %ISCC% (
    %ISCC% installer.iss || goto :err
    echo.
    echo DONE. Installer:      Output\ProspectorLiteSetup.exe
    echo       Portable copy:  dist\Prospector Lite\  (zip it to share)
) else (
    echo.
    echo PyInstaller build is in:  dist\Prospector Lite\
    echo Inno Setup not found - install it from https://jrsoftware.org/isdl.php
    echo then re-run, or zip the dist folder as-is.
)
goto :eof

:err
echo.
echo BUILD FAILED - see the error above.
exit /b 1
