# Update Church Check-in from a release ZIP on a configured file share.
# Example: powershell -ExecutionPolicy Bypass -File scripts\update-from-share.ps1 -Share "\\fileserver\installers"

param([string]$Share = "\\fileserver\installers")

$ErrorActionPreference = "Stop"
$AppDir = Split-Path -Parent $PSScriptRoot
$Downloads = Join-Path $env:USERPROFILE "Downloads"

function Get-Version($InitFile) {
    (Select-String -Path $InitFile -Pattern '__version__ = "(.+)"').Matches[0].Groups[1].Value
}

try {
    $Current = Get-Version "$AppDir\checkin\__init__.py"
    $Zips = Get-ChildItem "$Share\church-checkin-*.zip" | Where-Object { $_.Name -match '^church-checkin-(\d+\.\d+\.\d+)\.zip$' }
    if (-not $Zips) { throw "No church-checkin-X.Y.Z.zip release found in $Share." }
    $Newest = $Zips | Sort-Object { [version]($_.Name -replace '^church-checkin-|\.zip$', '') } | Select-Object -Last 1
    $NewVersion = $Newest.Name -replace '^church-checkin-|\.zip$', ''

    Write-Host "This computer runs Church Check-in $Current. The newest release is $NewVersion." -ForegroundColor Cyan
    if ([version]$NewVersion -le [version]$Current) {
        Write-Host "Nothing to update." -ForegroundColor Green
    } elseif ((Read-Host "Update $Current to $NewVersion now? Check-in will stop briefly. Type Y to continue") -eq "Y") {
        $Zip = Join-Path $Downloads $Newest.Name
        $Unzipped = Join-Path $Downloads "church-checkin-$NewVersion"
        Copy-Item $Newest.FullName $Zip -Force
        Expand-Archive -Path $Zip -DestinationPath $Unzipped -Force
        $Source = Join-Path $Unzipped "church-checkin"
        if (-not (Test-Path "$Source\checkin\__init__.py")) { throw "The release archive has an unexpected folder layout." }
        & "$AppDir\scripts\update-windows.ps1" -Source $Source
    } else {
        Write-Host "Not updated."
    }
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
}
Read-Host "Press Enter to close"
