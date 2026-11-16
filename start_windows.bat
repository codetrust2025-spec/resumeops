@echo off
cd /d "%~dp0"
powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "%~dp0start_windows.ps1" %*
exit /b %errorlevel%
