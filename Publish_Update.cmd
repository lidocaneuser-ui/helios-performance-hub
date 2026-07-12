@echo off
setlocal
cd /d "%~dp0"
title Publish Helios Production Update
set "REPO=%~1"
if "%REPO%"=="" set "REPO=lidocaneuser-ui/helios-performance-hub"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0Migrate_Develop_And_Publish.ps1" -Repository "%REPO%" -DevelopmentRoot "%~dp0"
if errorlevel 1 (
  echo.
  echo Publishing failed. No release should be trusted until the checks pass.
  pause
  exit /b 1
)
echo.
echo Release published, source committed, and GitHub updated.
pause
