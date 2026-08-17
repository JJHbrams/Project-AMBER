#Requires -Version 5
param(
    [string]$Deploy = "",
    [switch]$NoStart,
    [switch]$FreshBuild,
    [string]$CondaEnv = "intel_engram"
)

$ErrorActionPreference = "Stop"
$engine = Join-Path $PSScriptRoot "installer\build-overlay.ps1"
if (-not (Test-Path $engine)) {
    Write-Error "Overlay build engine not found: $engine"
    exit 1
}

$arguments = @{
    Mode = if ($FreshBuild) { "rebuild" } else { "auto" }
    CondaEnv = $CondaEnv
    Deploy = $(if ($Deploy) { $Deploy } else { Join-Path $PSScriptRoot "dist\\engram-overlay" })
}
if ($NoStart) { $arguments.NoStart = $true }

& $engine @arguments
exit $LASTEXITCODE
