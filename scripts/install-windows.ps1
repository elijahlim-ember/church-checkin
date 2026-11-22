# Church Check-in: install on the main Windows 11 computer.
#
# Run from the app folder (for example C:\ChurchCheckin) in PowerShell:
#   powershell -ExecutionPolicy Bypass -File scripts\install-windows.ps1
#
# It needs the internet once (to download Python packages). After that the app runs offline.
# Run it "as administrator" to also open the firewall for the iPads (local network only).

$ErrorActionPreference = "Stop"
$AppDir = Split-Path -Parent $PSScriptRoot
$Port = 8080
Set-Location $AppDir
Write-Host "Installing Church Check-in in $AppDir" -ForegroundColor Cyan

# 1. Python 3.10 or newer
function Find-Python {
    foreach ($cmd in @("py -3", "python")) {
        try {
            $version = & cmd /c "$cmd -c ""import sys; print('%d.%d' % sys.version_info[:2])""" 2>$null
            if ($LASTEXITCODE -eq 0 -and [version]$version -ge [version]"3.10") { return $cmd }
        } catch { }
    }
    return $null
}
$Python = Find-Python
if (-not $Python) {
    Write-Host "Python not found. Installing Python 3.12 with winget..." -ForegroundColor Yellow
    winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path", "User") + ";" + $env:Path
    $Python = Find-Python
    if (-not $Python) { throw "Python is still not available. Install Python 3.12 from python.org, then run this again." }
}
Write-Host "Using Python: $Python"

# 2. Private virtual environment with the two packages the app needs
if (-not (Test-Path "$AppDir\.venv\Scripts\python.exe")) {
    & cmd /c "$Python -m venv ""$AppDir\.venv"""
}
& "$AppDir\.venv\Scripts\python.exe" -m pip install --upgrade pip --quiet
& "$AppDir\.venv\Scripts\python.exe" -m pip install -r "$AppDir\requirements.txt" --quiet
Write-Host "Packages installed." -ForegroundColor Green

# 3. Check for printers (informational)
& "$AppDir\.venv\Scripts\python.exe" "$AppDir\run.py" printers

# 4. Start the server automatically when this Windows user logs in, and restart it if it ever stops
$TaskName = "Church Check-in Server"
$Action = New-ScheduledTaskAction -Execute "$AppDir\.venv\Scripts\pythonw.exe" -Argument "`"$AppDir\run.py`" serve" -WorkingDirectory $AppDir
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
    -MultipleInstances IgnoreNew
try {
    Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings -Force -ErrorAction Stop | Out-Null
    Write-Host "Server will start automatically at login (Task Scheduler: '$TaskName')." -ForegroundColor Green
} catch {
    if (-not (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue)) { throw }
    # An earlier run "as administrator" created the task; without administrator rights it can't be replaced.
    Write-Host "Keeping the existing '$TaskName' task (run as administrator to replace it)." -ForegroundColor Yellow
}

# 5. Full-screen check-in window on this computer at login
$Startup = [Environment]::GetFolderPath("Startup")
$Shell = New-Object -ComObject WScript.Shell
$Shortcut = $Shell.CreateShortcut("$Startup\Church Check-in Kiosk.lnk")
$Shortcut.TargetPath = "$AppDir\scripts\start-kiosk.cmd"
$Shortcut.WorkingDirectory = $AppDir
$Shortcut.WindowStyle = 7
$Shortcut.IconLocation = "$AppDir\scripts\icons\checkin-screen.ico,0"
$Shortcut.Save()
$Desktop = [Environment]::GetFolderPath("Desktop")
foreach ($item in @(@("Start Check-in", "start-checkin.cmd", "start-checkin.ico"),
                    @("Check-in screen", "start-kiosk.cmd", "checkin-screen.ico"),
                    @("Check-in admin", "open-admin.cmd", "checkin-admin.ico"))) {
    $s = $Shell.CreateShortcut("$Desktop\$($item[0]).lnk")
    $s.TargetPath = "$AppDir\scripts\$($item[1])"
    $s.WorkingDirectory = $AppDir
    $s.WindowStyle = 7
    $s.IconLocation = "$AppDir\scripts\icons\$($item[2]),0"
    $s.Save()
}
Write-Host "Kiosk window opens at login; desktop shortcuts created." -ForegroundColor Green

# 6. Firewall: let the iPads on the local network reach the server (administrator only)
$IsAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if ($IsAdmin) {
    Get-NetFirewallRule -DisplayName "Church Check-in" -ErrorAction SilentlyContinue | Remove-NetFirewallRule
    New-NetFirewallRule -DisplayName "Church Check-in" -Direction Inbound -Protocol TCP -LocalPort $Port `
        -RemoteAddress LocalSubnet -Action Allow -Profile Private | Out-Null
    Write-Host "Firewall opened for TCP $Port from the local network only (Private network profile)." -ForegroundColor Green
} else {
    Write-Host "Not running as administrator, so the firewall was not changed." -ForegroundColor Yellow
    Write-Host "For the iPads, run this script again 'as administrator', or allow Python when Windows asks." -ForegroundColor Yellow
}

# 7. Start now
Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 3
Write-Host ""
Write-Host "Done. Opening the check-in screen for first-time setup..." -ForegroundColor Cyan
Start-Process "$AppDir\scripts\start-checkin.cmd"
