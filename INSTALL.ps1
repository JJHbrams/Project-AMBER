<#
.SYNOPSIS
    Engram Installer — root entry point
    Install: .\INSTALL.ps1
    Install (overlay build mode): .\INSTALL.ps1 -OverlayBuildMode auto|rebuild|clean|skip
    Remove:  .\INSTALL.ps1 -Uninstall

    Delegates to: installer\install.ps1
#>

param(
    [switch]$Uninstall,
    [ValidateSet("auto", "rebuild", "clean", "skip")]
    [string]$OverlayBuildMode = "auto"
)

$installer = Join-Path $PSScriptRoot "installer\install.ps1"
if (-not (Test-Path $installer)) {
    Write-Error "Installer not found: $installer"
    exit 1
}

& $installer @PSBoundParameters
