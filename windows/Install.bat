@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"
echo ============================================
echo    Prospectors Plus  -  Installer
echo ============================================
echo This installs Python (if needed) and the packages the macro uses.
echo.

REM --- find an existing Python ---
set "PYCMD="
where py >nul 2>&1 && set "PYCMD=py -3"
if not defined PYCMD ( where python >nul 2>&1 && set "PYCMD=python" )

if not defined PYCMD (
  echo Python was not found. Downloading Python 3.12 ...
  powershell -Command "try{Invoke-WebRequest -Uri 'https://www.python.org/ftp/python/3.12.7/python-3.12.7-amd64.exe' -OutFile '%TEMP%\prospectors_python.exe'}catch{exit 1}"
  if errorlevel 1 (
    echo.
    echo Download failed. Please install Python yourself from python.org
    echo ^(check "Add python.exe to PATH"^), then run Install.bat again.
    pause & exit /b 1
  )
  echo Installing Python ^(this can take a minute^) ...
  "%TEMP%\prospectors_python.exe" /quiet InstallAllUsers=0 PrependPath=1 Include_pip=1 Include_launcher=1
  set "PYCMD=%LocalAppData%\Programs\Python\Python312\python.exe"
)

echo.
echo Installing required packages: mss, numpy, pywebview ...
%PYCMD% -m pip install --upgrade pip
%PYCMD% -m pip install mss numpy pywebview pythonnet
if errorlevel 1 (
  echo.
  echo Something went wrong installing packages. Try running Install.bat again,
  echo or run:  %PYCMD% -m pip install mss numpy pywebview pythonnet
  pause & exit /b 1
)

echo.
echo ============================================
echo    Done!  Double-click  "Prospectors Plus.bat"  to start.
echo ============================================
pause
