@echo off
rem Opens Chrome with the ResumeOps Overleaf profile so you can sign in once. Close it when done.
cd /d "%~dp0"
".venv\Scripts\python.exe" -m resume_bot overleaf-login --browser-profile "%LOCALAPPDATA%\ResumeBuilderChrome"
pause
