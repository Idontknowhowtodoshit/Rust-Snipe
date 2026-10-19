@echo off
rem Rust-Snipe launcher for Windows: sets itself up on first run, then opens the web UI.
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" goto run

set "PY="
where py >nul 2>&1 && set "PY=py -3"
if not defined PY where python >nul 2>&1 && set "PY=python"
if not defined PY goto nopython

echo First run: setting up Python packages (one time only)...
%PY% -m venv .venv || goto nopython
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt || goto fail

:run
".venv\Scripts\python.exe" rustloc.py ui %*
if errorlevel 1 pause
exit /b

:nopython
echo.
echo Python 3.9 or newer is needed. Get it from https://www.python.org/downloads/
echo (tick "Add python.exe to PATH" in the installer), then run this again.
if exist ".venv" rmdir /s /q ".venv"
pause
exit /b 1

:fail
echo.
echo Installing the Python packages failed. Check your internet connection and run this again.
rmdir /s /q ".venv"
pause
exit /b 1
