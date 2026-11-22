@echo off
rem Phase 1 hardware test (see docs\PRINTER-TEST.md).
rem Usage: printer-test.cmd [SERIAL]   (serial only needed when two printers are plugged in)
cd /d "%~dp0.."
echo Stopping the check-in server so the test has the printer to itself...
schtasks /end /tn "Church Check-in Server" >nul 2>&1
echo.
".venv\Scripts\python.exe" run.py printers
echo.
echo Printing 100 numbered labels, one at a time...
if "%~1"=="" (
  ".venv\Scripts\python.exe" run.py print-test --count 100
) else (
  ".venv\Scripts\python.exe" run.py print-test --count 100 --address %1
)
echo.
echo Now 10 jobs of 4 labels (like a family of four)...
if "%~1"=="" (
  ".venv\Scripts\python.exe" run.py print-test --count 40 --batch 4
) else (
  ".venv\Scripts\python.exe" run.py print-test --count 40 --batch 4 --address %1
)
echo.
echo Restarting the check-in server...
schtasks /run /tn "Church Check-in Server" >nul 2>&1
pause
