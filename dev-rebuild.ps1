#Requires -Version 5
<#
.SYNOPSIS
    Validate and restart Engram directly from this source checkout.

.DESCRIPTION
    This development loop never invokes PyInstaller or changes dist/. Frozen
    bundles are owned by installer/build-installer.ps1.
#>
param(
    [switch]$NoStart,
    [string]$CondaEnv = "intel_engram",
    [string]$PythonPath = "",
    [ValidateRange(10, 600)]
    [int]$ReadyTimeoutSeconds = 180,

    # Kept only to produce an actionable migration error for old automation.
    [string]$Deploy = "",
    [switch]$FreshBuild
)

$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
$Entry = Join-Path $Root "engram_overlay_entry.py"

function Write-Step([string]$Message) { Write-Host "$([Environment]::NewLine)==> $Message" -ForegroundColor Cyan }
function Write-Ok([string]$Message) { Write-Host "  [OK] $Message" -ForegroundColor Green }

function Resolve-SourcePython {
    if ($PythonPath) {
        if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
            throw "Requested -PythonPath does not exist: $PythonPath"
        }
        return (Resolve-Path -LiteralPath $PythonPath).Path
    }

    $conda = Get-Command conda -ErrorAction SilentlyContinue
    if ($conda) {
        foreach ($line in @(& $conda.Source info --envs 2>&1)) {
            if ($line.ToString() -match "^\s*$([regex]::Escape($CondaEnv))\s+\*?\s*(.+?)\s*$") {
                $candidate = Join-Path $Matches[1].Trim() "python.exe"
                if (Test-Path -LiteralPath $candidate -PathType Leaf) {
                    return (Resolve-Path -LiteralPath $candidate).Path
                }
            }
        }
    }

    foreach ($candidate in @(
        "$env:USERPROFILE\miniconda3\envs\$CondaEnv\python.exe",
        "$env:USERPROFILE\anaconda3\envs\$CondaEnv\python.exe",
        "$env:LOCALAPPDATA\miniconda3\envs\$CondaEnv\python.exe",
        "C:\miniconda3\envs\$CondaEnv\python.exe",
        "C:\anaconda3\envs\$CondaEnv\python.exe"
    )) {
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }
    throw "Python for conda environment '$CondaEnv' was not found. Pass -PythonPath explicitly."
}

function Get-HealthJson([string]$Uri) {
    try {
        return Invoke-RestMethod -Uri $Uri -TimeoutSec 2 -UseBasicParsing
    } catch {
        return $null
    }
}

function Get-DirectChild([int]$ParentPid, [string]$CommandFragment) {
    return Get-CimInstance Win32_Process -Filter "ParentProcessId = $ParentPid" -ErrorAction SilentlyContinue |
        Where-Object {
            $_.CommandLine -and
            $_.CommandLine.IndexOf($CommandFragment, [StringComparison]::OrdinalIgnoreCase) -ge 0
        } |
        Select-Object -First 1
}

function Save-SourceChildSnapshot([Diagnostics.Process]$Process, [string]$Python, [string]$SourceRoot, [string]$OutputPath) {
    $previousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        & $Python -m core.install.process_identity snapshot --parent-pid $Process.Id --source-root $SourceRoot --output $OutputPath 2>&1 | Out-Null
        return ($LASTEXITCODE -eq 0 -and (Test-Path -LiteralPath $OutputPath -PathType Leaf))
    } finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
}

function Remove-SnapshottedSourceChildren([string]$Python, [string]$SourceRoot, [string]$SnapshotPath) {
    if (-not (Test-Path -LiteralPath $SnapshotPath -PathType Leaf)) { return }
    $previousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        & $Python -m core.install.process_identity cleanup-snapshot --source-root $SourceRoot --snapshot $SnapshotPath 2>&1 |
            ForEach-Object { Write-Host "  [cleanup] $($_.ToString())" }
    } finally {
        $ErrorActionPreference = $previousErrorActionPreference
        Remove-Item -LiteralPath $SnapshotPath -Force -ErrorAction SilentlyContinue
    }
}

function Remove-ProvenSourceOrphans([string]$Python, [string]$SourceRoot) {
    $previousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        & $Python -m core.install.process_identity cleanup-source-orphans --source-root $SourceRoot 2>&1 |
            ForEach-Object { Write-Host "  [cleanup] $($_.ToString())" }
    } finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
}

function Stop-StartedOverlay(
    [Diagnostics.Process]$Process,
    [int]$StmPort,
    [string]$Python,
    [string]$SourceRoot
) {
    if (-not $Process) { return }
    if ($Process.HasExited) {
        Remove-ProvenSourceOrphans $Python $SourceRoot
        return
    }
    $snapshotPath = Join-Path ([IO.Path]::GetTempPath()) ("engram-dev-children-" + [Guid]::NewGuid().ToString("N") + ".json")
    Save-SourceChildSnapshot $Process $Python $SourceRoot $snapshotPath | Out-Null
    $health = Get-HealthJson "http://127.0.0.1:$StmPort/health"
    if ($health -and $health.role -eq "overlay-stm" -and [int]$health.pid -eq $Process.Id) {
        try {
            $shutdownArgs = @{
                Uri = "http://127.0.0.1:$StmPort/shutdown"
                Method = "Post"
                ContentType = "application/json"
                Body = "{}"
                TimeoutSec = 3
                UseBasicParsing = $true
            }
            Invoke-RestMethod @shutdownArgs | Out-Null
            # Normal quit can spend up to 15 seconds promoting STM before it
            # closes managed MCP/watcher/dashboard children.
            if ($Process.WaitForExit(25000)) {
                Remove-SnapshottedSourceChildren $Python $SourceRoot $snapshotPath
                Remove-ProvenSourceOrphans $Python $SourceRoot
                return
            }
        } catch {}
    }
    # Refresh while the exact parent still exists so late-spawned/recovered
    # direct children are captured before forced parent termination reparents them.
    Save-SourceChildSnapshot $Process $Python $SourceRoot $snapshotPath | Out-Null
    Stop-Process -Id $Process.Id -Force -ErrorAction SilentlyContinue
    $Process.WaitForExit(5000) | Out-Null
    Remove-SnapshottedSourceChildren $Python $SourceRoot $snapshotPath
    Remove-ProvenSourceOrphans $Python $SourceRoot
}

