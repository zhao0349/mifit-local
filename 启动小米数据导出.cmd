@echo off
setlocal
set "PWSH=%ProgramFiles%\PowerShell\7\pwsh.exe"
if not exist "%PWSH%" (
    echo PowerShell 7 is required.
    pause
    exit /b 1
)
"%PWSH%" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0Start-MiFit.ps1"
if errorlevel 1 pause
