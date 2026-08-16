#Requires -Version 5
<#
.SYNOPSIS
    Shared frozen overlay build engine.
#>
param(
    [ValidateSet("auto", "rebuild", "clean", "skip")]
    [string]$Mode = "auto",
    [string]$Deploy = "",
    [switch]$NoStart,
    [string]$CondaEnv = "intel_engram",
    [string]$PythonPath = "",
    [switch]$ValidateOnly
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Spec = Join-Path $Root "engram-overlay.spec"
$DefaultDist = Join-Path $Root "dist\engram-overlay"
$ModelDir = Join-Path $Root "resource\embedding-model"
$ModelManifest = Join-Path $ModelDir "manifest.json"
$ModelId = "intfloat/multilingual-e5-small"

function Write-OverlayStep([string]$Message) {
    Write-Host "`n==> $Message" -ForegroundColor Cyan
}

function Write-OverlayOk([string]$Message) {
    Write-Host "  [OK] $Message" -ForegroundColor Green
}

function Write-OverlayWarn([string]$Message) {
    Write-Host "  [!] $Message" -ForegroundColor Yellow
}

function Resolve-OverlayPython {
    param([string]$RequestedPath, [string]$EnvironmentName)

    if ($RequestedPath -and (Test-Path $RequestedPath)) {
        return (Resolve-Path $RequestedPath).Path
    }

    $conda = Get-Command conda -ErrorAction SilentlyContinue
    if ($conda) {
        $envLines = @(& $conda.Source info --envs 2>&1)
        foreach ($line in $envLines) {
            if ($line.ToString() -match "^\s*$([regex]::Escape($EnvironmentName))\s+\*?\s*(.+?)\s*$") {
                $candidate = Join-Path $Matches[1].Trim() "python.exe"
                if (Test-Path $candidate) {
                    return (Resolve-Path $candidate).Path
                }
            }
        }
    }

    $candidates = @(
        "$env:USERPROFILE\miniconda3\envs\$EnvironmentName\python.exe",
        "$env:USERPROFILE\anaconda3\envs\$EnvironmentName\python.exe",
        "$env:LOCALAPPDATA\miniconda3\envs\$EnvironmentName\python.exe",
        "C:\miniconda3\envs\$EnvironmentName\python.exe",
        "C:\anaconda3\envs\$EnvironmentName\python.exe"
    )
    foreach ($candidate in $candidates) {
        if (Test-Path $candidate) {
            return (Resolve-Path $candidate).Path
        }
    }

    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($python) {
        return $python.Source
    }
    throw "Python executable not found for environment '$EnvironmentName'"
}

function Invoke-OverlayPython {
    param(
        [Parameter(Mandatory)][string]$Python,
        [Parameter(Mandatory)][string[]]$Arguments
    )

    $output = @()
    $exitCode = 1
    Push-Location $Root
    try {
        $output = @(& $Python @Arguments 2>&1)
        $exitCode = $LASTEXITCODE
    } finally {
        Pop-Location
    }
    return [PSCustomObject]@{
        ExitCode = [int]$exitCode
        Output = $output
    }
}

function Stop-OverlayProcesses {
    $runningPath = ""
    $stoppedDesktopProcess = $false
    $processes = @(Get-Process -Name "engram-overlay" -ErrorAction SilentlyContinue)
    if ($processes.Count -gt 0) {
        try { $runningPath = $processes[0].Path } catch {}
        $processes | Stop-Process -Force -ErrorAction SilentlyContinue
        $stoppedDesktopProcess = $true
    }
    # The frozen dashboard lives beside the overlay artifact and keeps its exe
    # open while Streamlit is running. It must be stopped before the staged
    # artifact can replace that directory, but it is not a restart target.
    $dashboardProcesses = @(Get-Process -Name "engram-dashboard" -ErrorAction SilentlyContinue)
    if ($dashboardProcesses.Count -gt 0) {
        $dashboardProcesses | Stop-Process -Force -ErrorAction SilentlyContinue
        $stoppedDesktopProcess = $true
    }
    if ($stoppedDesktopProcess) {
        Start-Sleep -Milliseconds 800
    }
    foreach ($pattern in @("mcp_server.py", "engram_dashboard.py", "kg_watcher.py")) {
        $processIds = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
            Where-Object { $_.CommandLine -like "*$pattern*" } |
            Select-Object -ExpandProperty ProcessId
        foreach ($processId in $processIds) {
            try {
                Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
            } catch {}
        }
    }
    return $runningPath
}

function Invoke-OverlayRole {
    param(
        [Parameter(Mandatory)][string]$Executable,
        [Parameter(Mandatory)][string]$Role
    )

    $smokeLog = Join-Path ([IO.Path]::GetTempPath()) `
        ("engram-smoke-" + [Guid]::NewGuid().ToString("N") + ".log")
    $previousLog = $env:ENGRAM_SMOKE_LOG
    $previousSmokeDb = $env:ENGRAM_SMOKE_DB_DIR
    $smokeDb = $null
    $env:ENGRAM_SMOKE_LOG = $smokeLog
    if ($Role -eq "smoke-check") {
        $smokeDb = Join-Path ([IO.Path]::GetTempPath()) `
            ("engram-smoke-db-" + [Guid]::NewGuid().ToString("N"))
        New-Item -ItemType Directory -Path $smokeDb -Force | Out-Null
        $env:ENGRAM_SMOKE_DB_DIR = $smokeDb
    }
    try {
        $process = Start-Process -FilePath $Executable `
            -ArgumentList @("--role", $Role) `
            -Wait -PassThru -WindowStyle Hidden
        if ($process.ExitCode -ne 0 -and (Test-Path $smokeLog)) {
            Write-OverlayWarn (Get-Content $smokeLog -Raw)
        }
        return [int]$process.ExitCode
    } finally {
        $env:ENGRAM_SMOKE_LOG = $previousLog
        $env:ENGRAM_SMOKE_DB_DIR = $previousSmokeDb
        if ($smokeDb) {
            Remove-Item $smokeDb -Recurse -Force -ErrorAction SilentlyContinue
        }
        Remove-Item $smokeLog -Force -ErrorAction SilentlyContinue
    }
}

