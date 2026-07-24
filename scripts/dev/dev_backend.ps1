<#
.SYNOPSIS
    Engram 개발용 백엔드 스탠드얼론 런처
    overlay 개발·테스트 중에도 MCP/STM 백엔드가 끊기지 않도록 독립 프로세스로 기동합니다.

.DESCRIPTION
    서브커맨드:
      start   — STM 브로커(17384) + MCP SSE 서버(17385) 를 백그라운드에서 기동
      stop    — 기동된 백엔드 프로세스를 PID 파일로 종료
      status  — 현재 실행 상태 확인
      restart — stop 후 start

    overlay.exe 와 독립적으로 동작하므로, overlay GUI 를 재시작해도 MCP 중계가 유지됩니다.

.EXAMPLE
    .\scripts\dev\dev_backend.ps1 start
    .\scripts\dev\dev_backend.ps1 status
    .\scripts\dev\dev_backend.ps1 stop
    .\scripts\dev\dev_backend.ps1 restart
#>

param(
    [Parameter(Position = 0)]
    [ValidateSet("start", "stop", "status", "restart")]
    # [string]$Command = "start"
    [string]$Command = "status"
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()

$ProjectRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$CondaEnv    = "intel_engram"
$PidDir      = Join-Path $env:TEMP "engram_dev_backend"
$PidStm      = Join-Path $PidDir "stm.pid"
$PidMcp      = Join-Path $PidDir "mcp.pid"
$PidWatcher  = Join-Path $PidDir "kg_watcher.pid"

# ── Python 탐지 ─────────────────────────────────────────────────────────────
function Find-Python {
    $condaCmd = Get-Command conda -ErrorAction SilentlyContinue
    if ($condaCmd) {
        $envPath = (conda info --envs 2>&1 | Select-String "^\s*$CondaEnv\s") -replace "^\s*$CondaEnv\s+\*?\s*", "" |
                   ForEach-Object { $_.Trim() }
        if ($envPath) {
            $c = Join-Path $envPath "python.exe"
            if (Test-Path $c) { return $c }
        }
    }
    foreach ($c in @(
        "$env:USERPROFILE\miniconda3\envs\$CondaEnv\python.exe",
        "$env:USERPROFILE\anaconda3\envs\$CondaEnv\python.exe",
        "$env:LOCALAPPDATA\miniconda3\envs\$CondaEnv\python.exe"
    )) { if (Test-Path $c) { return $c } }
    $py = (Get-Command python -ErrorAction SilentlyContinue)?.Source
    return $py
}

# ── PID 헬퍼 ────────────────────────────────────────────────────────────────
function Read-Pid($path) {
    if (-not (Test-Path $path)) { return $null }
    $val = (Get-Content $path -Raw).Trim()
    if ($val -match '^\d+$') { return [int]$val }
    return $null
}

function Is-Running($pid_val) {
    if ($null -eq $pid_val) { return $false }
    return (Get-Process -Id $pid_val -ErrorAction SilentlyContinue) -ne $null
}

function Kill-Backend($label, $pidFile) {
    $pid_val = Read-Pid $pidFile
    if ($null -eq $pid_val) {
        Write-Host "  [ ] $label — PID 파일 없음" -ForegroundColor DarkGray
        return
    }
    if (Is-Running $pid_val) {
        Stop-Process -Id $pid_val -Force -ErrorAction SilentlyContinue
        Write-Host "  [-] $label 종료 (PID $pid_val)" -ForegroundColor Yellow
    } else {
        Write-Host "  [ ] $label — 이미 중단됨 (PID $pid_val)" -ForegroundColor DarkGray
    }
    Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
}

# ── START ────────────────────────────────────────────────────────────────────
function Start-Backend {
    $PythonExe = Find-Python
    if (-not $PythonExe) {
        Write-Host "  [X] Python not found. conda env '$CondaEnv' 를 확인하세요." -ForegroundColor Red
        exit 1
    }

    New-Item -ItemType Directory -Path $PidDir -Force | Out-Null

    # 이미 실행 중인지 체크
    $stmPid     = Read-Pid $PidStm
    $mcpPid     = Read-Pid $PidMcp
    $watcherPid = Read-Pid $PidWatcher
    if ((Is-Running $stmPid) -and (Is-Running $mcpPid) -and (Is-Running $watcherPid)) {
        Write-Host "  [!] 백엔드가 이미 실행 중입니다. (STM PID=$stmPid, MCP PID=$mcpPid, Watcher PID=$watcherPid)" -ForegroundColor Yellow
        Write-Host "  →  'status' 로 상태 확인, 'restart' 로 재시작하세요." -ForegroundColor DarkGray
        return
    }

    Write-Host ""
    Write-Host "  [dev] Engram 백엔드 기동" -ForegroundColor Cyan
    Write-Host "  Python : $PythonExe" -ForegroundColor DarkGray
    Write-Host "  Root   : $ProjectRoot" -ForegroundColor DarkGray
    Write-Host ""

    $Env:PYTHONPATH        = $ProjectRoot
    $Env:PYTHONIOENCODING  = "utf-8"

    # DB 루트 경로 — MCP 서버에 ENGRAM_DB_DIR로 주입 (overlay.exe 동작과 동일)
    $DbRootDir = (& $PythonExe -c "import sys; sys.path.insert(0, '$($ProjectRoot -replace '\\','/')'); from core.config.runtime_config import get_db_root_dir; print(get_db_root_dir())" 2>$null)
    if (-not $DbRootDir) { $DbRootDir = "D:\intel_engram" }

    # ── STM 브로커 (port 17384) ─────────────────────────────────────────────
    if (-not (Is-Running $stmPid)) {
        # 포트 점유 확인 — overlay.exe 내장 STM이 이미 실행 중이면 재사용 (overlay.exe 동작과 동일)
        $stm17384Occupied = $false
        try { $tc = [System.Net.Sockets.TcpClient]::new(); $tc.Connect("127.0.0.1", 17384); $tc.Close(); $stm17384Occupied = $true } catch {}

        if ($stm17384Occupied) {
            $stmHealth = [PSCustomObject]@{ status = "unknown"; role = "unknown" }
            try { $stmHealth = Invoke-RestMethod "http://127.0.0.1:17384/health" -TimeoutSec 2 } catch {}
            Write-Host "  [OK] STM 브로커    포트 17384 이미 점유 — 재사용 (role=$($stmHealth.role), status=$($stmHealth.status))" -ForegroundColor Cyan
        } else {
            $stmScript = @"
import sys, os, time
os.environ["ENGRAM_RUNTIME_ROLE"] = "overlay"
sys.path.insert(0, r'$($ProjectRoot -replace '\\','/')')
from overlay.stm_server import STMServer
s = STMServer(port=17384)
s.start()
print('[stm] STM broker listening on port 17384', flush=True)
try:
    while True:
        time.sleep(1)
except (KeyboardInterrupt, SystemExit):
    s.stop()
"@
            $stmTempPy = Join-Path $PidDir "stm_worker.py"
            [System.IO.File]::WriteAllText($stmTempPy, $stmScript, [System.Text.UTF8Encoding]::new($false))

            $stmProc = Start-Process -FilePath $PythonExe `
                -ArgumentList $stmTempPy `
                -WorkingDirectory $ProjectRoot `
                -WindowStyle Hidden `
                -PassThru
            $stmProc.Id | Set-Content $PidStm -NoNewline
            Start-Sleep -Milliseconds 800

            try {
                $h = Invoke-RestMethod "http://127.0.0.1:17384/health" -TimeoutSec 3
                Write-Host "  [OK] STM 브로커    PID=$($stmProc.Id)  http://127.0.0.1:17384  status=$($h.status)" -ForegroundColor Green
            } catch {
                Write-Host "  [!]  STM 브로커    PID=$($stmProc.Id)  헬스체크 미응답 (기동 중일 수 있음)" -ForegroundColor Yellow
            }
        }
    } else {
        Write-Host "  [OK] STM 브로커    PID=$stmPid  (이미 실행 중)" -ForegroundColor Green
    }

    # ── MCP SSE 서버 (port 17385) ───────────────────────────────────────────
    if (-not (Is-Running $mcpPid)) {
        # 포트 점유 확인 — overlay.exe 또는 외부 MCP가 이미 실행 중이면 재사용 (overlay.exe 동작과 동일)
        $mcp17385Occupied = $false
        try { $tc = [System.Net.Sockets.TcpClient]::new(); $tc.Connect("127.0.0.1", 17385); $tc.Close(); $mcp17385Occupied = $true } catch {}

        if ($mcp17385Occupied) {
            Write-Host "  [OK] MCP SSE 서버  포트 17385 이미 점유 — 재사용 http://127.0.0.1:17385/sse" -ForegroundColor Cyan
        } else {
            $mcpScript = Join-Path $ProjectRoot "mcp_server.py"
            # ENGRAM_DB_DIR 주입 — overlay.exe가 MCP subprocess에 주입하는 것과 동일
            $Env:ENGRAM_DB_DIR = $DbRootDir
            $mcpProc = Start-Process -FilePath $PythonExe `
                -ArgumentList $mcpScript, "--transport", "sse", "--port", "17385" `
                -WorkingDirectory $ProjectRoot `
                -WindowStyle Hidden `
                -PassThru
            $Env:ENGRAM_DB_DIR = ""
            $mcpProc.Id | Set-Content $PidMcp -NoNewline
            Start-Sleep -Milliseconds 1200

            $mcpOk = $false
            try {
                $tcpClient = [System.Net.Sockets.TcpClient]::new()
                $tcpClient.Connect("127.0.0.1", 17385)
                $tcpClient.Close()
                $mcpOk = $true
            } catch {}

            if ($mcpOk) {
                Write-Host "  [OK] MCP SSE 서버  PID=$($mcpProc.Id)  http://127.0.0.1:17385/sse" -ForegroundColor Green
            } else {
                Write-Host "  [!]  MCP SSE 서버  PID=$($mcpProc.Id)  포트 미응답 (기동 중일 수 있음)" -ForegroundColor Yellow
            }
        }
    } else {
        Write-Host "  [OK] MCP SSE 서버  PID=$mcpPid  (이미 실행 중)" -ForegroundColor Green
    }

    # ── kg_watcher ──────────────────────────────────────────────────────────
    if (-not (Is-Running $watcherPid)) {
        $watcherScript = Join-Path $ProjectRoot "scripts\kg\kg_watcher.py"
        $watcherProc = Start-Process -FilePath $PythonExe `
            -ArgumentList $watcherScript `
            -WorkingDirectory $ProjectRoot `
            -WindowStyle Hidden `
            -PassThru
        $watcherProc.Id | Set-Content $PidWatcher -NoNewline
        Start-Sleep -Milliseconds 800
        if (Is-Running $watcherProc.Id) {
            Write-Host "  [OK] kg_watcher    PID=$($watcherProc.Id)" -ForegroundColor Green
        } else {
            Write-Host "  [!]  kg_watcher    PID=$($watcherProc.Id)  기동 실패" -ForegroundColor Yellow
        }
    } else {
        Write-Host "  [OK] kg_watcher    PID=$watcherPid  (이미 실행 중)" -ForegroundColor Green
    }

    Write-Host ""
    Write-Host "  ─────────────────────────────────────────────────" -ForegroundColor DarkGray
    Write-Host "  MCP SSE : http://127.0.0.1:17385/sse" -ForegroundColor White
    Write-Host "  STM     : http://127.0.0.1:17384" -ForegroundColor White
    Write-Host "  PID 파일: $PidDir" -ForegroundColor DarkGray
    Write-Host "  종료 시 : .\scripts\dev\dev_backend.ps1 stop" -ForegroundColor DarkGray
    Write-Host "  ─────────────────────────────────────────────────" -ForegroundColor DarkGray
    Write-Host ""
}

# ── STOP ─────────────────────────────────────────────────────────────────────
function Stop-Backend {
    Write-Host ""
    Write-Host "  [dev] 백엔드 종료 중..." -ForegroundColor Yellow
    Kill-Backend "STM 브로커   " $PidStm
    Kill-Backend "MCP SSE 서버 " $PidMcp
    Kill-Backend "kg_watcher   " $PidWatcher
    Write-Host "  [OK] 완료" -ForegroundColor Green
    Write-Host ""
}

# ── STATUS ───────────────────────────────────────────────────────────────────
function Show-Status {
    $stmPid = Read-Pid $PidStm
    $mcpPid = Read-Pid $PidMcp

    Write-Host ""
    Write-Host "  [dev] 백엔드 상태" -ForegroundColor Cyan
    Write-Host ""

    # STM
    if (Is-Running $stmPid) {
        $stmStatus = "unknown"
        try { $h = Invoke-RestMethod "http://127.0.0.1:17384/health" -TimeoutSec 2; $stmStatus = $h.status } catch {}
        Write-Host "  [실행 중] STM 브로커   PID=$stmPid  http://127.0.0.1:17384  status=$stmStatus" -ForegroundColor Green
    } else {
        if ($null -ne $stmPid) {
            Write-Host "  [종료됨] STM 브로커   PID=$stmPid (프로세스 없음)" -ForegroundColor Red
            Remove-Item $PidStm -Force -ErrorAction SilentlyContinue
        }
        $extStm = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
                  Where-Object { $_.CommandLine -like "*stm_server*" -or $_.CommandLine -like "*stm_worker*" }
        if ($extStm) {
            $stmStatus = "unknown"
            try { $h = Invoke-RestMethod "http://127.0.0.1:17384/health" -TimeoutSec 2; $stmStatus = $h.status } catch {}
            Write-Host "  [외부실행] STM 브로커   PID=$($extStm.ProcessId)  (dev_backend 외 기동)  status=$stmStatus" -ForegroundColor Cyan
        } elseif ($null -eq $stmPid) {
            Write-Host "  [중단]   STM 브로커   — 실행 안 됨" -ForegroundColor DarkGray
        }
    }

    # MCP
    if (Is-Running $mcpPid) {
        $mcpOk = $false
        try { $tc = [System.Net.Sockets.TcpClient]::new(); $tc.Connect("127.0.0.1",17385); $tc.Close(); $mcpOk=$true } catch {}
        $portStatus = if ($mcpOk) { "포트 응답 OK" } else { "포트 미응답" }
        Write-Host "  [실행 중] MCP SSE 서버  PID=$mcpPid  http://127.0.0.1:17385/sse  $portStatus" -ForegroundColor $(if ($mcpOk) { "Green" } else { "Yellow" })
    } else {
        if ($null -ne $mcpPid) {
            Write-Host "  [종료됨] MCP SSE 서버  PID=$mcpPid (프로세스 없음)" -ForegroundColor Red
            Remove-Item $PidMcp -Force -ErrorAction SilentlyContinue
        }
        # PID 파일 없어도 외부 실행 프로세스 탐지 (fallback)
        $extMcp = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
                  Where-Object { $_.CommandLine -like "*mcp_server*" }
        if ($extMcp) {
            $mcpOk = $false
            try { $tc = [System.Net.Sockets.TcpClient]::new(); $tc.Connect("127.0.0.1",17385); $tc.Close(); $mcpOk=$true } catch {}
            $portStatus = if ($mcpOk) { "포트 응답 OK" } else { "포트 미응답" }
            Write-Host "  [외부실행] MCP SSE 서버  PID=$($extMcp.ProcessId)  (dev_backend 외 기동)  $portStatus" -ForegroundColor Cyan
        } elseif ($null -eq $mcpPid) {
            Write-Host "  [중단]   MCP SSE 서버  — 실행 안 됨" -ForegroundColor DarkGray
        }
    }

    # kg_watcher
    $watcherPid = Read-Pid $PidWatcher
    if (Is-Running $watcherPid) {
        Write-Host "  [실행 중] kg_watcher   PID=$watcherPid" -ForegroundColor Green
    } else {
        if ($null -ne $watcherPid) {
            Write-Host "  [종료됨] kg_watcher   PID=$watcherPid (프로세스 없음)" -ForegroundColor Red
            Remove-Item $PidWatcher -Force -ErrorAction SilentlyContinue
        }
        $extWatcher = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
                      Where-Object { $_.CommandLine -like "*kg_watcher*" }
        if ($extWatcher) {
            Write-Host "  [외부실행] kg_watcher   PID=$($extWatcher.ProcessId)  (dev_backend 외 기동)" -ForegroundColor Cyan
        } elseif ($null -eq $watcherPid) {
            Write-Host "  [중단]   kg_watcher   — 실행 안 됨" -ForegroundColor DarkGray
        }
    }

    Write-Host ""
}

# ── DISPATCH ─────────────────────────────────────────────────────────────────
switch ($Command) {
    "start"   { Start-Backend }
    "stop"    { Stop-Backend }
    "status"  { Show-Status }
    "restart" { Stop-Backend; Start-Sleep -Milliseconds 500; Start-Backend }
}
