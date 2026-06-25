@echo off
cd /d "%~dp0"
REM Launch the app window with no console (pythonw / pyw). Falls back to python.
where pyw >nul 2>&1 && ( start "" pyw -3 "%~dp0prospecting_app.py" & exit /b )
where pythonw >nul 2>&1 && ( start "" pythonw "%~dp0prospecting_app.py" & exit /b )
set "PW=%LocalAppData%\Programs\Python\Python312\pythonw.exe"
if exist "%PW%" ( start "" "%PW%" "%~dp0prospecting_app.py" & exit /b )
where py >nul 2>&1 && ( start "" py -3 "%~dp0prospecting_app.py" & exit /b )
start "" python "%~dp0prospecting_app.py"
