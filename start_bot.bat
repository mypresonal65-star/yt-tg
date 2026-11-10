@echo off
setlocal enabledelayedexpansion
title ytdl-telegram: Bot Runner & Watchdog
color 0B

cd /d "%~dp0"

echo.
echo ============================================================
echo          ytdl-telegram: Telegram Bot Supervisor
echo ============================================================
echo.

REM 1. Activate Virtual Environment if available
if exist "%~dp0.venv\Scripts\activate.bat" (
    echo   [+] Activating virtual environment (.venv)...
    call "%~dp0.venv\Scripts\activate.bat"
) else if exist "%~dp0venv\Scripts\activate.bat" (
    echo   [+] Activating virtual environment (venv)...
    call "%~dp0venv\Scripts\activate.bat"
) else (
    echo   [*] No virtual environment folder detected. Using system Python.
)

REM 2. Verify Python is callable
where python >nul 2>&1
if errorlevel 1 (
    echo.
    echo   [ERROR] Python is not found in your PATH!
    echo   Please install Python from https://www.python.org/
    pause
    exit /b 1
)

REM 3. Select bot script (prefers bot_v2.py, falls back to bot.py)
set "BOT_SCRIPT=bot_v2.py"
if not exist "%BOT_SCRIPT%" set "BOT_SCRIPT=bot.py"

echo   [+] Target Script: %BOT_SCRIPT%
echo   [+] Logs Directory: %~dp0logs\
echo.
echo   Press Ctrl+C at any time to permanently stop the supervisor.
echo ============================================================
echo.

:run_loop
echo   [%DATE% %TIME%] Starting %BOT_SCRIPT%...
python "%BOT_SCRIPT%"

set "EXIT_CODE=%ERRORLEVEL%"
echo.
echo ============================================================
echo   [!] %BOT_SCRIPT% stopped with exit code %EXIT_CODE%.

REM If cleanly exited with 0 (user shut down or Ctrl+C handled), we can offer clean exit
if "%EXIT_CODE%"=="0" (
    echo   Bot stopped normally.
    choice /C YN /T 10 /D Y /M "  Restart bot now? [Y/N]"
    if errorlevel 2 goto :end
    goto :run_loop
)

echo   [CRASH DETECTED] Unexpected termination.
echo   Restarting in 5 seconds... (Press Ctrl+C to abort)
echo ============================================================
timeout /t 5 /nobreak >nul
goto :run_loop

:end
echo.
echo   Supervisor stopped. Goodbye!
pause
