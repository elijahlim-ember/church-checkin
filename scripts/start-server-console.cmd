@echo off
rem Runs the server in a visible window, for troubleshooting. Normally Task Scheduler runs it hidden.
rem Stop the scheduled one first:  schtasks /end /tn "Church Check-in Server"
cd /d "%~dp0.."
".venv\Scripts\python.exe" run.py serve
pause
