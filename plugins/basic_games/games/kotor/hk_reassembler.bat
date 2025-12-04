@echo off
setlocal

:: ----------------------------------------------------------------------
:: Multi-Patcher launcher for PowerShell script
:: ----------------------------------------------------------------------

set "SCRIPT=%~dp0hk_reassembler.ps1"

:: Allow PowerShell script execution for this session only
powershell -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT%" %*

pause
endlocal