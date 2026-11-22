# Church Check-in: install a newer version of the app, keeping all data.
#
# 1. Unzip the new version anywhere, for example C:\Users\Admin\Downloads\church-checkin-1.1
# 2. In PowerShell, from the CURRENT install folder:
#      cd C:\ChurchCheckin
#      powershell -ExecutionPolicy Bypass -File scripts\update-windows.ps1 -Source "C:\Users\Admin\Downloads\church-checkin-1.1"
#
# What it does: makes an encrypted backup, stops the server, keeps a copy of the current program
# files in previous-versions\, copies in the new program files (never the data folder), installs any
# new packages, and starts the server again. Database upgrades run by themselves on start-up, after
# another automatic backup. To roll back, run this script again with -Source pointing at the
# folder in previous-versions\ (and, if the new version upgraded the database, restore the backup).

param([Parameter(Mandatory = $true)][string]$Source)

$ErrorActionPreference = "Stop"
$AppDir = Split-Path -Parent $PSScriptRoot
$Source = (Resolve-Path $Source).Path
$Python = "$AppDir\.venv\Scripts\python.exe"
$TaskName = "Church Check-in Server"
$ProgramItems = @("checkin", "web", "templates", "scripts", "docs", "tests", "run.py", "requirements.txt", "README.md")

if (-not (Test-Path "$Source\checkin\__init__.py")) { throw "$Source does not look like an Church Check-in folder." }
if ($Source -eq $AppDir) { throw "Source is the install folder itself. Point -Source at the new version." }

$NewVersion = (Select-String -Path "$Source\checkin\__init__.py" -Pattern '__version__ = "(.+)"').Matches[0].Groups[1].Value
$OldVersion = (Select-String -Path "$AppDir\checkin\__init__.py" -Pattern '__version__ = "(.+)"').Matches[0].Groups[1].Value
Write-Host "Updating Church Check-in $OldVersion -> $NewVersion in $AppDir" -ForegroundColor Cyan

Write-Host "1/5 Backing up the data..."
& $Python "$AppDir\run.py" backup
if ($LASTEXITCODE -ne 0) { Write-Host "Backup failed (is a backup passphrase set?). Stopping without changes." -ForegroundColor Red; exit 1 }

Write-Host "2/5 Stopping the server..."
schtasks /end /tn $TaskName 2>$null | Out-Null
Start-Sleep -Seconds 3

Write-Host "3/5 Keeping a copy of the current version..."
$Previous = "$AppDir\previous-versions\$OldVersion-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
New-Item -ItemType Directory -Force -Path $Previous | Out-Null
foreach ($item in $ProgramItems) {
    if (Test-Path "$AppDir\$item") { Copy-Item "$AppDir\$item" -Destination $Previous -Recurse -Force }
}

Write-Host "4/5 Copying in the new version (the data folder is not touched)..."
foreach ($item in $ProgramItems) {
    if (-not (Test-Path "$Source\$item")) { continue }
    if (Test-Path "$AppDir\$item" -PathType Container) { Remove-Item "$AppDir\$item" -Recurse -Force }
    Copy-Item "$Source\$item" -Destination "$AppDir\$item" -Recurse -Force
}
& $Python -m pip install -r "$AppDir\requirements.txt" --quiet

Write-Host "5/5 Starting the server..."
schtasks /run /tn $TaskName | Out-Null
Start-Sleep -Seconds 4
try {
    $ping = Invoke-RestMethod -TimeoutSec 5 http://localhost:8080/api/ping
    Write-Host "Running version $($ping.version)." -ForegroundColor Green
} catch {
    Write-Host "The server did not answer yet. Check data\logs\checkin.log, or run scripts\start-server-console.cmd." -ForegroundColor Yellow
}
Write-Host "Previous version kept in $Previous"
Write-Host "Label layouts you edited are kept. To use a new built-in layout: Admin -> Label templates -> Reset to default."
