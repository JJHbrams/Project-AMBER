<#
.SYNOPSIS
    freeze-probe.ps1 을 로그온 시 자동 시작하도록 등록/해제한다. 관리자 권한 불필요.

.DESCRIPTION
    행(hang) 은 강제 리셋으로만 벗어날 수 있으므로, 재부팅마다 사람이 프로브를
    다시 켜는 걸 잊으면 그 회차의 증거가 사라진다. 로그온 트리거 작업으로 상시 무장한다.

    현재 사용자 컨텍스트의 작업이므로 관리자 권한이 필요 없다.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\freeze-probe-autostart.ps1
    powershell -ExecutionPolicy Bypass -File scripts\freeze-probe-autostart.ps1 -Remove
    powershell -ExecutionPolicy Bypass -File scripts\freeze-probe-autostart.ps1 -Status
#>

param(
    [string]$TaskName = "EngramFreezeProbe",
    [string]$OutDir = "D:\engram-freeze-probe",
    [switch]$Remove,
    [switch]$Status
)

$ErrorActionPreference = "Stop"

$probe = Join-Path $PSScriptRoot "freeze-probe.ps1"
if (-not (Test-Path $probe)) { throw "freeze-probe.ps1 을 찾을 수 없습니다: $probe" }

if ($Status) {
    $t = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $t) { Write-Host "등록되지 않음: $TaskName"; exit 0 }
    $t | Select-Object TaskName, State | Format-List
    Get-ScheduledTaskInfo -TaskName $TaskName |
        Select-Object LastRunTime, LastTaskResult, NextRunTime | Format-List
    Write-Host "실행 중인 프로브 프로세스:"
    Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" |
        Where-Object { $_.CommandLine -like "*freeze-probe.ps1*" } |
        Select-Object ProcessId, CreationDate | Format-Table -AutoSize
    exit 0
}

if ($Remove) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "작업 제거: $TaskName" -ForegroundColor Green
    Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" |
        Where-Object { $_.CommandLine -like "*freeze-probe.ps1*" } |
        ForEach-Object {
            Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
            Write-Host "  실행 중 프로브 종료: pid=$($_.ProcessId)"
        }
    exit 0
}

$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-ExecutionPolicy Bypass -WindowStyle Hidden -NonInteractive -File `"$probe`" -OutDir `"$OutDir`""

$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

# 배터리/유휴 조건으로 중단되면 정작 필요한 순간에 꺼져 있다 — 전부 해제한다.
# ExecutionTimeLimit 0 = 무제한 (프로브는 상시 실행이 목적).
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew

$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "engram 기동 후 발생하는 전체 시스템 행의 직전 상태를 D: 에 기록하는 블랙박스 샘플러" `
    -Force | Out-Null

Write-Host "등록 완료: $TaskName" -ForegroundColor Green
Write-Host "  프로브 : $probe"
Write-Host "  출력   : $OutDir"
Write-Host "  트리거 : 로그온 시 ($env:USERNAME)"
Write-Host ""
Write-Host "지금 즉시 시작하려면: Start-ScheduledTask -TaskName $TaskName"
Write-Host "상태 확인          : .\scripts\freeze-probe-autostart.ps1 -Status"
