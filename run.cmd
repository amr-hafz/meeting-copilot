@echo off
setlocal
cd /d "%~dp0"

rem Keep Python output UTF-8 (logs and optional transcript).
set "PYTHONUTF8=1"
set "HF_HUB_DISABLE_XET=1"
set "HF_HUB_DOWNLOAD_TIMEOUT=120"

if not exist ".venv\Scripts\pythonw.exe" (
    echo.
    echo [!] Python environment not found.
    echo     Follow the Setup section in README.md first.
    echo.
    pause
    exit /b 1
)

rem Launch without a console window.  Diagnostics go to logs\gui.log.
start "" ".venv\Scripts\pythonw.exe" -m app.gui %*
exit /b 0
