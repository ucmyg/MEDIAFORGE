@echo off
rem Removes the shortcuts and the private Python environment. Your videos, clips and settings are only deleted if you say so.
setlocal
cd /d "%~dp0"
title ClipForge uninstall
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0installer\uninstall.ps1"
pause
endlocal
