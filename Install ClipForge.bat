@echo off
rem ClipForge setup launcher: double-click to install or update. No commands to type.
rem Everything it does is logged to %LOCALAPPDATA%\ClipForge\setup.log (no secrets are written there).
setlocal
cd /d "%~dp0"
title ClipForge setup
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0installer\setup.ps1"
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
  echo.
  echo Setup did not finish. The log is at %LOCALAPPDATA%\ClipForge\setup.log
  echo Press any key to close this window.
  pause >nul
)
endlocal
exit /b %RC%
