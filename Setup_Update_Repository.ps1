[CmdletBinding()]
param(
    [string]$RepositoryName = 'helios-performance-hub',
    [switch]$Private
)

$ErrorActionPreference = 'Stop'
if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
    throw 'GitHub CLI is required. Install it, then run: gh auth login'
}

gh auth status | Out-Host
if ($LASTEXITCODE -ne 0) {
    throw 'GitHub CLI is not authenticated. Run: gh auth login'
}

$Owner = (gh api user --jq .login).Trim()
if (-not $Owner) { throw 'Could not determine the authenticated GitHub username.' }
$Repo = "$Owner/$RepositoryName"

gh repo view $Repo *> $null
if ($LASTEXITCODE -ne 0) {
    $Visibility = if ($Private) { '--private' } else { '--public' }
    & gh repo create $Repo $Visibility --description 'Release channel for Helios Performance Control Hub' --disable-wiki
    if ($LASTEXITCODE -ne 0) { throw 'GitHub repository creation failed.' }
    Write-Host "Created $Repo"
}
else {
    Write-Host "$Repo already exists."
}

$DataRoot = Join-Path $env:LOCALAPPDATA 'HeliosPerformanceHub'
$BootstrapPath = Join-Path $DataRoot 'update_channel.json'
New-Item -ItemType Directory -Path $DataRoot -Force | Out-Null
$Configuration = [ordered]@{
    update_repository_owner = $Owner
    update_repository_name = $RepositoryName
    update_channel = 'stable'
    automatic_update_checks = $true
}
$Json = $Configuration | ConvertTo-Json -Depth 4
[System.IO.File]::WriteAllText($BootstrapPath, $Json, ([System.Text.UTF8Encoding]::new($false)))

Write-Host
Write-Host "Helios update channel configured: $Repo"
Write-Host 'Restart Helios, then use Publish_Update.cmd OWNER/REPOSITORY source'
