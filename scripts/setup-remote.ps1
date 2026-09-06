#Requires -Version 5
<#
.SYNOPSIS
  원격 서버에 engram MCP 를 등록한다. 토큰 전송 → 등록 → 검증까지 한 번에.

.DESCRIPTION
  engram 이 도는 로컬(Windows)에서 실행한다.

    1) 로컬 점검   — 원격 리스너(:remote_port) 가 떠 있는지, 토큰이 있는지
    2) 원격 점검   — SSH 접속, OS, 터널이 실제로 뚫렸는지(/health)
    3) 등록        — 원격 ~/.claude.json 에 engram MCP 항목 기록
    4) 검증        — 원격에서 tools/list 호출 → 로컬 감사 로그에 찍히는지 확인

  토큰 값은 화면·로그·argv 어디에도 노출되지 않는다. ssh stdin 으로만 전달한다.
  scope/deny 는 서버(로컬) 정책이라 원격으로 가지 않는다 — 원격은 불투명한
  문자열 하나만 갖는다.

  원격 OS 는 Linux / macOS / Windows 를 모두 지원한다. 등록 내용은 셋 다 동일하고
  (~/.claude.json 에 같은 JSON), 다른 것은 파이썬 탐색·터널 확인·인용 방식뿐이다.
  판별은 POSIX 프로브 → 실패 시 %OS% 순으로 하며, 지역화된 에러 문구는 쓰지 않는다.

.EXAMPLE
  .\scripts\setup-remote.ps1 -ListTokens
  .\scripts\setup-remote.ps1 -Target my-server
  .\scripts\setup-remote.ps1 -Target my-server -TokenName remote-dgx
#>
param(
    [string]$Target = "",
    [string]$TokenName = "",
    [int]$RemotePort = 0,
    [switch]$ListTokens,
    [switch]$ListHosts,
    [switch]$SkipTunnelCheck,
    # 비밀번호 프롬프트를 띄우지 않는다. TTY 가 없는 호출(자동화·GUI 캡처)에서
    # 프롬프트에 걸려 무한 대기하는 것을 막는다. 키 인증이 안 되어 있으면 즉시 실패.
    [switch]$BatchMode,
    # skill / SessionStart hook 배치를 건너뛴다. MCP 등록만 하고 싶을 때 쓴다.
    [switch]$SkipProvision,
    # 원격 ~/.claude.json 을 절대 건드리지 않는다(읽기만 — 터널 실측은 그대로).
    # 배치와는 독립이다. 이미 등록된 호스트에 skill/hook 만 놓을 때 쓴다.
    [switch]$SkipRegister,
    [string]$Python = "",
    # 원격 파이썬 경로를 직접 지정한다. 비대화형 SSH 는 ~/.bashrc 를 읽지 않으므로
    # conda 로 설치한 파이썬은 PATH 에 안 잡힌다. 자동 탐색이 실패하면 이걸 쓴다.
    #   예: -RemotePython /opt/conda/bin/python
    [string]$RemotePython = "",
    [string]$ResultLog = "",
    [string]$ProofNonce = ""
)

$ErrorActionPreference = "Stop"
$Root = Split-Path $PSScriptRoot -Parent

# 콘솔 인코딩을 UTF-8 로 맞춘다. 안 하면 이 스크립트의 한글 출력이 깨진다.
# (원격이 Windows 라 CP949 로 응답하는 경우는 아예 파싱하지 않는다 — OS 판별은
#  로케일 무관한 %OS% 로 하고, 지역화된 에러 문구는 판정에 쓰지 않는다.)
#
# ⚠️ 반드시 BOM 없는 UTF8Encoding 을 쓴다. [Text.Encoding]::UTF8 은 BOM 을 emit 하므로
#    $OutputEncoding 에 넣으면 네이티브 명령으로 파이프되는 첫 바이트에 U+FEFF 가 붙는다.
#    토큰이 그렇게 오염되면 원격에서 "'latin-1' codec can't encode character '﻿'" 로 터진다.
$Utf8NoBom = [Text.UTF8Encoding]::new($false)
try {
    [Console]::OutputEncoding = $Utf8NoBom
    $OutputEncoding = $Utf8NoBom
} catch {}

function Write-ResultLog($m) {
    if (-not $ResultLog) { return }
    $safe = ([string]$m) -replace '(?i)Bearer\s+[^\s"'']+', 'Bearer [REDACTED]'
    try { Add-Content -LiteralPath $ResultLog -Value $safe -Encoding utf8 } catch {}
}
function Step($m) { Write-Host "`n==> $m" -ForegroundColor Cyan; Write-ResultLog "==> $m" }
function Ok($m)   { Write-Host "  [OK] $m" -ForegroundColor Green; Write-ResultLog "[OK] $m" }
function Warn($m) { Write-Host "  [!] $m"  -ForegroundColor Yellow; Write-ResultLog "[!] $m" }
function Die($m)  { Write-Host "  [X] $m"  -ForegroundColor Red; Write-ResultLog "[X] $m"; exit 1 }
if (-not $ProofNonce) { $ProofNonce = [guid]::NewGuid().ToString("N") }
if ($ProofNonce -notmatch '^[A-Za-z0-9_-]{16,64}$') { Die "invalid provisioning proof nonce" }

