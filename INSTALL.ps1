<#
.SYNOPSIS
    Engram Installer — root entry point
    Install: .\INSTALL.ps1
    Joint login startup: .\INSTALL.ps1 -AutoStart on
    External runtime: -ExternalOverlayMode none|reuse|bolttagu-2d|later (default none)
    Selective install: -ExternalOverlayComponents rabbit-2d,robot-arm-3d-v3
    Independent authoring guide/example: -ExternalOverlaySdk yes (default no)
    Component installation preserves the active character and existing presets.
    Preserve startup (default): -AutoStart preserve; owned entries off: -AutoStart off
    Skip immediate renderer launch: -ExternalOverlay skip; skip both launches: -NoStart
    Install (overlay build mode): .\INSTALL.ps1 -OverlayBuildMode auto|rebuild|clean|skip
    Remove:  .\INSTALL.ps1 -Uninstall

    Delegates to: installer\install.ps1
#>

param(
    [switch]$Uninstall,
    [ValidateSet("auto", "rebuild", "clean", "skip")]
    [string]$OverlayBuildMode = "auto",
    [ValidateSet('preserve','on','off')][string]$AutoStart = 'preserve',
    [ValidateSet('start','skip')][string]$ExternalOverlay = 'start',
    [ValidateSet('none','reuse','bolttagu-2d','later')][string]$ExternalOverlayMode = 'none',
    # Install optional presets without changing the active character. Comma-separated IDs.
    [string]$ExternalOverlayComponents = '',
    [ValidateSet('yes','no')][string]$ExternalOverlaySdk = 'no',
    [switch]$NoStart,
    [switch]$Reconfigure
)

$installer = Join-Path $PSScriptRoot "installer\install.ps1"
if (-not (Test-Path $installer)) {
    Write-Error "Installer not found: $installer"
    exit 1
}

& $installer @PSBoundParameters
