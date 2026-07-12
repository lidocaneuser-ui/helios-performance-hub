@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Setup_Update_Repository.ps1"
if errorlevel 1 (
    echo.
    echo Repository setup failed. Review the error above.
    pause
    exit /b 1
)
pause
endlocal