# ── python 찾기 (yaml 파싱용) ────────────────────────────────────────────────
if (-not $Python) {
    foreach ($c in @(
        "$env:USERPROFILE\miniconda3\envs\intel_engram\python.exe",
        "$env:USERPROFILE\anaconda3\envs\intel_engram\python.exe"
    )) { if (Test-Path $c) { $Python = $c; break } }
}
if (-not $Python) { $Python = (Get-Command python -ErrorAction SilentlyContinue).Source }
if (-not $Python) { Die "python 을 찾을 수 없다. -Python 으로 지정할 것." }

# ── ssh_config 의 Host 별칭 ──────────────────────────────────────────────────
function Get-SshHostAliases {
    $cfg = Join-Path $env:USERPROFILE ".ssh\config"
    if (-not (Test-Path $cfg)) { return @() }
    $out = @()
    foreach ($line in Get-Content $cfg) {
        if ($line -match '^\s*Host\s+(.+?)\s*$') {
            foreach ($h in ($Matches[1] -split '\s+')) {
                if ($h -and $h -notmatch '[*?]') { $out += $h }
            }
        }
    }
    return $out
}
$sshHosts = Get-SshHostAliases

if ($ListHosts) {
    Write-Host "`n~/.ssh/config 의 Host 별칭" -ForegroundColor Cyan
    foreach ($h in $sshHosts) {
        $note = if ($h -match '[()\s]') { "   ← 괄호 포함: 반드시 따옴표로 감쌀 것  -Target `"$h`"" } else { "" }
        Write-Host "  $h$note"
    }
    Write-Host ""
    exit 0
}

$TokensPath = Join-Path $env:USERPROFILE ".engram\mcp-tokens.yaml"
if (-not (Test-Path $TokensPath)) {
    Die "토큰 파일이 없다: $TokensPath`n      원격 리스너를 한 번 켜면 자동 생성된다 (overlay.user.yaml 의 mcp.remote_enabled: true)."
}

# ── 토큰 목록 (값은 절대 출력하지 않음) ──────────────────────────────────────
$listPy = @'
import yaml, pathlib, sys
d = yaml.safe_load(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")) or {}
for t in d.get("tokens") or []:
    print("{}\t{}".format(t.get("name", "?"), t.get("scope") or "(미지정 - global 폴백)"))
'@
$rows = & $Python -c $listPy $TokensPath
if ($LASTEXITCODE -ne 0) { Die "토큰 파일 파싱 실패" }

if ($ListTokens -or -not $Target) {
    Write-Host "`n등록된 토큰 (값은 표시하지 않음)" -ForegroundColor Cyan
    Write-Host ("  {0,-20} {1}" -f "NAME", "SCOPE")
    foreach ($r in $rows) { $p = $r -split "`t"; Write-Host ("  {0,-20} {1}" -f $p[0], $p[1]) }
    if (-not $Target) { Write-Host "`n사용: .\scripts\setup-remote.ps1 -Target <ssh별칭> [-TokenName <name>]`n" }
    exit 0
}

# ── 토큰 선택 ────────────────────────────────────────────────────────────────
$names = @($rows | ForEach-Object { ($_ -split "`t")[0] })
if (-not $TokenName) {
    if ($names.Count -eq 1) { $TokenName = $names[0] }
    else { Die "토큰이 여러 개다. -TokenName 으로 고를 것: $($names -join ', ')" }
}
if ($names -notcontains $TokenName) { Die "그런 이름의 토큰이 없다: $TokenName (있는 것: $($names -join ', '))" }
$scope = (($rows | Where-Object { $_ -like "$TokenName`t*" }) -split "`t")[1]

# Capture an append-only audit marker *before* the eventual tools/list call.
# A merely recent unrelated entry is not proof that this selected principal
# reached MCP through this setup invocation.
$auditPath = Join-Path $env:USERPROFILE ".engram\logs\remote-audit.jsonl"
$auditBeforeLines = if (Test-Path $auditPath) { @(Get-Content $auditPath).Count } else { 0 }

# ── 원격 포트 결정 ───────────────────────────────────────────────────────────
if ($RemotePort -le 0) {
    $portPy = @'
import yaml, pathlib, sys
port = 0
for p in sys.argv[1:]:
    f = pathlib.Path(p)
    if not f.exists():
        continue
    d = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
    v = ((d.get("mcp") or {}).get("remote_port"))
    if v:
        port = int(v)
print(port or 17386)
'@
    $RemotePort = [int](& $Python -c $portPy `
        (Join-Path $Root "config\overlay.yaml") `
        (Join-Path $env:USERPROFILE ".engram\overlay.user.yaml"))
}

Step "로컬 점검"
Ok "토큰: $TokenName  (scope=$scope)"
Ok "원격 리스너 포트: $RemotePort"

$listening = @(Get-NetTCPConnection -State Listen -LocalPort $RemotePort -ErrorAction SilentlyContinue)
if ($listening.Count -eq 0) {
    Die "로컬 :$RemotePort 리스너가 없다.`n      ~/.engram/overlay.user.yaml 의 mcp.remote_enabled: true 확인 후 overlay 재시작."
}
Ok "로컬 :$RemotePort LISTENING"

Step "원격 점검 — $Target"

# PowerShell 은 따옴표 없는 -Target my-host(dev) 에서 (dev) 를 서브식으로 먹고
# 'my-host' 만 넘긴다. 조용히 잘리므로 ssh_config 과 대조해 잡아낸다.
if ($sshHosts.Count -gt 0 -and $sshHosts -notcontains $Target) {
    $near = @($sshHosts | Where-Object { $_ -like "$Target*" -or $_ -like "*$Target*" })
    if ($near.Count -gt 0) {
        Die @"
'$Target' 은 ~/.ssh/config 에 없다. 혹시 이건가: $($near -join ', ')
      괄호·공백이 있는 별칭은 PowerShell 이 잘라먹는다. 반드시 따옴표로 감쌀 것:
        .\scripts\setup-remote.ps1 -Target "$($near[0])"
"@
    }
    Warn "'$Target' 이 ~/.ssh/config 에 없다 — 직접 host 문자열로 접속 시도한다. (목록: -ListHosts)"
}

# ── 원격 점검: ssh 호출 1회로 OS·파이썬·터널을 한꺼번에 본다 ────────────────
# ssh 를 여러 번 부르면 키 인증이 없을 때 비밀번호를 그 횟수만큼 물어본다.
# (Windows OpenSSH 는 ControlMaster 다중화를 지원하지 않아 연결 재사용도 안 된다.)
# 센티널은 cmd.exe 가 흉내낼 수 없는 것이어야 한다. 'echo ENGRAM_PROBE=1' 은 cmd 에도
# echo 가 있어 그대로 출력되므로 POSIX 판별에 쓸 수 없다(실제로 Windows 원격을
# POSIX 로 오인했다). 명령 치환 결과를 실어 보내 cmd 에서는 리터럴로 남게 한다.
$probeSh = @"
echo ENGRAM_PROBE=`$(uname -s 2>/dev/null)
echo OS=`$(uname -s 2>/dev/null)
PY=
for c in python3 python; do
  p=`$(command -v "`$c" 2>/dev/null) && { PY=`$p; break; }
done
if [ -z "`$PY" ]; then
  for c in "`$CONDA_PREFIX/bin/python" "`$HOME/miniconda3/bin/python" "`$HOME/anaconda3/bin/python" \
           "`$HOME/miniforge3/bin/python" /opt/conda/bin/python /usr/local/bin/python3; do
    [ -x "`$c" ] && { PY=`$c; break; }
  done
fi
if [ -z "`$PY" ]; then
  for c in "`$HOME"/*conda*/bin/python /opt/*conda*/bin/python; do
    [ -x "`$c" ] && { PY=`$c; break; }
  done
fi
echo PY=`$PY
echo CAND=`$(ls -d "`$HOME"/*conda*/bin/python /opt/*conda*/bin/python 2>/dev/null | head -3 | tr '\n' ' ')
if [ -n "`$PY" ]; then
  echo HEALTH=`$("`$PY" -c "import urllib.request as u;print(u.urlopen('http://127.0.0.1:$RemotePort/health',timeout=5).read().decode())" 2>/dev/null)
else
  echo HEALTH=`$(curl -s -m 5 http://127.0.0.1:$RemotePort/health 2>/dev/null)
fi
"@

# POSIX 셸이 실제로 돌았는지 = uname 결과가 센티널에 실려 왔는지로 판정한다.
$posixRe = '(?m)^ENGRAM_PROBE=(Linux|Darwin|FreeBSD)'

# 프로브는 명령줄이 아니라 stdin 으로 POSIX 셸에 먹인다.
#
# 명령줄로 넘기면 원격 *로그인 셸* 이 우리 스크립트를 파싱한다. 로그인 셸이 zsh 면
# sh 스크립트가 zsh 문법으로 해석되고, 실제로 24행(마지막 fi)에서 parse error 로 죽었다.
# 게다가 이 .ps1 은 CRLF 라 here-string 의 줄바꿈도 CRLF 다 — 원격에는 then/fi 뒤에
# CR 이 붙어 도착해 if 블록이 닫히지 않는다. 둘이 겹쳐 POSIX 원격을 판별 실패로 만들었다.
#
# 'sh -s' 는 stdin 에서 스크립트를 읽는다. 로그인 셸은 그 다섯 글자만 파싱하므로
# 셸 방언(zsh/fish/csh)에 영향받지 않는다. CR 은 여기서 벗겨 LF 로만 보낸다.
#
# 주의: 이 주석에 백틱을 쓰지 말 것. 줄 끝 백틱은 PowerShell 의 줄 연속 문자라
# 다음 줄이 코드로 이어붙는다 — 실제로 그렇게 깨졌다.
# 끝에 'exit 0' 을 덧붙이는 이유:
#   PowerShell 은 문자열을 네이티브 명령의 stdin 으로 파이프할 때 자기 줄바꿈(CRLF)을
#   뒤에 붙인다. 프로브가 'fi' 로 끝나므로 원격에는 'fi' + CR 로 도착하고, 그것은 fi 가
#   아니라서 if 블록이 닫히지 않는다 — sh 가 "end of file unexpected (expecting fi)" 로
#   죽고, 마지막 HEALTH 줄이 사라져 터널이 멀쩡한데도 "터널이 안 뚫렸다" 로 오진했다.
#   'exit 0' 을 마지막 문장으로 두면 그 CR 이 파싱 대상 밖으로 밀려난다.
#   (stdin 으로 base64 를 보내는 다른 단계들은 원격에서 strip() 하므로 영향이 없다.)
$probeShUnix = ($probeSh -replace "`r`n", "`n") + "`nexit 0`n"

$probeOut = $probeShUnix | ssh -o BatchMode=yes -o ConnectTimeout=10 $Target "sh -s" 2>$null
$sshExit = $LASTEXITCODE

# 인증 실패와 "원격 명령이 실패함"은 다른 사건이다. ssh 는 자기 문제(연결 불가, 인증
# 거부, 호스트키 불일치)일 때만 255 를 내고, 원격 명령이 실패하면 그 명령의 코드를
# 그대로 넘긴다. 둘을 같이 묶으면 원격 스크립트가 깨진 것을 인증 실패로 오진한다 —
# 실제로 로그인 셸이 zsh 인 호스트에서 키가 멀쩡한데도 "키 인증 실패" 를 띄웠다.
$authOk = ($sshExit -ne 255) -or ("$probeOut" -match $posixRe)

if (-not $authOk) {
    if ($BatchMode) {
        Die @"
ssh 접속 실패 (exit 255, -BatchMode 라 비밀번호를 묻지 않는다): $Target
      인증 거부라면 키를 심고(ssh-copy-id), 그 외라면 아래로 원인을 직접 확인할 것:
        ssh -v -o BatchMode=yes "$Target" true
"@
    }
    Warn "ssh 접속 실패(exit 255) — 비밀번호를 최대 3번 물어본다 (점검/등록/검증 각 1회)."
    Warn "TTY 가 없는 환경이면 여기서 멈춘다. 그럴 땐 -BatchMode 를 쓸 것."
    Warn "매번 입력하기 싫으면: ssh-copy-id `"$Target`""
    $probeOut = $probeShUnix | ssh -o ConnectTimeout=10 $Target "sh -s"
    # 여기서 exit code 만으로 실패를 단정하면 안 된다 — Windows 원격은 POSIX 스크립트가
    # 에러를 내며 비영 코드로 끝나는 게 정상이고, 그건 다음 단계에서 처리한다.
}

$probeText = ($probeOut | Out-String)
$remoteIsWin = $false

if ($probeText -notmatch $posixRe) {
    # POSIX 셸이 아니다. cmd.exe 의 %OS% 는 로케일과 무관하게 Windows_NT 를 낸다
    # (localized 에러 메시지를 파싱하면 한글 CP949 가 깨져 판별이 불가능하다).
    # cmd.exe 만으로 OS 확인과 파이썬 탐색을 한 번에 한다.
    #
    # powershell -EncodedCommand 는 쓰지 않는다. 인코딩된 명령은 악성코드 패턴으로
    # 분류돼 AppLocker/EDR 이 차단하는 경우가 흔하고, 실제로 "액세스가 거부되었습니다"
    # 로 막혔다. cmd 의 where / if exist 만 쓰면 그 표면을 건드리지 않는다.
    # %OS% 는 로케일과 무관하게 Windows_NT 를 낸다.
    $winProbeInner = 'echo ENGRAM_WIN=%OS%' `
        + '& where python python3 py 2>nul' `
        + '& if exist "%USERPROFILE%\miniconda3\python.exe" echo %USERPROFILE%\miniconda3\python.exe' `
        + '& if exist "%USERPROFILE%\anaconda3\python.exe" echo %USERPROFILE%\anaconda3\python.exe' `
        + '& if exist "%USERPROFILE%\miniforge3\python.exe" echo %USERPROFILE%\miniforge3\python.exe' `
        + '& if exist "C:\ProgramData\miniconda3\python.exe" echo C:\ProgramData\miniconda3\python.exe' `
        + '& if exist "C:\ProgramData\Anaconda3\python.exe" echo C:\ProgramData\Anaconda3\python.exe'
    # The SSH account can have PowerShell as its login shell.  Send cmd syntax
    # only through an explicit cmd.exe wrapper, never to the login shell.
    $winProbe = 'cmd.exe /d /s /c "' + $winProbeInner + '"'

    $winOut = ($(ssh -o ConnectTimeout=15 $Target $winProbe 2>&1) | Out-String)
    if ($winOut -notmatch '(?m)^ENGRAM_WIN=Windows_NT') {
        if (-not $probeText.Trim() -and -not $winOut.Trim()) {
            Die "SSH 접속 자체가 실패했다: $Target (응답 없음)"
        }
        Die @"
원격 셸이 POSIX 도 Windows cmd 도 아니다 — 판별 실패.
      POSIX 프로브 응답: '$($probeText.Trim())'
      cmd 프로브 응답  : '$($winOut.Trim())'
      응답에 'parse error' 가 보이면 원격에 /bin/sh 가 없거나 로그인 셸이
      'sh -s' 조차 처리하지 못하는 경우다. 원격에서 'sh -c "uname -s"' 를 직접 확인할 것.
      이 정보를 그대로 알려줄 것.
"@
    }
    Ok "POSIX 셸 아님 → Windows 원격으로 판별 (%OS%=Windows_NT)"

    # WindowsApps 아래 python.exe 는 MS Store 스텁이라 실행하면 스토어가 열린다.
    # 실제 인터프리터가 아니므로 후순위로 민다.
    $allPy = @([regex]::Matches($winOut, '(?m)^\s*([A-Za-z]:\\[^\r\n]*?\.exe)\s*$') |
        ForEach-Object { $_.Groups[1].Value.Trim() } | Select-Object -Unique)
    $realPy = @($allPy | Where-Object { $_ -notmatch '\\WindowsApps\\' })
    # Prefer the Windows py launcher: it selects the current installed Python
    # (3.12 on the validated host) instead of an arbitrary old PythonNN path.
    $launchers = @($realPy | Where-Object { $_ -match '\\py\.exe$' })
    $pick = if ($launchers.Count -gt 0) { $launchers[0] } elseif ($realPy.Count -gt 0) { $realPy[0] } elseif ($allPy.Count -gt 0) { $allPy[0] } else { "" }

    $probeText = "ENGRAM_PROBE=Windows`nOS=Windows`nPY=$pick`nCAND=$($allPy -join ' ')`nHEALTH=SKIPPED`n"
    $remoteIsWin = $true

}

function Get-ProbeValue([string]$key) {
    $m = [regex]::Match($probeText, "(?m)^$key=(.*)$")
    if ($m.Success) { return $m.Groups[1].Value.Trim() }
    return ""
}
$uname  = Get-ProbeValue "OS"
$foundPy = Get-ProbeValue "PY"
$candPy = Get-ProbeValue "CAND"
$health = Get-ProbeValue "HEALTH"

if (-not $remoteIsWin -and $uname -notmatch "Linux|Darwin") {
    Die "지원하지 않는 원격 OS: '$uname'"
}
Ok "접속 OK (원격 OS: $uname)"

$tunnelHint = @"
      별도 터미널에서 아래를 띄운 뒤 다시 실행할 것:
        ssh -N -R ${RemotePort}:127.0.0.1:$RemotePort "$Target"
      (또는 ~/.ssh/config 의 해당 Host 에 'RemoteForward $RemotePort 127.0.0.1:$RemotePort')
"@

if (-not $SkipTunnelCheck) {
    if ($health -eq "SKIPPED") {
        # Windows 경로는 cmd 프로브만으로 /health 를 못 본다(curl 이 없을 수 있다).
        # 등록 payload 안에서 파이썬으로 확인하고, 실패하면 쓰기 전에 중단한다.
        Ok "터널 확인은 등록 단계에서 함께 수행 (Windows)"
    } elseif ($health -notmatch '"status"\s*:\s*"ok"') {
        Die "원격에서 :$RemotePort 에 닿지 않는다 — 터널이 안 뚫렸다.`n$tunnelHint"
    } else {
        Ok "터널 정상 — 원격에서 /health 응답"
    }
}

# ── 등록 ─────────────────────────────────────────────────────────────────────
# 스크립트는 base64 로 argv 에 실어 보내고(비밀 아님), 토큰만 stdin 으로 넘긴다.
Step "원격 ~/.claude.json 에 등록"
$registerPy = @"
import json, pathlib, shutil, sys
# BOM(U+FEFF)·개행·공백을 모두 제거한다. PowerShell 이 파이프 인코딩에 따라
# 선두에 BOM 을 붙일 수 있고, 그대로 두면 HTTP 헤더 인코딩(latin-1)에서 터진다.
tok = sys.stdin.read().strip().lstrip("﻿").strip()
if not tok:
    print("EMPTY_TOKEN"); raise SystemExit(1)
if not all(ord(ch) < 128 for ch in tok):
    print("BAD_TOKEN non-ascii"); raise SystemExit(1)

# 터널을 먼저 확인한다 — 닿지 않으면 설정 파일을 건드리지 않고 중단한다.
import urllib.request
try:
    _h = urllib.request.urlopen("http://127.0.0.1:$RemotePort/health", timeout=8).read().decode()
except Exception as _e:
    print("HEALTH=ERR %s" % _e); raise SystemExit(1)
if '"ok"' not in _h:
    print("HEALTH=BAD %s" % _h); raise SystemExit(1)
print("HEALTH=ok")

p = pathlib.Path.home() / ".claude.json"
try:
    cfg = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
except Exception:
    print("CLAUDE=PARSE_FAIL"); raise SystemExit(1)
if not isinstance(cfg, dict):
    print("CLAUDE=NOT_OBJECT"); raise SystemExit(1)
if "mcpServers" in cfg and not isinstance(cfg["mcpServers"], dict):
    print("CLAUDE=MCP_SERVERS_NOT_OBJECT"); raise SystemExit(1)
desired = {
    "type": "http",
    "url": "http://127.0.0.1:$RemotePort/mcp",
    "headers": {"Authorization": "Bearer " + tok},
}

# ~/.claude.json 은 engram 전용 파일이 아니다 — Claude Code 자기 상태(projects, 캐시
# 수십 개)가 같이 들어 있고, 세션이 돌고 있으면 그쪽도 수시로 쓴다. 우리가 읽고→쓰는
# 사이에 그쪽이 쓰면 그 변경이 날아간다(lock 이 없다). 그래서 바꿀 것이 있을 때만 쓴다.
# 이미 같은 값이면 파일을 열지 않으므로 경쟁 창 자체가 생기지 않는다.
existing = (cfg.get("mcpServers") or {}).get("engram")
if existing == desired:
    print("REGISTER=UNCHANGED")
elif "$SkipRegisterFlag" == "1":
    print("REGISTER=SKIPPED-DIFFERS")
else:
    cfg.setdefault("mcpServers", {})["engram"] = desired
    if p.exists():
        backup = p.with_name(p.name + ".engram-bak")
        if not backup.exists(): backup.write_bytes(p.read_bytes())
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".engram-tmp")
    tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)
    try:
        p.chmod(0o600)
    except Exception:
        pass
    print("REGISTER=WRITTEN")

# 같은 호출 안에서 되읽어 검증한다 — ssh 를 또 부르면 비밀번호를 또 묻는다.
back = json.loads(p.read_text(encoding="utf-8"))
print("URL=" + back.get("mcpServers", {}).get("engram", {}).get("url", "NONE"))
print("PROVIDER=claude-code registered")

# Codex uses the documented native TOML MCP table, but must not be reported as
# configured on hosts where the Codex client is not installed. The token arrived only on
# stdin and is written only to this protected remote client config; never to a
# local state record, argv, UI, or log.  Preserve unrelated TOML verbatim and
# refuse a malformed file rather than attempting a lossy rewrite.
codex_path = pathlib.Path.home() / ".codex" / "config.toml"
if not shutil.which("codex"):
    print("PROVIDER=codex unavailable")
else:
 try:
    codex_raw = codex_path.read_text(encoding="utf-8") if codex_path.exists() else ""
    if codex_raw:
        try:
            import tomllib
            tomllib.loads(codex_raw)
        except Exception:
            print("CODEX=PARSE_FAIL")
            raise SystemExit(1)
    # Remove the managed table and every nested managed subtable, retaining
    # all other user tables byte-for-byte.
    kept, managed = [], False
    for line in codex_raw.splitlines(keepends=True):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            managed = stripped[1:-1] == "mcp_servers.engram" or stripped[1:-1].startswith("mcp_servers.engram.")
        if not managed: kept.append(line)
    codex_raw = "".join(kept).rstrip()
    codex_entry = '\n\n[mcp_servers.engram]\nurl = "http://127.0.0.1:$RemotePort/mcp"\nhttp_headers = { Authorization = "Bearer ' + tok + '" }\n'
    desired_raw = codex_raw + codex_entry
    if desired_raw != (codex_path.read_text(encoding="utf-8") if codex_path.exists() else ""):
        if codex_path.exists():
            codex_backup = codex_path.with_name(codex_path.name + ".engram-bak")
            if not codex_backup.exists(): codex_backup.write_bytes(codex_path.read_bytes())
        codex_path.parent.mkdir(parents=True, exist_ok=True)
        codex_tmp = codex_path.with_name(codex_path.name + ".engram-tmp")
        with open(str(codex_tmp), "w", encoding="utf-8", newline="\n") as handle: handle.write(desired_raw)
        codex_tmp.replace(codex_path)
        try: codex_path.chmod(0o600)
        except Exception: pass
    codex_back = codex_path.read_text(encoding="utf-8")
    if 'http://127.0.0.1:$RemotePort/mcp' not in codex_back or 'Authorization = "Bearer ' + tok + '"' not in codex_back: raise RuntimeError("Codex config readback failed")
    print("PROVIDER=codex registered")
 except SystemExit: raise
 except Exception as exc:
    print("CODEX=FAIL %s" % exc); raise SystemExit(1)

# 등록한 설정 그대로 실제 호출까지 해본다.
import urllib.request
s = back["mcpServers"]["engram"]
req = urllib.request.Request(
    s["url"],
    data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}).encode(),
    headers={
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "X-Engram-Provision-Proof": "$ProofNonce",
        **s.get("headers", {}),
    },
)
try:
    with urllib.request.urlopen(req, timeout=25) as r:
        print("PROBE=%d %d" % (r.status, r.read().decode("utf-8", "replace").count('"name"')))
except Exception as e:
    print("PROBE=ERR %s %s" % (getattr(e, "code", ""), e))
print("REGISTERED")
"@
$SkipRegisterFlag = if ($SkipRegister) { "1" } else { "0" }
$registerPy = $registerPy.Replace('$SkipRegisterFlag', $SkipRegisterFlag)
$b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($registerPy))

