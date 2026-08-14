#
# 04_dependencies.ps1 — Python 패키지, Sentence Transformer, Ollama 모델 확인/설치
#

# 3. MCP server script
Write-Step "MCP server script..."
if (-not (Test-Path $McpServerScript)) { Write-Err "Not found: $McpServerScript"; exit 1 }
Write-Ok $McpServerScript

# 4. Python dependencies
Write-Step "Python dependencies..."
$check = & $PythonExe -c "from mcp.server.fastmcp import FastMCP; import yaml; print('ok')" 2>&1
$checkExitCode = $LASTEXITCODE
if ($checkExitCode -ne 0 -or (($check | ForEach-Object { "$_" }) -notcontains "ok")) {
    Invoke-LiveProcessLog -FilePath $PythonExe -ArgumentList @("-m", "pip", "install", "-r", (Join-Path $ProjectRoot "requirements.txt")) -Activity "pip install requirements" | Out-Null
    Write-Ok "Installed"
} else { Write-Ok "OK" }

# 4d. Sentence Transformer 임베딩 모델 (semantic search용)
Write-Step "Sentence Transformer model (intfloat/multilingual-e5-small)..."
$EmbeddingModel = "intfloat/multilingual-e5-small"

# 패키지 존재 확인
$stPkgCheck = try {
    $prev = $ErrorActionPreference; $ErrorActionPreference = "Continue"
    (& $PythonExe -c "import os; os.environ['TOKENIZERS_PARALLELISM']='false'; from sentence_transformers import SentenceTransformer; print('ok')" 2>&1 | ForEach-Object { "$_" }) -join ""
} catch { "err" } finally { $ErrorActionPreference = $prev }

if ($stPkgCheck -notlike "*ok*") {
    Write-Err "sentence-transformers 패키지가 설치되지 않았습니다."
    Write-Err "  Python 환경 구성(03단계)이 실패했을 가능성이 높습니다."
    Write-Err "  수동 조치: conda run -n $CondaEnv pip install sentence-transformers"
    exit 1
}

# 공통 Python 헬퍼 — 모델 하나를 시도하고 ok/error 반환
function Invoke-StModel([string]$endpoint) {
    $py = @"
import sys, os
os.environ['TOKENIZERS_PARALLELISM'] = 'false'
if '$endpoint': os.environ['HF_ENDPOINT'] = '$endpoint'
try:
    from sentence_transformers import SentenceTransformer
    SentenceTransformer('$EmbeddingModel', local_files_only=True) if not '$endpoint' else None
    SentenceTransformer('$EmbeddingModel'$(if ($endpoint) { "" } else { ", local_files_only=True" }))
    sys.stdout.write('ok\n'); sys.stdout.flush()
except Exception as e:
    sys.stdout.write(f'error:{e}\n'); sys.stdout.flush()
"@
    $prev = $ErrorActionPreference; $ErrorActionPreference = "Continue"
    $out = try { (& $PythonExe -c $py 2>&1 | ForEach-Object { "$_" } | Where-Object { $_ -match '^(ok|error:)' } | Select-Object -Last 1) } catch { "error:exception" } finally { $ErrorActionPreference = $prev }
    return ($out -as [string]).Trim()
}

$stModelOk = $false

