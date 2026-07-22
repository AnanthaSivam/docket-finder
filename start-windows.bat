@echo off
REM Double-click this file to launch Docket Finder.
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo Python isn't installed. Install it from https://www.python.org/downloads/
    echo IMPORTANT: during install, check the box "Add python.exe to PATH".
    pause
    exit /b 1
)

echo Checking dependencies (first run only, may take a minute)...
python -m pip install --quiet -r requirements.txt

echo Starting Docket Finder...
python app.py
pause
