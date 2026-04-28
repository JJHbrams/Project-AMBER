#
# 08_env.ps1 — PATH 등록, 영구 환경변수, persona.user.yaml 템플릿, overlay.png 동기화
#

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

# 8c. overlay.png 동기화 (character.name → resource/overlay.png)
#     우선순위: ~/.engram/overlay.user.yaml > config/overlay.yaml
Write-Step "Syncing overlay.png from character config..."
$OverlayPngPath = Join-Path $ProjectRoot "resource\overlay.png"
$CharacterDir   = Join-Path $ProjectRoot "resource\character"
$UserOverlayYaml = Join-Path $env:USERPROFILE ".engram\overlay.user.yaml"
$ProjectOverlayYaml = Join-Path $ProjectRoot "config\overlay.yaml"
$syncedChar = $null
try {
    $resolveCharNameScript = @"
import yaml, sys

def get_char_name(path):
    try:
        cfg = yaml.safe_load(open(path, encoding='utf-8')) or {}
        return (cfg.get('overlay') or {}).get('character', {}).get('name', '')
    except Exception:
        return ''

user_yaml   = r'$($UserOverlayYaml -replace '\\', '/')'
project_yaml = r'$($ProjectOverlayYaml -replace '\\', '/')'

name = get_char_name(user_yaml) or get_char_name(project_yaml)
print(name.strip())
"@
    $charName = & $PythonExe -c $resolveCharNameScript 2>$null
    $charName = ($charName | Select-Object -Last 1).Trim()
    if ($charName) {
        $leafName = Split-Path $charName -Leaf
        $localBaseName = [System.IO.Path]::GetFileNameWithoutExtension($leafName)
        if (-not $localBaseName) { $localBaseName = $charName }

        $directPathCandidates = @(
            (Join-Path $CharacterDir "$($localBaseName)_00.png"),
            (Join-Path $CharacterDir "$($localBaseName)_0.png"),
            (Join-Path $CharacterDir "$($localBaseName).png"),
            (Join-Path $CharacterDir $leafName),
            (Join-Path $ProjectRoot $charName),
            $charName
        )
        foreach ($src in $directPathCandidates | Select-Object -Unique) {
            if ($src -and (Test-Path $src -PathType Leaf)) {
                Copy-Item $src $OverlayPngPath -Force
                $syncedChar = $src
                break
            }
        }

        if (-not $syncedChar) {
            $baseName = [System.IO.Path]::GetFileNameWithoutExtension($leafName)
            if (-not $baseName) { $baseName = $charName }
            $namedCandidates = @(
                (Join-Path $CharacterDir "$($baseName)_00.png"),
                (Join-Path $CharacterDir "$($baseName)_0.png"),
                (Join-Path $CharacterDir "$($baseName).png")
            )
            foreach ($src in $namedCandidates | Select-Object -Unique) {
                if (Test-Path $src -PathType Leaf) {
                    Copy-Item $src $OverlayPngPath -Force
                    $syncedChar = $src
                    break
                }
            }
        }
    }
} catch { }
if ($syncedChar) {
    $srcLabel = if (Test-Path $UserOverlayYaml) { "user" } else { "project" }
    Write-Ok "overlay.png ← $(Split-Path $syncedChar -Leaf)  (from $srcLabel yaml)"
} elseif ($charName) { Write-Warn "Character '$charName' not found in $CharacterDir — overlay.png unchanged" }
else { Write-Warn "No character name resolved — overlay.png unchanged" }
