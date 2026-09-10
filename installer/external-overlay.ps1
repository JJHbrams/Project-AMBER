# external-overlay.ps1 — 외부 renderer 런타임 설치·복구·제거 (공용)
#
# 설치본은 configure.ps1 이, 소스 설치는 installer/install.ps1 이 이 파일을
# dot-source 해서 같은 함수를 쓴다. 진입점이 둘이라 로직을 한쪽에 두면 갈린다.
#
# 소유권 규칙 한 문장 — 표식이 없으면 우리 것이 아니다.
#   런타임은 %LOCALAPPDATA%\engram-overlay\runtime 단일 디렉토리이고, 자동시작은
#   HKCU Run 의 EngramOverlay 값 하나다. 사용자가 SDK 로 자기 renderer 를 깔았다면
#   같은 자리를 쓴다. 그래서 우리가 만든 것만 골라 건드릴 근거가 필요하다.
#   docs/dev/external-overlay-install-plan.md §7.1 참조.

Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot 'external-components.ps1')

$script:ExternalOverlayRoot = Join-Path $env:LOCALAPPDATA "engram-overlay"
$script:ExternalOverlayRuntime = Join-Path $script:ExternalOverlayRoot "runtime"
$script:ExternalOverlayMarkerName = ".installed-by-amber"
$script:ExternalOverlayRunKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
$script:ExternalOverlayRunValue = "EngramOverlay"
$script:ExternalOverlayMinPython = [Version]"3.11"

function Get-ExternalOverlayPaths {
    [CmdletBinding()]
    param()
    $generation = Get-EngramComponentGeneration -Root $script:ExternalOverlayRoot
    $runtime = if ($generation) { $generation.Runtime } else { $script:ExternalOverlayRuntime }
    return [pscustomobject]@{
        Root      = $script:ExternalOverlayRoot
        Runtime   = $runtime
        Marker    = Join-Path $runtime $script:ExternalOverlayMarkerName
        Python    = Join-Path $runtime "Scripts\python.exe"
        Pip       = Join-Path $runtime "Scripts\pip.exe"
        # 콘솔 없는 gui-script. 자동시작에 콘솔 런처를 걸면 로그인마다 검은 창이 뜬다.
        Windowed  = Join-Path $runtime "Scripts\engram-custom-overlayw.exe"
        Console   = Join-Path $runtime "Scripts\engram-custom-overlay.exe"
        RunKey    = $script:ExternalOverlayRunKey
        RunValue  = $script:ExternalOverlayRunValue
    }
}

function Get-PinnedExternalOverlayVersion {
    <#
        검증된 renderer 버전의 단일 출처. 리터럴로 박으면 릴리스 한 번에 낡는다.
    #>
    [CmdletBinding()]
    param([string]$InstallerDir = $PSScriptRoot)
    $pin = Join-Path $InstallerDir "external-overlay.pin"
    if (-not (Test-Path -LiteralPath $pin)) { return "unpinned" }
    $value = (Get-Content -LiteralPath $pin -Raw).Trim()
    if ($value -notmatch '^\d+\.\d+\.\d+(\.\d+)?$') { return "unpinned" }
    return $value
}

