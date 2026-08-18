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
$ProcessStopHelper = Join-Path $PSScriptRoot "stop-engram-processes.ps1"

if (-not (Test-Path $ProcessStopHelper)) {
    throw "Engram process stop helper not found: $ProcessStopHelper"
}
. $ProcessStopHelper

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
    $previousErrorActionPreference = $ErrorActionPreference
    Push-Location $Root
    try {
        # PyInstaller writes normal progress records to stderr. Under Windows
        # PowerShell, ErrorActionPreference=Stop can turn the first INFO line
        # into a terminating NativeCommandError before we can inspect its exit
        # code or persist the build log.
        $ErrorActionPreference = "Continue"
        $output = @(& $Python @Arguments 2>&1)
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previousErrorActionPreference
        Pop-Location
    }
    return [PSCustomObject]@{
        ExitCode = [int]$exitCode
        Output = $output
    }
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
        # The verified artifact is disposable build output. Moving its directory
        # avoids copying 1+ GiB / 20k files before the atomic publish swap.
        Move-Item -LiteralPath $SourceDir -Destination $stage

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

function Invoke-SourceRuntimeContract([string]$Python) {
    $entry = Join-Path $Root "engram_overlay_entry.py"
    $result = Invoke-OverlayPython $Python @($entry, "--role", "runtime-contract")
    if ($result.ExitCode -ne 0) {
        Write-OverlayWarn "Source runtime contract failed: $(Get-LastOutput $result.Output)"
        return 1
    }
    try {
        $payload = (Get-LastOutput $result.Output) | ConvertFrom-Json
        $expectedRoot = [IO.Path]::GetFullPath($Root).TrimEnd('\')
        $actualRoot = [IO.Path]::GetFullPath([string]$payload.source_root).TrimEnd('\')
        if ($payload.runtime -ne "source" -or $actualRoot -ne $expectedRoot) {
            throw "runtime=$($payload.runtime), source_root=$($payload.source_root)"
        }
    } catch {
        Write-OverlayWarn "Source runtime contract returned invalid provenance: $($_.Exception.Message)"
        return 1
    }
    return 0
}

if ($Mode -eq "skip") {
    Write-OverlayWarn "Overlay build skipped"
    exit 0
}

$python = $null
$previousOverlayPaths = @()
$tempRoot = $null
$success = $false
$exitCode = 1

try {
    $python = Resolve-OverlayPython $PythonPath $CondaEnv
    if (-not (Test-Path $Spec)) {
        throw "Overlay spec not found: $Spec"
    }

    Write-OverlayStep "Running source runtime contract"
    if ((Invoke-SourceRuntimeContract $python) -ne 0) {
        throw "Source runtime contract failed before frozen build"
    }
    Write-OverlayOk "Source runtime contract passed"

    # Callers own the publish destination.  A running process is never used to
    # choose it, because multiple installed/development copies can coexist.
    $deployTarget = if ($Deploy) { $Deploy } else { $DefaultDist }
    if (-not [IO.Path]::IsPathRooted($deployTarget)) {
        $deployTarget = Join-Path $Root $deployTarget
    }
    $deployTarget = [IO.Path]::GetFullPath($deployTarget)

    if ($ValidateOnly) {
        $validation = Invoke-OverlayPython $python @(
            "-m", "core.install.overlay_manifest",
            "--root", $Root,
            "--artifact", $deployTarget,
            "--model-manifest", $ModelManifest,
            "--validate"
        )
        $last = Get-LastOutput $validation.Output
        if ($validation.ExitCode -eq 0) {
            $frozenContract = Invoke-OverlayRole (Join-Path $deployTarget "engram-overlay.exe") "runtime-contract"
            if ($frozenContract -ne 0) {
                Write-OverlayWarn "Existing artifact failed frozen runtime contract"
                exit 1
            }
            Write-OverlayOk "Validated overlay artifact: $deployTarget"
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
    $deploysToDefault = (-not $Deploy)
    if ($Deploy) {
        $requestedDeploy = if ([IO.Path]::IsPathRooted($Deploy)) {
            [IO.Path]::GetFullPath($Deploy)
        } else {
            [IO.Path]::GetFullPath((Join-Path $Root $Deploy))
        }
        $deploysToDefault = $requestedDeploy.TrimEnd('\') -eq `
            ([IO.Path]::GetFullPath($DefaultDist)).TrimEnd('\')
    }
    if ($Mode -eq "auto" -and $reuseValid -and $deploysToDefault) {
        $frozenContract = Invoke-OverlayRole (Join-Path $DefaultDist "engram-overlay.exe") "runtime-contract"
        if ($frozenContract -ne 0) {
            throw "Reusable artifact failed frozen runtime contract"
        }
        Write-OverlayOk "Reusing validated overlay artifact: $DefaultDist"
        if (-not $NoStart) {
            $targetExe = Join-Path $DefaultDist "engram-overlay.exe"
            Start-Process -FilePath $targetExe
        }
        $success = $true
        $exitCode = 0
        exit 0
    }

    $stoppedProcesses = Stop-EngramArtifactProcesses -ArtifactDir $deployTarget
    $previousOverlayPaths = @($stoppedProcesses.OverlayPaths)
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
                Write-OverlayStep "Running frozen runtime contract and role smoke tests"
                $runtimeExit = Invoke-OverlayRole (Join-Path $tempArtifact "engram-overlay.exe") "runtime-contract"
                $embeddingExit = if ($runtimeExit -eq 0) {
                    Invoke-OverlayRole (Join-Path $tempArtifact "engram-overlay.exe") "embedding-check"
                } else {
                    1
                }
                $smokeExit = if ($runtimeExit -eq 0 -and $embeddingExit -eq 0) {
                    Invoke-OverlayRole (Join-Path $tempArtifact "engram-overlay.exe") "smoke-check"
                } else {
                    1
                }
                $dashboardExit = if ($runtimeExit -eq 0 -and $embeddingExit -eq 0 -and $smokeExit -eq 0) {
                    Invoke-DashboardSmoke $tempArtifact
                } else {
                    1
                }
                if ($runtimeExit -eq 0 -and $embeddingExit -eq 0 -and $smokeExit -eq 0 -and $dashboardExit -eq 0) {
                    Publish-OverlayArtifact $tempArtifact $deployTarget
                    Write-OverlayOk "Built and published: $deployTarget"
                    if (-not $NoStart) {
                        Start-Process -FilePath (Join-Path $deployTarget "engram-overlay.exe")
                    }
                    $success = $true
                    $exitCode = 0
                    break
                }
                Write-OverlayWarn "Role smoke tests failed (runtime=$runtimeExit, embedding=$embeddingExit, smoke=$smokeExit, dashboard=$dashboardExit)"
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
    if (-not $success -and -not $NoStart) {
        foreach ($previousOverlayPath in $previousOverlayPaths) {
            if (Test-Path $previousOverlayPath) {
                Start-Process -FilePath $previousOverlayPath
            }
        }
    }
}

exit $exitCode
