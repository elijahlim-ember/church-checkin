@echo off
rem Opens the check-in screen full screen in Microsoft Edge once the server is answering.
rem To leave kiosk mode press Alt+F4.
rem The ping uses 127.0.0.1: "localhost" tries IPv6 first, which the server doesn't listen on, and Windows
rem spends about 2 seconds on that before falling back, longer than the ping's timeout.
title Church Check-in
set URL=http://localhost:8080/
echo Starting Church Check-in, please wait...
for /l %%i in (1,1,60) do (
  powershell -NoProfile -Command "try { Invoke-WebRequest -UseBasicParsing -TimeoutSec 2 http://127.0.0.1:8080/api/ping | Out-Null; exit 0 } catch { exit 1 }" && goto ready
  timeout /t 1 /nobreak >nul
)
echo The check-in server is not answering. Opening the screen anyway; see data\logs\checkin.log if it stays blank.
:ready
start "" msedge.exe --kiosk %URL% --edge-kiosk-type=fullscreen --no-first-run --disable-features=Translate --user-data-dir="%LOCALAPPDATA%\ChurchCheckin-Kiosk"
