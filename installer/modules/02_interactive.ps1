#
# 02_interactive.ps1 — 사용자 대화형 설정 수집
#   설정 항목: DB 경로, 작업 디렉토리, CLI provider, Ollama 모델, 자동시작
#   출력 변수: $DbDir, $WorkDir, $McpSharedCommand, $McpSharedArgs,
#              $SelectedCliProvider, $SelectedOllamaModel, $DefaultCliProvider,
#              $EnableAutoStart, $ProviderAvailability
#

# ── 기존 설정값 로드 ────────────────────────────────────────
$ExistingDbDir   = ""
$ExistingWorkDir = ""
$ExistingCliProvider = ""
if ((Test-Path $UserConfigPath) -and $PythonExe) {
    $existingVals = & $PythonExe -c "import yaml; d=yaml.safe_load(open(r'$($UserConfigPath -replace '\\','/')',encoding='utf-8')) or {}; db=(d.get('db') or {}).get('root_dir',''); wd=d.get('workdir',''); print(db+'|'+wd)" 2>$null
    if ($existingVals -and $existingVals -like "*|*") {
        $parts = ($existingVals.Trim() -split '\|', 2)
        if ($parts.Count -ge 1 -and $parts[0].Trim()) { $ExistingDbDir   = $parts[0].Trim() }
        if ($parts.Count -ge 2 -and $parts[1].Trim()) { $ExistingWorkDir = $parts[1].Trim() }
    }
}
$ExistingOllamaModel = ""
if ((Test-Path $OverlayUserConfigPath) -and $PythonExe) {
    $existingOverlayVals = & $PythonExe -c "import yaml; d=yaml.safe_load(open(r'$($OverlayUserConfigPath -replace '\\','/')',encoding='utf-8')) or {}; cli=d.get('cli') if isinstance(d,dict) else {}; cli=cli if isinstance(cli,dict) else {}; print(cli.get('provider','')+'|'+cli.get('ollama_model',''))" 2>$null
    if ($existingOverlayVals -and $existingOverlayVals -like "*|*") {
        $ovParts = $existingOverlayVals.Trim() -split '\|', 2
        if ($ovParts[0].Trim()) { $ExistingCliProvider  = $ovParts[0].Trim() }
        if ($ovParts.Count -ge 2 -and $ovParts[1].Trim()) { $ExistingOllamaModel = $ovParts[1].Trim() }
    }
}

# ── DB 경로 ────────────────────────────────────────────────
$DbDefault = if ($ExistingDbDir) { $ExistingDbDir } else { $DefaultDbDir }
Write-Host "  [설정] DB 경로 — engram 데이터 저장 위치" -ForegroundColor White
Write-Host "         기본값: $DbDefault" -ForegroundColor DarkGray
$DbInput = Read-Host "  DB 경로 (Enter = 기본값)"
$DbDir = if ($DbInput.Trim()) { $DbInput.Trim() } else { $DbDefault }

# ── 작업 디렉토리 ─────────────────────────────────────────
$WdDefault = $ProjectRoot
Write-Host ""
Write-Host "  [설정] 작업 디렉토리 — engram 실행 시 자동 이동할 경로" -ForegroundColor White
Write-Host "         기본값: $WdDefault" -ForegroundColor DarkGray
$WdInput = Read-Host "  작업 디렉토리 (Enter = 기본값)"
$WorkDir = if ($WdInput.Trim()) { $WdInput.Trim() } else { $WdDefault }

# ── MCP 인터프리터 (안내) ──────────────────────────────────
Write-Host ""
$McpInterpDefault = "python"
$McpSharedCommand = $McpInterpDefault
$McpSharedArgs = @("mcp_server.py")
Write-Host "  [설정] MCP 인터프리터 명령 — 팀 공유용(.mcp.json/.vscode/mcp.json)" -ForegroundColor White
Write-Host "         기본값: $McpInterpDefault" -ForegroundColor DarkGray
Write-Host "         Python 환경 구성 결과에 따라 자동으로 설정됩니다." -ForegroundColor DarkGray

# ── CLI provider 선택 ─────────────────────────────────────
$ProviderAvailability = @{
    "copilot" = ($null -ne $CopilotCmdDetected)
    "gemini" = ($null -ne $GeminiCmdDetected)
    "claude-code" = ($null -ne $ClaudeCliCmdDetected)
    "claude-code-ollama" = (($null -ne $ClaudeCliCmdDetected) -and ($null -ne $OllamaCmdDetected))
    "ollama" = ($null -ne $OllamaCmdDetected)
}
$CliProviderDefault = Resolve-AvailableCliProvider $ExistingCliProvider $ProviderAvailability
Write-Host ""
Write-Host "  [설정] 기본 CLI 서비스 — 오버레이에서 기본으로 사용할 provider" -ForegroundColor White
$_providerItems  = @("copilot", "gemini", "claude-code", "claude-code(ollama)", "ollama")
$_providerBadges = @(
    $(if ($ProviderAvailability['copilot'])     { 'installed' } else { 'missing' }),
    $(if ($ProviderAvailability['gemini'])      { 'installed' } else { 'missing' }),
    $(if ($ProviderAvailability['claude-code']) { 'installed' } else { 'missing' }),
    $(if ($ProviderAvailability['claude-code-ollama']) { 'installed' } else { 'missing' }),
    $(if ($ProviderAvailability['ollama'])      { 'installed' } else { 'missing' })
)
$CliProviderDefaultDisplay = if ($CliProviderDefault -eq "claude-code-ollama") { "claude-code(ollama)" } else { $CliProviderDefault }
$_defaultIdx = [math]::Max(0, [array]::IndexOf($_providerItems, $CliProviderDefaultDisplay))
$SelectedCliProviderRaw = Select-WithArrowKeys `
    -Items $_providerItems `
    -Badges $_providerBadges `
    -DefaultIndex $_defaultIdx `
    -Prompt "기본 CLI provider"
