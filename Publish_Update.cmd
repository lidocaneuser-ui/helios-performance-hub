@echo off
setlocal
cd /d "%~dp0"
title Publish Helios Production Update
set "REPO=%~1"
if "%REPO%"=="" set "REPO=lidocaneuser-ui/helios-performance-hub"

rem If this is already the permanent Git development repository, publish in place.
rem Otherwise perform the one-time migration first. %~dp0. avoids the quoted
rem trailing-backslash bug that caused "Illegal characters in path."
if exist "%~dp0.git\" (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0Migrate_Develop_And_Publish.ps1" -Repository "%REPO%" -DevelopmentRoot "%~dp0."
) else (
  echo This is the downloaded release folder, so Helios will first migrate it to:
  echo %USERPROFILE%\Documents\GitHub\helios-performance-hub
  echo.
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0Migrate_Develop_And_Publish.ps1" -Repository "%REPO%"
)

if errorlevel 1 (
  echo.
  echo Publishing failed. No release should be trusted until the checks pass.
  pause
  exit /b 1
)
echo.
echo Release published, source committed, and GitHub updated.
pause
