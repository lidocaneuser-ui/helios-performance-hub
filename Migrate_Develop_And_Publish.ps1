[CmdletBinding()]
param(
    [string]$Repository = 'lidocaneuser-ui/helios-performance-hub',
    [string]$DevelopmentRoot = "$env:USERPROFILE\Documents\GitHub\helios-performance-hub",
    [switch]$SkipPublish,
    [switch]$SkipPush
)

$ErrorActionPreference = 'Stop'
$SourceRoot = [System.IO.Path]::GetFullPath((Split-Path -Parent $MyInvocation.MyCommand.Path))

# Windows batch files can accidentally pass a trailing quote when a quoted path
# ends in a backslash (for example C:\Folder\"). Normalize defensively so
# first-run setup and future publishing both accept ordinary Windows paths.
if ([string]::IsNullOrWhiteSpace($DevelopmentRoot)) {
    $DevelopmentRoot = Join-Path $env:USERPROFILE 'Documents\GitHub\helios-performance-hub'
}
$DevelopmentRoot = [Environment]::ExpandEnvironmentVariables([string]$DevelopmentRoot).Trim()
$DevelopmentRoot = $DevelopmentRoot.Trim([char[]]@([char]34, [char]39))
try {
    $DevelopmentRoot = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($DevelopmentRoot)
}
catch {
    throw "The development path is invalid: <$DevelopmentRoot>. Use a normal local folder path without embedded quote characters."
}
$PrivateKey = Join-Path $env:USERPROFILE '.helios-release\ed25519-private.pem'

function Resolve-Git {
    $Command = Get-Command git -ErrorAction SilentlyContinue
    if ($Command) { return $Command.Source }

    Write-Host 'Git for Windows is not installed. Installing it with WinGet...'
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        throw 'WinGet is unavailable. Install Git for Windows, then run this script again.'
    }
    & winget install --id Git.Git -e --source winget --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) { throw 'Git for Windows installation failed.' }

    $Candidates = @(
        "$env:ProgramFiles\Git\cmd\git.exe",
        "$env:LOCALAPPDATA\Programs\Git\cmd\git.exe"
    )
    foreach ($Candidate in $Candidates) {
        if (Test-Path $Candidate -PathType Leaf) { return $Candidate }
    }
    throw 'Git installed, but git.exe could not be located. Reopen PowerShell and run the script again.'
}

function Resolve-Python {
    if (Get-Command py -ErrorAction SilentlyContinue) { return @{ Command = 'py'; Prefix = @('-3') } }
    if (Get-Command python -ErrorAction SilentlyContinue) { return @{ Command = 'python'; Prefix = @() } }
    throw 'Python 3.11 or newer is required.'
}

function Invoke-Python {
    param([Parameter(ValueFromRemainingArguments = $true)][object[]]$Arguments)
    $Command = $script:Python.Command
    $Prefix = @($script:Python.Prefix)
    & $Command @Prefix @Arguments
}

if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
    throw 'GitHub CLI is required. Install it and run gh auth login first.'
}
& gh auth status | Out-Host
if ($LASTEXITCODE -ne 0) { throw 'GitHub CLI is not authenticated.' }

$Git = Resolve-Git
$GitDirectory = Split-Path -Parent $Git
if (($env:Path -split ';') -notcontains $GitDirectory) {
    $env:Path = "$GitDirectory;$env:Path"
}
& gh auth setup-git
if ($LASTEXITCODE -ne 0) { throw 'GitHub could not configure Git credentials.' }
$Python = Resolve-Python
$Parent = Split-Path -Parent $DevelopmentRoot
New-Item -ItemType Directory -Path $Parent -Force | Out-Null

$SameRoot = $false
try { $SameRoot = (Resolve-Path $SourceRoot).Path -eq (Resolve-Path $DevelopmentRoot -ErrorAction Stop).Path } catch {}

