#Requires -Version 5
<#
.SYNOPSIS
    Fast exe-only build: reuses/incrementally builds the overlay artifact into
    dist\engram-overlay without running INSTALL steps 1-8.
    -FullBuild : force a full rebuild (maps to build-overlay -Mode rebuild)
    -FullSmoke : pass through to build-overlay
    -Start     : launch the overlay after publishing (default: -NoStart)
#>
param(
    [switch]$FullBuild,
    [switch]$FullSmoke,
    [switch]$Start
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$BuildOverlay = Join-Path $PSScriptRoot "build-overlay.ps1"
$DeployDir = Join-Path $Root "dist\engram-overlay"

$buildArgs = @{
    Mode   = if ($FullBuild) { "rebuild" } else { "auto" }
    Deploy = $DeployDir
}
if ($FullSmoke) { $buildArgs.FullSmoke = $true }
if (-not $Start) { $buildArgs.NoStart = $true }

& $BuildOverlay @buildArgs
exit $LASTEXITCODE
