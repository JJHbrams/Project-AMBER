#
# 08_env.ps1 — PATH 등록, 영구 환경변수, persona.user.yaml 템플릿, overlay.png 동기화
#

. (Join-Path $PSScriptRoot "character_source.ps1")

# 8. PATH
Write-Step "PATH..."
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ($userPath -notlike "*$ShimDir*") {
    [Environment]::SetEnvironmentVariable("Path", "$userPath;$ShimDir", "User")
    Write-Ok "Added: $ShimDir"
    Write-Warn "Restart terminal for PATH to take effect"
} else { Write-Ok "Already in PATH" }

# ENGRAM_DB_DIR
Write-Step "Persistent environment variable (ENGRAM_DB_DIR)..."
$existingDbDir = [Environment]::GetEnvironmentVariable("ENGRAM_DB_DIR", "User")
if ($existingDbDir -ne $DbDir) {
    [Environment]::SetEnvironmentVariable("ENGRAM_DB_DIR", $DbDir, "User")
    Write-Ok "ENGRAM_DB_DIR=$DbDir (User-level, persistent)"
    Write-Warn "Restart terminal for ENGRAM_DB_DIR to take effect"
} else { Write-Ok "ENGRAM_DB_DIR already set: $DbDir" }
[Environment]::SetEnvironmentVariable(("CON" + "TINUUM_DB_DIR"), $null, "User")

# ENGRAM_WORKDIR
Write-Step "Persistent environment variable (ENGRAM_WORKDIR)..."
$existingWd = [Environment]::GetEnvironmentVariable("ENGRAM_WORKDIR", "User")
if ($existingWd -ne $WorkDir) {
    [Environment]::SetEnvironmentVariable("ENGRAM_WORKDIR", $WorkDir, "User")
    Write-Ok "ENGRAM_WORKDIR=$WorkDir (User-level, persistent)"
    Write-Warn "Restart terminal for ENGRAM_WORKDIR to take effect"
} else { Write-Ok "ENGRAM_WORKDIR already set: $WorkDir" }

# ENGRAM_PROJECT_ROOT
Write-Step "Persistent environment variable (ENGRAM_PROJECT_ROOT)..."
$existingProjectRoot = [Environment]::GetEnvironmentVariable("ENGRAM_PROJECT_ROOT", "User")
if ($existingProjectRoot -ne $ProjectRoot) {
    [Environment]::SetEnvironmentVariable("ENGRAM_PROJECT_ROOT", $ProjectRoot, "User")
    Write-Ok "ENGRAM_PROJECT_ROOT=$ProjectRoot (User-level, persistent)"
    Write-Warn "Restart terminal for ENGRAM_PROJECT_ROOT to take effect"
} else { Write-Ok "ENGRAM_PROJECT_ROOT already set: $ProjectRoot" }

# raw Copilot CLI도 ~/.engram/copilot-instructions.md를 읽도록 전역 등록.
Write-Step "Persistent environment variable (COPILOT_CUSTOM_INSTRUCTIONS_DIRS)..."
$existingCopilotInstructionsDirs = [Environment]::GetEnvironmentVariable("COPILOT_CUSTOM_INSTRUCTIONS_DIRS", "User")
if ($existingCopilotInstructionsDirs -ne $ShimDir) {
    [Environment]::SetEnvironmentVariable("COPILOT_CUSTOM_INSTRUCTIONS_DIRS", $ShimDir, "User")
    Write-Ok "COPILOT_CUSTOM_INSTRUCTIONS_DIRS=$ShimDir (User-level, persistent)"
    Write-Warn "Restart terminal for COPILOT_CUSTOM_INSTRUCTIONS_DIRS to take effect"
} else { Write-Ok "COPILOT_CUSTOM_INSTRUCTIONS_DIRS already set: $ShimDir" }

