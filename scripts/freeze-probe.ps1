<#
.SYNOPSIS
    시스템 전체 행(hang) 원인 추적용 1초 해상도 블랙박스 샘플러.

.DESCRIPTION
    engram 기동 후 발생하는 "커서는 움직이나 전체 무반응" 행의 직전 상태를 보존한다.

    핵심 설계:
      - 정지 의심 대상인 C:(NVMe) 가 아니라 D:(HDD) 에 기록한다.
      - FileOptions.WriteThrough + AutoFlush 로 OS 캐시를 우회한다.
        → 강제 리셋으로도 마지막 샘플이 디스크에 남는다.
      - Win32_Process 전체 열거 같은 비싼 WMI 호출은 쓰지 않는다
        (그 자체가 부하 용의자이므로 관측자 효과를 피한다).

    수집 항목:
      perf.csv    — 1초마다 디스크 지연/큐, 메모리, 커널 풀, CPU, 프로세스 큐
      procs.csv   — 10초마다 상위 프로세스의 CPU/WS/핸들/스레드
      events.log  — 관측자 자신의 생애주기 및 임계값 초과 경고

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\freeze-probe.ps1
    powershell -ExecutionPolicy Bypass -File scripts\freeze-probe.ps1 -OutDir D:\engram-freeze-probe
#>

param(
    [string]$OutDir = "D:\engram-freeze-probe",
    [int]$IntervalSec = 1,
    [int]$ProcEverySec = 10
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path $OutDir)) { New-Item -ItemType Directory -Path $OutDir -Force | Out-Null }

# 기록 대상은 반드시 감시 대상 볼륨과 달라야 한다. C: 에 쓰면 행이 걸리는 순간 기록도 함께 멈춘다.
$outRoot = [System.IO.Path]::GetPathRoot((Resolve-Path $OutDir).Path)
if ($outRoot -match '^C:') {
    Write-Warning "출력 경로가 C: 입니다 — C: 스톨 시 기록도 함께 멈춥니다. D: 등 다른 볼륨을 쓰세요."
}

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$perfPath  = Join-Path $OutDir "perf-$stamp.csv"
$procPath  = Join-Path $OutDir "procs-$stamp.csv"
$eventPath = Join-Path $OutDir "events-$stamp.log"

# WriteThrough: OS 파일 캐시를 우회해 물리 디스크까지 밀어넣는다.
function New-ThroughWriter([string]$path) {
    $fs = New-Object System.IO.FileStream(
        $path,
        [System.IO.FileMode]::Create,
        [System.IO.FileAccess]::Write,
        [System.IO.FileShare]::Read,
        4096,
        [System.IO.FileOptions]::WriteThrough
    )
    $sw = New-Object System.IO.StreamWriter($fs, [System.Text.UTF8Encoding]::new($false))
    $sw.AutoFlush = $true
    return $sw
}

$perfW  = New-ThroughWriter $perfPath
$procW  = New-ThroughWriter $procPath
$eventW = New-ThroughWriter $eventPath

function Write-Event([string]$msg) {
    $eventW.WriteLine("$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss.fff') $msg")
}

