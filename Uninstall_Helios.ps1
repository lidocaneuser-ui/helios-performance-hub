[CmdletBinding()]
param([switch]$RemoveData)
$ErrorActionPreference = 'Stop'
$InstallRoot = Join-Path $env:LOCALAPPDATA 'Programs\HeliosPerformanceHub'
$DataRoot = Join-Path $env:LOCALAPPDATA 'HeliosPerformanceHub'

Get-CimInstance Win32_Process | Where-Object {
    $_.CommandLine -and $_.CommandLine -like '*HeliosPerformanceHub*'
} | ForEach-Object {
    try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop } catch {}
}

$RunKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
Remove-ItemProperty -Path $RunKey -Name 'HeliosPerformanceHub' -ErrorAction SilentlyContinue
Remove-Item -LiteralPath (Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Helios Performance Control Hub.lnk') -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath (Join-Path ([Environment]::GetFolderPath('Desktop')) 'Helios Performance Control Hub.lnk') -Force -ErrorAction SilentlyContinue
if (Test-Path $InstallRoot) { Remove-Item -LiteralPath $InstallRoot -Recurse -Force }
if ($RemoveData -and (Test-Path $DataRoot)) { Remove-Item -LiteralPath $DataRoot -Recurse -Force }
Write-Host 'Helios was removed.'
if (-not $RemoveData) { Write-Host "Settings, telemetry, logs, and backups were preserved at $DataRoot" }
