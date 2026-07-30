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
py -m pip install pyinstaller pywebview pythonnet mss numpy pillow certifi || goto :err

echo [2/5] Regenerating icon.ico from icon.png (always, never a stale reuse)...
py -c "from PIL import Image;Image.open('icon.png').convert('RGBA').save('icon.ico',sizes=[(16,16),(24,24),(32,32),(48,48),(64,64),(128,128),(256,256)])" || goto :err

echo [3/5] Stamping build identity + trust manifest...
py -c "import json,subprocess,datetime,os,re;c=subprocess.run(['git','rev-parse','HEAD'],capture_output=True,text=True).stdout.strip();d=bool(subprocess.run(['git','status','--porcelain','--untracked-files=no'],capture_output=True,text=True).stdout.strip());v=re.search(r'VERSION\s*=\s*\"([^\"]+)\"',open('prospecting_app.py',encoding='utf-8').read()).group(1);json.dump({'commit':c,'date':datetime.date.today().isoformat(),'version':v,'dirty':d,'package':'windows','project_url':os.environ.get('PP_PROJECT_URL',''),'signed':False,'notarized':False},open('build_info.json','w'))"
py ..\lite_trust.py --emit trust_manifest.json

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
