#
# deploy_agent_definitions.ps1 — 공급자별 subagent 정의 배치 (planner/coder/servant)
#
# 공급자마다 형식과 경로가 다르다. 한 파일을 세 곳에 복사하면 안 된다.
#
# 소유권 규칙 — 우리가 쓴 그대로면 갱신하고, 사용자가 손댔으면 건드리지 않는다.
#   planner/coder/servant 는 아무나 쓸 만한 이름이라 사용자가 같은 이름으로 자기
#   에이전트를 만들어 두었을 수 있다. 이전에는 Copy-Item -Force 로 무조건 덮어써서
#   그걸 백업도 경고도 없이 지웠다. 이제 우리가 배치한 내용의 해시를 provenance 로
#   남기고, 현재 파일이 그 해시와 같을 때만(= 아무도 안 고쳤을 때만) 교체한다.
#   docs/dev/external-overlay-install-plan.md §7.1 의 표식 규칙과 같은 원리다.
#
# -Force 를 주면 사용자 수정을 덮어쓰되, 먼저 .engram-bak 으로 백업한다.

param(
    [Parameter(Mandatory)][string]$ProjectRoot,
    [Parameter(Mandatory)][string]$UserProfile,
    [ValidateSet("All", "Claude", "Copilot", "Codex")][string]$Provider = "All",
    [switch]$Force
)

$agentsSourceDir = Join-Path $ProjectRoot "config\agents"
$provenancePath = Join-Path $UserProfile ".engram\agent-definitions.json"
$providers = @(
    @{
        Key = "Claude"
        Name = "Claude Code"
        SourceDir = Join-Path $agentsSourceDir "claude"
        DestinationDir = Join-Path $UserProfile ".claude\agents"
        Extension = ".md"
    },
    @{
        Key = "Copilot"
        Name = "Copilot CLI"
        SourceDir = Join-Path $agentsSourceDir "copilot"
        DestinationDir = Join-Path $UserProfile ".copilot\agents"
        Extension = ".agent.md"
    },
    @{
        Key = "Codex"
        Name = "Codex"
        SourceDir = Join-Path $agentsSourceDir "codex"
        DestinationDir = Join-Path $UserProfile ".codex\agents"
        Extension = ".toml"
    }
)

if ($Provider -ne "All") {
    $providers = @($providers | Where-Object { $_.Key -eq $Provider })
}

function Get-ContentHash([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return "" }
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash
}

function Read-Provenance([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return @{} }
    try {
        $raw = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
    } catch {
        return @{}
    }
    $map = @{}
    foreach ($property in $raw.PSObject.Properties) { $map[$property.Name] = [string]$property.Value }
    return $map
}

$provenance = Read-Provenance $provenancePath
$updated = @{}
foreach ($key in $provenance.Keys) { $updated[$key] = $provenance[$key] }

foreach ($providerSpec in $providers) {
    if (-not (Test-Path -LiteralPath $providerSpec.DestinationDir)) {
        New-Item -Path $providerSpec.DestinationDir -ItemType Directory -Force | Out-Null
    }

    foreach ($role in @("planner", "coder", "servant")) {
        $source = Join-Path $providerSpec.SourceDir ($role + $providerSpec.Extension)
        $destination = Join-Path $providerSpec.DestinationDir ($role + $providerSpec.Extension)
        if (-not (Test-Path -LiteralPath $source)) {
            throw "Managed $($providerSpec.Name) agent source not found: $source"
        }

        $currentHash = Get-ContentHash $destination
        $sourceHash = Get-ContentHash $source
        $recordedHash = if ($provenance.ContainsKey($destination)) { $provenance[$destination] } else { "" }

        # provenance 가 없던 시절에 우리가 깔아둔 파일을 입양한다. 디스크 내용이
        # 배포 원본과 바이트 동일하면 사용자 작업물이 아니다 — 기록만 없을 뿐이다.
        # 이 입양이 없으면 기존 설치본의 정의가 SKIP 으로 얼어붙어, 앞으로 정의를
        # 개선해도 소스 설치 사용자에게 도달하지 않는다.
        if ($currentHash -and -not $recordedHash -and $currentHash -eq $sourceHash) {
            $recordedHash = $currentHash
        }

        if ($currentHash -and $currentHash -ne $recordedHash) {
            # 우리가 쓴 내용이 아니다. 사용자의 것이거나 다른 도구의 것이다.
            if (-not $Force) {
                Write-Output "SKIP  $destination — 사용자가 만들거나 수정한 파일이라 건드리지 않았습니다"
                continue
            }
            $backup = "$destination.engram-bak"
            Copy-Item -LiteralPath $destination -Destination $backup -Force
            Write-Output "BACKUP $backup"
        }

        Copy-Item -LiteralPath $source -Destination $destination -Force
        $updated[$destination] = Get-ContentHash $destination
        Write-Output $destination
    }
}

New-Item -Path (Split-Path $provenancePath) -ItemType Directory -Force | Out-Null
($updated | ConvertTo-Json -Depth 3) | Set-Content -LiteralPath $provenancePath -Encoding UTF8
