[CmdletBinding()]
param(
    [switch]$NoStartup,
    [switch]$NoDesktopShortcut
)

$ErrorActionPreference = 'Stop'
$SourceRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$InstallRoot = Join-Path $env:LOCALAPPDATA 'Programs\HeliosPerformanceHub'
$VenvRoot = Join-Path $InstallRoot '.venv'
$VenvPython = Join-Path $VenvRoot 'Scripts\python.exe'
$VenvPythonw = Join-Path $VenvRoot 'Scripts\pythonw.exe'
$Launcher = Join-Path $InstallRoot 'helios_launcher.py'
$DataRoot = Join-Path $env:LOCALAPPDATA 'HeliosPerformanceHub'

Write-Host '============================================================'
Write-Host ' HELIOS PERFORMANCE CONTROL HUB 5.0 INSTALLER'
Write-Host '============================================================'
Write-Host "Install location: $InstallRoot"
Write-Host

$Items = @(
    'helios_performance_hub.py',
    'helios_update.py',
    'helios_update_worker.py',
    'helios_launcher.py',
    'helios_release.py',
    'helios_core',
    'requirements.txt',
    'Install_and_Run.cmd',
    'Install_Helios.ps1',
    'Run_Helios.cmd',
    'Build_EXE.cmd',
    'Publish_Update.cmd',
    'Setup_Update_Repository.cmd',
    'Setup_Update_Repository.ps1',
    'Developer_Setup.cmd',
    'Migrate_Develop_And_Publish.ps1',
    'Uninstall_Helios.cmd',
    'Uninstall_Helios.ps1',
    'README.md',
    'START_HERE.txt',
    'UPDATE_SYSTEM.md',
    'PRODUCTION.md',
    'SECURITY.md',
    'RELEASE_NOTES.md',
    'CHANGELOG.md',
    'release_public_key.pem',
    'release.json'
)

New-Item -ItemType Directory -Path $InstallRoot -Force | Out-Null
if ((Resolve-Path $SourceRoot).Path -ne (Resolve-Path $InstallRoot).Path) {
    foreach ($Name in $Items) {
        $Source = Join-Path $SourceRoot $Name
        if (-not (Test-Path $Source)) { throw "Installation item is missing: $Source" }
        $Destination = Join-Path $InstallRoot $Name
        if (Test-Path $Source -PathType Container) {
            if (Test-Path $Destination) { Remove-Item -LiteralPath $Destination -Recurse -Force }
            Copy-Item -LiteralPath $Source -Destination $Destination -Recurse -Force
        }
        else {
            Copy-Item -LiteralPath $Source -Destination $Destination -Force
        }
    }
}

if (-not (Test-Path $VenvPython -PathType Leaf)) {
    Write-Host 'Creating isolated Python environment...'
    if (Get-Command py -ErrorAction SilentlyContinue) { & py -3 -m venv $VenvRoot }
    elseif (Get-Command python -ErrorAction SilentlyContinue) { & python -m venv $VenvRoot }
    else { throw 'Python 3.11 or newer was not found. Install Python and enable Add Python to PATH.' }
    if ($LASTEXITCODE -ne 0) { throw 'Python could not create the Helios virtual environment.' }
}

Write-Host 'Installing or updating Helios dependencies...'
& $VenvPython -m pip install --disable-pip-version-check --upgrade pip
if ($LASTEXITCODE -ne 0) { throw 'pip upgrade failed.' }
& $VenvPython -m pip install --disable-pip-version-check --upgrade -r (Join-Path $InstallRoot 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'Helios dependency installation failed.' }

$RuntimeRoot = Join-Path $DataRoot 'runtime'
New-Item -ItemType Directory -Path $RuntimeRoot -Force | Out-Null
$RequirementsHash = (Get-FileHash (Join-Path $InstallRoot 'requirements.txt') -Algorithm SHA256).Hash.ToLowerInvariant()
@{
    sha256 = $RequirementsHash
    updated_epoch = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    python = $VenvPython
} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $RuntimeRoot 'requirements_state.json') -Encoding UTF8

$Shell = New-Object -ComObject WScript.Shell
$StartMenu = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Helios Performance Control Hub.lnk'
$StartShortcut = $Shell.CreateShortcut($StartMenu)
$StartShortcut.TargetPath = $VenvPythonw
$StartShortcut.Arguments = '"' + $Launcher + '"'
$StartShortcut.WorkingDirectory = $InstallRoot
$StartShortcut.Description = 'Helios Performance Control Hub'
$StartShortcut.Save()

if (-not $NoDesktopShortcut) {
    $Desktop = [Environment]::GetFolderPath('Desktop')
    $DesktopLink = Join-Path $Desktop 'Helios Performance Control Hub.lnk'
    $DesktopShortcut = $Shell.CreateShortcut($DesktopLink)
    $DesktopShortcut.TargetPath = $VenvPythonw
    $DesktopShortcut.Arguments = '"' + $Launcher + '"'
    $DesktopShortcut.WorkingDirectory = $InstallRoot
    $DesktopShortcut.Description = 'Helios Performance Control Hub'
    $DesktopShortcut.Save()
}

if (-not $NoStartup) {
    $RunKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
    New-Item -Path $RunKey -Force | Out-Null
    $StartupCommand = '"' + $VenvPythonw + '" "' + $Launcher + '" --minimized'
    New-ItemProperty -Path $RunKey -Name 'HeliosPerformanceHub' -Value $StartupCommand -PropertyType String -Force | Out-Null
    Write-Host 'Helios was enabled to start minimized when you sign in.'
}

Write-Host
Write-Host 'Installation complete. Starting Helios...'
Start-Process -FilePath $VenvPythonw -ArgumentList @('"' + $Launcher + '"') -WorkingDirectory $InstallRoot
