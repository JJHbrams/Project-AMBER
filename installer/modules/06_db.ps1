#
# 06_db.ps1 — DB 초기화, Identity 이름 설정, Wiki vault 구조, Directives 시딩
#

$LegacyDbDir = if ($DbDir.StartsWith("D:")) { "D:\intel_" + ("con" + "tinuum") } else { "C:\intel_" + ("con" + "tinuum") }

# 6. DB init
Write-Step "Database..."
if ((Test-Path $LegacyDbDir) -and -not (Test-Path $DbDir)) {
    Move-Item -Path $LegacyDbDir -Destination $DbDir
    Write-Ok "Migrated legacy data directory: $LegacyDbDir -> $DbDir"
}
$env:ENGRAM_DB_DIR = $DbDir
$escapedRoot = $ProjectRoot -replace '\\', '\\\\'
$escapedDbDir = $DbDir -replace '\\', '\\\\'
$dbPath = & $PythonExe -c "import sys, os; sys.path.insert(0,'$escapedRoot'); os.environ['ENGRAM_DB_DIR']='$escapedDbDir'; from core.storage.db import initialize_db; print(initialize_db())" 2>&1
Write-Ok "DB: $dbPath"

# 6a. Identity name setup (interactive)
Write-Step "Identity name..."
$getIdentityNameScript = @"
import os, sys
sys.path.insert(0, r'$($ProjectRoot -replace '\\', '/')')
os.environ['ENGRAM_DB_DIR'] = r'$($DbDir -replace '\\', '/')'
from core.storage.db import initialize_db, get_connection
initialize_db()
conn = get_connection()
row = conn.execute("SELECT name FROM identity WHERE id=1").fetchone()
conn.close()
name = ""
if row:
    try:
        name = (row["name"] or "").strip()
    except Exception:
        name = ""
print(name)
"@
$currentIdentityName = Invoke-PythonScriptText -PythonPath $PythonExe -ScriptText $getIdentityNameScript
$identityLookupExitCode = $LASTEXITCODE
$currentIdentityNameValue = ""
if ($identityLookupExitCode -eq 0 -and $null -ne $currentIdentityName) {
    $lastIdentityName = $currentIdentityName | Select-Object -Last 1
    if ($null -ne $lastIdentityName) {
        $currentIdentityNameValue = ("$lastIdentityName").Trim()
    }
}

$isUnsetIdentityName = [string]::IsNullOrWhiteSpace($currentIdentityNameValue) -or $currentIdentityNameValue -eq "이름 없음"
if ($identityLookupExitCode -ne 0) {
    Write-Warn "Identity name 조회 실패 — install 단계에서는 스킵하고 첫 실행 시 설정합니다."
    $currentIdentityName | Select-Object -Last 3 | ForEach-Object { Write-Host "      $_" -ForegroundColor DarkYellow }
} elseif (-not $isUnsetIdentityName) {
    Write-Ok "Identity name already set: $currentIdentityNameValue (skip)"
} else {
    Write-Host ""
    Write-Host "  ┌─────────────────────────────────────────────────────────┐" -ForegroundColor Yellow
    Write-Host "  │  이름은 engram의 정체성 핵심입니다.                       │" -ForegroundColor Yellow
    Write-Host "  │                                                         │" -ForegroundColor Yellow
    Write-Host "  │  • 이름은 서사(narrative)와 기억에 깊이 연결됩니다.        │" -ForegroundColor Yellow
    Write-Host "  │  • 나중에 변경하면 기존 서사와 불일치가 생깁니다.          │" -ForegroundColor Yellow
    Write-Host "  │  • 한 번 정하면 돌이키기 어려우니 신중히 선택하세요.       │" -ForegroundColor Yellow
    Write-Host "  └─────────────────────────────────────────────────────────┘" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "  [설정] 이름/호칭 — engram이 자신을 부를 이름" -ForegroundColor White
    Write-Host "         현재값: (없음)" -ForegroundColor DarkGray
    $IdentityNameInput = Read-Host "  이름/호칭 (Enter = 나중에 설정)"
    $IdentityName = $IdentityNameInput.Trim()

    if (-not $IdentityName) {
        Write-Warn "Identity name 미설정 — 첫 실행 시 이름을 물어봅니다."
    } else {
        $env:ENGRAM_INSTALL_NAME = $IdentityName
        $setIdentityNameScript = @"
import os, sys
sys.path.insert(0, r'$($ProjectRoot -replace '\\', '/')')
os.environ['ENGRAM_DB_DIR'] = r'$($DbDir -replace '\\', '/')'
from core.storage.db import initialize_db, get_connection
initialize_db()
name = os.environ.get('ENGRAM_INSTALL_NAME', '').strip()
if name:
    conn = get_connection()
    with conn:
        conn.execute(
            "UPDATE identity SET name=?, updated_at=datetime('now','localtime') WHERE id=1",
            (name,)
        )
    conn.close()
print('updated')
"@
        $setIdentityNameResult = Invoke-PythonScriptText -PythonPath $PythonExe -ScriptText $setIdentityNameScript
        Remove-Item Env:ENGRAM_INSTALL_NAME -ErrorAction SilentlyContinue
        if ($setIdentityNameResult -like "*updated*") {
            Write-Ok "Identity name: $IdentityName"
        } else {
            Write-Warn "Identity name update failed: $setIdentityNameResult"
        }
    }
}

