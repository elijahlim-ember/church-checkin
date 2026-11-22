# Create or repair the standard Church Check-in shortcuts on this Windows desktop.
# Run from the project folder, or pass -AppDir "C:\ChurchCheckin".

param([string]$AppDir = "")

$ErrorActionPreference = "Stop"
if (-not $AppDir) { $AppDir = Split-Path -Parent $PSScriptRoot }
if (-not (Test-Path "$AppDir\checkin\__init__.py")) { throw "$AppDir is not a Church Check-in folder. Use -AppDir." }

$Shell = New-Object -ComObject WScript.Shell
$Desktop = [Environment]::GetFolderPath("Desktop")
$Icons = "$AppDir\scripts\icons"
$Items = @(
    @("Start Check-in", "start-checkin.cmd", "start-checkin.ico"),
    @("Check-in screen", "start-kiosk.cmd", "checkin-screen.ico"),
    @("Check-in admin", "open-admin.cmd", "checkin-admin.ico")
)
foreach ($item in $Items) {
    $shortcut = $Shell.CreateShortcut("$Desktop\$($item[0]).lnk")
    $shortcut.TargetPath = "$AppDir\scripts\$($item[1])"
    $shortcut.WorkingDirectory = $AppDir
    $shortcut.WindowStyle = 7
    $shortcut.IconLocation = "$Icons\$($item[2]),0"
    $shortcut.Save()
    Write-Host "Created $($item[0]) shortcut."
}