function Invoke-DashboardSmoke {
    param([Parameter(Mandatory)][string]$ArtifactDir)

    $dashboardExe = Join-Path $ArtifactDir "engram-dashboard.exe"
    if (-not (Test-Path $dashboardExe)) {
        Write-OverlayWarn "Dashboard sidecar missing: $dashboardExe"
        return 1
    }

    $smokeLog = Join-Path ([IO.Path]::GetTempPath()) `
        ("engram-dashboard-smoke-" + [Guid]::NewGuid().ToString("N") + ".log")
    $previousLog = $env:ENGRAM_SMOKE_LOG
    $env:ENGRAM_SMOKE_LOG = $smokeLog
    try {
        $render = Start-Process -FilePath $dashboardExe `
            -ArgumentList @("--smoke-check") `
            -Wait -PassThru -WindowStyle Hidden
        if ($render.ExitCode -ne 0) {
            if (Test-Path $smokeLog) {
                Write-OverlayWarn (Get-Content $smokeLog -Raw)
            }
            Write-OverlayWarn "Dashboard render smoke failed (exit $($render.ExitCode))"
            return [int]$render.ExitCode
        }
    } finally {
        $env:ENGRAM_SMOKE_LOG = $previousLog
        Remove-Item $smokeLog -Force -ErrorAction SilentlyContinue
    }

    $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 0)
    $listener.Start()
    $port = ([Net.IPEndPoint]$listener.LocalEndpoint).Port
    $listener.Stop()

    $server = $null
    try {
        $server = Start-Process -FilePath $dashboardExe `
            -ArgumentList @("--port", "$port") `
            -PassThru -WindowStyle Hidden
        $deadline = (Get-Date).AddSeconds(45)
        while ((Get-Date) -lt $deadline) {
            if ($server.HasExited) {
                Write-OverlayWarn "Dashboard server exited during smoke test (exit $($server.ExitCode))"
                return [int]$server.ExitCode
            }
            try {
                $response = Invoke-WebRequest -Uri "http://127.0.0.1:$port/_stcore/health" `
                    -TimeoutSec 2 -UseBasicParsing
                if ($response.StatusCode -eq 200 -and $response.Content.Trim() -eq "ok") {
                    return 0
                }
            } catch {}
            Start-Sleep -Milliseconds 500
        }
        Write-OverlayWarn "Dashboard server health smoke timed out"
        return 1
    } finally {
        if ($server -and -not $server.HasExited) {
            Stop-Process -Id $server.Id -Force -ErrorAction SilentlyContinue
        }
    }
}

