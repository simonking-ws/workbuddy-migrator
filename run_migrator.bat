@echo off
cd /d "%~dp0"
where python.exe >nul 2>nul
if errorlevel 1 (
    echo Python not found in PATH.
    echo Please run manually: python workbuddy_migrator.py
    pause
    exit /b 1
)
where pythonw.exe >nul 2>nul
if errorlevel 1 (
    python.exe "workbuddy_migrator.py"
) else (
    start "" pythonw.exe "workbuddy_migrator.py"
)
