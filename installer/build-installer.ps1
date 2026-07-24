<#
.SYNOPSIS
    Engram Overlay — 통짜 setup.exe 빌드 (개발자 머신 전용)

    1) (옵션) frozen 번들 재빌드: PyInstaller → dist\engram-overlay\
    2) Inno Setup(ISCC)로 .iss 컴파일 → installer\Output\EngramOverlay_<ver>_x64-setup.exe

.EXAMPLE
    .\build-installer.ps1              # 풀 빌드 (frozen 재빌드 + 패키징)
    .\build-installer.ps1 -SkipBuild   # 기존 dist 재사용, 패키징만
#>
param(
    [switch]$SkipBuild,
    [string]$CondaEnv = "intel_engram"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path $PSScriptRoot          # 프로젝트 루트 (build-installer.ps1 은 installer\ 안)
$Spec = Join-Path $Root "engram-overlay.spec"
$Iss  = Join-Path $PSScriptRoot "engram-overlay.iss"
$DistExe = Join-Path $Root "dist\engram-overlay\engram-overlay.exe"

function Write-Step($m) { Write-Host "`n==> $m" -ForegroundColor Cyan }
function Write-Ok($m)   { Write-Host "  [OK] $m" -ForegroundColor Green }
function Write-Err($m)  { Write-Host "  [X] $m" -ForegroundColor Red; exit 1 }

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

# ── 0) 오프라인 임베딩 모델 export ───────────────────────────
# HuggingFace 서버 의존 제거: 모델을 flat 디렉토리로 미리 저장해 번들에 포함.
# (기존 HF 캐시 재사용 — 캐시 있으면 네트워크 불필요)
if (-not $SkipBuild) {
    Write-Step "임베딩 모델 export (오프라인 번들용)"
    $ModelDir = Join-Path $Root "resource\embedding-model"
    if (Test-Path (Join-Path $ModelDir "config.json")) {
        Write-Ok "이미 export됨: $ModelDir"
    } else {
        $exportPy = "from sentence_transformers import SentenceTransformer; SentenceTransformer('paraphrase-multilingual-MiniLM-L12-v2', device='cpu').save(r'$ModelDir')"
        & conda run -n $CondaEnv python -c $exportPy
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path (Join-Path $ModelDir "config.json"))) {
            Write-Err "모델 export 실패 — HF 캐시 확인 필요"
        }
        Write-Ok "export 완료: $ModelDir"
    }
}

# ── 1) frozen 번들 빌드 ──────────────────────────────────────
if (-not $SkipBuild) {
    Write-Step "PyInstaller — frozen 번들 빌드"
    Get-Process -Name "engram-overlay" -ErrorAction SilentlyContinue | Stop-Process -Force
    Start-Sleep -Seconds 1
    Push-Location $Root
    try {
        & conda run -n $CondaEnv python -m PyInstaller --noconfirm $Spec
        if ($LASTEXITCODE -ne 0) { Write-Err "PyInstaller 빌드 실패" }
    } finally { Pop-Location }
    Write-Ok "번들 빌드 완료"
} else {
    Write-Step "frozen 빌드 건너뜀 (-SkipBuild)"
}

if (-not (Test-Path $DistExe)) {
    Write-Err "번들 없음: $DistExe — -SkipBuild 없이 먼저 빌드하세요"
}
Write-Ok "번들 확인: $DistExe"

# ── 2) Inno Setup 컴파일 ─────────────────────────────────────
Write-Step "ISCC — setup.exe 패키징"
& $Iscc $Iss
if ($LASTEXITCODE -ne 0) { Write-Err "ISCC 컴파일 실패" }

# OutputDir=.. → setup.exe 는 프로젝트 루트에 생성됨
$Output = Get-ChildItem $Root -Filter "EngramOverlay_*_x64-setup.exe" -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1
if ($Output) {
    Write-Ok "완성: $($Output.FullName)  ($([math]::Round($Output.Length/1MB,1)) MB)"
} else {
    Write-Err "출력 setup.exe 를 찾을 수 없음"
}
