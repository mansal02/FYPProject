@echo off
REM ============================================
REM AI Assistant - Windows Setup Script
REM ============================================
REM Complete setup with dependency check

setlocal enabledelayedexpansion

cd /d "%~dp0"

echo.
echo ============================================
echo  AI ASSISTANT - SETUP
echo ============================================
echo.

REM Check Python
echo [CHECK] Python version...
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo X Python not found in PATH
    echo   Download from: https://www.python.org
    pause
    exit /b 1
)
python --version
echo.

REM Check pip
echo [CHECK] pip...
python -m pip --version >nul 2>&1
if %errorlevel% neq 0 (
    echo X pip not working
    pause
    exit /b 1
)
echo OK
echo.

REM Check app structure
echo [CHECK] App structure...
if not exist "aiassistant\core" (
    echo X aiassistant\core not found
    pause
    exit /b 1
)
if not exist "models" (
    echo X models directory not found
    pause
    exit /b 1
)
echo OK
echo.

REM Install requirements
echo [INSTALL] Python packages...
pip install -r requirements.txt
if %errorlevel% neq 0 (
    echo X Package installation failed
    pause
    exit /b 1
)
echo.

REM Check Ollama
echo [CHECK] Ollama...
ollama --version >nul 2>&1
if %errorlevel% neq 0 (
    echo ! Ollama not found in PATH
    echo   Download from: https://ollama.ai
    echo.
) else (
    ollama --version
    echo.
    echo Checking installed models...
    ollama list
    echo.
)

REM Summary
echo ============================================
echo  SETUP COMPLETE
echo ============================================
echo.
echo Next steps:
echo 1. Install Ollama: https://ollama.ai
echo 2. Download models:
echo    - ollama pull qwen2.5-coder:7b
echo    - ollama pull qwen2.5vl:7b
echo    - ollama pull qwen2.5:3b (optional: for 3.4x speed)
echo 3. Start Ollama service
echo 4. Run the app:
echo    python marie.bat
echo.
echo For more details:
echo - README.md
echo - OPTIMIZATION.md
echo - config.yaml
echo.
pause
