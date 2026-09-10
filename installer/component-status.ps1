param([Parameter(Mandatory)][string]$OutputPath, [string]$Root = (Join-Path $env:LOCALAPPDATA 'engram-overlay'))
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'external-components.ps1')
# Read-only inventory. The only write is the caller's installer-temporary result.
$inventory = Get-EngramInstalledComponents -Root $Root
$lines = @('ownership=' + $inventory.Ownership) + @($inventory.InstalledComponents)
[IO.File]::WriteAllLines($OutputPath, [string[]]$lines, [Text.UTF8Encoding]::new($false))