if (-not $SameRoot) {
    if (Test-Path (Join-Path $DevelopmentRoot '.git')) {
        Write-Host "Updating existing development clone: $DevelopmentRoot"
        & $Git -C $DevelopmentRoot pull --ff-only
        if ($LASTEXITCODE -ne 0) { throw 'Could not update the existing development repository.' }
    }
    elseif (Test-Path $DevelopmentRoot) {
        $Backup = "$DevelopmentRoot.backup.$(Get-Date -Format yyyyMMdd_HHmmss)"
        Move-Item -LiteralPath $DevelopmentRoot -Destination $Backup
        Write-Host "Moved the existing non-Git folder to $Backup"
        & $Git clone "https://github.com/$Repository.git" $DevelopmentRoot
        if ($LASTEXITCODE -ne 0) { throw 'Repository clone failed.' }
    }
    else {
        & $Git clone "https://github.com/$Repository.git" $DevelopmentRoot
        if ($LASTEXITCODE -ne 0) { throw 'Repository clone failed.' }
    }

    $Excluded = @('.git', '.venv', '__pycache__', 'build', 'dist', 'release_artifacts')
    Get-ChildItem -LiteralPath $SourceRoot -Force | Where-Object { $Excluded -notcontains $_.Name } | ForEach-Object {
        $Destination = Join-Path $DevelopmentRoot $_.Name
        if ($_.PSIsContainer) {
            if (Test-Path $Destination) { Remove-Item -LiteralPath $Destination -Recurse -Force }
            Copy-Item -LiteralPath $_.FullName -Destination $Destination -Recurse -Force
        }
        else {
            Copy-Item -LiteralPath $_.FullName -Destination $Destination -Force
        }
    }
}

Set-Location $DevelopmentRoot
$Login = (& gh api user --jq .login).Trim()
$UserId = (& gh api user --jq .id).Trim()
if (-not (& $Git config user.name)) { & $Git config user.name $Login }
if (-not (& $Git config user.email)) { & $Git config user.email "$UserId+$Login@users.noreply.github.com" }

Write-Host 'Installing development dependencies...'
Invoke-Python -m pip install --disable-pip-version-check --upgrade -r requirements-dev.txt
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }

Write-Host 'Verifying the private release key and public trust key...'
Invoke-Python helios_release.py --generate-signing-key --signing-key $PrivateKey
if ($LASTEXITCODE -ne 0) { throw 'Release-key verification or recovery failed.' }

Write-Host 'Running production validation...'
Invoke-Python -m compileall -q helios_performance_hub.py helios_update.py helios_update_worker.py helios_launcher.py helios_release.py helios_core
if ($LASTEXITCODE -ne 0) { throw 'Compilation validation failed.' }
Invoke-Python -m unittest discover -s tests -v
if ($LASTEXITCODE -ne 0) { throw 'Unit tests failed.' }

& $Git add -A
$Changes = & $Git status --porcelain
if ($Changes) {
    & $Git commit -m 'Release Helios Performance Control Hub 5.0.0'
    if ($LASTEXITCODE -ne 0) { throw 'Git commit failed.' }
}
else {
    Write-Host 'No uncommitted source changes were found.'
}
& $Git branch -M main
if (-not $SkipPush) {
    & $Git push -u origin main
    if ($LASTEXITCODE -ne 0) { throw 'Git push failed.' }
}

if (-not $SkipPublish) {
    Invoke-Python helios_release.py --mode source --publish $Repository --signing-key $PrivateKey --repository-bundle
    if ($LASTEXITCODE -ne 0) { throw 'GitHub release publishing failed.' }
}

Write-Host
Write-Host '============================================================' -ForegroundColor Green
Write-Host ' HELIOS 5.0.0 PRODUCTION RELEASE IS READY' -ForegroundColor Green
Write-Host '============================================================' -ForegroundColor Green
Write-Host "Development repository: $DevelopmentRoot"
Write-Host "GitHub repository:       https://github.com/$Repository"
Write-Host "Private signing key:     $PrivateKey"
Write-Host 'Keep the private signing key backed up and never upload it.'
