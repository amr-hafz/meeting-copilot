@echo off
setlocal
cd /d "%~dp0"
title Meeting Copilot - diagnostics

chcp 65001 >nul
set "PYTHONUTF8=1"
set "HF_HUB_DISABLE_XET=1"
set "HF_HUB_DOWNLOAD_TIMEOUT=120"

if not exist ".venv\Scripts\python.exe" (
    echo.
    echo [!] Python environment not found.
    echo     Follow the Setup section in README.md first.
    echo.
    pause
    exit /b 1
)

echo Starting Meeting Copilot with console output...
echo Close the app window to stop.
echo.
".venv\Scripts\python.exe" -m app.gui %*

set "EXITCODE=%ERRORLEVEL%"
echo.
echo [Meeting Copilot stopped - exit code %EXITCODE%]
pause
exit /b %EXITCODE%