if ($PSBoundParameters.ContainsKey("Deploy") -or $PSBoundParameters.ContainsKey("FreshBuild")) {
    throw "-Deploy and -FreshBuild are no longer supported by dev-rebuild.ps1. Use installer/build-installer.ps1 for frozen artifacts."
}
if (-not (Test-Path -LiteralPath $Entry -PathType Leaf)) {
    throw "Canonical source entrypoint not found: $Entry"
}

$python = Resolve-SourcePython
Write-Step "Source runtime contract"
$previousErrorActionPreference = $ErrorActionPreference
try {
    # Windows PowerShell promotes native stderr to NativeCommandError when Stop
    # is active; imported modules legitimately emit diagnostics during this gate.
    $ErrorActionPreference = "Continue"
    $contractOutput = @(& $python $Entry --role runtime-contract 2>&1)
    $contractExit = [int]$LASTEXITCODE
} finally {
    $ErrorActionPreference = $previousErrorActionPreference
}
if ($contractExit -ne 0) {
    throw "Source runtime contract failed (exit $contractExit): $($contractOutput -join [Environment]::NewLine)"
}
$contractLine = $contractOutput | ForEach-Object { $_.ToString() } |
    Where-Object { $_.Trim().StartsWith("{") } | Select-Object -Last 1
if (-not $contractLine) { throw "Source runtime contract returned no JSON evidence" }
$contract = $contractLine | ConvertFrom-Json
$expectedRoot = [IO.Path]::GetFullPath($Root).TrimEnd('\')
$contractRoot = [IO.Path]::GetFullPath([string]$contract.source_root).TrimEnd('\')
if ($contract.runtime -ne "source" -or $contractRoot -ne $expectedRoot) {
    throw "Source runtime provenance mismatch: runtime=$($contract.runtime), root=$($contract.source_root)"
}
Write-Ok "source contract passed: $python"

if ($NoStart) {
    Write-Ok "NoStart requested; source validation complete (dist/ unchanged)"
    exit 0
}

Write-Step "Restarting canonical source entrypoint"
$previousDevRestartMarker = $env:ENGRAM_DEV_SOURCE_RESTART
try {
    $env:ENGRAM_DEV_SOURCE_RESTART = "1"
    $overlay = Start-Process -FilePath $python -ArgumentList @($Entry) -WorkingDirectory $Root -PassThru -WindowStyle Hidden
} finally {
    $env:ENGRAM_DEV_SOURCE_RESTART = $previousDevRestartMarker
}
$ready = $false
$lastState = "waiting for overlay"
$deadline = (Get-Date).AddSeconds($ReadyTimeoutSeconds)
try {
    while ((Get-Date) -lt $deadline) {
        if ($overlay.HasExited) {
            throw "Source overlay exited before readiness (exit $($overlay.ExitCode)). Check ~/.engram/overlay.log."
        }

        $stm = Get-HealthJson "http://127.0.0.1:$($contract.stm_port)/health"
        if (-not $stm -or $stm.role -ne "overlay-stm" -or [int]$stm.pid -ne $overlay.Id) {
            $lastState = "STM does not belong to new overlay PID $($overlay.Id)"
            Start-Sleep -Milliseconds 500
            continue
        }

        $mcp = Get-HealthJson "http://127.0.0.1:$($contract.mcp_port)/health"
        $mcpRoot = if ($mcp) { [string]$mcp.source_root } else { "" }
        if (-not $mcp -or -not $mcpRoot -or $mcp.runtime -ne "source" -or [int]$mcp.parent_pid -ne $overlay.Id -or
            [IO.Path]::GetFullPath($mcpRoot).TrimEnd('\') -ne $expectedRoot) {
            $lastState = "MCP missing or stale (expected source parent PID $($overlay.Id) at $expectedRoot)"
            Start-Sleep -Milliseconds 500
            continue
        }

        $watcher = Get-DirectChild $overlay.Id (Join-Path $Root "scripts\kg\kg_watcher.py")
        if (-not $watcher) {
            $lastState = "kg-watcher child not ready for overlay PID $($overlay.Id)"
            Start-Sleep -Milliseconds 500
            continue
        }

        if ([bool]$contract.dashboard_enabled) {
            $dashboard = Get-DirectChild $overlay.Id (Join-Path $Root "scripts\engram_dashboard.py")
            $dashboardHealth = Get-HealthJson "http://127.0.0.1:$($contract.dashboard_port)/_stcore/health"
            if (-not $dashboard -or $dashboardHealth -ne "ok") {
                $lastState = "enabled dashboard child/health not ready"
                Start-Sleep -Milliseconds 500
                continue
            }
        }

        $ready = $true
        break
    }
    if (-not $ready) {
        throw "Source overlay readiness timed out after $ReadyTimeoutSeconds seconds: $lastState"
    }
    Write-Ok "source overlay ready: PID=$($overlay.Id), MCP PID=$($mcp.pid), frozen=False"
} catch {
    Stop-StartedOverlay $overlay ([int]$contract.stm_port) $python $Root
    throw
}

exit 0
