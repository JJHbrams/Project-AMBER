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
    [switch]$ValidateOnly,
    [switch]$FullBuild,
    [switch]$FullSmoke
)

$ErrorActionPreference = "Stop"
if ($FullBuild -and $Mode -eq "auto") { $Mode = "rebuild" }
$script:ForceFullSmoke = [bool]$FullSmoke
$Root = Split-Path -Parent $PSScriptRoot
$Spec = Join-Path $Root "engram-overlay.spec"
$DefaultDist = Join-Path $Root "dist\engram-overlay"
$ModelDir = Join-Path $Root "resource\embedding-model"
$ModelManifest = Join-Path $ModelDir "manifest.json"
$ModelId = "intfloat/multilingual-e5-small"
$VersionSnapshot = Join-Path $Root "build\engram-version.json"
$ProcessStopHelper = Join-Path $PSScriptRoot "stop-engram-processes.ps1"

if (-not (Test-Path $ProcessStopHelper)) {
    throw "Engram process stop helper not found: $ProcessStopHelper"
}
. $ProcessStopHelper
. (Join-Path $PSScriptRoot 'smoke-profile.ps1')

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
        [Parameter(Mandatory)][string]$Role,
        [string]$ModelCacheDir = ""
    )

    $savedProfile = Enter-EngramBuildSmokeProfile
    $smokeLog = Join-Path $script:EngramBuildSmokeProfile `
        ("engram-smoke-" + [Guid]::NewGuid().ToString("N") + ".log")
    $previousLog = $env:ENGRAM_SMOKE_LOG
    $previousModelCache = $env:ENGRAM_MODEL_CACHE_DIR
    $env:ENGRAM_SMOKE_LOG = $smokeLog
    if ($ModelCacheDir) {
        $env:ENGRAM_MODEL_CACHE_DIR = $ModelCacheDir
    }
    try {
        $process = Start-Process -FilePath $Executable `
            -ArgumentList @("--role", $Role) `
            -PassThru -WindowStyle Hidden
        if (-not $process.WaitForExit(600000)) {
            $process.Kill()
            $process.WaitForExit()
            throw "Build role timed out: $Role"
        }
        if ($process.ExitCode -ne 0 -and (Test-Path $smokeLog)) {
            Write-OverlayWarn (Get-Content $smokeLog -Raw)
        }
        return [int]$process.ExitCode
    } finally {
        $env:ENGRAM_SMOKE_LOG = $previousLog
        $env:ENGRAM_MODEL_CACHE_DIR = $previousModelCache
        Exit-EngramBuildSmokeProfile $savedProfile
    }
}

function Invoke-DashboardSmoke {
    param([Parameter(Mandatory)][string]$ArtifactDir)

    $dashboardExe = Join-Path $ArtifactDir "engram-dashboard.exe"
    if (-not (Test-Path $dashboardExe)) {
        Write-OverlayWarn "Dashboard sidecar missing: $dashboardExe"
        return 1
    }

    $savedProfile = Enter-EngramBuildSmokeProfile
    try {
    $smokeLog = Join-Path $script:EngramBuildSmokeProfile `
        ("engram-dashboard-smoke-" + [Guid]::NewGuid().ToString("N") + ".log")
    $previousLog = $env:ENGRAM_SMOKE_LOG
    $env:ENGRAM_SMOKE_LOG = $smokeLog
    try {
        $render = Start-Process -FilePath $dashboardExe `
            -ArgumentList @("--smoke-check") `
            -PassThru -WindowStyle Hidden
        if (-not $render.WaitForExit(120000)) {
            $render.Kill()
            $render.WaitForExit()
            throw 'Dashboard render smoke timed out'
        }
        if ($render.ExitCode -ne 0) {
            if (Test-Path $smokeLog) {
                Write-OverlayWarn (Get-Content $smokeLog -Raw)
            }
            Write-OverlayWarn "Dashboard render smoke failed (exit $($render.ExitCode))"
            return [int]$render.ExitCode
        }
    } finally {
        $env:ENGRAM_SMOKE_LOG = $previousLog
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
                $owners = @(Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction Stop | Select-Object -ExpandProperty OwningProcess -Unique)
                if ($response.StatusCode -eq 200 -and $response.Content.Trim() -eq "ok" -and
                    $owners.Count -eq 1 -and $owners[0] -eq $server.Id -and -not $server.HasExited) {
                    return 0
                }
            } catch {}
            Start-Sleep -Milliseconds 500
        }
        Write-OverlayWarn "Dashboard server health smoke timed out"
        return 1
    } finally {
        if ($server -and -not $server.HasExited) {
            $server.Kill()
            $server.WaitForExit()
        }
    }
    } finally { Exit-EngramBuildSmokeProfile $savedProfile }
}

