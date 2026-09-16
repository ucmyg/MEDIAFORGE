@echo off
rem Starts the ClipForge web UI (the shortcuts point here). Keeps the window open if something goes wrong.
setlocal
cd /d "%~dp0"
title ClipForge
if not exist ".venv\Scripts\clipforge.exe" (
  echo ClipForge is not installed yet. Double-click "Install ClipForge.bat" first.
  pause
  exit /b 1
)
echo Starting ClipForge... your browser will open at http://127.0.0.1:8765
echo Keep this window open while you use ClipForge. Close it to stop.
".venv\Scripts\clipforge.exe" ui
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
  echo.
  echo ClipForge stopped with an error ^(code %RC%^). Details: logs\clipforge.log in this folder.
  echo If the port is busy, close the other ClipForge window and try again.
  pause
)
endlocal
exit /b %RC%
