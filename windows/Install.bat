@echo off
cd /d "%~dp0"
echo ============================================
echo    Prospectors Plus  -  Installer
echo ============================================
echo This installs Python (if needed) and the packages the macro uses,
echo then opens the app. You only need to run this once.
echo.

REM --- find a working Python (the "py" launcher avoids the Store stub) ---
set "PYEXE="
py -3 -c "import sys" >nul 2>&1 && set "PYEXE=py -3"
if not defined PYEXE python -c "import sys" >nul 2>&1 && set "PYEXE=python"
if defined PYEXE goto havePy

echo Python not found. Installing Python 3.12 ...
where winget >nul 2>&1
if errorlevel 1 goto dlPy
winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements
goto findPy

:dlPy
echo Downloading Python from python.org ...
powershell -Command "try{Invoke-WebRequest 'https://www.python.org/ftp/python/3.12.7/python-3.12.7-amd64.exe' -OutFile '%TEMP%\pp_py.exe'}catch{exit 1}"
if errorlevel 1 (
  echo.
  echo Could not download Python. Install Python 3.12 from the Microsoft Store
  echo ^(open Store, search "Python 3.12", click Get^), then run Install.bat again.
  pause & exit /b 1
)
"%TEMP%\pp_py.exe" /quiet InstallAllUsers=0 PrependPath=1 Include_pip=1 Include_launcher=1

:findPy
REM PATH isn't refreshed in this window after install, so use the py launcher
REM that the installer drops in C:\Windows, or a known install folder.
if exist "%SystemRoot%\py.exe" set "PYEXE=%SystemRoot%\py.exe -3"
if not defined PYEXE if exist "%LocalAppData%\Programs\Python\Python312\python.exe" set "PYEXE=%LocalAppData%\Programs\Python\Python312\python.exe"
if not defined PYEXE if exist "%ProgramFiles%\Python312\python.exe" set "PYEXE=%ProgramFiles%\Python312\python.exe"

:havePy
if not defined PYEXE (
  echo.
  echo Python was installed but this window can't see it yet.
  echo Please RESTART your PC, then run Install.bat again.
  pause & exit /b 1
)

echo.
echo Using Python: %PYEXE%
echo Installing packages: mss numpy pywebview pythonnet ...
%PYEXE% -m pip install --upgrade pip
%PYEXE% -m pip install mss numpy pywebview pythonnet
if errorlevel 1 (
  echo.
  echo Package install failed. Run Install.bat again, or run this yourself:
  echo    %PYEXE% -m pip install mss numpy pywebview pythonnet
  pause & exit /b 1
)

echo.
echo ============================================
echo    Done!  Opening Prospectors Plus ...
echo ============================================
start "" "%~dp0Prospectors Plus.bat"
exit /b 0
