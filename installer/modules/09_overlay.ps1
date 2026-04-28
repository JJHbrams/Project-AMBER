#
# 09_overlay.ps1 — Overlay exe 빌드 (PyInstaller), launcher cmd, overlay.user.yaml, .env 템플릿
#

# 9. Overlay exe
Write-Step "Overlay build..."
$DistExe = Join-Path $ProjectRoot "dist\engram-overlay\engram-overlay.exe"
$specFile = Join-Path $ProjectRoot "engram-overlay.spec"

if (-not (Test-Path $specFile)) {
    Write-Warn "Spec not found — skipping overlay build"
} elseif ($OverlayBuildMode -eq "skip") {
    Write-Warn "Overlay build skipped by option: -OverlayBuildMode skip"
} else {
    $buildDecision = switch ($OverlayBuildMode) {
        "clean" { @{ Required = $true; Reason = "forced clean build" } }
        "rebuild" { @{ Required = $true; Reason = "forced rebuild" } }
        default { Test-OverlayBuildRequired -projectRoot $ProjectRoot -specPath $specFile -distExePath $DistExe }
    }

    if (-not [bool]$buildDecision.Required) {
        Write-Ok "Skip overlay build ($($buildDecision.Reason))"
    } else {
        Write-Step "Building overlay exe... ($($buildDecision.Reason))"

        # PyInstaller COLLECT는 항상 dist 폴더를 먼저 삭제 시도하므로
        # 빌드 전에 overlay 및 모든 engram 자식 프로세스(python.exe)를 종료해야 한다.
        $overlayProcs = Get-Process -Name "engram-overlay" -ErrorAction SilentlyContinue
        if ($overlayProcs) {
            Write-Warn "실행 중인 engram-overlay 종료 중..."
            $overlayProcs | Stop-Process -Force
        }
        # mcp_server / dashboard / kg_watcher — python.exe 고아 프로세스 정리
        foreach ($pattern in @("mcp_server.py", "engram_dashboard.py", "kg_watcher.py")) {
            $procIds = (Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
                Where-Object { $_.CommandLine -like "*$pattern*" } |
                Select-Object -ExpandProperty ProcessId)
            foreach ($procId in $procIds) {
                try { Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue } catch {}
            }
        }
        # 커널이 핸들을 해제할 때까지 충분히 대기
        if ($overlayProcs) { Start-Sleep -Seconds 2 }

        $buildLog = Join-Path $env:TEMP "engram_pyinstaller_build.log"
        $attemptCleanBuild = ($OverlayBuildMode -eq "clean")
        $cleanRetried = $false

        # VS Code 파일 워처가 dist\engram-overlay\ 디렉토리 핸들을 유지하면
        # shutil.rmtree 가 os.rmdir 단계에서 WinError 32 로 실패한다.
        # 회피책: --distpath 를 %TEMP% 임시 경로로 지정해 빌드 후 최종 위치로 이동.
        $TempDistPath = Join-Path $env:TEMP "engram-pyinstaller-dist"
        $TempDistExe  = Join-Path $TempDistPath "engram-overlay\engram-overlay.exe"
        $DistDir      = Join-Path $ProjectRoot "dist\engram-overlay"

        while ($true) {
            if ($attemptCleanBuild) {
                # build/ 캐시 삭제 (--clean 과 동일한 효과)
                $BuildWorkDir = Join-Path $ProjectRoot "build\engram-overlay"
                if (Test-Path $BuildWorkDir) {
                    try {
                        Get-ChildItem $BuildWorkDir -Recurse | ForEach-Object { $_.Attributes = 'Normal' }
                        Remove-Item $BuildWorkDir -Recurse -Force -ErrorAction Stop
                    } catch {
                        Write-Warn "build/ 사전 정리 실패 (계속 진행): $_"
                    }
                }
                # 임시 dist 도 초기화
                if (Test-Path $TempDistPath) {
                    Remove-Item $TempDistPath -Recurse -Force -ErrorAction SilentlyContinue
                }
            }

            # Build via conda-run so Tcl/Tk DLLs are resolved from the target env,
            # not from base miniconda (prevents Tcl 8.6.x version mismatch at runtime).
            # Note: use `python -m PyInstaller` instead of bare `pyinstaller` because
            # `conda run` may not expose the env's Scripts/ directory in PATH.
            # --distpath 를 TEMP 로 지정해 VS Code 워처 락 회피.
            if ($condaCmd -and $HasNamedCondaEnv) {
                if ($attemptCleanBuild) {
                    $buildOutput = & conda run -n $CondaEnv python -m PyInstaller --noconfirm --clean --distpath $TempDistPath $specFile 2>&1
                } else {
                    $buildOutput = & conda run -n $CondaEnv python -m PyInstaller --noconfirm --distpath $TempDistPath $specFile 2>&1
                }
            } else {
                Write-Warn "Named conda env unavailable — falling back to python -m PyInstaller"
                if ($attemptCleanBuild) {
                    $buildOutput = & $PythonExe -m PyInstaller --noconfirm --clean --distpath $TempDistPath $specFile 2>&1
                } else {
                    $buildOutput = & $PythonExe -m PyInstaller --noconfirm --distpath $TempDistPath $specFile 2>&1
                }
            }
            $buildExitCode = $LASTEXITCODE

            # 빌드 로그 저장
            $buildOutput | Out-File -FilePath $buildLog -Encoding utf8 -Force

            if ($buildExitCode -eq 0 -and (Test-Path $TempDistExe)) {
                # TEMP → 최종 위치로 복사 (robocopy — rmdir 없이 파일 단위 덮어쓰기)
                # VS Code 워처가 디렉토리 핸들을 보유해 rmdir 이 실패해도 robocopy 는 파일 단위로 동작하므로 성공.
                $TempSrc = Join-Path $TempDistPath "engram-overlay"
                if (-not (Test-Path $DistDir)) { New-Item $DistDir -ItemType Directory -Force | Out-Null }
                $null = robocopy $TempSrc $DistDir /E /PURGE /NFL /NDL /NJH /NJS /R:2 /W:1
                $robocopyExit = $LASTEXITCODE
                if ($robocopyExit -ge 8) {
                    Write-Warn "robocopy 실패 (exit $robocopyExit) — 빌드 결과: $TempSrc"
                } else {
                    $modeLabel = if ($attemptCleanBuild) { "clean" } else { "incremental" }
                    Write-Ok "Built ($modeLabel): $DistExe"
                    break
                }
            }

            if ($OverlayBuildMode -eq "auto" -and -not $attemptCleanBuild -and -not $cleanRetried) {
                Write-Warn "Incremental build failed — retrying once with clean build"
                $attemptCleanBuild = $true
                $cleanRetried = $true
                continue
            }

            Write-Err "Build failed. Log: $buildLog"
            Write-Host ""
            Write-Host "  --- PyInstaller output (last 30 lines) ---" -ForegroundColor Yellow
            $buildOutput | Select-Object -Last 30 | ForEach-Object { Write-Host "  $_" -ForegroundColor DarkYellow }
            Write-Host "  ------------------------------------------" -ForegroundColor Yellow
            Write-Host ""
            Write-Warn "Overlay will not be available. Fix the error above and re-run install.ps1"
            if ($OverlayBuildMode -ne "clean") {
                Write-Warn "필요 시 clean 빌드: .\install.ps1 -OverlayBuildMode clean"
            }
            break
        }
    }
}

