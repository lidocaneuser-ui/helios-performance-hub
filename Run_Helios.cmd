@echo off
setlocal
set "INSTALLED=%LOCALAPPDATA%\Programs\HeliosPerformanceHub"
if exist "%INSTALLED%\.venv\Scripts\pythonw.exe" if exist "%INSTALLED%\helios_launcher.py" (
    start "" "%INSTALLED%\.venv\Scripts\pythonw.exe" "%INSTALLED%\helios_launcher.py"
    exit /b 0
)

cd /d "%~dp0"
if exist ".venv\Scripts\pythonw.exe" (
    start "" ".venv\Scripts\pythonw.exe" "helios_launcher.py"
) else if exist "helios_launcher.py" (
    where pyw >nul 2>nul
    if not errorlevel 1 (
        start "" pyw "helios_launcher.py"
    ) else (
        python "helios_launcher.py"
    )
) else (
    echo Helios is not installed. Run Install_and_Run.cmd first.
    pause
)
endlocal
