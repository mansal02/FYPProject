@echo off
setlocal
TITLE MARIE Launcher

where python >nul 2>nul
if errorlevel 1 (
	echo [MARIE] Python was not found in PATH.
	pause
	exit /b 1
)

set "MODE=assistant"
echo [MARIE] Select startup mode:
echo   [1] Assistant (new GUI only)
echo   [2] Legacy (reasoning + voice + legacy GUI)
echo   [3] Hybrid (reasoning + voice + new GUI)
set /p START_MODE=Enter choice [1-3, default 1]: 

if "%START_MODE%"=="2" set "MODE=legacy"
if "%START_MODE%"=="3" set "MODE=hybrid"

echo [MARIE] Launching mode: %MODE%
python -m aiassistant.launchers.runsys --mode %MODE%
set "APP_EXIT=%ERRORLEVEL%"

if not "%APP_EXIT%"=="0" (
	echo [MARIE] Main app exited with code %APP_EXIT%.
)

pause
endlocal