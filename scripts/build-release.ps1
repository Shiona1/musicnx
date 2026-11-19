# Build the prebuilt Pi release bundle (frontend static + backend app + deps).
# Run from the repo root on the dev machine:
#   pwsh -File scripts/build-release.ps1
#   pwsh -File scripts/build-release.ps1 -SkipFrontend
#
# The archive includes backend/requirements.txt and pyproject.toml so the
# deploy scripts can install/verify Python dependencies without a manual pip
# step. Node.js is needed only here, never on the Pi.

param(
    [switch]$SkipFrontend
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$Frontend = Join-Path $Root 'frontend'
$Bundle = Join-Path $Root ("dist\musicnx-app-{0}.tgz" -f (Get-Date -Format 'yyyyMMdd'))

if (-not $SkipFrontend) {
    Push-Location $Frontend
    try {
        pnpm run build
        if ($LASTEXITCODE -ne 0) { throw 'frontend build failed' }
    }
    finally {
        Pop-Location
    }
}

New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Bundle) | Out-Null
Remove-Item $Bundle -ErrorAction SilentlyContinue
# Use Windows' bsdtar; a dev machine may have MSYS tar first on PATH, which
# interprets an absolute Windows drive path as a remote host.
$Tar = Join-Path $env:WINDIR 'System32\tar.exe'
if (-not (Test-Path $Tar)) { throw "Windows tar not found: $Tar" }
& $Tar -czf $Bundle --exclude=__pycache__ --exclude=*.pyc -C $Root backend/app backend/requirements.txt backend/pyproject.toml
if ($LASTEXITCODE -ne 0) { throw 'tar failed' }

Write-Host "Release bundle: $Bundle"