# [빠른 확인] Python(torch 임포트 포함)을 띄우지 않고 HuggingFace 캐시 폴더에 스냅샷
# 파일이 실제로 있는지만 파일시스템으로 먼저 본다 — 이미 캐시돼 있으면 아래 [1/3]의
# "python -c 'from sentence_transformers import ...'" 호출(수 초 소요)을 완전히 스킵한다.
# 애매하면(폴더 구조가 다르거나 파일이 불완전하면) 안전하게 기존 [1/3]~[3/3]으로 넘어간다.
$HfRepoDirName = "models--sentence-transformers--$EmbeddingModel"
$HfSnapshotsDir = Join-Path $env:USERPROFILE ".cache\huggingface\hub\$HfRepoDirName\snapshots"
$fastCacheHit = $false
if (Test-Path $HfSnapshotsDir) {
    foreach ($snap in Get-ChildItem $HfSnapshotsDir -Directory -ErrorAction SilentlyContinue) {
        $hasConfig = Test-Path (Join-Path $snap.FullName "config.json")
        $hasWeights = @(Get-ChildItem $snap.FullName -Filter "*.safetensors" -ErrorAction SilentlyContinue).Count -gt 0 `
            -or @(Get-ChildItem $snap.FullName -Filter "pytorch_model.bin" -ErrorAction SilentlyContinue).Count -gt 0
        if ($hasConfig -and $hasWeights) { $fastCacheHit = $true; break }
    }
}

if ($fastCacheHit) {
    Write-Host "    [빠른 확인] 캐시 파일 존재 확인됨 — python 임포트 생략" -ForegroundColor Green
    Write-Ok "Sentence Transformer 모델 준비 완료 (캐시 파일 확인, 다운로드 스킵)"
    $stModelOk = $true
}

# [1/3] 로컬 캐시 (빠른 확인이 애매했을 때만 — python으로 실제 로드 가능한지 검증)
if (-not $stModelOk) {
    Write-Host "    [1/3] 로컬 캐시 확인..." -NoNewline -ForegroundColor Cyan
    $r1py = @"
import sys, os
os.environ['TOKENIZERS_PARALLELISM'] = 'false'
try:
    from sentence_transformers import SentenceTransformer
    SentenceTransformer('$EmbeddingModel', local_files_only=True)
    sys.stdout.write('ok\n'); sys.stdout.flush()
except Exception as e:
    sys.stdout.write(f'miss:{e}\n'); sys.stdout.flush()
"@
    $prev = $ErrorActionPreference; $ErrorActionPreference = "Continue"
    $r1 = try { (& $PythonExe -c $r1py 2>&1 | ForEach-Object { "$_" } | Where-Object { $_ -match '^(ok|miss:)' } | Select-Object -Last 1) } catch { "miss:exception" } finally { $ErrorActionPreference = $prev }
    $r1 = ($r1 -as [string]).Trim()
    if ($r1 -like "ok*") {
        Write-Host " OK (캐시됨)" -ForegroundColor Green
        Write-Ok "Sentence Transformer 모델 준비 완료 (로컬 캐시)"
        $stModelOk = $true
    } else {
        Write-Host " 없음" -ForegroundColor DarkGray
    }
}  # [빠른 확인]이 애매해서 [1/3]까지 왔던 경우 종료

# [2/3] HuggingFace 직접 다운로드
if (-not $stModelOk) {
    Write-Host "    [2/3] HuggingFace에서 다운로드 중..." -NoNewline -ForegroundColor Cyan
    $r2py = @"
import sys, os
os.environ['TOKENIZERS_PARALLELISM'] = 'false'
try:
    from sentence_transformers import SentenceTransformer
    SentenceTransformer('$EmbeddingModel')
    sys.stdout.write('ok\n'); sys.stdout.flush()
except Exception as e:
    sys.stdout.write(f'error:{e}\n'); sys.stdout.flush()
"@
    $prev = $ErrorActionPreference; $ErrorActionPreference = "Continue"
    $r2 = try { (& $PythonExe -c $r2py 2>&1 | ForEach-Object { "$_" } | Where-Object { $_ -match '^(ok|error:)' } | Select-Object -Last 1) } catch { "error:exception" } finally { $ErrorActionPreference = $prev }
    $r2 = ($r2 -as [string]).Trim()
    if ($r2 -like "ok*") {
        Write-Host " 완료" -ForegroundColor Green
        Write-Ok "Sentence Transformer 모델 준비 완료 (HuggingFace)"
        $stModelOk = $true
    } else {
        Write-Host " 실패" -ForegroundColor Yellow
        Write-Host "      원인: $($r2 -replace '^error:','')" -ForegroundColor DarkGray
    }
}

# [3/3] HF 미러 (방화벽/기업망 대응)
if (-not $stModelOk) {
    Write-Host "    [3/3] 미러 서버(hf-mirror.com) 재시도 중..." -NoNewline -ForegroundColor Cyan
    $r3py = @"
import sys, os
os.environ['TOKENIZERS_PARALLELISM'] = 'false'
os.environ['HF_ENDPOINT'] = 'https://hf-mirror.com'
try:
    from sentence_transformers import SentenceTransformer
    SentenceTransformer('$EmbeddingModel')
    sys.stdout.write('ok\n'); sys.stdout.flush()
except Exception as e:
    sys.stdout.write(f'error:{e}\n'); sys.stdout.flush()
"@
    $prev = $ErrorActionPreference; $ErrorActionPreference = "Continue"
    $r3 = try { (& $PythonExe -c $r3py 2>&1 | ForEach-Object { "$_" } | Where-Object { $_ -match '^(ok|error:)' } | Select-Object -Last 1) } catch { "error:exception" } finally { $ErrorActionPreference = $prev }
    $r3 = ($r3 -as [string]).Trim()
    if ($r3 -like "ok*") {
        Write-Host " 완료" -ForegroundColor Green
        Write-Ok "Sentence Transformer 모델 준비 완료 (미러)"
        Write-Warn "  HuggingFace 직접 접속이 차단되어 미러(hf-mirror.com)를 통해 다운로드했습니다."
        $stModelOk = $true
    } else {
        Write-Host " 실패" -ForegroundColor Yellow
        Write-Host "      원인: $($r3 -replace '^error:','')" -ForegroundColor DarkGray
    }
}

# 최종 결과 고지
if (-not $stModelOk) {
    Write-Host ""
    Write-Err "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    Write-Err "  임베딩 모델 설치 실패 — 3단계 시도 모두 실패"
    Write-Err "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    Write-Host ""
    Write-Host "  [파급 효과]" -ForegroundColor Red
    Write-Host "    - 지식 그래프(KG) 시맨틱 검색 완전 비활성화" -ForegroundColor Red
    Write-Host "    - kg_semantic_search, kg_semantic_neighbors MCP 도구 무응답" -ForegroundColor Red
    Write-Host "    - context_builder 의 시맨틱 연관 기억 주입 불가" -ForegroundColor Red
    Write-Host ""
    Write-Host "  [원인]" -ForegroundColor Yellow
    Write-Host "    HuggingFace 직접 접속 및 미러(hf-mirror.com) 모두 차단된 상태" -ForegroundColor Yellow
    Write-Host "    (기업 방화벽, 인터넷 미연결, 또는 DNS 차단 가능성)" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "  [해결 방법]" -ForegroundColor Cyan
    Write-Host "    1) 네트워크 연결 확인 후 INSTALL.ps1 재실행" -ForegroundColor Cyan
    Write-Host "    2) 인터넷 가능한 PC에서 모델 캐시를 복사:" -ForegroundColor Cyan
    Write-Host "         conda run -n $CondaEnv python -c `"from sentence_transformers import SentenceTransformer; SentenceTransformer('$EmbeddingModel')`"" -ForegroundColor White
    Write-Host "       복사 경로: %USERPROFILE%\.cache\huggingface\hub\" -ForegroundColor White
    Write-Host "       이 PC의 동일 경로에 붙여넣기 후 INSTALL.ps1 재실행" -ForegroundColor White
    Write-Host ""
    # 비대화형에서는 기본값(중단) — 임베딩 모델은 실제 의존성이므로 무인 설치도 여기서 멈추는 게 맞다.
    $cont = Read-HostOrDefault "  KG 시맨틱 기능 없이 설치를 계속하시겠습니까? (y=계속, Enter=중단)" ""
    if ($cont -ne "y" -and $cont -ne "Y") {
        Write-Err "설치를 중단합니다. 위 방법으로 해결 후 INSTALL.ps1 을 재실행하세요."
        exit 1
    }
    Write-Warn "KG 시맨틱 기능 없이 계속합니다. 나중에 모델 설치 후 INSTALL.ps1 을 재실행하면 활성화됩니다."
    Write-Host ""
}

# 4e. Ollama 모델
# Ollama를 명시적으로 선택한 사용자만 모델을 확인한다.
# STM/working-memory 요약은 Claude Code 단발 호출을 사용하므로 별도 Ollama 모델이 필요 없다.
$OllamaOverlayModel = if ($SelectedOllamaModel) { $SelectedOllamaModel.Trim() } else { "" }
$OllamaRequired = $SelectedCliProvider -in @("claude-code-ollama", "ollama")
$ollamaCmd = $OllamaCmdDetected
if (-not $OllamaRequired -and -not $OllamaOverlayModel) {
    Write-Step "Ollama model..."
    Write-Ok "현재 provider는 Ollama를 사용하지 않음 — 확인/다운로드 스킵"
} elseif (-not $ollamaCmd) {
    Write-Step "Ollama model..."
    Write-Warn "Ollama 미설치 — 선택한 provider를 사용할 수 없음."
    Write-Warn "  설치: https://ollama.ai"
    if ($OllamaOverlayModel) {
        Write-Warn "  설치 후 수동 실행: ollama pull $OllamaOverlayModel"
    }
} else {
    # overlay.yaml 에 지정된 provider 모델
    if ($OllamaOverlayModel) {
        Write-Step "Ollama model (overlay): $OllamaOverlayModel..."
        $ollamaList = & ollama list 2>&1
        $modelBase = $OllamaOverlayModel.Split(":")[0]
        if ($ollamaList -match [regex]::Escape($modelBase)) {
            Write-Ok "Already available: $OllamaOverlayModel"
        } else {
            Write-Host "  Pulling $OllamaOverlayModel (시간이 걸릴 수 있음)..." -ForegroundColor DarkGray
            try {
                & ollama pull $OllamaOverlayModel 2>&1 | ForEach-Object {
                    if ($_ -and $_.ToString().Trim()) { Write-Host "    $_" -ForegroundColor DarkGray }
                }
                $verifyList = & ollama list 2>&1
                if ($verifyList -match [regex]::Escape($modelBase)) {
                    Write-Ok "Pulled: $OllamaOverlayModel"
                } else {
                    Write-Warn "Pull 완료됐으나 목록에서 확인 불가 — 직접 확인: ollama list"
                }
            } catch {
                Write-Warn "ollama pull 실패: $_"
                Write-Warn "  Ollama 서버 실행 확인 후 수동으로: ollama pull $OllamaOverlayModel"
            }
        }
    }

}
