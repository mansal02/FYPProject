@echo off
title MARIE AI Assistant Launcher
color 0B

echo ========================================================
echo             MARIE AI ASSISTANT INITIALIZATION
echo ========================================================
echo.

:: Ensure the script runs in the correct directory
cd /d "%~dp0"

echo [1] Checking for Virtual Environment...
if not exist ".venv\Scripts\activate.bat" (
    echo [ERROR] Virtual environment not found! Please run setup first.
    pause
    exit /b
)

echo [2] Activating Virtual Environment...
call .venv\Scripts\activate.bat

echo [3] Booting System Launcher...
python aiassistant\launchers\runsys.py

echo.
echo [4] System Shutdown Safely.
pause