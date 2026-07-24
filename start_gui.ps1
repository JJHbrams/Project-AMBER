# start_gui.ps1 — Engram Tauri GUI 개발 런처
# 필수 백엔드(overlay.exe 또는 dev_backend)가 실행 중인지 확인 후 Tauri dev 서버를 띄운다.

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$GuiDir      = Join-Path $ProjectRoot "gui"
$OverlayExe  = Join-Path $ProjectRoot "build\engram-overlay\engram-overlay.exe"
$DevBackend  = Join-Path $ProjectRoot "scripts\dev\dev_backend.ps1"

# ── 포트 응답 확인 ────────────────────────────────────────────────────────────
function Test-Port($port) {
    try {
        $tc = [System.Net.Sockets.TcpClient]::new()
        $tc.Connect("127.0.0.1", $port)
        $tc.Close()
        return $true
    } catch { return $false }
}

# ── 스테일 프로세스 정리 ──────────────────────────────────────────────────────
function Stop-StaleProcessByPattern {
    param([string]$Name, [string]$Pattern, [string]$Label)
    $targets = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -eq $Name -and $_.CommandLine -match $Pattern }
    foreach ($t in $targets) {
        try {
            Stop-Process -Id $t.ProcessId -Force -ErrorAction Stop
            Write-Host "  [-] 스테일 $Label 종료 (PID $($t.ProcessId))" -ForegroundColor Yellow
        } catch {
            Write-Warning "  [!] $Label 종료 실패 (PID $($t.ProcessId)): $($_.Exception.Message)"
        }
    }
}

# ── PATH 보강 ─────────────────────────────────────────────────────────────────
function Ensure-Path {
    # Rust/Cargo
    $cargoBin = "$env:USERPROFILE\.cargo\bin"
    if ((Test-Path $cargoBin) -and ($env:PATH -notlike "*$cargoBin*")) {
        $env:PATH = "$cargoBin;$env:PATH"
    }
    # Node/npm
    foreach ($nodeBin in @(
        "$env:ProgramFiles\nodejs",
        "$env:ProgramFiles(x86)\nodejs",
        "$env:LOCALAPPDATA\Programs\nodejs"
    )) {
        if ((Test-Path $nodeBin) -and ($env:PATH -notlike "*$nodeBin*")) {
            $env:PATH = "$nodeBin;$env:PATH"
        }
    }
}

# ═════════════════════════════════════════════════════════════════════════════
Write-Host ""
Write-Host "  ╔═══════════════════════════════════════╗" -ForegroundColor Magenta
Write-Host "  ║       Engram GUI — Dev Launcher       ║" -ForegroundColor Magenta
Write-Host "  ╚═══════════════════════════════════════╝" -ForegroundColor Magenta
Write-Host ""

# ── 1. 백엔드 상태 확인 ───────────────────────────────────────────────────────
$stmOk = Test-Port 17384
$mcpOk = Test-Port 17385

Write-Host "  [백엔드 상태]" -ForegroundColor Cyan
Write-Host "    STM  :17384  $(if ($stmOk) { '[OK]' } else { '[X]' })" -ForegroundColor $(if ($stmOk) { 'Green' } else { 'Red' })
Write-Host "    MCP  :17385  $(if ($mcpOk) { '[OK]' } else { '[X]' })" -ForegroundColor $(if ($mcpOk) { 'Green' } else { 'Red' })
Write-Host ""

if (-not ($stmOk -and $mcpOk)) {
    Write-Host "  [!] 필수 백엔드가 실행되고 있지 않습니다." -ForegroundColor Yellow
    Write-Host ""
    Write-Host "  백엔드 시작 방법을 선택하세요:" -ForegroundColor White
    Write-Host "    [1] overlay.exe 실행  ($OverlayExe)" -ForegroundColor Gray
    Write-Host "    [2] dev_backend start (독립 프로세스, overlay 없이)" -ForegroundColor Gray
    Write-Host "    [3] 그냥 진행         (백엔드 없이 GUI만 띄움)" -ForegroundColor Gray
    Write-Host ""
    $choice = Read-Host "  선택 (1/2/3) [기본: 2]"
    if (-not $choice) { $choice = "2" }

    switch ($choice.Trim()) {
        "1" {
            if (Test-Path $OverlayExe) {
                Write-Host ""
                Write-Host "  overlay.exe 시작 중..." -ForegroundColor Cyan
                Start-Process -FilePath $OverlayExe -WindowStyle Hidden
                Write-Host "  overlay.exe 기동 대기 중 (최대 8초)..." -ForegroundColor DarkGray
                $waited = 0
                while (-not (Test-Port 17385) -and $waited -lt 8) {
                    Start-Sleep -Seconds 1
                    $waited++
                }
                if (Test-Port 17385) {
                    Write-Host "  [OK] 백엔드 준비됨" -ForegroundColor Green
                } else {
                    Write-Host "  [!] 타임아웃 — 백엔드 미응답. GUI는 계속 실행됩니다." -ForegroundColor Yellow
                }
            } else {
                Write-Host "  [X] overlay.exe 를 찾을 수 없습니다: $OverlayExe" -ForegroundColor Red
                Write-Host "  →  먼저 빌드하거나 dev_backend를 사용하세요." -ForegroundColor DarkGray
            }
        }
        "2" {
            if (Test-Path $DevBackend) {
                Write-Host ""
                Write-Host "  dev_backend start 실행 중..." -ForegroundColor Cyan
                & $DevBackend start
            } else {
                Write-Host "  [X] dev_backend.ps1 을 찾을 수 없습니다: $DevBackend" -ForegroundColor Red
            }
        }
        "3" {
            Write-Host "  [!] 백엔드 없이 진행합니다. MCP/KG 기능은 비활성화됩니다." -ForegroundColor Yellow
        }
        default {
            Write-Host "  [!] 알 수 없는 선택. 백엔드 없이 진행합니다." -ForegroundColor Yellow
        }
    }
    Write-Host ""
}

# ── 2. 스테일 프로세스 정리 ───────────────────────────────────────────────────
$guiRegex = [Regex]::Escape($GuiDir)
Stop-StaleProcessByPattern -Name "node.exe"  -Pattern "$guiRegex.*vite.*1420"  -Label "Vite (gui)"
Stop-StaleProcessByPattern -Name "gui.exe"   -Pattern "target\\debug\\gui\.exe" -Label "Tauri gui.exe"

# ── 3. PATH 보강 ──────────────────────────────────────────────────────────────
Ensure-Path

$npmCmd = Get-Command npm.cmd -ErrorAction SilentlyContinue
if (-not $npmCmd) {
    Write-Host "  [X] npm.cmd 를 찾을 수 없습니다. Node.js 를 설치하거나 PATH 를 확인하세요." -ForegroundColor Red
    exit 1
}

# ── 4. Tauri dev 실행 ─────────────────────────────────────────────────────────
Write-Host "  Tauri GUI (dev) 시작 중..." -ForegroundColor Cyan
Write-Host "  -> $GuiDir" -ForegroundColor DarkGray
Write-Host ""

Push-Location $GuiDir
try {
    & $npmCmd.Source run tauri -- dev
} finally {
    Pop-Location
}
