<#
.SYNOPSIS
    전체 행(hang) 진단을 위한 커널 덤프 수집 활성화. **관리자 권한 필요.**

.DESCRIPTION
    현재 이 PC 는 크래시 덤프를 물리적으로 남길 수 없는 상태다:
      - CrashDumpEnabled=3 (커널 덤프) 이고 대상은 C:\WINDOWS\MEMORY.DMP 인데
      - 페이지파일이 D: 에만 있고 부팅 볼륨(C:) 에는 없다.
      - Windows 는 부팅 볼륨 페이지파일 또는 전용 덤프 파일이 없으면 덤프를 못 쓴다.
      - 그 결과 매 부팅마다 volmgr 45/46 ("크래시 덤프 드라이버 로드 실패") 가 기록되고
        Minidump 폴더와 MEMORY.DMP 가 계속 비어 있다.

    이 스크립트는 C: 여유 공간(약 42GB)을 잡아먹지 않도록 페이지파일을 늘리는 대신
    **D: 에 전용 덤프 파일**을 두는 방식을 쓴다. D: 는 여유 659GB 이고,
    C: NVMe 스톨이 원인일 경우 덤프를 다른 물리 디스크에 쓰는 편이 성공률도 높다.

    변경 내용:
      1. DedicatedDumpFile = D:\dedicateddump.sys (크기 지정)
      2. CrashDumpEnabled  = 1 (전체) 또는 7 (활성 메모리 덤프, 기본)
         → 활성 덤프는 유저모드 페이지까지 포함해 "모든 프로세스가 무엇을 기다리는지" 볼 수 있다.
      3. AlwaysKeepMemoryDump = 1 (디스크 여유 부족해도 덤프 보존)
      4. kbdhid CrashOnCtrlScroll = 1 (Ctrl 누른 채 ScrollLock 2회 → 강제 버그체크)

    ⚠ 이 PC 의 키보드는 전부 Bluetooth HID 다. CrashOnCtrlScroll 은 버그체크 시점에
      BT 스택이 살아있지 않아 **동작하지 않을 가능성이 높다.** 강제 덤프를 쓰려면
      유선 USB 키보드를 연결해 두어야 한다.

    재부팅 후 적용된다.

.EXAMPLE
    # 관리자 PowerShell 에서
    powershell -ExecutionPolicy Bypass -File scripts\enable-hang-dump.ps1
    powershell -ExecutionPolicy Bypass -File scripts\enable-hang-dump.ps1 -Revert
#>

param(
    [string]$DumpVolume = "D:",
    [int]$DumpSizeMB = 24576,          # 활성 덤프 여유분 포함 (RAM 64GB 기준)
    [ValidateSet("kernel", "active", "complete")]
    [string]$DumpType = "active",
    [switch]$Revert
)

$ErrorActionPreference = "Stop"

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
           ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Error "관리자 권한이 필요합니다. 관리자 PowerShell 에서 다시 실행하세요."
    exit 1
}

$crashKey = 'HKLM:\SYSTEM\CurrentControlSet\Control\CrashControl'
$kbdKey   = 'HKLM:\SYSTEM\CurrentControlSet\Services\kbdhid\Parameters'
$i8042Key = 'HKLM:\SYSTEM\CurrentControlSet\Services\i8042prt\Parameters'

if ($Revert) {
    Write-Host "== 되돌리기" -ForegroundColor Cyan
    foreach ($n in 'DedicatedDumpFile', 'DumpFileSize', 'AlwaysKeepMemoryDump') {
        Remove-ItemProperty -Path $crashKey -Name $n -ErrorAction SilentlyContinue
        Write-Host "  제거: $n"
    }
    Set-ItemProperty -Path $crashKey -Name CrashDumpEnabled -Value 3 -Type DWord
    Write-Host "  CrashDumpEnabled = 3 (커널 덤프, 기본값)"
    foreach ($k in $kbdKey, $i8042Key) {
        if (Test-Path $k) { Remove-ItemProperty -Path $k -Name CrashOnCtrlScroll -ErrorAction SilentlyContinue }
    }
    Write-Host "  CrashOnCtrlScroll 제거"
    $dumpFile = Join-Path "$DumpVolume\" "dedicateddump.sys"
    if (Test-Path $dumpFile) {
        Write-Host "  참고: $dumpFile 은 재부팅 후 수동 삭제하세요 (현재 사용 중일 수 있음)."
    }
    Write-Host "`n재부팅 후 적용됩니다." -ForegroundColor Yellow
    exit 0
}

