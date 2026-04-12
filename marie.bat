@echo off
setlocal
TITLE MARIE Launcher

where python >nul 2>nul
if errorlevel 1 (
	echo [MARIE] Python was not found in PATH.
	pause
	exit /b 1
)

if not exist server_reasoning.py (
	echo [MARIE] Missing file: server_reasoning.py
	pause
	exit /b 1
)

if not exist server_voice.py (
	echo [MARIE] Missing file: server_voice.py
	pause
	exit /b 1
)

set "MODE=both"
echo [MARIE] Select startup mode:
echo   [1] Both Voice + GUI
echo   [2] GUI only
echo   [3] Voice mode
set /p START_MODE=Enter choice [1-3, default 1]: 

if "%START_MODE%"=="2" set "MODE=gui"
if "%START_MODE%"=="3" set "MODE=voice"

set "TRANSPARENT_ARG="
set /p USE_TRANSPARENT=Enable transparent face background? [y/N]: 
if /I "%USE_TRANSPARENT%"=="y" set "TRANSPARENT_ARG=--transparent-face"

echo [MARIE] Starting Brain Server (Port 8000)...
start /MIN "MARIE Brain" cmd /c "python server_reasoning.py"

if /I not "%MODE%"=="gui" (
	echo [MARIE] Starting Voice Server (Port 8001)...
	start /MIN "MARIE Voice" cmd /c "python server_voice.py"
)

echo [MARIE] Waiting 4 seconds for services to warm up...
timeout /t 4 /nobreak >nul

echo [MARIE] Launching main app in %MODE% mode...
python main.py --mode %MODE% %TRANSPARENT_ARG%
set "APP_EXIT=%ERRORLEVEL%"

echo [MARIE] Closing launched services...
taskkill /F /FI "WINDOWTITLE eq MARIE Brain*" >nul 2>nul
taskkill /F /FI "WINDOWTITLE eq MARIE Voice*" >nul 2>nul

if not "%APP_EXIT%"=="0" (
	echo [MARIE] Main app exited with code %APP_EXIT%.
)

pause
endlocal