function Resolve-EligiblePython {
    <#
        "python 이 있는가" 로는 부족하다. 세 가지가 각각 다르게 실패한다.
          1) PATH 에 없다
          2) Windows Store 별칭 스텁이다 — 실행은 되지만 인터프리터가 아니다
          3) 3.11 미만이다 — venv 는 만들어지고 pip install 이 중간에 깨져
             반쯤 설치된 상태가 남는다. 그래서 시작조차 하지 않는다.
        판정 결과는 표식에 기록한다. 나중에 그 인터프리터가 사라졌는지 볼 근거다.
    #>
    [CmdletBinding()]
    param([string]$Command = "python")

    $result = [pscustomobject]@{
        Ok         = $false
        Executable = ""
        Version    = ""
        Reason     = ""
    }

    $probe = 'import sys; print(sys.executable); print(str(sys.version_info.major)+chr(46)+str(sys.version_info.minor))'
    try {
        $lines = @(& $Command -c $probe 2>$null)
    } catch {
        $result.Reason = "PATH 에서 '$Command' 를 실행할 수 없습니다."
        return $result
    }
    if ($LASTEXITCODE -ne 0 -or $lines.Count -lt 2) {
        $result.Reason = "'$Command' 가 Python 인터프리터로 응답하지 않았습니다."
        return $result
    }

    $executable = $lines[0].Trim()
    $versionText = $lines[1].Trim()
    $result.Executable = $executable
    $result.Version = $versionText

    if (-not $executable) {
        $result.Reason = "인터프리터 경로를 확인할 수 없습니다."
        return $result
    }
    if ($executable -match '\\WindowsApps\\') {
        $result.Reason = "PATH 의 python 이 Microsoft Store 별칭입니다. 실제 Python 을 설치하거나 PATH 순서를 조정하세요."
        return $result
    }
    if (-not (Test-Path -LiteralPath $executable)) {
        $result.Reason = "인터프리터 경로가 존재하지 않습니다: $executable"
        return $result
    }

    $parsed = $null
    if (-not [Version]::TryParse($versionText, [ref]$parsed)) {
        $result.Reason = "Python 버전을 해석할 수 없습니다: '$versionText'"
        return $result
    }
    if ($parsed -lt $script:ExternalOverlayMinPython) {
        $result.Reason = "Python $versionText 는 너무 낮습니다. 외부 오버레이는 $($script:ExternalOverlayMinPython) 이상이 필요합니다."
        return $result
    }

    $result.Ok = $true
    return $result
}

function Get-ExternalOverlayOwnership {
    <#
        런타임이 있는가, 그리고 그것이 우리 것인가. 두 질문은 다르다.
        사용자가 먼저 같은 자리에 깔아두었으면 우리는 소유권을 주장하지 않는다.
    #>
    [CmdletBinding()]
    param()
    $paths = Get-ExternalOverlayPaths
    $exists = Test-Path -LiteralPath $paths.Runtime
    $marker = $null
    if (Test-Path -LiteralPath $paths.Marker) {
        try {
            $marker = Get-Content -LiteralPath $paths.Marker -Raw | ConvertFrom-Json
        } catch {
            $marker = $null
        }
    }
    return [pscustomobject]@{
        Exists       = $exists
        OwnedByAmber = [bool]$marker
        Marker       = $marker
        Paths        = $paths
    }
}

function Write-ExternalOverlayMarker {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$AmberVersion,
        [Parameter(Mandatory)][string]$OverlayVersion,
        [Parameter(Mandatory)][string]$Interpreter,
        [string]$Overlay = "",
        [string]$AutostartValue = ""
    )
    $paths = Get-ExternalOverlayPaths
    $payload = [ordered]@{
        amber_version    = $AmberVersion
        overlay_version  = $OverlayVersion
        overlay          = $Overlay
        interpreter      = $Interpreter
        autostart_value  = $AutostartValue
        installed_at     = (Get-Date).ToString("o")
    }
    $json = $payload | ConvertTo-Json -Depth 4
    Set-Content -LiteralPath $paths.Marker -Value $json -Encoding UTF8
}