function Remove-DirectoryDetached([string]$Path) {
    # Fire-and-forget delete so 20k-file trees do not block the build.
    try {
        Start-Process -FilePath $env:ComSpec `
            -ArgumentList '/c', 'rd', '/s', '/q', "`"$Path`"" -WindowStyle Hidden | Out-Null
    } catch {
        Write-OverlayWarn "Detached delete failed for ${Path}: $($_.Exception.Message)"
    }
}

function Clear-StaleOverlayTrash {
    param(
        [Parameter(Mandatory)][string]$TargetDir,
        [string[]]$Keep = @()
    )

    try {
        $target = [IO.Path]::GetFullPath($TargetDir).TrimEnd('')
        $parent = Split-Path -Parent $target
        if (-not $parent -or -not (Test-Path $parent)) { return }
        $keepFull = @($Keep | Where-Object { $_ } | ForEach-Object { [IO.Path]::GetFullPath($_).TrimEnd('') })
        foreach ($pattern in @('.engram-overlay-backup-*', '.engram-overlay-stage-*')) {
            foreach ($dir in @(Get-ChildItem -LiteralPath $parent -Directory -Filter $pattern -Force -ErrorAction SilentlyContinue)) {
                $full = $dir.FullName.TrimEnd('')
                if ($full -eq $target -or $keepFull -contains $full) { continue }
                Remove-DirectoryDetached $full
            }
        }
    } catch {
        Write-OverlayWarn "Stale overlay trash cleanup failed: $($_.Exception.Message)"
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
            Remove-DirectoryDetached $stage
        }
        if ($published -and (Test-Path $backup)) {
            Remove-DirectoryDetached $backup
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
    $savedProfile = Enter-EngramBuildSmokeProfile
    try {
        $stdout = Join-Path $script:EngramBuildSmokeProfile 'source-contract.stdout.log'
        $stderr = Join-Path $script:EngramBuildSmokeProfile 'source-contract.stderr.log'
        $process = Start-Process -FilePath $Python -ArgumentList @(('"' + $entry + '"'), '--role', 'runtime-contract') `
            -WorkingDirectory $Root -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru -WindowStyle Hidden
        if (-not $process.WaitForExit(120000)) {
            $process.Kill()
            $process.WaitForExit()
            throw 'Source runtime contract timed out'
        }
        $result = [PSCustomObject]@{ ExitCode = [int]$process.ExitCode; Output = @(Get-Content -LiteralPath $stdout) }
        if ($result.ExitCode -ne 0) { Write-OverlayWarn (Get-Content -LiteralPath $stderr -Raw) }
    }
    finally { Exit-EngramBuildSmokeProfile $savedProfile }
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

$script:PhaseTimes = [System.Collections.Generic.List[object]]::new()
$script:TotalWatch = [Diagnostics.Stopwatch]::StartNew()
$script:PhaseName = $null
$script:PhaseWatch = $null

function Stop-OverlayPhase {
    if ($script:PhaseName) {
        $script:PhaseWatch.Stop()
        $seconds = [Math]::Round($script:PhaseWatch.Elapsed.TotalSeconds, 1)
        $script:PhaseTimes.Add([PSCustomObject]@{ Phase = $script:PhaseName; Seconds = $seconds })
        Write-Host ("  [time] {0}: {1}s" -f $script:PhaseName, $seconds) -ForegroundColor DarkGray
        $script:PhaseName = $null
        $script:PhaseWatch = $null
    }
}

function Start-OverlayPhase([string]$Name) {
    Stop-OverlayPhase
    $script:PhaseName = $Name
    $script:PhaseWatch = [Diagnostics.Stopwatch]::StartNew()
}

function Write-OverlayTimings {
    Stop-OverlayPhase
    if ($script:PhaseTimes.Count -eq 0) { return }
    Write-Host "`n  Phase timing" -ForegroundColor Cyan
    foreach ($entry in $script:PhaseTimes) {
        Write-Host ("    {0,-28} {1,8:N1}s" -f $entry.Phase, $entry.Seconds)
    }
    Write-Host ("    {0,-28} {1,8:N1}s" -f "total", $script:TotalWatch.Elapsed.TotalSeconds) -ForegroundColor Cyan
}

function Invoke-ManifestCli {
    param([Parameter(Mandatory)][string]$Python, [Parameter(Mandatory)][string]$Artifact, [string[]]$Extra = @())
    return Invoke-OverlayPython $Python (@(
        "-m", "core.install.overlay_manifest",
        "--root", $Root,
        "--artifact", $Artifact,
        "--model-manifest", $ModelManifest
    ) + $Extra)
}

function ConvertFrom-CliJson([object[]]$Output) {
    $text = (@($Output | ForEach-Object { $_.ToString() }) -join "`n")
    $first = $text.IndexOf('{')
    $last = $text.LastIndexOf('}')
    if ($first -lt 0 -or $last -le $first) { throw "no JSON object in CLI output: $text" }
    return ($text.Substring($first, $last - $first + 1) | ConvertFrom-Json)
}

function Get-FullSmokeSelection {
    return [PSCustomObject]@{
        'runtime-contract' = $true; embedding = $true; smoke = $true
        dashboard = $true; embedding_in_smoke = $true
    }
}

function Get-OverlayPlan([string]$Python) {
    $result = Invoke-ManifestCli $Python $DefaultDist @("--plan")
    try {
        if ($result.ExitCode -ne 0) { throw "plan exit $($result.ExitCode)" }
        return ConvertFrom-CliJson $result.Output
    } catch {
        return [PSCustomObject]@{
            build = "full"; reasons = @("plan unreadable: $($_.Exception.Message)")
            changed_files = @(); smoke = (Get-FullSmokeSelection); bridge_rebuild = $true
            restamp = $true; version = $null
        }
    }
}

function Write-OverlayPlan($Plan, $Smoke) {
    Write-Host ("  Build kind : {0}" -f $Plan.build) -ForegroundColor White
    foreach ($reason in @($Plan.reasons)) { Write-Host "    - $reason" }
    Write-Host ("  Changed files: {0}" -f @($Plan.changed_files).Count)
    foreach ($file in (@($Plan.changed_files) | Select-Object -First 15)) { Write-Host "      $file" }
    if (@($Plan.changed_files).Count -gt 15) { Write-Host "      ..." }
    Write-Host ("  Bridge rebuild: {0}   Restamp: {1}" -f [bool]$Plan.bridge_rebuild, [bool]$Plan.restamp)
    $selected = @()
    foreach ($role in @("runtime-contract", "embedding", "smoke", "dashboard")) {
        $p = $Smoke.PSObject.Properties[$role]
        if ($p -and $p.Value) { $selected += $role }
    }
    Write-Host ("  Smoke roles: {0}{1}" -f ($selected -join ", "),
        $(if ($script:ForceFullSmoke) { "   (forced by -FullSmoke)" } else { "" }))
}

function Resolve-SmokeModelCache([string]$Python) {
    # Persistent cache keyed by manifest SHA-256; repopulated only when invalid.
    $cache = Join-Path $Root "build\smoke-model-cache"
    New-Item -ItemType Directory -Path $cache -Force | Out-Null
    $cliArgs = @(
        "-m", "core.install.model_manifest",
        "--model-dir", $ModelDir,
        "--model-id", $ModelId,
        "--ensure-cache",
        "--cache-root", $cache,
        "--legacy-model-dir", $ModelDir
    )
    $check = Invoke-OverlayPython $Python $cliArgs
    if ($check.ExitCode -eq 0) { return $cache }
    Write-OverlayWarn "Smoke model cache could not be validated; the frozen role will populate $cache"
    return $cache
}

function Invoke-SmokeSelection {
    param(
        [Parameter(Mandatory)][string]$Artifact,
        [Parameter(Mandatory)]$Smoke,
        [Parameter(Mandatory)][string]$ModelCache
    )

    $exe = Join-Path $Artifact "engram-overlay.exe"
    $results = [ordered]@{}
    $failed = $false
    $roles = @(
        @{ Key = "runtime-contract"; Cli = "runtime-contract" },
        @{ Key = "embedding"; Cli = "embedding-check" },
        @{ Key = "smoke"; Cli = "smoke-check" },
        @{ Key = "dashboard"; Cli = $null }
    )
    $smokeProp = $Smoke.PSObject.Properties["smoke"]
    $embProp = $Smoke.PSObject.Properties["embedding_in_smoke"]
    $smokeSelected = [bool]($smokeProp -and $smokeProp.Value)
    $embeddingInSmoke = [bool]($embProp -and $embProp.Value)
    foreach ($role in $roles) {
        $key = $role.Key
        $prop = $Smoke.PSObject.Properties[$key]
        $selected = ($key -eq "runtime-contract") -or [bool]($prop -and $prop.Value)
        if ($key -eq "embedding" -and $embeddingInSmoke -and $smokeSelected) {
            $results[$key] = "skipped:covered-by-smoke"
            continue
        }
        if (-not $selected) { $results[$key] = "skipped:not-affected"; continue }
        if ($failed) { $results[$key] = "skipped:prior-failure"; continue }
        Start-OverlayPhase "smoke:$key"
        $exit = if ($role.Cli) {
            Invoke-OverlayRole $exe $role.Cli $ModelCache
        } else {
            Invoke-DashboardSmoke $Artifact
        }
        if ($exit -eq 0) { $results[$key] = "pass"; Write-OverlayOk "smoke $key passed" }
        else { $results[$key] = "fail"; $failed = $true; Write-OverlayWarn "smoke $key failed (exit $exit)" }
    }
    Stop-OverlayPhase
    return [PSCustomObject]@{ Results = $results; Failed = $failed }
}

function Get-SmokeRecordArgs($Results) {
    $out = @()
    foreach ($key in $Results.Keys) { $out += @("--record-smoke", "$key=$($Results[$key])") }
    return $out
}

function Invoke-BridgeSmoke([string]$BridgeExe) {
    if (-not (Test-Path -LiteralPath $BridgeExe)) { return 1 }
    $process = Start-Process -FilePath $BridgeExe -ArgumentList @("--help") `
        -PassThru -WindowStyle Hidden
    if (-not $process.WaitForExit(60000)) {
        $process.Kill(); $process.WaitForExit()
        return 1
    }
    return [int]$process.ExitCode
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

    Write-OverlayStep "Validating canonical embedding model manifest"
    $modelCheck = Invoke-OverlayPython $python @(
        "-m", "core.install.model_manifest",
        "--model-dir", $ModelDir,
        "--model-id", $ModelId,
        "--validate-metadata"
    )
    if ($modelCheck.ExitCode -ne 0) {
        throw "Embedding model manifest validation failed: $(Get-LastOutput $modelCheck.Output)"
    }
    Write-OverlayOk "Embedding model manifest validated (payload excluded from frozen bundle)"

    Write-OverlayStep "Resolving four-part build version"
    $versionResult = Invoke-OverlayPython $python @(
        "-m", "core.install.versioning",
        "--root", $Root,
        "--write-snapshot", $VersionSnapshot
    )
    if ($versionResult.ExitCode -ne 0 -or -not (Test-Path -LiteralPath $VersionSnapshot)) {
        throw "Version snapshot generation failed: $(Get-LastOutput $versionResult.Output)"
    }
    $versionMetadata = Get-Content -LiteralPath $VersionSnapshot -Raw | ConvertFrom-Json
    Write-OverlayOk "Build version: $($versionMetadata.version) ($($versionMetadata.build_source))"

    Start-OverlayPhase "plan"
    Write-OverlayStep "Planning build against $DefaultDist"
    $deploysToDefault = (-not $Deploy)
    if ($Deploy) {
        $deploysToDefault = $deployTarget.TrimEnd([char]92) -eq `
            ([IO.Path]::GetFullPath($DefaultDist)).TrimEnd([char]92)
    }
    $validation = Invoke-ManifestCli $python $DefaultDist @("--validate")
    $reuseValid = ($validation.ExitCode -eq 0)
    $plan = Get-OverlayPlan $python
    $planSmoke = if ($script:ForceFullSmoke) { Get-FullSmokeSelection } else { $plan.smoke }
    Write-OverlayPlan $plan $planSmoke

    $buildKind = [string]$plan.build
    if ($FullBuild -or $Mode -in @("rebuild", "clean")) {
        $buildKind = "full"
        $forcedBy = if ($FullBuild) { "-FullBuild" } else { "-Mode $Mode" }
        Write-OverlayWarn "Full build forced ($forcedBy)"
    } elseif (-not $deploysToDefault) {
        $buildKind = "full"
        Write-OverlayWarn "Custom deploy target: incremental baseline is $DefaultDist, building fully"
    } elseif ($buildKind -eq "reuse" -and -not $reuseValid) {
        $buildKind = "full"
        Write-OverlayWarn "Plan says reuse but artifact validation failed: $(Get-LastOutput $validation.Output)"
    }
    Write-OverlayOk "Selected build path: $buildKind"
    if ($buildKind -eq "full") { $planSmoke = Get-FullSmokeSelection }
    Stop-OverlayPhase

    if ($buildKind -eq "reuse") {
        Start-OverlayPhase "smoke:runtime-contract"
        $frozenContract = Invoke-OverlayRole (Join-Path $DefaultDist "engram-overlay.exe") "runtime-contract"
        if ($frozenContract -ne 0) {
            throw "Reusable artifact failed frozen runtime contract"
        }
        Stop-OverlayPhase
        if ($script:ForceFullSmoke) {
            # -FullSmoke on an unchanged artifact still has to prove every role.
            $smokeModelCache = Resolve-SmokeModelCache $python
            $reuseSmoke = Invoke-SmokeSelection $DefaultDist $planSmoke $smokeModelCache
            $record = Invoke-ManifestCli $python $DefaultDist (Get-SmokeRecordArgs $reuseSmoke.Results)
            if ($record.ExitCode -ne 0) { throw "Could not record smoke results: $(Get-LastOutput $record.Output)" }
            if ($reuseSmoke.Failed) { throw "Full smoke failed on the reused artifact" }
        }
        Write-OverlayOk "Reusing validated overlay artifact: $DefaultDist"
        if (-not $NoStart) {
            Start-Process -FilePath (Join-Path $DefaultDist "engram-overlay.exe")
        }
        $success = $true
        $exitCode = 0
        exit 0
    }

    if ($buildKind -eq "fast") {
        $fastFailure = $null
        $fastStage = $null
        try {
            $smokeModelCache = Resolve-SmokeModelCache $python
            Clear-StaleOverlayTrash -TargetDir $deployTarget
            $fastStage = Join-Path (Split-Path -Parent $deployTarget) `
                (".engram-overlay-stage-" + [Guid]::NewGuid().ToString("N"))

            Start-OverlayPhase "clone"
            Write-OverlayStep "Cloning current artifact (hardlinks) into fast stage"
            $copyArgs = @("--copy", "engram-overlay.exe", "--copy", "engram-dashboard.exe")
            if ($plan.bridge_rebuild) { $copyArgs += @("--copy", "engram-mcp-bridge.exe") }
            $clone = Invoke-OverlayPython $python (@(
                "-m", "core.install.stage_clone", "--src", $deployTarget, "--dst", $fastStage
            ) + $copyArgs)
            if ($clone.ExitCode -ne 0) { throw "Stage clone failed: $(Get-LastOutput $clone.Output)" }
            Write-OverlayOk "Stage: $fastStage ($(Get-LastOutput $clone.Output))"

            Start-OverlayPhase "apply"
            Write-OverlayStep "Applying changed files to stage"
            $apply = Invoke-ManifestCli $python $fastStage @("--apply")
            if ($apply.ExitCode -ne 0) { throw "Fast apply failed: $(Get-LastOutput $apply.Output)" }
            Write-OverlayOk "Fast apply complete"

            $bridgeExe = Join-Path $fastStage "engram-mcp-bridge.exe"
            if ($plan.bridge_rebuild) {
                Start-OverlayPhase "bridge"
                Write-OverlayStep "Rebuilding engram-mcp-bridge.exe"
                $bridgeSpec = Join-Path $Root "engram-mcp-bridge.spec"
                if (-not (Test-Path -LiteralPath $bridgeSpec)) { throw "Bridge spec not found: $bridgeSpec" }
                $bridgeTemp = Join-Path ([IO.Path]::GetTempPath()) ("engram-bridge-build-" + [Guid]::NewGuid().ToString("N"))
                try {
                    $bridgeBuild = Invoke-OverlayPython $python @(
                        "-m", "PyInstaller", "--noconfirm", "engram-mcp-bridge.spec",
                        "--distpath", $bridgeTemp,
                        "--workpath", (Join-Path $Root "build\engram-mcp-bridge")
                    )
                    $bridgeBuilt = Join-Path $bridgeTemp "engram-mcp-bridge.exe"
                    if ($bridgeBuild.ExitCode -ne 0 -or -not (Test-Path -LiteralPath $bridgeBuilt)) {
                        throw "Bridge build failed (exit $($bridgeBuild.ExitCode)): $(Get-LastOutput $bridgeBuild.Output)"
                    }
                    # Remove first so a hardlinked file is never rewritten in place.
                    Remove-Item -LiteralPath $bridgeExe -Force -ErrorAction SilentlyContinue
                    Move-Item -LiteralPath $bridgeBuilt -Destination $bridgeExe
                } finally {
                    if (Test-Path -LiteralPath $bridgeTemp) { Remove-DirectoryDetached $bridgeTemp }
                }
                Write-OverlayOk "Bridge replaced in stage"
            }

            Start-OverlayPhase "manifest"
            $manifestWrite = Invoke-ManifestCli $python $fastStage @("--mode", $Mode, "--write", "--kind", "fast-patch")
            if ($manifestWrite.ExitCode -ne 0) { throw "Build manifest generation failed: $(Get-LastOutput $manifestWrite.Output)" }

            Write-OverlayStep "Running selected smoke roles on stage"
            $fastSmoke = Invoke-SmokeSelection $fastStage $planSmoke $smokeModelCache
            $fastResults = $fastSmoke.Results
            if ($plan.bridge_rebuild) {
                Start-OverlayPhase "smoke:bridge"
                if ($fastSmoke.Failed) {
                    $fastResults["bridge"] = "skipped:prior-failure"
                } elseif ((Invoke-BridgeSmoke $bridgeExe) -eq 0) {
                    $fastResults["bridge"] = "pass"
                    Write-OverlayOk "bridge --help passed"
                } else {
                    $fastResults["bridge"] = "fail"
                    $fastSmoke.Failed = $true
                    Write-OverlayWarn "bridge --help failed"
                }
                Stop-OverlayPhase
            }
            $record = Invoke-ManifestCli $python $fastStage (Get-SmokeRecordArgs $fastResults)
            if ($record.ExitCode -ne 0) { throw "Could not record smoke results: $(Get-LastOutput $record.Output)" }
            if ($fastSmoke.Failed) { throw "Fast-patch smoke tests failed" }

            Start-OverlayPhase "publish"
            $stoppedProcesses = Stop-EngramArtifactProcesses -ArtifactDir $deployTarget
            $previousOverlayPaths = @($stoppedProcesses.OverlayPaths)
            Publish-OverlayArtifact $fastStage $deployTarget
            Stop-OverlayPhase
            Write-OverlayOk "Fast-patched and published: $deployTarget"
            if (-not $NoStart) {
                Start-Process -FilePath (Join-Path $deployTarget "engram-overlay.exe")
            }
            $success = $true
            $exitCode = 0
        } catch {
            $fastFailure = $_.Exception.Message
        } finally {
            if ($fastStage -and (Test-Path -LiteralPath $fastStage)) { Remove-DirectoryDetached $fastStage }
        }
        if (-not $success) {
            Write-OverlayWarn "Fast build failed: $fastFailure"
            Write-OverlayWarn "Existing artifact was preserved. There is no automatic fallback; re-run with -FullBuild."
            throw "Fast build failed; existing artifact was preserved"
        }
        exit 0
    }

    # ---- full build ----
    Start-OverlayPhase "native-bubble"
    & (Join-Path $PSScriptRoot 'build-native-bubble.ps1')
    if ($LASTEXITCODE -ne 0) { throw 'Native bubble shell build failed' }
    Stop-OverlayPhase

    # The running overlay keeps serving MCP during PyInstaller and smoke; it is
    # stopped only right before the publish swap below.
    Clear-StaleOverlayTrash -TargetDir $deployTarget
    Write-OverlayOk "Deploy target: $deployTarget"

    $cleanRetried = $false
    $attemptClean = ($Mode -eq "clean")
    while ($true) {
        $tempRoot = Join-Path ([IO.Path]::GetTempPath()) ("engram-overlay-build-" + [Guid]::NewGuid().ToString("N"))
        $tempDist = Join-Path $tempRoot "dist"
        $tempArtifact = Join-Path $tempDist "engram-overlay"
        $buildWorkPath = if ($attemptClean) {
            Join-Path $tempRoot "work"
        } else {
            Join-Path $Root "build\engram-overlay"
        }
        New-Item -ItemType Directory -Path $tempRoot -Force | Out-Null

        Start-OverlayPhase "pyinstaller"
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
        Stop-OverlayPhase
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
            # The frozen runtime-contract reads build-manifest.json, so the
            # manifest is written first and the smoke results merged after.
            Start-OverlayPhase "manifest"
            $manifest = Invoke-ManifestCli $python $tempArtifact @("--mode", $Mode, "--write", "--kind", "full")
            if ($manifest.ExitCode -eq 0) {
                Write-OverlayStep "Running frozen runtime contract and role smoke tests"
                $smokeModelCache = Resolve-SmokeModelCache $python
                $fullSmokeRun = Invoke-SmokeSelection $tempArtifact (Get-FullSmokeSelection) $smokeModelCache
                $record = Invoke-ManifestCli $python $tempArtifact (Get-SmokeRecordArgs $fullSmokeRun.Results)
                if ($record.ExitCode -ne 0) { throw "Could not record smoke results: $(Get-LastOutput $record.Output)" }
                if (-not $fullSmokeRun.Failed) {
                    Start-OverlayPhase "publish"
                    $stoppedProcesses = Stop-EngramArtifactProcesses -ArtifactDir $deployTarget
                    $previousOverlayPaths = @($stoppedProcesses.OverlayPaths)
                    Publish-OverlayArtifact $tempArtifact $deployTarget
                    Stop-OverlayPhase
                    Write-OverlayOk "Built and published: $deployTarget"
                    if (-not $NoStart) {
                        Start-Process -FilePath (Join-Path $deployTarget "engram-overlay.exe")
                    }
                    $success = $true
                    $exitCode = 0
                    break
                }
                $summary = ($fullSmokeRun.Results.Keys | ForEach-Object { "$_=$($fullSmokeRun.Results[$_])" }) -join ", "
                Write-OverlayWarn "Role smoke tests failed ($summary)"
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
    Write-Host $_.InvocationInfo.PositionMessage -ForegroundColor DarkGray
    Write-Host $_.ScriptStackTrace -ForegroundColor DarkGray
    $exitCode = 1
} finally {
    Write-OverlayTimings
    if ($tempRoot -and (Test-Path $tempRoot)) {
        # In clean mode tempRoot holds the PyInstaller work dir (large).
        Remove-DirectoryDetached $tempRoot
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
