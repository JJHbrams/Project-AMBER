#Requires -Version 5
<#!
.SYNOPSIS
    Moves a running engram-mcp-bridge.exe out of the way during setup and puts it back on failure.
.DESCRIPTION
    Bridge processes belong to client sessions, so setup must not stop them. NTFS lets a
    running exe be renamed, which releases its file name for the new copy while the
    session keeps running from the renamed file. Only the exact image path under
    ArtifactDir is considered; a same-named exe elsewhere is ignored.
#>
param(
    [ValidateSet("", "Sidestep", "Restore", "Cleanup")][string]$Action = "",
    [string]$ArtifactDir = "",
    [string]$ManifestPath = ""
)

$ErrorActionPreference = "Stop"

if (-not ('EngramBridgeProcessNative' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.Text;
using System.Runtime.InteropServices;
public static class EngramBridgeProcessNative {
    [DllImport("kernel32.dll", SetLastError=true, CharSet=CharSet.Unicode)]
    public static extern bool QueryFullProcessImageName(IntPtr process, uint flags, StringBuilder path, ref uint size);
    [DllImport("kernel32.dll", SetLastError=true)]
    public static extern bool GetExitCodeProcess(IntPtr process, out uint code);
}
'@
}

$script:BridgeName = "engram-mcp-bridge.exe"

function Test-EngramBridgeRunning {
    param([Parameter(Mandatory)][string]$BridgePath)

    $target = [IO.Path]::GetFullPath($BridgePath)
    foreach ($process in @(Get-Process -Name ([IO.Path]::GetFileNameWithoutExtension($script:BridgeName)) -ErrorAction SilentlyContinue)) {
        try {
            # Pin the OS object before reading its image (PID reuse safe).
            $handle = $process.Handle
            $buffer = [Text.StringBuilder]::new(32768)
            [uint32]$size = $buffer.Capacity
            if (-not [EngramBridgeProcessNative]::QueryFullProcessImageName($handle, 0, $buffer, [ref]$size)) {
                throw 'Unable to query process image'
            }
            $processPath = [IO.Path]::GetFullPath($buffer.ToString())
            [uint32]$code = 0
            if (-not [EngramBridgeProcessNative]::GetExitCodeProcess($handle, [ref]$code)) {
                throw 'Unable to query process state'
            }
        } catch {
            if ($process.HasExited) { continue }
            throw "Cannot verify bridge process image (pid $($process.Id)): $($_.Exception.Message)"
        }
        if ($code -ne 259) { continue }
        if ([string]::Equals($processPath, $target, [StringComparison]::OrdinalIgnoreCase)) {
            return $true
        }
    }
    return $false
}

function Move-EngramRunningBridgeAside {
    param(
        [Parameter(Mandatory)][string]$ArtifactDir,
        [Parameter(Mandatory)][string]$ManifestPath
    )

    $bridge = [IO.Path]::GetFullPath((Join-Path $ArtifactDir $script:BridgeName))
    if (-not (Test-Path -LiteralPath $bridge -PathType Leaf)) {
        Write-Host "Bridge sidestep: no installed bridge at $bridge."
        return $null
    }
    if (-not (Test-EngramBridgeRunning -BridgePath $bridge)) {
        Write-Host "Bridge sidestep: bridge is not running; leaving it in place."
        return $null
    }

    $stamp = [DateTime]::Now
    do {
        $aside = "$bridge.$($stamp.ToString('yyyyMMddHHmmss')).old"
        $stamp = $stamp.AddSeconds(1)
    } while (Test-Path -LiteralPath $aside)

    Move-Item -LiteralPath $bridge -Destination $aside
    $manifestDir = Split-Path -Parent $ManifestPath
    if ($manifestDir -and -not (Test-Path -LiteralPath $manifestDir)) {
        $null = New-Item -ItemType Directory -Path $manifestDir -Force
    }
    [IO.File]::AppendAllText($ManifestPath, "$aside|$bridge`r`n", (New-Object Text.UTF8Encoding($false)))
    Write-Host "Bridge sidestep: renamed running bridge to $aside"
    return $aside
}

function Restore-EngramBridgeAside {
    param([Parameter(Mandatory)][string]$ManifestPath)

    if (-not (Test-Path -LiteralPath $ManifestPath)) {
        Write-Host "Bridge restore: nothing to restore."
        return
    }
    foreach ($line in @(Get-Content -LiteralPath $ManifestPath -Encoding UTF8)) {
        if (-not $line -or -not $line.Contains('|')) { continue }
        $parts = $line.Split([char[]]'|', 2)
        $aside = $parts[0]
        $original = $parts[1]
        if (-not (Test-Path -LiteralPath $aside -PathType Leaf)) {
            Write-Host "Bridge restore: renamed file is gone, skipping: $aside"
            continue
        }
        if (Test-Path -LiteralPath $original) {
            Write-Host "Bridge restore: original already exists, not restoring: $original (kept $aside)"
            continue
        }
        try {
            Move-Item -LiteralPath $aside -Destination $original
            Write-Host "Bridge restore: restored $original"
        } catch {
            Write-Host "Bridge restore: could not restore ${original}: $($_.Exception.Message)"
        }
    }
    Remove-Item -LiteralPath $ManifestPath -Force -ErrorAction SilentlyContinue
}

function Remove-EngramBridgeAsideFiles {
    param([Parameter(Mandatory)][string]$ArtifactDir)

    $root = [IO.Path]::GetFullPath($ArtifactDir)
    if (-not (Test-Path -LiteralPath $root -PathType Container)) { return }
    foreach ($file in @(Get-ChildItem -LiteralPath $root -Filter "$($script:BridgeName).*.old" -File -ErrorAction SilentlyContinue)) {
        if ($file.Name -notmatch '^engram-mcp-bridge\.exe\.\d{14}\.old$') { continue }
        try {
            Remove-Item -LiteralPath $file.FullName -Force
            Write-Host "Bridge cleanup: removed $($file.FullName)"
        } catch {
            Write-Host "Bridge cleanup: still in use, kept $($file.FullName)"
        }
    }
}

if ($MyInvocation.InvocationName -ne ".") {
    switch ($Action) {
        "Sidestep" {
            if (-not $ArtifactDir -or -not $ManifestPath) { throw "ArtifactDir and ManifestPath are required." }
            $null = Move-EngramRunningBridgeAside -ArtifactDir $ArtifactDir -ManifestPath $ManifestPath
        }
        "Restore" {
            if (-not $ManifestPath) { throw "ManifestPath is required." }
            Restore-EngramBridgeAside -ManifestPath $ManifestPath
        }
        "Cleanup" {
            if (-not $ArtifactDir) { throw "ArtifactDir is required." }
            Remove-EngramBridgeAsideFiles -ArtifactDir $ArtifactDir
        }
        default { throw "Action is required (Sidestep, Restore, Cleanup)." }
    }
}