# 6b. LLM Wiki (Zettelkasten vault) 디렉토리 구조 + 스타터 파일
Write-Step "Wiki vault structure..."
$WikiDirs = @(
    "$DbDir\docs",
    "$DbDir\docs\_inbox",
    "$DbDir\docs\_templates",
    "$DbDir\docs\concepts",
    "$DbDir\docs\guides",
    "$DbDir\docs\moc",
    "$DbDir\docs\notes",
    "$DbDir\docs\people",
    "$DbDir\docs\projects",
    "$DbDir\docs\protocols",
    "$DbDir\docs\references",
    "$DbDir\docs\research",
    "$DbDir\docs\tools"
)
foreach ($d in $WikiDirs) {
    if (-not (Test-Path $d)) { New-Item -ItemType Directory -Path $d -Force | Out-Null; Write-Ok "Created: $d" }
    else { Write-Ok "Exists:  $d" }
}

# 스타터 파일 경로/템플릿 메타
$TemplateDate = Get-Date -Format "yyyy-MM-dd"
$InstallerTemplatesDir = Join-Path $ProjectRoot "installer\templates"
$WikiHome = "$DbDir\docs\moc\000-HOME.md"
$WikiGuide = "$DbDir\docs\guides\Wiki 관리 지침.md"
$TemplateTargets = @(
    @{ Src = "concept.md";  Dest = "$DbDir\docs\_templates\concept.md" },
    @{ Src = "project.md";  Dest = "$DbDir\docs\_templates\project.md" },
    @{ Src = "research.md"; Dest = "$DbDir\docs\_templates\research.md" },
    @{ Src = "person.md";   Dest = "$DbDir\docs\_templates\person.md" },
    # Protocol guides
    @{ Src = "protocols\_protocol-wiki-management-guide.md";   Dest = "$DbDir\docs\protocols\wiki-management-guide.md" },
    @{ Src = "protocols\_protocol-git-branch-guide.md";        Dest = "$DbDir\docs\protocols\git-branch-guide.md" },
    @{ Src = "protocols\_protocol-wiki-reminder-guide.md";     Dest = "$DbDir\docs\protocols\wiki-reminder-guide.md" },
    @{ Src = "protocols\_protocol-activity-log-guide.md";      Dest = "$DbDir\docs\protocols\activity-log-guide.md" },
    @{ Src = "protocols\_protocol-narrative-update-guide.md";  Dest = "$DbDir\docs\protocols\narrative-update-guide.md" },
    @{ Src = "protocols\_protocol-reflection-trigger-guide.md"; Dest = "$DbDir\docs\protocols\reflection-trigger-guide.md" },
    @{ Src = "protocols\_protocol-agent-collaboration-guide.md"; Dest = "$DbDir\docs\protocols\agent-collaboration-guide.md" }
)

# 충돌 파일 일괄 확인: 기본 정책은 유지(N), 명시적으로 y일 때만 덮어쓰기
$WikiStarterConflictPaths = [System.Collections.Generic.List[string]]::new()
if (Test-Path $WikiHome) { [void]$WikiStarterConflictPaths.Add($WikiHome) }
if (Test-Path $WikiGuide) { [void]$WikiStarterConflictPaths.Add($WikiGuide) }
foreach ($tmpl in $TemplateTargets) {
    if (Test-Path $tmpl.Dest) {
        [void]$WikiStarterConflictPaths.Add($tmpl.Dest)
    }
}

$OverwriteWikiStarters = $false
if ($WikiStarterConflictPaths.Count -gt 0) {
    Write-Step "Wiki starter conflict check..."
    Write-Host "  다음 파일이 이미 존재합니다:" -ForegroundColor White
    foreach ($existingPath in $WikiStarterConflictPaths) {
        Write-Host "    - $existingPath" -ForegroundColor DarkGray
    }

    $overwriteAnswer = (Read-Host "  겹치는 파일을 템플릿으로 덮어쓸까요? [y/N]").Trim().ToLower()
    if ($overwriteAnswer -in @("y", "yes")) {
        $OverwriteWikiStarters = $true
        Write-Ok "Conflict policy: overwrite starter files"
    } else {
        Write-Ok "Conflict policy: keep existing starter files"
    }
}

