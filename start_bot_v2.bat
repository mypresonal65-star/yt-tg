@echo off
setlocal
title ytdl-telegram: Bot Runner and Watchdog
color 0B

cd /d "%~dp0"

echo.
echo ============================================================
echo          ytdl-telegram: Telegram Bot Supervisor
echo ============================================================
echo.

REM 1. Activate virtual environment if available
if exist "%~dp0.venv\Scripts\activate.bat" goto :use_dotvenv
if exist "%~dp0venv\Scripts\activate.bat" goto :use_venv
echo   [*] No virtual environment folder detected. Using system Python.
goto :check_python

:use_dotvenv
echo   [+] Activating virtual environment: .venv
call "%~dp0.venv\Scripts\activate.bat"
goto :check_python

:use_venv
echo   [+] Activating virtual environment: venv
call "%~dp0venv\Scripts\activate.bat"
goto :check_python

REM 2. Verify Python is callable
:check_python
where python >nul 2>&1
if errorlevel 1 goto :no_python
goto :pick_script

:no_python
echo.
echo   [ERROR] Python was not found in your PATH.
echo   Please install Python from https://www.python.org/
pause
exit /b 1

REM 3. Select bot script - prefers bot_v2.py, falls back to bot.py
:pick_script
set "BOT_SCRIPT=bot_v2.py"
if not exist "%BOT_SCRIPT%" set "BOT_SCRIPT=bot.py"

echo   [+] Target Script: %BOT_SCRIPT%
echo   [+] Logs Directory: %~dp0logs\
echo.
echo   Press Ctrl+C at any time to permanently stop the supervisor.
echo ============================================================
echo.

:run_loop
echo   [%DATE% %TIME%] Starting %BOT_SCRIPT% ...
python "%BOT_SCRIPT%"

set "EXIT_CODE=%ERRORLEVEL%"
echo.
echo ============================================================
echo   %BOT_SCRIPT% stopped with exit code %EXIT_CODE%.

if "%EXIT_CODE%"=="0" goto :clean_exit

echo   [CRASH DETECTED] Unexpected termination.
echo   Restarting in 5 seconds. Press Ctrl+C to abort.
echo ============================================================
timeout /t 5 /nobreak >nul
goto :run_loop

:clean_exit
echo   Bot stopped normally.
choice /C YN /T 10 /D Y /M "  Restart bot now"
if errorlevel 2 goto :end
goto :run_loop

:end
echo.
echo   Supervisor stopped. Goodbye.
pause
