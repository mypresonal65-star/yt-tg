@echo off
setlocal
title ytdl-telegram: Telegram User Account Login
color 0A

cd /d "%~dp0"

echo.
echo ============================================================
echo      ytdl-telegram: Telegram User Login Helper
echo ============================================================
echo.

REM 1. Activate virtual environment if available
if exist "%~dp0.venv\Scripts\activate.bat" (
    echo   [+] Activating virtual environment: .venv
    call "%~dp0.venv\Scripts\activate.bat"
    goto :run_login
)

if exist "%~dp0venv\Scripts\activate.bat" (
    echo   [+] Activating virtual environment: venv
    call "%~dp0venv\Scripts\activate.bat"
    goto :run_login
)

echo   [*] No virtual environment folder detected. Using system Python.

:run_login
where python >nul 2>&1
if errorlevel 1 (
    echo.
    echo   [ERROR] Python was not found in your PATH.
    echo   Please install Python from https://www.python.org/
    pause
    exit /b 1
)

echo.
echo   [*] Starting interactive Telegram user login...
echo.
python uploader_v4.py --login --mode user

echo.
pause