function Publish-OverlayArtifact {
    param(
        [Parameter(Mandatory)][string]$SourceDir,
        [Parameter(Mandatory)][string]$TargetDir
    )

    $target = [IO.Path]::GetFullPath($TargetDir)
    $parent = Split-Path -Parent $target
    if (-not (Test-Path $parent)) {
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
    }
    $stage = Join-Path $parent (".engram-overlay-stage-" + [Guid]::NewGuid().ToString("N"))
    $backup = Join-Path $parent (".engram-overlay-backup-" + [Guid]::NewGuid().ToString("N"))
    $oldMoved = $false
    $published = $false
    try {
        New-Item -ItemType Directory -Path $stage -Force | Out-Null
        Copy-Item -Path (Join-Path $SourceDir "*") -Destination $stage -Recurse -Force

        if (Test-Path $target) {
            Move-Item -Path $target -Destination $backup
            $oldMoved = $true
        }
        try {
            Move-Item -Path $stage -Destination $target
            $published = $true
        } catch {
            if ($oldMoved -and -not (Test-Path $target)) {
                Move-Item -Path $backup -Destination $target -ErrorAction SilentlyContinue
            }
            throw
        }
    } catch {
        if (Test-Path $stage) {
            Remove-Item -Path $stage -Recurse -Force -ErrorAction SilentlyContinue
        }
        if ($oldMoved -and (Test-Path $backup) -and -not (Test-Path $target)) {
            Move-Item -Path $backup -Destination $target -ErrorAction SilentlyContinue
        }
        throw
    } finally {
        if (Test-Path $stage) {
            Remove-Item -Path $stage -Recurse -Force -ErrorAction SilentlyContinue
        }
        if ($published -and (Test-Path $backup)) {
            Remove-Item -Path $backup -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
}

function Get-LastOutput([object[]]$Output) {
    $lines = @($Output | ForEach-Object { $_.ToString() } | Where-Object { $_.Trim() })
    if ($lines.Count -eq 0) { return "" }
    return $lines[$lines.Count - 1]
}

if ($Mode -eq "skip") {
    Write-OverlayWarn "Overlay build skipped"
    exit 0
}

$python = $null
$runningPath = ""
$tempRoot = $null
$success = $false
$exitCode = 1

try {
    $python = Resolve-OverlayPython $PythonPath $CondaEnv
    if (-not (Test-Path $Spec)) {
        throw "Overlay spec not found: $Spec"
    }

    if ($ValidateOnly) {
        $validation = Invoke-OverlayPython $python @(
            "-m", "core.install.overlay_manifest",
            "--root", $Root,
            "--artifact", $DefaultDist,
            "--model-manifest", $ModelManifest,
            "--validate"
        )
        $last = Get-LastOutput $validation.Output
        if ($validation.ExitCode -eq 0) {
            Write-OverlayOk "Validated overlay artifact: $DefaultDist"
            exit 0
        }
        Write-OverlayWarn "Overlay artifact is not reusable: $last"
        exit 1
    }

    Write-OverlayStep "Validating offline embedding model"
    $modelCheck = Invoke-OverlayPython $python @(
        "-m", "core.install.model_manifest",
        "--model-dir", $ModelDir,
        "--model-id", $ModelId,
        "--ensure",
        "--allow-download"
    )
    if ($modelCheck.ExitCode -ne 0) {
        throw "Embedding model validation/export failed: $(Get-LastOutput $modelCheck.Output)"
    }
    Write-OverlayOk "Embedding model manifest validated"

    $validation = Invoke-OverlayPython $python @(
        "-m", "core.install.overlay_manifest",
        "--root", $Root,
        "--artifact", $DefaultDist,
        "--model-manifest", $ModelManifest,
        "--validate"
    )
    $reuseValid = ($validation.ExitCode -eq 0)
    if ($Mode -eq "auto" -and $reuseValid -and -not $Deploy) {
        Write-OverlayOk "Reusing validated overlay artifact: $DefaultDist"
        if (-not $NoStart) {
            $targetExe = Join-Path $DefaultDist "engram-overlay.exe"
            Start-Process -FilePath $targetExe
        }
        $success = $true
        $exitCode = 0
        exit 0
    }

    $runningPath = Stop-OverlayProcesses
    $deployTarget = if ($Deploy) {
        $Deploy
    } elseif ($runningPath) {
        Split-Path -Parent $runningPath
    } else {
        $DefaultDist
    }
    if (-not [IO.Path]::IsPathRooted($deployTarget)) {
        $deployTarget = Join-Path $Root $deployTarget
    }
    $buildWorkPath = Join-Path $Root "build\engram-overlay"
    Write-OverlayOk "Deploy target: $deployTarget"

    $cleanRetried = $false
    $attemptClean = ($Mode -eq "clean")
    while ($true) {
        $tempRoot = Join-Path ([IO.Path]::GetTempPath()) ("engram-overlay-build-" + [Guid]::NewGuid().ToString("N"))
        $tempDist = Join-Path $tempRoot "dist"
        $tempArtifact = Join-Path $tempDist "engram-overlay"
        New-Item -ItemType Directory -Path $tempRoot -Force | Out-Null

        Write-OverlayStep "PyInstaller build ($(if ($attemptClean) { "clean" } else { "incremental" }))"
        $buildArgs = @(
            "-m", "PyInstaller",
            "--noconfirm",
            "--distpath", $tempDist,
            "--workpath", $buildWorkPath
        )
        if ($attemptClean) {
            $buildArgs += "--clean"
        }
        $buildArgs += $Spec
        $build = Invoke-OverlayPython $python $buildArgs
        $buildLog = Join-Path ([IO.Path]::GetTempPath()) `
            ("engram-overlay-build-" + [Guid]::NewGuid().ToString("N") + ".log")
        [IO.File]::WriteAllText(
            $buildLog,
            (($build.Output | ForEach-Object { $_.ToString() }) -join [Environment]::NewLine),
            [Text.UTF8Encoding]::new($false)
        )

        $artifactsReady = (
            (Test-Path (Join-Path $tempArtifact "engram-overlay.exe")) -and
            (Test-Path (Join-Path $tempArtifact "engram-dashboard.exe"))
        )
        if ($build.ExitCode -eq 0 -and $artifactsReady) {
            $manifest = Invoke-OverlayPython $python @(
                "-m", "core.install.overlay_manifest",
                "--root", $Root,
                "--artifact", $tempArtifact,
                "--model-manifest", $ModelManifest,
                "--mode", $Mode,
                "--write"
            )
            if ($manifest.ExitCode -eq 0) {
                Write-OverlayStep "Running frozen role smoke tests"
                $embeddingExit = Invoke-OverlayRole (Join-Path $tempArtifact "engram-overlay.exe") "embedding-check"
                $smokeExit = if ($embeddingExit -eq 0) {
                    Invoke-OverlayRole (Join-Path $tempArtifact "engram-overlay.exe") "smoke-check"
                } else {
                    1
                }
                $dashboardExit = if ($embeddingExit -eq 0 -and $smokeExit -eq 0) {
                    Invoke-DashboardSmoke $tempArtifact
                } else {
                    1
                }
                if ($embeddingExit -eq 0 -and $smokeExit -eq 0 -and $dashboardExit -eq 0) {
                    Publish-OverlayArtifact $tempArtifact $deployTarget
                    Write-OverlayOk "Built and published: $deployTarget"
                    if (-not $NoStart) {
                        Start-Process -FilePath (Join-Path $deployTarget "engram-overlay.exe")
                    }
                    $success = $true
                    $exitCode = 0
                    break
                }
                Write-OverlayWarn "Role smoke tests failed (embedding=$embeddingExit, smoke=$smokeExit, dashboard=$dashboardExit)"
                throw "Frozen role smoke tests failed; existing artifact was preserved"
            } else {
                throw "Build manifest generation failed: $(Get-LastOutput $manifest.Output)"
            }
        } else {
            Write-OverlayWarn "PyInstaller failed or required executables are missing; see $buildLog"
        }

        if (($Mode -in @("auto", "rebuild")) -and -not $attemptClean -and -not $cleanRetried) {
            Write-OverlayWarn "Retrying once with a clean build"
            $attemptClean = $true
            $cleanRetried = $true
            if ($tempRoot) {
                Remove-Item -Path $tempRoot -Recurse -Force -ErrorAction SilentlyContinue
                $tempRoot = $null
            }
            continue
        }
        throw "Overlay build failed; existing artifact was preserved"
    }
} catch {
    Write-OverlayWarn $_.Exception.Message
    $exitCode = 1
} finally {
    if ($tempRoot -and (Test-Path $tempRoot)) {
        Remove-Item -Path $tempRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
    if (-not $success -and -not $NoStart -and $runningPath -and (Test-Path $runningPath)) {
        Start-Process -FilePath $runningPath
    }
}

exit $exitCode