# 10. Overlay launcher (engram-overlay command)
Write-Step "Overlay launcher..."
$OverlayCmdPath = Join-Path $ShimDir "engram-overlay.cmd"
$overlayCmdContent = "@echo off`r`nstart `"`" `"$DistExe`""
[System.IO.File]::WriteAllText($OverlayCmdPath, $overlayCmdContent, [System.Text.ASCIIEncoding]::new())
Write-Ok $OverlayCmdPath

# 11. User config (~/.engram/overlay.user.yaml)
Write-Step "User config..."
$TemplateOverlayConfigPath = Join-Path $ProjectRoot "config\overlay.user.yaml"
if (-not (Test-Path $OverlayUserConfigPath)) {
    if (Test-Path $TemplateOverlayConfigPath) {
        Copy-Item $TemplateOverlayConfigPath $OverlayUserConfigPath
        Write-Ok "Created: $OverlayUserConfigPath"
    } else {
        Write-Warn "Template not found — will be auto-generated on first run"
    }
} else { Write-Ok "Exists: $OverlayUserConfigPath" }

if (-not (Test-Path $OverlayUserConfigPath)) {
    $minimalOverlayUserConfig = @"
cli:
  provider: "$DefaultCliProvider"
"@
    [System.IO.File]::WriteAllText($OverlayUserConfigPath, $minimalOverlayUserConfig, [System.Text.UTF8Encoding]::new($false))
    Write-Ok "Created minimal config: $OverlayUserConfigPath"
}

$setOverlayProviderScript = @"
import yaml
path = r'$($OverlayUserConfigPath -replace '\\', '/')'
with open(path, encoding='utf-8') as f:
    data = yaml.safe_load(f) or {}
if not isinstance(data, dict):
    data = {}
cli = data.get('cli')
if not isinstance(cli, dict):
    cli = {}
cli['provider'] = r'$DefaultCliProvider'
if r'$SelectedOllamaModel':
    cli['ollama_model'] = r'$SelectedOllamaModel'
data['cli'] = cli
# MCP HTTP 서버 python_exe 저장 (overlay.exe 동결 모드 대비)
mcp = data.get('mcp')
if not isinstance(mcp, dict):
    mcp = {}
mcp['python_exe'] = r'$($PythonExe -replace '\\', '/')'
mcp['http_port'] = $MCP_HTTP_PORT
data['mcp'] = mcp
with open(path, 'w', encoding='utf-8') as f:
    yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
print('updated')
"@
$setOverlayProviderResult = & $PythonExe -c $setOverlayProviderScript 2>&1
if ($setOverlayProviderResult -like "*updated*") {
    Write-Ok "Set overlay default provider: $DefaultCliProvider"
} else {
    Write-Warn "Could not update overlay default provider: $setOverlayProviderResult"
}

# 11b. .env 템플릿 (~/.engram/.env)
Write-Step ".env 파일..."
$EnvPath = Join-Path $ShimDir ".env"
if (-not (Test-Path $EnvPath)) {
    $envTemplate = @"
# Engram 환경변수 설정
# 이 파일은 ~/.engram/.env 에 위치합니다 (git에 포함되지 않음)

# Discord Bot Token (Discord Developer Portal에서 발급)
# https://discord.com/developers/applications
DISCORD_BOT_TOKEN=
"@
    [System.IO.File]::WriteAllText($EnvPath, $envTemplate, [System.Text.UTF8Encoding]::new($false))
    Write-Ok "Created: $EnvPath"
    Write-Warn "DISCORD_BOT_TOKEN을 $EnvPath 에 입력하세요"
} else { Write-Ok "Exists: $EnvPath" }