$SelectedCliProvider = Normalize-CliProvider $SelectedCliProviderRaw

# ── Provider별 추가 설정 ───────────────────────────────────
$SelectedOllamaModel = ""

if ($SelectedCliProvider -eq "claude-code" -and $OllamaCmdDetected) {
    Write-Host ""
    Write-Host "  [설정] claude-code 백엔드" -ForegroundColor White
    $backendChoice = Select-WithArrowKeys `
        -Items @("claude (직접)", "ollama (로컬 라우팅)") `
        -DefaultIndex $(if ($ExistingOllamaModel) { 1 } else { 0 }) `
        -Prompt "백엔드 선택"
    if ($backendChoice -eq "ollama (로컬 라우팅)") {
        $modelInfos = Get-OllamaModelInfoList
        if ($modelInfos.Count -gt 0) {
            $modelNames  = @($modelInfos | ForEach-Object { $_.Name })
            $modelBadges = @($modelInfos | ForEach-Object { Format-OllamaModelBadge $_ })
            Write-Host ""
            Write-Host "  [설정] Ollama 모델 (claude-code 라우팅용)" -ForegroundColor White
            $_defaultModelIdx = [math]::Max(0, [array]::IndexOf($modelNames, $ExistingOllamaModel))
            $SelectedOllamaModel = Select-WithArrowKeys `
                -Items $modelNames `
                -Badges $modelBadges `
                -DefaultIndex $_defaultModelIdx `
                -Prompt "모델 선택"
        } else {
            Write-Warn "설치된 ollama 모델이 없습니다. 'ollama pull <model>' 후 재실행하세요."
        }
    }
}

if ($SelectedCliProvider -eq "claude-code-ollama") {
    if ($OllamaCmdDetected) {
        $modelInfos = Get-OllamaModelInfoList
        if ($modelInfos.Count -gt 0) {
            $modelNames  = @($modelInfos | ForEach-Object { $_.Name })
            $modelBadges = @($modelInfos | ForEach-Object { Format-OllamaModelBadge $_ })
            Write-Host ""
            Write-Host "  [설정] Ollama 모델 (claude-code(ollama) 백엔드)" -ForegroundColor White
            $_defaultModelIdx = [math]::Max(0, [array]::IndexOf($modelNames, $ExistingOllamaModel))
            $SelectedOllamaModel = Select-WithArrowKeys `
                -Items $modelNames `
                -Badges $modelBadges `
                -DefaultIndex $_defaultModelIdx `
                -Prompt "모델 선택"
        } else {
            Write-Warn "설치된 ollama 모델이 없습니다. 'ollama pull <model>' 후 재실행하세요."
        }
    } else {
        Write-Warn "Ollama가 설치되지 않아 claude-code(ollama)용 모델을 선택할 수 없습니다."
    }
}

if ($SelectedCliProvider -eq "ollama") {
    if ($OllamaCmdDetected) {
        $modelInfos = Get-OllamaModelInfoList
        if ($modelInfos.Count -gt 0) {
            $modelNames  = @($modelInfos | ForEach-Object { $_.Name })
            $modelBadges = @($modelInfos | ForEach-Object { Format-OllamaModelBadge $_ })
            Write-Host ""
            Write-Host "  [설정] Ollama 모델" -ForegroundColor White
            $_defaultModelIdx = [math]::Max(0, [array]::IndexOf($modelNames, $ExistingOllamaModel))
            $SelectedOllamaModel = Select-WithArrowKeys `
                -Items $modelNames `
                -Badges $modelBadges `
                -DefaultIndex $_defaultModelIdx `
                -Prompt "모델 선택"
        } else {
            Write-Warn "설치된 ollama 모델이 없습니다. 'ollama pull <model>' 후 재실행하세요."
        }
    } else {
        Write-Warn "Ollama가 설치되지 않아 모델 목록을 가져올 수 없습니다."
    }
}

$DefaultCliProvider = Resolve-AvailableCliProvider $SelectedCliProvider $ProviderAvailability
if ($DefaultCliProvider -ne $SelectedCliProvider) {
    Write-Warn "선택한 provider '$SelectedCliProvider'를 실행할 수 없어 '$DefaultCliProvider'로 대체합니다."
}
$_providerSummary = $DefaultCliProvider
if ($SelectedOllamaModel) { $_providerSummary += " (ollama: $SelectedOllamaModel)" }
Write-Ok "기본 CLI provider: $_providerSummary"

# ── 자동시작 설정 ─────────────────────────────────────────
$_existingStartupLink = Join-Path ([Environment]::GetFolderPath("Startup")) "engram-overlay.lnk"
$_autoStartDefault = if (Test-Path $_existingStartupLink) { 0 } else { 1 }
Write-Host ""
Write-Host "  [설정] Windows 시작 시 자동실행 — 재부팅 후 overlay가 자동으로 켜집니다" -ForegroundColor White
$_autoStartChoice = Select-WithArrowKeys `
    -Items @("예 — 시작 시 자동실행 등록", "아니오 — 수동 실행만") `
    -DefaultIndex $_autoStartDefault `
    -Prompt "자동시작"
$EnableAutoStart = $_autoStartChoice -like "예*"
Write-Ok "자동시작: $(if ($EnableAutoStart) { '활성화' } else { '비활성화' })"

Write-Host ""