# 원격 파이썬 인터프리터를 먼저 확정한다.
#
# 두 가지가 걸린다:
#  - 슬림 이미지엔 python3 없이 python 만 있기도 하다.
#  - 비대화형 SSH 는 ~/.bashrc 를 읽지 않는다. conda init 블록이 거기 있으므로
#    conda 파이썬은 PATH 에 안 잡힌다 (로그인해서 치면 되는데 ssh 로는 안 되는 이유).
# 없는 인터프리터를 호출하면 표준출력이 비어 "등록 실패" 로만 보이고 원인이 안 드러나므로,
# 여기서 확정하고 실패 시 무엇을 지정해야 하는지 알려준다.
#
# 원격에서 쓰는 모듈은 json/pathlib/urllib/base64 뿐 — 표준 라이브러리라 아무 파이썬이나 된다.
$remotePy = if ($RemotePython) { $RemotePython } else { $foundPy }

if (-not $remotePy) {
    $hintLine = if ($candPy) { "`n      원격에서 찾은 후보: $candPy" } else { "" }
    $findCmd = if ($remoteIsWin) {
        "ssh `"$Target`" `"where python & dir /b /s %USERPROFILE%\*conda*\python.exe`""
    } else {
        "ssh `"$Target`" 'ls -d ~/*conda*/bin/python /opt/*conda*/bin/python 2>/dev/null'"
    }
    $whyLine = if ($remoteIsWin) {
        "원격 PATH 에 python 이 없다."
    } else {
        "비대화형 SSH 는 ~/.bashrc 를 읽지 않아 conda 파이썬이 PATH 에 안 잡힌다."
    }
    Die @"
원격 파이썬을 찾지 못했다.
      $whyLine
      원격에서 경로를 확인한 뒤 -RemotePython 으로 넘길 것:
        $findCmd
        .\scripts\setup-remote.ps1 -Target "$Target" -RemotePython <경로>$hintLine
"@
}
Ok "원격 파이썬: $remotePy$(if ($RemotePython) { ' (직접 지정)' })"

$tokenPy = @'
import yaml, pathlib, sys
d = yaml.safe_load(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")) or {}
for t in d.get("tokens") or []:
    if t.get("name") == sys.argv[2]:
        sys.stdout.write(str(t.get("token") or ""))
        break
'@
# 원격 셸에 맞춰 인용 방식을 바꾼다. base64 는 영숫자·+/= 뿐이라 메타문자가 없고,
# 파이썬 코드는 전부 그 안에 들어 있으므로 따옴표 중첩이 발생하지 않는다.
#   POSIX  : '<path>' -c "..."      (경로에 공백이 있어도 안전)
#   Windows: "<path>" -c "..."      (cmd.exe 는 작은따옴표를 인용으로 안 본다)
    if ($remoteIsWin) {
        # Explicit cmd wrapper also makes a PowerShell-default SSH account
        # invoke the selected Python executable rather than parse it as text.
        $remoteCmd = 'cmd.exe /d /s /c """{0}"" -c ""import base64;exec(base64.b64decode(''{1}''))"""' -f $remotePy, $b64
} else {
    $remoteCmd = "'$remotePy' -c `"import base64;exec(base64.b64decode('$b64'))`""
}
$raw = (& $Python -c $tokenPy $TokensPath $TokenName) | ssh $Target $remoteCmd 2>&1
$result = ($raw | Out-String).Trim()

# 마커는 빠른 경로일 뿐이다. 배너·stderr 섞임으로 마커를 놓칠 수 있으므로
# 최종 판정은 원격이 되읽어 보낸 URL 로 한다. (같은 ssh 호출에 실려 온다)
$expectedUrl = "http://127.0.0.1:$RemotePort/mcp"
$mUrl   = [regex]::Match($result, '(?m)^URL=(.*)$')
$mProbe = [regex]::Match($result, '(?m)^PROBE=(.*)$')
$registeredUrl = if ($mUrl.Success) { $mUrl.Groups[1].Value.Trim() } else { "" }

if ($result -match '(?m)^HEALTH=(ERR|BAD)(.*)$') {
    Die "원격에서 :$RemotePort 에 닿지 않는다 — 터널이 안 뚫렸다. ($($Matches[1])$($Matches[2]))`n$tunnelHint"
}
if ($registeredUrl -ne $expectedUrl) {
    Die @"
등록 실패.
      원격 ~/.claude.json 의 engram.url = '$registeredUrl' (기대: '$expectedUrl')
      원격 응답 전문:
        $($result -replace "`n", "`n        ")
"@
}
if ($result -notmatch "REGISTERED") {
    Warn "완료 마커를 못 받았지만 원격 파일에는 정상 기록됨"
}
switch -Regex ($result) {
    'REGISTER=UNCHANGED'        { Ok "등록 확인 — 이미 같은 값이라 파일을 쓰지 않았다"; break }
    'REGISTER=WRITTEN'          { Ok "등록 완료 — 원격 ~/.claude.json 갱신"; break }
    'REGISTER=SKIPPED-DIFFERS'  { Warn "등록 건너뜀(-SkipRegister) — 기존 값이 기대와 다르다"; break }
    default                     { Ok "등록 확인 — $registeredUrl" }
}
Ok "engram.url = $registeredUrl"

# ── skill / SessionStart hook 배치 ──────────────────────────────────────────
# hook 과 skill 은 MCP 로 넘어오지 않는다 — 순수 클라이언트 사이드 기능이라 원격
# 파일시스템에 물건이 있어야 한다. 렌더링은 파이썬(core.integrations.remote_provision)이
# 단일 출처로 담당하고, 여기서는 바이트만 옮긴다.
#
# payload 는 stdin 으로 보낸다. skill 본문 네 개면 base64 가 수만 바이트라 argv
# 한도(Windows CreateProcess 32767)를 넘긴다. 토큰은 payload 에 들어가지 않는다.
if ($SkipProvision) {
    Warn "배치 건너뜀(-SkipProvision) — 원격에 skill 과 SessionStart hook 이 없다"
} else {
    Step "원격에 skill / SessionStart hook 배치"

    $remoteOs = if ($remoteIsWin) { "windows" } else { "posix" }
    $provModule = "core.integrations.remote_provision"

    $instB64 = (& $Python -m $provModule --emit installer) | Out-String
    if ($LASTEXITCODE -ne 0) { Die "배치 스크립트 렌더링 실패" }
    $instB64 = $instB64.Trim()

    $payloadB64 = (& $Python -m $provModule --emit payload --remote-os $remoteOs --scope-key $scope) | Out-String
    if ($LASTEXITCODE -ne 0) { Die "배치 payload 렌더링 실패 — .github/skills 확인" }
    $payloadB64 = $payloadB64.Trim()

    $skillList = @(& $Python -m $provModule --emit skills)
    Ok "보낼 skill: $($skillList -join ', ')"

    if ($remoteIsWin) {
        # Windows command lines cap at 32767 chars.  The installer is large,
        # so keep argv short and frame installer+payload as ASCII stdin.
        $bootB64 = (& $Python -m $provModule --emit framed-bootstrap) | Out-String
        if ($LASTEXITCODE -ne 0) { Die "Windows provision bootstrap render failed" }
        $bootB64 = $bootB64.Trim()
        $provCmd = 'cmd.exe /d /s /c """{0}"" -c ""import base64;exec(base64.b64decode(''{1}''))"""' -f $remotePy, $bootB64
        $provFrame = '{"installer":"' + $instB64 + '","payload":"' + $payloadB64 + '"}'
        if ($provCmd.Length -ge 32767) { Die "Windows provision bootstrap command exceeds command-line limit" }
    } else {
        $provCmd = "'$remotePy' -c `"import base64;exec(base64.b64decode('$instB64'))`""
        $provFrame = $payloadB64
    }
    $provRaw = ($provFrame | ssh $Target $provCmd 2>&1 | Out-String).Trim()

    if ($provRaw -notmatch "PROVISIONED") {
        Die @"
배치 실패.
      원격 응답 전문:
        $($provRaw -replace "`n", "`n        ")
"@
    }
    foreach ($line in ($provRaw -split "`n")) {
        $line = $line.Trim()
        if ($line -match '^SKILL=(.+)$')    { Ok "skill  $($Matches[1])" }
        elseif ($line -match '^HOOK=(.+)$') { Ok "hook   $($Matches[1])" }
        elseif ($line -match '^SETTINGS=(.+) registered=(\d+)$') {
            if ($Matches[2] -eq "1") {
                Ok "등록   $($Matches[1]) (SessionStart 1건)"
            } else {
                Warn "SessionStart 등록 수가 1 이 아니다: $($Matches[2]) — $($Matches[1])"
            }
        }
    }
}

# ── 검증 ─────────────────────────────────────────────────────────────────────
# 원격 호출도 위 등록과 같은 ssh 세션에서 이미 수행됐다(PROBE=). 여기서 결과만 판정한다.
Step "검증"
$probe = if ($mProbe.Success) { $mProbe.Groups[1].Value.Trim() } else { "" }
if ($probe -notmatch '^200\s') {
    Die @"
등록은 됐으나 원격에서 실제 호출이 실패했다: '$probe'
      401 이면 토큰 불일치, 그 외엔 터널을 확인할 것.
"@
}
$toolCount = ($probe -split '\s+')[1]
Ok "원격 tools/list → HTTP 200 (도구 $toolCount 개)"

# 진짜 증거는 로컬 감사 로그다 — 원격이 200 을 받았다는 것만으로는
# 그 요청이 이 머신의 engram 까지 왔다는 보장이 안 된다.
# 방금 호출이 마지막 줄로 남았는지(최근 2분 이내) 확인한다. ts 는 UTC.
if (-not (Test-Path $auditPath)) {
    Die "로컬 감사 로그 파일이 없다: $auditPath (tools/list 200 뒤의 fresh audit 증거가 필요)"
} else {
    $newAuditRows = @(Get-Content $auditPath | Select-Object -Skip $auditBeforeLines)
    $matchedAudit = $false
    foreach ($row in $newAuditRows) {
        try {
            $entry = $row | ConvertFrom-Json
            $age = (Get-Date).ToUniversalTime() - [datetime]::Parse([string]$entry.ts)
            # The streamable transport records the JSON-RPC method as the tool
            # field.  The nonce distinguishes this probe from concurrent calls
            # made with the same named principal on another remote host.
            if ($entry.principal -eq $TokenName -and $entry.action -eq "allow" -and $entry.path -eq "/mcp" -and $entry.tool -eq "tools/list" -and $entry.detail -match [regex]::Escape("provision_nonce=$ProofNonce") -and $age.TotalSeconds -ge 0 -and $age.TotalSeconds -lt 120) {
                $matchedAudit = $true
                break
            }
        } catch {}
    }
    if ($matchedAudit) {
        Ok "로컬 감사 로그에 선택 토큰의 방금 tools/list 호출이 기록됨"
    } else {
        Die "선택 토큰의 새 tools/list 감사 기록이 없다: $auditPath"
    }
}

if (-not $SkipProvision) {
    # First-provision success is durable only after both the remote tools/list
    # HTTP 200 and this fresh local audit proof.  The record contains no token.
    $recorded = (& $Python -m core.integrations.remote_provision --emit record --host $Target --remote-python $remotePy --remote-os $remoteOs) | Out-String
    if ($recorded -match "RECORDED") {
        Ok "자동 갱신 등록 — 이후 터널이 붙을 때 overlay 가 알아서 최신으로 맞춘다"
    } else {
        Die "자동 갱신 등록 실패 — 성공 배치 기록을 남기지 못했다"
    }
}

Write-Host "`n완료. 원격에서 'claude mcp list' 로 engram 이 connected 인지 확인." -ForegroundColor Green
Write-Host "scope=$scope 로 고정되어 있으므로 원격 세션은 이 스코프의 기억을 이어받는다."
if (-not $SkipProvision) {
    Write-Host "이미 열려 있던 원격 CLI 세션은 새 skill 목록과 hook 을 다시 읽지 않는다 — 세션을 새로 시작할 것.`n"
} else {
    Write-Host ""
}
