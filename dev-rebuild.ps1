#Requires -Version 5
param(
    [string]$Deploy = "",
    [switch]$NoStart,
    [string]$CondaEnv = "intel_engram"
)

$ErrorActionPreference = "Stop"
$engine = Join-Path $PSScriptRoot "installer\build-overlay.ps1"
if (-not (Test-Path $engine)) {
    Write-Error "Overlay build engine not found: $engine"
    exit 1
}

$arguments = @{
    Mode = "rebuild"
    CondaEnv = $CondaEnv
}
if ($Deploy) { $arguments.Deploy = $Deploy }
if ($NoStart) { $arguments.NoStart = $true }

& $engine @arguments
exit $LASTEXITCODE