# 성능 카운터는 지역화된 이름을 쓰므로 영문 이름을 레지스트리로 번역한다.
# (한국어 Windows 에서 "\Memory\Available MBytes" 는 그대로 통하지 않는다)
# Perflib 의 Counter 값은 [인덱스, 이름, 인덱스, 이름, ...] 평면 배열이다.
# 009(영문)와 CurrentLanguage(현재 언어)는 배열 순서가 같다는 보장이 없으므로
# 반드시 "영문이름 → 인덱스 → 지역화이름" 두 단계로 번역해야 한다.
# (순서가 같다고 가정하면 인덱스가 어긋나 Pool Nonpaged Bytes 같은 항목이 엉뚱한 이름으로 바뀐다)
$counterIndex = @{}
try {
    $eng = (Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Perflib\009' -Name Counter).Counter
    $loc = (Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Perflib\CurrentLanguage' -Name Counter).Counter

    $locByIdx = @{}
    for ($i = 0; $i -lt $loc.Count - 1; $i += 2) {
        $locByIdx[$loc[$i]] = $loc[$i + 1]
    }

    $engToIdx = @{}
    for ($i = 0; $i -lt $eng.Count - 1; $i += 2) {
        # 같은 이름이 여러 인덱스에 걸릴 수 있다 — 첫 항목을 쓴다.
        if (-not $engToIdx.ContainsKey($eng[$i + 1])) { $engToIdx[$eng[$i + 1]] = $eng[$i] }
    }

    foreach ($name in $engToIdx.Keys) {
        $idx = $engToIdx[$name]
        if ($locByIdx.ContainsKey($idx)) { $counterIndex[$name] = $locByIdx[$idx] }
    }
} catch {
    Write-Event "WARN 카운터 이름 번역 실패 — 영문 이름 그대로 시도: $_"
}

function Loc([string]$english) {
    if ($counterIndex.ContainsKey($english)) { return $counterIndex[$english] }
    return $english
}

$objDisk = Loc "PhysicalDisk"
$objMem  = Loc "Memory"
$objProc = Loc "Processor"
$objSys  = Loc "System"

$counters = @(
    "\$objDisk(_Total)\$(Loc 'Avg. Disk sec/Transfer')"
    "\$objDisk(_Total)\$(Loc 'Current Disk Queue Length')"
    "\$objDisk(_Total)\$(Loc 'Disk Bytes/sec')"
    "\$objMem\$(Loc 'Available MBytes')"
    "\$objMem\$(Loc 'Committed Bytes')"
    "\$objMem\$(Loc 'Pool Nonpaged Bytes')"
    "\$objMem\$(Loc 'Pool Paged Bytes')"
    "\$objMem\$(Loc 'Pages/sec')"
    "\$objProc(_Total)\$(Loc '% Processor Time')"
    "\$objSys\$(Loc 'Processor Queue Length')"
    "\$objSys\$(Loc 'Context Switches/sec')"
)

# 사용 가능한 카운터만 남긴다 — 하나라도 실패하면 Get-Counter 전체가 던진다.
$usable = @()
foreach ($c in $counters) {
    try { Get-Counter -Counter $c -MaxSamples 1 -ErrorAction Stop | Out-Null; $usable += $c }
    catch { Write-Event "WARN 카운터 사용 불가 — 제외: $c" }
}
if ($usable.Count -eq 0) { throw "사용 가능한 성능 카운터가 없습니다." }

$perfW.WriteLine((@("timestamp") + $usable) -join ",")
# __TOTAL__ 행에서 pid 열은 전체 프로세스 수를 의미한다.
$procW.WriteLine("timestamp,name,pid,cpu_s,ws_mb,private_mb,handles,threads")

# engram 프로세스는 순위에서 밀려도 항상 기록한다 — 상관관계를 볼 대상이 바로 이들이다.
$engramNames = @(
    'engram-overlay', 'python', 'pythonw', 'streamlit',
    'ssh', 'node', 'claude', 'powershell', 'pwsh', 'WmiPrvSE'
)

Write-Event "START pid=$PID out=$OutDir interval=${IntervalSec}s counters=$($usable.Count)"
Write-Event "INFO 감시 대상 카운터: $($usable -join ' | ')"
Write-Event "INFO Ctrl+C 로 종료. 행 발생 후 재부팅하면 이 파일들의 마지막 줄이 직전 상태다."

$lastProc = [datetime]::MinValue

try {
    while ($true) {
        $now = Get-Date
        $ts = $now.ToString("yyyy-MM-dd HH:mm:ss.fff")

        try {
            $sample = Get-Counter -Counter $usable -MaxSamples 1 -ErrorAction Stop
            $map = @{}
            foreach ($s in $sample.CounterSamples) { $map[$s.Path] = $s.CookedValue }

            $vals = foreach ($c in $usable) {
                $hit = $sample.CounterSamples | Where-Object { $_.Path -like "*$($c.Substring($c.LastIndexOf('\')))" } | Select-Object -First 1
                if ($hit) { [math]::Round($hit.CookedValue, 4) } else { "" }
            }
            $perfW.WriteLine((@($ts) + $vals) -join ",")

            # 임계값 경고 — 나중에 events.log 만 훑어도 이상 구간을 찾을 수 있게 한다.
            $lat = $sample.CounterSamples | Where-Object { $_.Path -like "*$(Loc 'Avg. Disk sec/Transfer')*" } | Select-Object -First 1
            if ($lat -and $lat.CookedValue -gt 0.5) {
                Write-Event ("ALERT 디스크 지연 {0:N3}s — I/O 스톨 징후" -f $lat.CookedValue)
            }
            $avail = $sample.CounterSamples | Where-Object { $_.Path -like "*$(Loc 'Available MBytes')*" } | Select-Object -First 1
            if ($avail -and $avail.CookedValue -lt 2048) {
                Write-Event ("ALERT 여유 메모리 {0} MB — 고갈 징후" -f [int]$avail.CookedValue)
            }
        } catch {
            Write-Event "WARN 카운터 수집 실패: $_"
        }

        if (($now - $lastProc).TotalSeconds -ge $ProcEverySec) {
            try {
                # Get-Process 는 커널 구조체를 직접 읽는다 — WMI 열거보다 훨씬 가볍다.
                $all = Get-Process -ErrorAction SilentlyContinue

                # 메모리 상위 15개 + engram 관련 전체 (중복 제거)
                $watch = @($all | Sort-Object -Property WorkingSet64 -Descending | Select-Object -First 15)
                $watch += @($all | Where-Object { $engramNames -contains $_.ProcessName })
                $watch = $watch | Sort-Object -Property Id -Unique

                foreach ($p in $watch) {
                    $cpu = 0; try { $cpu = [math]::Round($p.CPU, 1) } catch {}
                    $thr = 0; try { $thr = $p.Threads.Count } catch {}
                    $procW.WriteLine(
                        "$ts,$($p.ProcessName),$($p.Id),$cpu," +
                        "$([int]($p.WorkingSet64/1MB)),$([int]($p.PrivateMemorySize64/1MB))," +
                        "$($p.HandleCount),$thr"
                    )
                }

                # 합계는 핸들/스레드/프로세스 누수 판별용 — 단조 증가하면 그게 원인이다.
                # __TOTAL__ 행은 proc_count 열에 전체 프로세스 수를 담는다.
                $thrSum = 0
                foreach ($p in $all) { try { $thrSum += $p.Threads.Count } catch {} }
                $procW.WriteLine(
                    "$ts,__TOTAL__,$($all.Count),0,0,0," +
                    "$(($all | Measure-Object HandleCount -Sum).Sum),$thrSum"
                )
            } catch {
                Write-Event "WARN 프로세스 수집 실패: $_"
            }
            $lastProc = $now
        }

        # Get-Counter 는 rate 카운터 때문에 자체적으로 약 1초를 쓴다.
        # 경과 시간을 빼서 실제 샘플 간격이 IntervalSec 를 넘지 않게 한다.
        $elapsed = ((Get-Date) - $now).TotalSeconds
        $rest = $IntervalSec - $elapsed
        if ($rest -gt 0) { Start-Sleep -Milliseconds ([int]($rest * 1000)) }
    }
} finally {
    Write-Event "STOP 정상 종료"
    foreach ($w in @($perfW, $procW, $eventW)) { try { $w.Dispose() } catch {} }
}
