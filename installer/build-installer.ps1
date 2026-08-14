<#
.SYNOPSIS
    Engram Overlay — frozen bundle and Inno Setup release build.

.EXAMPLE
    .\build-installer.ps1
    .\build-installer.ps1 -SkipBuild
#>
param(
    [switch]$SkipBuild,
    [string]$CondaEnv = "intel_engram"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path $PSScriptRoot
$Iss = Join-Path $PSScriptRoot "engram-overlay.iss"
$Engine = Join-Path $PSScriptRoot "build-overlay.ps1"
$DistDir = Join-Path $Root "dist\engram-overlay"
$DistExe = Join-Path $DistDir "engram-overlay.exe"
$DashboardExe = Join-Path $DistDir "engram-dashboard.exe"

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
if ($SkipBuild) {
    Write-Step "Validating existing frozen bundle (-SkipBuild)"
    & $Engine -Mode auto -CondaEnv $CondaEnv -ValidateOnly
    if ($LASTEXITCODE -ne 0) {
        Write-Err "-SkipBuild requires a current build-manifest.json and validated inputs"
    }
    Write-Ok "Existing bundle is current and validated"
} else {
    Write-Step "Building frozen bundle with shared engine"
    & $Engine -Mode rebuild -CondaEnv $CondaEnv -NoStart
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

# ── 2) release smoke tests ──────────────────────────────────
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

# ── 3) Inno Setup compile ────────────────────────────────────
Write-Step "ISCC — setup.exe 패키징"
& $Iscc $Iss
if ($LASTEXITCODE -ne 0) { Write-Err "ISCC 컴파일 실패" }

$Output = Get-ChildItem $Root -Filter "EngramOverlay_*_x64-setup.exe" -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1
if ($Output) {
    Write-Ok "완성: $($Output.FullName)  ($([math]::Round($Output.Length/1MB,1)) MB)"
} else {
    Write-Err "출력 setup.exe 를 찾을 수 없음"
}
