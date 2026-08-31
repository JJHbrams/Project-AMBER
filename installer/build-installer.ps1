<#
.SYNOPSIS
    Engram Overlay — frozen bundle and Inno Setup release build.

.EXAMPLE
    .\build-installer.ps1
    .\build-installer.ps1 -Release
    .\build-installer.ps1 -FreshBuild -Release
    .\build-installer.ps1 -SkipBuild
#>
param(
    [switch]$SkipBuild,
    [switch]$FreshBuild,
    [switch]$Release,
    [string]$CondaEnv = "intel_engram"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path $PSScriptRoot
$Iss = Join-Path $PSScriptRoot "engram-overlay.iss"
$Engine = Join-Path $PSScriptRoot "build-overlay.ps1"
$DistDir = Join-Path $Root "dist\engram-overlay"
$DistExe = Join-Path $DistDir "engram-overlay.exe"
$DashboardExe = Join-Path $DistDir "engram-dashboard.exe"
$CacheHelpers = Join-Path $PSScriptRoot "build-cache.ps1"

function Write-Step($Message) { Write-Host "`n==> $Message" -ForegroundColor Cyan }
function Write-Ok($Message) { Write-Host "  [OK] $Message" -ForegroundColor Green }
function Write-Err($Message) {
    Write-Host "  [X] $Message" -ForegroundColor Red
    exit 1
}

function Invoke-FrozenRole([string]$Role) {
    $process = Start-Process -FilePath $DistExe `
        -ArgumentList @("--role", $Role) `
        -Wait -PassThru -WindowStyle Hidden
    return [int]$process.ExitCode
}

function Invoke-IsccCompile(
    [string]$Compression,
    [string]$SolidCompression,
    [string]$OutputSuffix,
    [string]$AppVersion
) {
    $mappedDrive = ""
    $compileIss = $Iss
    if ($Root.Length -gt 80) {
        foreach ($letter in @("Z", "Y", "X", "W", "V", "U", "T", "S", "R")) {
            $candidate = "${letter}:"
            if (-not (Test-Path "${candidate}\")) {
                & subst.exe $candidate $Root
                if ($LASTEXITCODE -eq 0) {
                    $mappedDrive = $candidate
                    $compileIss = "${candidate}\installer\engram-overlay.iss"
                    Write-Ok "ISCC short path: $mappedDrive -> $Root"
                    break
                }
            }
        }
        if (-not $mappedDrive) {
            Write-Err "No free drive letter available for ISCC long-path workaround"
        }
    }
    try {
        $versionDefine = "/DAppVersion=$AppVersion"
        & $Iscc "/DBuildCompression=$Compression" `
            "/DBuildSolidCompression=$SolidCompression" `
            "/DBuildOutputSuffix=$OutputSuffix" $versionDefine $compileIss |
            Where-Object { $_ -notmatch '^\s+Compressing:' } |
            ForEach-Object { Write-Host $_ }
        $compileExit = [int]$LASTEXITCODE
        return $compileExit
    } finally {
        if ($mappedDrive) { & subst.exe $mappedDrive /d }
    }
}

if ($SkipBuild -and $FreshBuild) {
    Write-Err "-SkipBuild and -FreshBuild cannot be used together"
}
if (-not (Test-Path -LiteralPath $CacheHelpers)) {
    Write-Err "Installer cache helpers not found: $CacheHelpers"
}
. $CacheHelpers

if (-not (Test-Path $Engine)) {
    Write-Err "Shared overlay build engine not found: $Engine"
}

# ── ISCC 탐지 ────────────────────────────────────────────────
$IsccCandidates = @(
    (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"),
    (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe"),
    (Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe")
)
$Iscc = $IsccCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $Iscc) {
    Write-Err "ISCC.exe 없음 — 설치: winget install --id JRSoftware.InnoSetup"
}
Write-Ok "ISCC: $Iscc"

# ── 1) shared frozen bundle engine ───────────────────────────
$BuildManifestPath = Join-Path $DistDir "build-manifest.json"
$manifestWriteBefore = if (Test-Path -LiteralPath $BuildManifestPath) {
    (Get-Item -LiteralPath $BuildManifestPath).LastWriteTimeUtc.Ticks
} else { 0 }
if ($SkipBuild) {
    Write-Step "Validating existing frozen bundle (-SkipBuild)"
    & $Engine -Mode auto -CondaEnv $CondaEnv -Deploy $DistDir -ValidateOnly
    if ($LASTEXITCODE -ne 0) {
        Write-Err "-SkipBuild requires a current build-manifest.json and validated inputs"
    }
    Write-Ok "Existing bundle is current and validated"
} else {
    $overlayMode = if ($FreshBuild) { "rebuild" } else { "auto" }
    Write-Step "Preparing frozen bundle ($overlayMode)"
    & $Engine -Mode $overlayMode -CondaEnv $CondaEnv -Deploy $DistDir -NoStart
    if ($LASTEXITCODE -ne 0) {
        Write-Err "Shared overlay build failed"
    }
    Write-Ok "Frozen bundle build completed"
}

if (-not (Test-Path $DistExe)) {
    Write-Err "번들 없음: $DistExe"
}
if (-not (Test-Path $DashboardExe)) {
    Write-Err "대시보드 sidecar 없음: $DashboardExe"
}
Write-Ok "번들 확인: $DistExe"

# ── 2) Resolve output and validated installer cache ──────────
try {
    $frozenManifest = Get-Content -LiteralPath $BuildManifestPath -Raw | ConvertFrom-Json
    $version = [string]$frozenManifest.version.version
} catch {
    Write-Err "Frozen build version not found in $BuildManifestPath"
}
if ($version -notmatch '^\d+\.\d+\.\d+\.\d+$') {
    Write-Err "Frozen build version is not Major.Minor.Patch.Build: $version"
}
$outputSuffix = if ($Release) { "" } else { "-dev" }
$OutputPath = Join-Path $Root "AMBER_${version}${outputSuffix}_x64-setup.exe"
$BuildProfile = if ($Release) { "release-lzma2-solid" } else { "development-zip" }
$CachePath = Join-Path $Root "build\installer-cache\EngramOverlay_${version}_${BuildProfile}.json"
$installerCacheHit = Test-EngramInstallerCache $Root $DistDir $BuildProfile $OutputPath $CachePath
$manifestWriteAfter = (Get-Item -LiteralPath $BuildManifestPath).LastWriteTimeUtc.Ticks
$frozenBuiltNow = (-not $SkipBuild) -and ($manifestWriteAfter -gt $manifestWriteBefore)

if ($installerCacheHit) {
    Write-Ok "Reusing validated installer: $OutputPath"
} else {
    # A fresh frozen build already passed the same roles inside build-overlay.
    if ($frozenBuiltNow) {
        Write-Ok "Release smoke already passed during fresh frozen build"
    } else {
        Write-Step "Release role smoke tests"
        $embeddingExit = Invoke-FrozenRole "embedding-check"
        $smokeExit = if ($embeddingExit -eq 0) {
            Invoke-FrozenRole "smoke-check"
        } else {
            1
        }
        if ($embeddingExit -ne 0 -or $smokeExit -ne 0) {
            Write-Err "Release smoke tests failed (embedding=$embeddingExit, roles=$smokeExit)"
        }
        $dashboardSmoke = Start-Process -FilePath $DashboardExe `
            -ArgumentList @("--smoke-check") `
            -Wait -PassThru -WindowStyle Hidden
        if ($dashboardSmoke.ExitCode -ne 0) {
            Write-Err "Dashboard sidecar render smoke failed (exit $($dashboardSmoke.ExitCode))"
        }
        Write-Ok "embedding-check, mcp-server, kg-watcher, overlay, and dashboard smoke checks passed"
    }

    # ── 3) Inno Setup compile ────────────────────────────────
    $compression = if ($Release) { "lzma2" } else { "zip" }
    $solidCompression = if ($Release) { "yes" } else { "no" }
    Write-Step "ISCC — setup.exe packaging ($BuildProfile)"
    $isccExit = Invoke-IsccCompile $compression $solidCompression $outputSuffix $version
    if ($isccExit -ne 0) { Write-Err "ISCC compile failed" }
    if (-not (Test-Path -LiteralPath $OutputPath -PathType Leaf)) {
        Write-Err "Output setup.exe not found: $OutputPath"
    }
    Write-EngramInstallerCache $Root $DistDir $BuildProfile $OutputPath $CachePath
    Write-Ok "Installer cache written: $CachePath"
}

$Output = Get-Item -LiteralPath $OutputPath -ErrorAction SilentlyContinue
if ($Output) {
    Write-Ok "완성: $($Output.FullName)  ($([math]::Round($Output.Length/1MB,1)) MB)"
} else {
    Write-Err "출력 setup.exe 를 찾을 수 없음"
}
