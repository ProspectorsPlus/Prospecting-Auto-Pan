@echo off
REM ===================================================================
REM  Build "Prospectors Plus Setup.exe" on a Windows machine.
REM  Requires: Python 3 (py launcher) and Inno Setup 6 (for the installer).
REM  Run from this folder:  build.bat
REM ===================================================================
setlocal
cd /d "%~dp0"

echo [1/4] Installing build dependencies...
py -m pip install --upgrade pip                                  >nul
py -m pip install pyinstaller pywebview pythonnet mss numpy pillow || goto :err

echo [2/4] Making icon.ico from icon.png (if needed)...
if not exist icon.ico py -c "from PIL import Image;Image.open('icon.png').convert('RGBA').save('icon.ico',sizes=[(16,16),(32,32),(48,48),(64,64),(128,128),(256,256)])"

echo [3/4] Building the app with PyInstaller...
rmdir /s /q build dist 2>nul
py -m PyInstaller --noconfirm prospecting.spec || goto :err

echo [4/4] Building the installer with Inno Setup...
set ISCC="C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
if not exist %ISCC% set ISCC="C:\Program Files\Inno Setup 6\ISCC.exe"
if exist %ISCC% (
    %ISCC% installer.iss || goto :err
    echo.
    echo DONE. Installer is in:  Output\Prospectors Plus Setup.exe
) else (
    echo.
    echo PyInstaller build is in:  dist\Prospectors Plus\
    echo Inno Setup not found - install it from https://jrsoftware.org/isdl.php
    echo then re-run, or zip the dist folder as-is.
)
goto :eof

:err
echo.
echo BUILD FAILED - see the error above.
exit /b 1
