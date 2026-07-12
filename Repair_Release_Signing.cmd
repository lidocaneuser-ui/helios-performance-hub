@echo off
setlocal
cd /d "%~dp0"
title Repair Helios Release Signing

where py >nul 2>nul
if %errorlevel%==0 (
  set "PY=py -3"
) else (
  set "PY=python"
)

%PY% "helios_release.py" --generate-signing-key --signing-key "%USERPROFILE%\.helios-release\ed25519-private.pem"
if errorlevel 1 (
  echo.
  echo Signing repair failed. Do not publish until this is resolved.
  pause
  exit /b 1
)

echo.
echo Release signing key pair verified and repaired.
echo You can now rerun Migrate_Develop_And_Publish or Publish_Update.
pause
