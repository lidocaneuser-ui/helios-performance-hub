@echo off
setlocal
cd /d "%~dp0"
title Build Helios Performance Control Hub

where py >nul 2>nul
if %errorlevel%==0 (set "PY=py -3") else (set "PY=python")

%PY% -m pip install --disable-pip-version-check --upgrade -r requirements-dev.txt
if errorlevel 1 goto :failed

%PY% -m unittest discover -s tests -v
if errorlevel 1 goto :failed

if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

%PY% -m PyInstaller --noconfirm --clean --onefile --windowed ^
  --name "HeliosPerformanceHub" ^
  --hidden-import pystray._win32 ^
  --collect-submodules helios_core ^
  "helios_performance_hub.py"
if errorlevel 1 goto :failed

%PY% -m PyInstaller --noconfirm --clean --onefile --windowed ^
  --name "HeliosLauncher" ^
  "helios_launcher.py"
if errorlevel 1 goto :failed

%PY% -m PyInstaller --noconfirm --clean --onefile --windowed ^
  --name "HeliosUpdater" ^
  --collect-submodules helios_core ^
  "helios_update_worker.py"
if errorlevel 1 goto :failed

%PY% "helios_release.py" --mode hybrid
if errorlevel 1 goto :failed

for /f "delims=" %%V in ('%PY% -c "from pathlib import Path; import helios_release; print(helios_release.app_version(Path('.')))"') do set "VERSION=%%V"

echo.
echo Build complete.
echo Application: %~dp0dist\HeliosPerformanceHub.exe
echo Launcher:    %~dp0dist\HeliosLauncher.exe
echo Updater:     %~dp0dist\HeliosUpdater.exe
echo Release assets are under release_artifacts\%VERSION%\
pause
exit /b 0

:failed
echo.
echo Build failed. Review the error above.
pause
exit /b 1
