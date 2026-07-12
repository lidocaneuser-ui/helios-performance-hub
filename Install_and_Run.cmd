@echo off
setlocal
cd /d "%~dp0"
title Install Helios Performance Control Hub 5.0
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0Install_Helios.ps1"
if errorlevel 1 (
  echo.
  echo Installation failed. Review the error above.
  pause
  exit /b 1
)
