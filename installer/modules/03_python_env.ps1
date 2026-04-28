#
# 03_python_env.ps1 — CLI provider 확인 출력, Python 환경 bootstrap
#   conda env create/update 또는 venv fallback
#   갱신 변수: $PythonExe, $HasNamedCondaEnv, $McpSharedCommand, $McpSharedArgs
#

# 1. CLI providers — 탐지 결과 요약 출력
Write-Step "CLI providers..."
if ($CopilotCmdDetected) { Write-Ok "Copilot CLI: $(& copilot --version 2>&1 | Select-Object -First 1)" } else { Write-Warn "Copilot CLI not found (선택적 — 유료 구독 필요)" }

Write-Step "Optional CLI providers..."
if ($GeminiCmdDetected) { Write-Ok "Gemini CLI: $($GeminiCmdDetected.Source)" } else { Write-Warn "Gemini CLI not found — engram-gemini 사용 시 설치 필요" }
if ($ClaudeCliCmdDetected) { Write-Ok "Claude Code CLI: $($ClaudeCliCmdDetected.Source)" } else { Write-Warn "Claude Code CLI not found — engram-claude 사용 시 설치 필요" }
if ($OllamaCmdDetected) { Write-Ok "Ollama CLI: $($OllamaCmdDetected.Source)" } else { Write-Warn "Ollama CLI not found — provider=ollama 사용 시 설치 필요" }
if ($GooseCmdDetected)  { Write-Ok "Goose (MCP agent): $($GooseCmdDetected.Source)" } else { Write-Warn "Goose not found — provider=ollama는 MCP 없이 실행됨 (https://block.github.io/goose)" }
if ($WtCmdDetected) { Write-Ok "Windows Terminal: $($WtCmdDetected.Source)" } else { Write-Warn "Windows Terminal not found — 오버레이 터미널 UX가 제한될 수 있음" }