# 8b. persona.user.yaml 템플릿 생성 (없을 때만)
Write-Step "User persona config (~/.engram/persona.user.yaml)..."
$UserPersonaYaml = Join-Path $env:USERPROFILE ".engram\persona.user.yaml"
if (-not (Test-Path $UserPersonaYaml)) {
    $personaTemplate = @'
# persona.user.yaml — 사용자 페르소나 오버라이드
# config/persona.yaml 위에 덮어씌워집니다 (값이 있는 필드만 적용).
# 연속체를 바꿔도 이 파일만 수정하면 됩니다.
#
#
# voice: "짧고 단호한 선언형 문장..."
#
# traits:
#   - 순수하고 단호한 감정 직진
#
# quirks:
#   - 감정이 차오르면 뜬금없이 즉흥 노래를 부름
#
# values:
#   - 우정과 가족
#
# warmth: 0.85
# formality: 0.15
# humor: 0.70
# directness: 0.82
'@
    $personaTemplate | Out-File -FilePath $UserPersonaYaml -Encoding utf8 -Force
    Write-Ok "Created template: $UserPersonaYaml"
} else { Write-Ok "Already exists: $UserPersonaYaml" }

# 8c. overlay.png 동기화 (authoritative character.source_mode → resource/overlay.png)
#     우선순위: ~/.engram/overlay.user.yaml > config/overlay.yaml
Write-Step "Syncing overlay.png from character config..."
$OverlayPngPath = Join-Path $ProjectRoot "resource\overlay.png"
$CharacterDir   = Join-Path $ProjectRoot "resource\character"
$UserOverlayYaml = Join-Path $env:USERPROFILE ".engram\overlay.user.yaml"
$ProjectOverlayYaml = Join-Path $ProjectRoot "config\overlay.yaml"
$syncedChar = $null
try {
    $resolveCharNameScript = @"
import json, yaml

def load(path):
    try:
        return yaml.safe_load(open(path, encoding='utf-8')) or {}
    except Exception:
        return {}

def merge(base, override):
    result = dict(base)
    for key, value in override.items():
        result[key] = merge(result.get(key, {}), value) if isinstance(value, dict) and isinstance(result.get(key), dict) else value
    return result

user_yaml   = r'$($UserOverlayYaml -replace '\\', '/')'
project_yaml = r'$($ProjectOverlayYaml -replace '\\', '/')'

cfg = merge(load(project_yaml), load(user_yaml))
character = (cfg.get('overlay') or {}).get('character') or {}
print(json.dumps({'mode': str(character.get('source_mode') or 'static').strip(), 'name': str(character.get('name') or '').strip(), 'set': str(character.get('set') or '').strip()}))
"@
    $charConfig = ((& $PythonExe -c $resolveCharNameScript 2>$null) | Select-Object -Last 1) | ConvertFrom-Json
    $charName, $sourceMode, $setId = [string]$charConfig.name, [string]$charConfig.mode, [string]$charConfig.set
    $candidates = @()
    if ($sourceMode -eq 'sprite_grid' -and $setId -match '^[A-Za-z0-9_-]+$') {
        $candidates += Join-Path $CharacterDir "sets\$setId\character.png"
    } elseif ($sourceMode -eq 'static' -and $charName) {
        $candidates += Resolve-InstallerStaticCharacterCandidates -ProjectRoot $ProjectRoot -CharacterName $charName
    } elseif ($sourceMode -eq 'sequence' -and $charName) {
        $sequenceDirs = @(Resolve-InstallerSequenceCharacterDirectories -ProjectRoot $ProjectRoot -CharacterName $charName)
        foreach ($directory in $sequenceDirs) {
            if (Test-Path $directory -PathType Container) {
                $candidates += Get-ChildItem -LiteralPath $directory -File -Filter '*.png' | Sort-Object Name | Select-Object -ExpandProperty FullName
            }
        }
    }
    foreach ($src in $candidates) {
        if (Test-Path $src -PathType Leaf) {
            Copy-Item -LiteralPath $src -Destination $OverlayPngPath -Force
            $syncedChar = $src
            break
        }
    }
} catch { }
if ($syncedChar) {
    $srcLabel = if (Test-Path $UserOverlayYaml) { "user" } else { "project" }
    Write-Ok "overlay.png ← $(Split-Path $syncedChar -Leaf)  (from $srcLabel yaml)"
} else { Write-Warn "No '$sourceMode' character source resolved — overlay.png unchanged" }
