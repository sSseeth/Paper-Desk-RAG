@echo off
REM Scheduler entry (guide p17): one AUTO_PAPER cycle, log exit status, return it.
cd /d "%~dp0"
if not exist logs mkdir logs
".venv\Scripts\python.exe" desk.py cycle --mode AUTO_PAPER
set RC=%ERRORLEVEL%
echo %DATE% %TIME% run-paper exit=%RC%>> logs\launcher.log
exit /b %RC%
