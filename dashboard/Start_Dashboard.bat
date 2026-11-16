@echo off
cd /d "%~dp0"
py -3 "%~dp0resume_dashboard.py" --open %*
if errorlevel 1 pause
