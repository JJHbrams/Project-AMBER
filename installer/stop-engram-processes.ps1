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

if (-not ('EngramArtifactProcessNative' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.Text;
using System.Runtime.InteropServices;
public static class EngramArtifactProcessNative {
    [DllImport("kernel32.dll", SetLastError=true, CharSet=CharSet.Unicode)]
    public static extern bool QueryFullProcessImageName(IntPtr process, uint flags, StringBuilder path, ref uint size);
    [DllImport("kernel32.dll", SetLastError=true)]
    public static extern bool GetExitCodeProcess(IntPtr process, out uint code);
    [DllImport("kernel32.dll", SetLastError=true)]
    public static extern bool TerminateProcess(IntPtr process, uint code);
}
'@
}

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
                # Pin the OS object before reading its image. A later PID reuse
                # cannot redirect termination to a different process.
                $handle = $process.Handle
                $buffer = [Text.StringBuilder]::new(32768)
                [uint32]$size = $buffer.Capacity
                if (-not [EngramArtifactProcessNative]::QueryFullProcessImageName($handle, 0, $buffer, [ref]$size)) {
                    throw 'Unable to query process image'
                }
                $processPath = [IO.Path]::GetFullPath($buffer.ToString())
            } catch {
                if (-not $process.HasExited) { throw }
                continue
            }
            if (-not $managedExecutables.ContainsKey($processPath)) {
                continue
            }
            [uint32]$code = 0
            if (-not [EngramArtifactProcessNative]::GetExitCodeProcess($handle, [ref]$code)) {
                throw "Cannot verify process liveness: $processPath"
            }
            if ($code -ne 259) { continue }
            if (-not [EngramArtifactProcessNative]::TerminateProcess($handle, 1)) {
                throw "Could not stop verified artifact process: $processPath"
            }
            $stopped += [PSCustomObject]@{
                Process = $process
                Path = $processPath
                Name = $managedExecutables[$processPath]
            }
        }
    }

    foreach ($entry in $stopped) {
        $null = $entry.Process.WaitForExit($TimeoutSeconds * 1000)
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
