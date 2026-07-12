@echo off
setlocal
cd /d "%~dp0"
title Helios Production Repository Setup
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0Migrate_Develop_And_Publish.ps1" %*
if errorlevel 1 (
  echo.
  echo Production setup failed. Review the error above.
  pause
  exit /b 1
)
echo.
echo Production setup and release completed.
pause