function Install-ExternalOverlayRuntime {
    <#
        번들된 wheel 로 런타임을 만든다. 실패해도 예외를 밖으로 던지지 않는다 —
        AMBER core 설치는 계속되어야 한다(불변식 2). 결과를 객체로 돌려주고
        호출부가 사용자에게 보이는 문장으로 끝낸다.
    #>
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$WheelPath,
        [Parameter(Mandatory)][string]$AmberVersion,
        [string]$Overlay = "bolttagu-2d",
        [string]$PythonCommand = "python",
        [switch]$Autostart
    )

    $outcome = [pscustomobject]@{
        Installed = $false
        Skipped   = $false
        Reason    = ""
        Runtime   = (Get-ExternalOverlayPaths).Runtime
    }

    # 소유권 가드가 가장 먼저다. 다른 실패(wheel 없음 등)를 먼저 반환하면 "남의 것을
    # 건드리지 않는다"는 판정이 그 경로에 가려진다. 가장 위험한 결정을 맨 앞에 둔다.
    $ownership = Get-ExternalOverlayOwnership
    if ($ownership.Exists -and -not $ownership.OwnedByAmber) {
        # 사용자가 먼저 깐 런타임이다. 표식을 새로 써서 빼앗지 않는다.
        $outcome.Skipped = $true
        $outcome.Reason = "이미 설치된 외부 오버레이 런타임을 사용합니다 ($($ownership.Paths.Runtime)). 설치를 건너뜁니다."
        return $outcome
    }

    if (-not (Test-Path -LiteralPath $WheelPath)) {
        $outcome.Reason = "번들된 renderer wheel 을 찾을 수 없습니다: $WheelPath"
        return $outcome
    }

    $python = Resolve-EligiblePython -Command $PythonCommand
    if (-not $python.Ok) {
        $outcome.Reason = $python.Reason
        return $outcome
    }

    $paths = $ownership.Paths
    try {
        if (-not (Test-Path -LiteralPath $paths.Runtime)) {
            New-Item -ItemType Directory -Force -Path $paths.Root | Out-Null
            & $python.Executable -m venv $paths.Runtime
            if ($LASTEXITCODE -ne 0) {
                $outcome.Reason = "런타임 venv 를 만들 수 없습니다: $($paths.Runtime)"
                return $outcome
            }
        }
        # Pillow 는 pip 가 조달한다 — 사용자 Python ABI 에 맞는 wheel 을 번들에
        # 담는 것은 매트릭스가 되어 현재 범위 밖이다. 계획 §7.4.
        # The setup bundle uses a generic filename, which pip rightly rejects.
        # Recover the wheel's validated distribution/version/tag from its own
        # metadata into a private staging directory; never rename user assets.
        $stagedWheel = New-ExternalOverlayWheelStage -WheelPath $WheelPath
        try { & $paths.Python -m pip install --disable-pip-version-check --quiet $stagedWheel.Path }
        finally {
            Remove-Item -LiteralPath $stagedWheel.Path -Force -ErrorAction SilentlyContinue
            Remove-Item -LiteralPath $stagedWheel.Directory -Force -ErrorAction SilentlyContinue
        }
        if ($LASTEXITCODE -ne 0) {
            $outcome.Reason = "renderer 설치에 실패했습니다. 네트워크가 막혀 Pillow 를 받지 못했을 수 있습니다. 수동으로: `"$($paths.Python)`" -m pip install `"$WheelPath`""
            return $outcome
        }
        if (-not (Test-Path -LiteralPath $paths.Windowed)) {
            $outcome.Reason = "설치는 끝났지만 실행 파일이 없습니다: $($paths.Windowed)"
            return $outcome
        }

        $autostartValue = if ($ownership.Marker -and $ownership.Marker.PSObject.Properties['autostart_value']) { [string]$ownership.Marker.autostart_value } else { '' }
        if ($Autostart) {
            $autostartValue = '"{0}" --provider' -f $paths.Windowed
            $existingRun = Get-ItemProperty -LiteralPath $paths.RunKey -Name $paths.RunValue -ErrorAction SilentlyContinue
            $oldValue = if ($existingRun) { [string]$existingRun.($paths.RunValue) } else { '' }
            $recordedValue = if ($ownership.Marker) { [string]$ownership.Marker.autostart_value } else { '' }
            if ($oldValue -and $oldValue -cne $autostartValue -and $oldValue -cne $recordedValue) {
                throw 'External startup was changed by the user; preserved.'
            }
            New-Item -Path $paths.RunKey -Force | Out-Null
            Set-ItemProperty -Path $paths.RunKey -Name $paths.RunValue -Value $autostartValue
        }

        Write-ExternalOverlayMarker -AmberVersion $AmberVersion `
            -OverlayVersion (Get-PinnedExternalOverlayVersion) `
            -Interpreter $python.Executable -Overlay $Overlay -AutostartValue $autostartValue

        $outcome.Installed = $true
        return $outcome
    } catch {
        $outcome.Reason = "renderer 설치 중 오류: $($_.Exception.Message)"
        return $outcome
    }
}

