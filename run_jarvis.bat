@echo off
rem Double-click to start JARVIS on Windows.
rem First run sets everything up; later runs start in a few seconds.
cd /d "%~dp0"

where py >nul 2>nul
if %errorlevel%==0 (
    py -3 run.py %*
) else (
    where python >nul 2>nul
    if errorlevel 1 (
        echo Python is not installed. Install Python 3.12 from https://www.python.org/downloads/
        echo and tick "Add python.exe to PATH", then double-click this file again.
        pause
        exit /b 1
    )
    python run.py %*
)

if errorlevel 1 (
    echo.
    echo JARVIS stopped with an error. Read the messages above.
    pause
)
