@echo off
REM Scheduler entry (guide p17): one AUTO_PAPER cycle on the main paper account.
REM Refuses until the mechanics lifecycle gate has passed. Log exit status, return it.
cd /d "%~dp0"
if not exist logs mkdir logs
".venv\Scripts\python.exe" desk.py cycle --mode AUTO_PAPER
set RC=%ERRORLEVEL%
echo %DATE% %TIME% run-paper exit=%RC%>> logs\launcher.log
exit /b %RC%