# 스타터 파일: HOME (moc)
$WikiIsNew = $false
$HomeExists = Test-Path $WikiHome
if ((-not $HomeExists) -or $OverwriteWikiStarters) {
    $WikiIsNew = -not $HomeExists
    $today = Get-Date -Format "yyyy-MM-dd"
    $homeSrc = Join-Path $ProjectRoot "installer\templates\_home.md"
    if (Test-Path $homeSrc) {
        $homeContent = (Get-Content $homeSrc -Raw -Encoding UTF8).Replace("__DATE__", $today)
        [System.IO.File]::WriteAllText($WikiHome, $homeContent, [System.Text.UTF8Encoding]::new($false))
    } else {
        Write-Warn "HOME template not found: $homeSrc — using inline fallback"
        @"
---
title: HOME
note_type: moc
tags:
  - home
  - index
created: $today
updated: $today
---

# LLM Wiki — HOME

이 vault는 engram 연속체의 지식 베이스입니다.
관리 지침: ``docs/guides/Wiki 관리 지침.md``
"@ | Out-File -FilePath $WikiHome -Encoding utf8 -NoNewline
    }

    if ($HomeExists) { Write-Ok "Overwritten: $WikiHome" }
    else { Write-Ok "Created: $WikiHome" }
} else { Write-Ok "Exists:  $WikiHome" }

# 스타터 파일: 템플릿들 (installer/templates/ 에서 읽어 날짜 치환 후 배포)
foreach ($tmpl in $TemplateTargets) {
    $dest = $tmpl.Dest
    $destExists = Test-Path $dest
    if ((-not $destExists) -or $OverwriteWikiStarters) {
        $srcPath = Join-Path $InstallerTemplatesDir $tmpl.Src
        if (Test-Path $srcPath) {
            $content = (Get-Content $srcPath -Raw -Encoding UTF8).Replace("__DATE__", $TemplateDate)
            [System.IO.File]::WriteAllText($dest, $content, [System.Text.UTF8Encoding]::new($false))
            if ($destExists) { Write-Ok "Overwritten: $dest" }
            else { Write-Ok "Created: $dest" }
        } else {
            Write-Warn "Template source not found: $srcPath — skipping $($tmpl.Src)"
        }
    } else { Write-Ok "Exists:  $dest" }
}

# 스타터 파일: Wiki 관리 지침 (guides/) - installer/templates/_wiki-guide.md에서 읽음
$GuideExists = Test-Path $WikiGuide
if ((-not $GuideExists) -or $OverwriteWikiStarters) {
    $today = Get-Date -Format "yyyy-MM-dd"
    $guideSrc = Join-Path $ProjectRoot "installer\templates\_wiki-guide.md"
    if (Test-Path $guideSrc) {
        $guideContent = (Get-Content $guideSrc -Raw -Encoding UTF8).Replace("__DATE__", $today)
    } else {
        Write-Warn "Wiki guide template not found: $guideSrc — creating minimal guide"
        $guideContent = "---`ntitle: Wiki 관리 지침`nnote_type: concept`ntags:`n  - guide`ncreated: $today`nupdated: $today`n---`n`n# Wiki 관리 지침`n"
    }
    [System.IO.File]::WriteAllText($WikiGuide, $guideContent, [System.Text.UTF8Encoding]::new($false))
    if ($GuideExists) { Write-Ok "Overwritten: $WikiGuide" }
    else { Write-Ok "Created: $WikiGuide" }
} else { Write-Ok "Exists:  $WikiGuide" }

# 6c. Directives 템플릿 시딩 (INSERT OR IGNORE — 기존 커스터마이징 보존)
Write-Step "Seeding default directives..."
$dirTemplPath = Join-Path $ProjectRoot "installer\templates\directives.json"
if (Test-Path $dirTemplPath) {
    $dirSeedScript = @"
import sys, os, json
sys.path.insert(0, r'$($ProjectRoot -replace '\\', '/')')
os.environ['ENGRAM_DB_DIR'] = r'$($DbDir -replace '\\', '/')'
from core.storage.db import initialize_db, get_connection
initialize_db()
template_path = r'$($dirTemplPath -replace '\\', '/')'
vault_dir = r'$($DbDir -replace '\\', '/')'
with open(template_path, encoding='utf-8') as f:
    directives = json.load(f)
conn = get_connection()
seeded = 0
with conn:
    for d in directives:
        content = d['content'].replace('__VAULT_DIR__', vault_dir)
        cursor = conn.execute(
            'INSERT OR IGNORE INTO directives (key, content, source, scope, priority, trigger_type) VALUES (?, ?, ?, ?, ?, ?)',
            (d['key'], content, 'install', d.get('scope', 'all'), d.get('priority', 0), d.get('trigger_type', 'always'))
        )
        if cursor.rowcount > 0:
            seeded += 1
conn.close()
print(f'directives seeded: {seeded}')
"@
    $seedResult = Invoke-PythonScriptText -PythonPath $PythonExe -ScriptText $dirSeedScript
    if ($seedResult -match "seeded: (\d+)") {
        Write-Ok "Directives seeded: $($Matches[1]) new (existing preserved)"
    } else {
        Write-Warn "Directive seeding: $seedResult"
    }
} else {
    Write-Warn "Directives template not found: $dirTemplPath"
}
