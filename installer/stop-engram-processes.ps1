#Requires -Version 5
<#!
.SYNOPSIS
    Stops only Engram executables that belong to one frozen artifact directory.
#>
param(
    [string]$ArtifactDir = "",
    [ValidateRange(1, 60)][int]$TimeoutSeconds = 10
)

$ErrorActionPreference = "Stop"

function Stop-EngramArtifactProcesses {
    param(
        [Parameter(Mandatory)][string]$ArtifactDir,
        [ValidateRange(1, 60)][int]$TimeoutSeconds = 10
    )

    $artifactRoot = [IO.Path]::GetFullPath($ArtifactDir).TrimEnd('\', '/')
    $managedExecutables = @{}
    foreach ($name in @("engram-overlay.exe", "engram-dashboard.exe")) {
        $managedExecutables[[IO.Path]::GetFullPath((Join-Path $artifactRoot $name))] = $name
    }

    $stopped = @()
    foreach ($processName in @("engram-overlay", "engram-dashboard")) {
        foreach ($process in @(Get-Process -Name $processName -ErrorAction SilentlyContinue)) {
            try {
                $processPath = [IO.Path]::GetFullPath($process.Path)
            } catch {
                continue
            }
            if (-not $managedExecutables.ContainsKey($processPath)) {
                continue
            }
            $stopped += [PSCustomObject]@{
                Process = $process
                Path = $processPath
                Name = $managedExecutables[$processPath]
            }
            Stop-Process -Id $process.Id -Force -ErrorAction Stop
        }
    }

    foreach ($entry in $stopped) {
        $entry.Process.WaitForExit($TimeoutSeconds * 1000)
        if (-not $entry.Process.HasExited) {
            throw "Timed out stopping Engram process: $($entry.Path)"
        }
    }

    return [PSCustomObject]@{
        OverlayPaths = @($stopped | Where-Object { $_.Name -eq "engram-overlay.exe" } | Select-Object -ExpandProperty Path -Unique)
        StoppedPaths = @($stopped | Select-Object -ExpandProperty Path -Unique)
    }
}

if ($MyInvocation.InvocationName -ne ".") {
    if (-not $ArtifactDir) {
        throw "ArtifactDir is required."
    }
    $result = Stop-EngramArtifactProcesses -ArtifactDir $ArtifactDir -TimeoutSeconds $TimeoutSeconds
    foreach ($path in $result.StoppedPaths) {
        Write-Host "Stopped: $path"
    }
}
