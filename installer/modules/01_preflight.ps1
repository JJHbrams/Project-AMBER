#
# 01_preflight.ps1 — CLI 도구 탐지, 의존성 상태 출력, 필수 조건 검증
#

$CopilotCmdDetected = Get-Command copilot -ErrorAction SilentlyContinue
$GeminiCmdDetected = Get-Command gemini -ErrorAction SilentlyContinue
$ClaudeCliCmdDetected = Get-Command claude -ErrorAction SilentlyContinue
$OllamaCmdDetected = Get-Command ollama -ErrorAction SilentlyContinue
$GooseCmdDetected  = Get-Command goose -ErrorAction SilentlyContinue
if (-not $GooseCmdDetected -and (Test-Path $GooseExePath)) {
    $GooseCmdDetected = [PSCustomObject]@{ Source = $GooseExePath }
}
$WtCmdDetected = Get-Command wt -ErrorAction SilentlyContinue
$GitCmdDetected = Get-Command git -ErrorAction SilentlyContinue

Write-Host "  [체크] 의존성 상태" -ForegroundColor White
Write-DepStatus "Conda (Miniconda/Anaconda)" ($null -ne $condaCmd) $false "https://docs.conda.io/en/latest/miniconda.html"
Write-DepStatus "Python runtime" ($null -ne $PythonExe) $true "권장: conda create -n $CondaEnv python=3.11 -y"
Write-DepStatus "Copilot CLI" ($null -ne $CopilotCmdDetected) $false "https://docs.github.com/copilot/how-tos/copilot-cli (유료 구독 필요)"
Write-DepStatus "Gemini CLI" ($null -ne $GeminiCmdDetected) $false "https://ai.google.dev/gemini-api/docs/cli"
Write-DepStatus "Claude Code CLI" ($null -ne $ClaudeCliCmdDetected) $false "https://docs.anthropic.com"
Write-DepStatus "Ollama CLI" ($null -ne $OllamaCmdDetected) $false "https://ollama.ai"
Write-DepStatus "Goose (MCP agent)" ($null -ne $GooseCmdDetected) $false "https://block.github.io/goose — Ollama MCP 연동용"
Write-DepStatus "Windows Terminal (wt)" ($null -ne $WtCmdDetected) $false "Microsoft Store에서 Windows Terminal 설치"
Write-DepStatus "Git" ($null -ne $GitCmdDetected) $false "https://git-scm.com/download/win"
Write-Host ""

# ── 필수 의존성 조기 검증 ────────────────────────────────────
$AnyProviderAvailable = ($null -ne $CopilotCmdDetected) -or ($null -ne $GeminiCmdDetected) -or ($null -ne $ClaudeCliCmdDetected) -or ($null -ne $OllamaCmdDetected) -or ($null -ne $GooseCmdDetected)
if (-not $AnyProviderAvailable) {
    Write-Warn "CLI provider가 하나도 감지되지 않았습니다."
    Write-Host "      구독 없이 무료로 시작하려면 Gemini CLI를 권장합니다:" -ForegroundColor DarkGray
    Write-Host "        npm install -g @google/gemini-cli" -ForegroundColor DarkGray
    Write-Host "        gemini  (첫 실행 시 Google 계정으로 인증)" -ForegroundColor DarkGray
    Write-Host "        https://ai.google.dev/gemini-api/docs/cli" -ForegroundColor DarkGray
    Write-Host "      provider를 나중에 설치한 뒤 install.ps1을 재실행하거나 overlay 설정에서 변경하세요." -ForegroundColor DarkGray
    Write-Host ""
}
if (($null -eq $condaCmd) -and ($null -eq $PythonExe)) {
    Write-Err "Conda/Python 모두 감지되지 않았습니다. Miniconda 설치 후 터미널을 다시 열어주세요."
    Write-Host "      -> https://docs.conda.io/en/latest/miniconda.html" -ForegroundColor DarkGray
    exit 1
}
Write-Host ""