function New-ExternalOverlayWheelStage {
    param([Parameter(Mandatory)][string]$WheelPath)
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = [IO.Compression.ZipFile]::OpenRead($WheelPath)
    try {
        $metadata = @($archive.Entries | Where-Object { $_.FullName -match '^[A-Za-z0-9_.-]+\.dist-info/WHEEL$' })
        if ($metadata.Count -ne 1) { throw 'External wheel has no unique safe WHEEL metadata.' }
        $reader = [IO.StreamReader]::new($metadata[0].Open())
        try { $content = $reader.ReadToEnd() } finally { $reader.Dispose() }
        $tag = [regex]::Match($content, '(?m)^Tag:\s*([A-Za-z0-9_.]+-[A-Za-z0-9_.]+-[A-Za-z0-9_.]+)\s*$')
        if (-not $tag.Success) { throw 'External wheel has no valid compatibility tag.' }
        $stem = $metadata[0].FullName.Split('/')[0] -replace '\.dist-info$', ''
        $pinned = Get-PinnedExternalOverlayVersion
        if ($pinned -eq 'unpinned' -or $stem -cne ('engram_custom_overlay-' + $pinned)) {
            throw 'External wheel distribution/version does not match the pinned Engram renderer.'
        }
        $name = $stem + '-' + $tag.Groups[1].Value + '.whl'
        $directory = Join-Path ([IO.Path]::GetTempPath()) ('engram-wheel-' + [Guid]::NewGuid().ToString('N'))
        New-Item -ItemType Directory -Path $directory | Out-Null
        $path = Join-Path $directory $name
        Copy-Item -LiteralPath $WheelPath -Destination $path
        return [pscustomobject]@{ Path=$path; Directory=$directory }
    } finally { $archive.Dispose() }
}

function Remove-ExternalOverlayRuntime {
    <#
        표식이 없으면 지우지 않는다. 자동시작도 우리가 써넣은 값과 지금 값이
        같을 때만 지운다 — 사용자가 그 뒤에 다시 설정했으면 그건 그의 것이다.
    #>
    [CmdletBinding()]
    param([switch]$Force)

    $outcome = [pscustomobject]@{ Removed = $false; AutostartRemoved = $false; Reason = "" }
    $ownership = Get-ExternalOverlayOwnership
    if (-not $ownership.Exists) {
        $outcome.Reason = "제거할 런타임이 없습니다."
        return $outcome
    }
    if (-not $ownership.OwnedByAmber) {
        $outcome.Reason = "이 런타임은 AMBER 가 설치한 것이 아닙니다. 직접 만드신 오버레이는 지우지 않습니다."
        return $outcome
    }

    $paths = $ownership.Paths
    $recorded = ""
    if ($ownership.Marker.PSObject.Properties.Name -contains "autostart_value") {
        $recorded = [string]$ownership.Marker.autostart_value
    }
    if ($recorded) {
        $current = $null
        try {
            $current = (Get-ItemProperty -Path $paths.RunKey -Name $paths.RunValue -ErrorAction Stop).($paths.RunValue)
        } catch {
            $current = $null
        }
        if ($current -eq $recorded) {
            Remove-ItemProperty -Path $paths.RunKey -Name $paths.RunValue -ErrorAction SilentlyContinue
            $outcome.AutostartRemoved = $true
        }
    }

    try {
        Remove-Item -LiteralPath $paths.Runtime -Recurse -Force -ErrorAction Stop
        $outcome.Removed = $true
    } catch {
        $outcome.Reason = "런타임을 지울 수 없습니다(실행 중일 수 있습니다): $($_.Exception.Message)"
    }
    return $outcome
}

function Test-ExternalOverlayRuntimeHealth {
    <#
        venv 는 자립하지 않는다. base 인터프리터가 사라지면 런타임이 깨지고,
        그 실패는 조용하다 — 자동시작은 배경 프로세스라 아무 말이 없고 Engram 은
        번들 캐릭터를 띄운다. 사용자에게는 "그냥 없어졌다" 다. 계획 §7.3.
    #>
    [CmdletBinding()]
    param()
    $ownership = Get-ExternalOverlayOwnership
    $report = [pscustomobject]@{
        Healthy = $false
        Reason  = ""
        Marker  = $ownership.Marker
    }
    if (-not $ownership.Exists) {
        $report.Reason = "런타임이 설치되어 있지 않습니다."
        return $report
    }
    $paths = $ownership.Paths
    if (-not (Test-Path -LiteralPath $paths.Windowed)) {
        $report.Reason = "런타임 실행 파일이 없습니다: $($paths.Windowed)"
        return $report
    }
    if ($ownership.OwnedByAmber -and $ownership.Marker.PSObject.Properties.Name -contains "interpreter") {
        $recorded = [string]$ownership.Marker.interpreter
        if ($recorded -and -not (Test-Path -LiteralPath $recorded)) {
            $report.Reason = "런타임이 기반으로 삼은 Python 이 사라졌습니다: $recorded"
            return $report
        }
    }
    $report.Healthy = $true
    return $report
}
