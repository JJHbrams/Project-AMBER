<#
.SYNOPSIS
    build-addons.ps1 이 만든 installer\addons\addons.json 을 읽어 Inno Setup
    include 3개(tree.iss / files.iss / code.iss)를 생성한다. build-components.ps1
    의 [Code] 계약과 같은 모양을 따른다.

    .iss 쪽에서는 부모 컴포넌트 `Name: "addons"` 를 본문에서 직접 선언하고,
    이 파일이 만드는 tree.iss 는 그 자식만 추가한다. 없는 zip 은 참조하지
    않는다 — 조달 실패한 addon 은 files.iss/tree.iss 어디에도 나타나지 않는다.

    노출 함수(.iss [Code] 계약):
      function AllAddonComponentNames(): String;
      function SelectedAddonComponentIds(): String;
      procedure PrepareAddonBundle();

.EXAMPLE
    .\build-addons-includes.ps1
#>
param()

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot 'addons-schema.ps1')
$AddonsDir = Join-Path $PSScriptRoot "addons"
$ManifestPath = Join-Path $AddonsDir "addons.json"

function Write-Step($Message) { Write-Host "`n==> $Message" -ForegroundColor Cyan }
function Write-Ok($Message) { Write-Host "  [OK] $Message" -ForegroundColor Green }
function Write-Warn($Message) { Write-Host "  [!] $Message" -ForegroundColor Yellow }

function Get-EngramAddonComponentId {
    param([string]$AddonId)
    return $AddonId.Replace('-', '_')
}

function Write-EngramAddonIncludes {
    [CmdletBinding()]
    param([string]$AddonsDir = $AddonsDir, [string]$ManifestPath = $ManifestPath)
    if (-not (Test-Path -LiteralPath $ManifestPath -PathType Leaf)) {
        throw "Addon manifest not found: $ManifestPath (run build-addons.ps1 first)"
    }
    $manifest = Get-Content -LiteralPath $ManifestPath -Raw | ConvertFrom-Json -ErrorAction Stop
    if ($manifest.schema -ne $EngramAddonManifestSchema) { throw "Unsupported addons.json schema (expected $EngramAddonManifestSchema, got $($manifest.schema))." }
    $available = @($manifest.addons | Where-Object { $_.status -eq 'available' })
    foreach ($addon in $available) {
        if ($addon.name -notmatch '^[A-Za-z0-9 ()-]{1,80}$') { throw "Unsafe addon display label: $($addon.name)" }
        $zipPath = Join-Path $AddonsDir ($addon.name + '.zip')
        if (-not (Test-Path -LiteralPath $zipPath -PathType Leaf)) {
            throw "Addon marked available but zip missing: $zipPath"
        }
    }

    $tree = [Collections.Generic.List[string]]::new()
    $files = [Collections.Generic.List[string]]::new()
    $code = [Collections.Generic.List[string]]::new()

    $files.Add('Source: "addons\addons.json"; Flags: dontcopy')
    foreach ($addon in $available) {
        $componentId = Get-EngramAddonComponentId $addon.name
        $tree.Add(('Name: "addons\{0}"; Description: "{1}"' -f $componentId, $addon.name))
        $files.Add(('Source: "addons\{0}.zip"; Flags: dontcopy' -f $addon.name))
    }

    $code.Add('function AllAddonComponentNames(): String;')
    $code.Add('begin')
    $allNames = @($available | ForEach-Object { 'addons\' + (Get-EngramAddonComponentId $_.name) }) -join ','
    $code.Add(('  Result := ''{0}'';' -f $allNames))
    $code.Add('end;')

    $code.Add('function SelectedAddonComponentIds(): String;')
    $code.Add('begin')
    $code.Add('  Result := '''';')
    foreach ($addon in $available) {
        $componentId = Get-EngramAddonComponentId $addon.name
        $code.Add(('  if WizardIsComponentSelected(''addons\{0}'') then begin if Result <> '''' then Result := Result + '',''; Result := Result + ''{1}''; end;' -f $componentId, $addon.name))
    }
    $code.Add('end;')

    $code.Add('procedure PrepareAddonBundle();')
    $code.Add('begin')
    $code.Add('  ForceDirectories(ExpandConstant(''{tmp}\addon-bundle''));')
    foreach ($addon in $available) {
        $componentId = Get-EngramAddonComponentId $addon.name
        $zipName = $addon.name + '.zip'
        $code.Add(('  if WizardIsComponentSelected(''addons\{0}'') then begin' -f $componentId))
        $code.Add(('    ExtractTemporaryFile(''{0}'');' -f $zipName))
        $code.Add(('    if not FileCopy(ExpandConstant(''{{tmp}}\{0}''), ExpandConstant(''{{tmp}}\addon-bundle\{0}''), False) then' -f $zipName))
        $code.Add(('      RaiseException(''Unable to stage selected addon: {0}.'');' -f $addon.name))
        $code.Add('  end;')
    }
    $code.Add('end;')

    $utf8 = [Text.UTF8Encoding]::new($true)
    [IO.File]::WriteAllLines((Join-Path $AddonsDir 'tree.iss'), $tree, $utf8)
    [IO.File]::WriteAllLines((Join-Path $AddonsDir 'files.iss'), $files, $utf8)
    [IO.File]::WriteAllLines((Join-Path $AddonsDir 'code.iss'), $code, $utf8)
    return [pscustomobject]@{ Available = $available.Count; Total = @($manifest.addons).Count }
}

if ($MyInvocation.InvocationName -ne '.') {
    Write-Step "generating addon includes"
    $result = Write-EngramAddonIncludes
    Write-Ok "tree.iss / files.iss / code.iss written for $($result.Available)/$($result.Total) available addons"
}