# 2. Python environment bootstrap
Write-Step "Python environment..."
if ($condaCmd) {
    $existingCondaPython = Resolve-CondaEnvPythonPath $CondaEnv
    if ($existingCondaPython) {
        $HasNamedCondaEnv = $true
        $PythonExe = $existingCondaPython
        if (Test-Path $EnvironmentYamlPath) {
            Write-Step "Conda env update (environment.yml)..."
            Push-Location $ProjectRoot
            $condaUpdateOutput = Invoke-LiveLog { conda env update -n $CondaEnv -f $EnvironmentYamlPath }
            $condaUpdateExit = $LASTEXITCODE
            Pop-Location
            if ($condaUpdateExit -ne 0) {
                Write-Err "Failed: conda env update -n $CondaEnv -f environment.yml"
                $condaUpdateOutput | Select-Object -Last 30 | ForEach-Object { Write-Host "      $_" -ForegroundColor DarkYellow }
                exit 1
            }
            Write-Ok "Updated env '$CondaEnv' from environment.yml"
        } else {
            Write-Warn "environment.yml not found — skip conda env update"
        }
    } else {
        if (Test-Path $EnvironmentYamlPath) {
            Write-Step "Conda env create (environment.yml)..."
            Push-Location $ProjectRoot
            $condaCreateOutput = Invoke-LiveLog { conda env create -n $CondaEnv -f $EnvironmentYamlPath }
            $condaCreateExit = $LASTEXITCODE
            Pop-Location
            if ($condaCreateExit -ne 0) {
                Write-Err "Failed: conda env create -n $CondaEnv -f environment.yml"
                $condaCreateOutput | Select-Object -Last 30 | ForEach-Object { Write-Host "      $_" -ForegroundColor DarkYellow }
                exit 1
            }
            Write-Ok "Created env '$CondaEnv' from environment.yml"
        } else {
            Write-Warn "environment.yml not found — creating '$CondaEnv' with python=3.11 + requirements.txt"
            $condaCreateOutput = Invoke-LiveLog { conda create -n $CondaEnv python=3.11 -y }
            if ($LASTEXITCODE -ne 0) {
                Write-Err "Failed: conda create -n $CondaEnv python=3.11 -y"
                $condaCreateOutput | Select-Object -Last 30 | ForEach-Object { Write-Host "      $_" -ForegroundColor DarkYellow }
                exit 1
            }
            if (-not (Test-Path $RequirementsPath)) {
                Write-Err "requirements.txt not found: $RequirementsPath"
                exit 1
            }
            $condaReqOutput = Invoke-LiveLog { conda run -n $CondaEnv python -m pip install -r $RequirementsPath }
            if ($LASTEXITCODE -ne 0) {
                Write-Err "Failed: conda run -n $CondaEnv python -m pip install -r requirements.txt"
                $condaReqOutput | Select-Object -Last 30 | ForEach-Object { Write-Host "      $_" -ForegroundColor DarkYellow }
                exit 1
            }
        }

        $resolvedCondaPython = Resolve-CondaEnvPythonPath $CondaEnv
        if (-not $resolvedCondaPython) {
            Write-Err "Could not resolve python.exe for conda env '$CondaEnv' after setup"
            exit 1
        }
        $HasNamedCondaEnv = $true
        $PythonExe = $resolvedCondaPython
    }
} else {
    Write-Warn "Conda not found — creating project venv from requirements.txt"
    if (-not (Test-Path $RequirementsPath)) {
        Write-Err "requirements.txt not found: $RequirementsPath"
        exit 1
    }

    $venvCreated = $false
    if (-not (Test-Path $ProjectVenvDir)) {
        if ($PythonExe -and (Test-Path $PythonExe)) {
            & $PythonExe -m venv $ProjectVenvDir 2>&1 | Out-Null
        } else {
            $pyLauncher = Get-Command py -ErrorAction SilentlyContinue
            if ($pyLauncher) {
                & py -3.11 -m venv $ProjectVenvDir 2>&1 | Out-Null
            } else {
                $pythonLauncher = Get-Command python -ErrorAction SilentlyContinue
                if ($pythonLauncher) {
                    & python -m venv $ProjectVenvDir 2>&1 | Out-Null
                } else {
                    Write-Err "Python launcher not found (python/py)."
                    Write-Warn "Python 3.11 설치 후 install.ps1을 다시 실행하세요."
                    exit 1
                }
            }
        }
        if (-not (Test-Path $ProjectVenvDir)) {
            Write-Err "Failed to create venv: $ProjectVenvDir"
            exit 1
        }
        $venvCreated = $true
    }

    $venvPython = Join-Path $ProjectVenvDir "Scripts\python.exe"
    if (-not (Test-Path $venvPython)) {
        Write-Err "venv python not found: $venvPython"
        exit 1
    }
    $PythonExe = $venvPython

    Invoke-LiveLog { & $PythonExe -m pip install --upgrade pip } | Out-Null
    $venvInstallOutput = Invoke-LiveLog { & $PythonExe -m pip install -r $RequirementsPath }
    if ($LASTEXITCODE -ne 0) {
        Write-Err "Failed: $PythonExe -m pip install -r requirements.txt"
        $venvInstallOutput | Select-Object -Last 30 | ForEach-Object { Write-Host "      $_" -ForegroundColor DarkYellow }
        exit 1
    }

    if ($venvCreated) {
        Write-Ok "Created venv and installed requirements: $ProjectVenvDir"
    } else {
        Write-Ok "Updated venv requirements: $ProjectVenvDir"
    }
}

if (-not $PythonExe -or -not (Test-Path $PythonExe)) {
    Write-Err "Python runtime resolution failed."
    exit 1
}

$pythonVersion = & $PythonExe -c "import sys; print(sys.version.split()[0])" 2>$null
if ($pythonVersion) {
    Write-Ok "$PythonExe (python $pythonVersion)"
} else {
    Write-Ok $PythonExe
}

if (-not $HasNamedCondaEnv) {
    Write-Warn "Using project venv interpreter: $PythonExe"
}

$VenvPythonPath = Join-Path $ProjectVenvDir "Scripts\python.exe"
if ($condaCmd -and $HasNamedCondaEnv) {
    $McpSharedCommand = "conda"
    $McpSharedArgs = @("run", "-n", $CondaEnv, "python", "mcp_server.py")
    Write-Ok "MCP 인터프리터(공유): conda run -n $CondaEnv python"
} elseif ($PythonExe -and (Test-Path $PythonExe) -and ($PythonExe -ieq $VenvPythonPath)) {
    $McpSharedCommand = ".\\.venv\\Scripts\\python.exe"
    $McpSharedArgs = @("mcp_server.py")
    Write-Ok "MCP 인터프리터(공유): .\\.venv\\Scripts\\python.exe"
} else {
    $McpSharedCommand = $McpInterpDefault
    $McpSharedArgs = @("mcp_server.py")
    Write-Warn "MCP 인터프리터(공유)는 기본값 'python'을 사용합니다."
}