# 대상 볼륨 검증 — 덤프 파일이 들어갈 자리가 실제로 있는지 확인한다.
$vol = Get-Volume -DriveLetter $DumpVolume.TrimEnd(':') -ErrorAction Stop
$freeMB = [int]($vol.SizeRemaining / 1MB)
if ($freeMB -lt ($DumpSizeMB + 5120)) {
    Write-Error "$DumpVolume 여유 공간 부족: ${freeMB}MB (필요 약 $($DumpSizeMB + 5120)MB)"
    exit 1
}
Write-Host "== 대상 볼륨 $DumpVolume — 여유 ${freeMB}MB, 덤프 파일 ${DumpSizeMB}MB" -ForegroundColor Cyan

$dumpEnabledValue = switch ($DumpType) {
    "complete" { 1 }
    "kernel"   { 3 }
    "active"   { 7 }
}

Write-Host "== 현재 설정 백업" -ForegroundColor Cyan
$backup = Get-ItemProperty -Path $crashKey |
    Select-Object CrashDumpEnabled, DumpFile, DedicatedDumpFile, DumpFileSize, AlwaysKeepMemoryDump
$backupPath = Join-Path $env:USERPROFILE "crashcontrol-backup-$(Get-Date -Format yyyyMMdd-HHmmss).json"
$backup | ConvertTo-Json | Set-Content -Path $backupPath -Encoding UTF8
Write-Host "  → $backupPath"

Write-Host "== 덤프 설정 적용" -ForegroundColor Cyan
Set-ItemProperty -Path $crashKey -Name DedicatedDumpFile   -Value "$DumpVolume\dedicateddump.sys" -Type String
Set-ItemProperty -Path $crashKey -Name DumpFileSize        -Value $DumpSizeMB   -Type DWord
Set-ItemProperty -Path $crashKey -Name CrashDumpEnabled    -Value $dumpEnabledValue -Type DWord
Set-ItemProperty -Path $crashKey -Name AlwaysKeepMemoryDump -Value 1            -Type DWord
Set-ItemProperty -Path $crashKey -Name Overwrite           -Value 1             -Type DWord
Write-Host "  DedicatedDumpFile   = $DumpVolume\dedicateddump.sys"
Write-Host "  DumpFileSize        = $DumpSizeMB MB"
Write-Host "  CrashDumpEnabled    = $dumpEnabledValue ($DumpType)"
Write-Host "  AlwaysKeepMemoryDump = 1"

Write-Host "== 키보드 강제 덤프(Ctrl + ScrollLock x2) 활성화" -ForegroundColor Cyan
foreach ($k in $kbdKey, $i8042Key) {
    if (-not (Test-Path $k)) { New-Item -Path $k -Force | Out-Null }
    Set-ItemProperty -Path $k -Name CrashOnCtrlScroll -Value 1 -Type DWord
    Write-Host "  $k → 1"
}

Write-Host ""
Write-Host "완료 — 재부팅해야 적용됩니다." -ForegroundColor Green
Write-Host ""
Write-Host "재부팅 후 확인:" -ForegroundColor Yellow
Write-Host "  Get-WinEvent -FilterHashtable @{LogName='System';ProviderName='volmgr'} -MaxEvents 5"
Write-Host "  → 45/46 오류가 사라져야 정상입니다."
Write-Host ""
Write-Host "⚠ 이 PC 키보드는 전부 Bluetooth 입니다. 강제 덤프를 쓰려면" -ForegroundColor Yellow
Write-Host "  유선 USB 키보드를 연결해 두세요. 행 발생 시 Ctrl 을 누른 채 ScrollLock 2회." -ForegroundColor Yellow
