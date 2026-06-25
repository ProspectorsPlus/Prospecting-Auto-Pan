@echo off
cd /d "%~dp0"
REM Launch the app window with NO console. The py/pyw launcher in C:\Windows is
REM the most reliable (works even before PATH refreshes after a fresh install).
if exist "%SystemRoot%\pyw.exe" ( start "" "%SystemRoot%\pyw.exe" -3 "%~dp0prospecting_app.py" & exit /b )
where pyw >nul 2>&1 && ( start "" pyw -3 "%~dp0prospecting_app.py" & exit /b )
where pythonw >nul 2>&1 && ( start "" pythonw "%~dp0prospecting_app.py" & exit /b )
if exist "%LocalAppData%\Programs\Python\Python312\pythonw.exe" ( start "" "%LocalAppData%\Programs\Python\Python312\pythonw.exe" "%~dp0prospecting_app.py" & exit /b )
if exist "%ProgramFiles%\Python312\pythonw.exe" ( start "" "%ProgramFiles%\Python312\pythonw.exe" "%~dp0prospecting_app.py" & exit /b )
where py >nul 2>&1 && ( start "" py -3 "%~dp0prospecting_app.py" & exit /b )
start "" python "%~dp0prospecting_app.py"
