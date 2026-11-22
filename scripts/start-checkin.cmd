@echo off
rem Starts the check-in server if it isn't running, then opens the check-in screen full screen.
rem This is the desktop "Start Check-in" shortcut. To leave the full-screen window press Alt+F4.
schtasks /run /tn "Church Check-in Server" >nul 2>&1 || start "" "%~dp0..\.venv\Scripts\pythonw.exe" "%~dp0..\run.py" serve
call "%~dp0start-kiosk.cmd"
