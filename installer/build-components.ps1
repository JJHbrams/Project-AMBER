# Mechanical manifest -> Inno include generation. Called only by build-installer.
function Write-EngramComponentIncludes {
    param([Parameter(Mandatory)][string]$BundleRoot)
    . (Join-Path $PSScriptRoot 'external-components.ps1')
    $manifestPath = Join-Path $BundleRoot 'engram-overlay-components.json'
    Assert-EngramComponentPin $manifestPath
    $plan = Get-EngramComponentPlan -ManifestPath $manifestPath
    $manifest = $plan.Manifest
    $tree = [Collections.Generic.List[string]]::new()
    $files = [Collections.Generic.List[string]]::new()
    $code = [Collections.Generic.List[string]]::new()
    $files.Add('Source: "external-components\engram-overlay-components.json"; Flags: dontcopy')
    foreach ($artifact in @($manifest.common, $manifest.sdk) + @($manifest.payloads)) {
        Test-EngramComponentArtifact $BundleRoot $artifact | Out-Null
        $files.Add(('Source: "external-components\{0}"; Flags: dontcopy' -f $artifact.file.Replace('/', '\')))
    }
    foreach ($group in @('2D','3D')) {
        $tree.Add(('Name: "external\{0}"; Description: "{1} overlays"' -f $group.ToLowerInvariant(), $group))
        foreach ($component in $manifest.components | Where-Object { $_.group -eq $group }) {
            if ($component.display_name -notmatch '^[A-Za-z0-9 ()-]{1,80}$') { throw 'Unsafe installer display label.' }
            $tree.Add(('Name: "external\{0}\{1}"; Description: "{2}"' -f $group.ToLowerInvariant(), $component.id.Replace('-', '_'), $component.display_name))
        }
    }
    $code.Add('function ExternalOverlayComponentsCode(): String;')
    $code.Add('begin')
    $code.Add('  Result := '''';')
    $code.Add('  if PreserveUserOwnedExternalDefault then Exit;')
    foreach ($component in $manifest.components) {
        $code.Add(('  if WizardIsComponentSelected(''external\{0}\{1}'') then begin if Result <> '''' then Result := Result + '',''; Result := Result + ''{2}''; end;' -f $component.group.ToLowerInvariant(), $component.id.Replace('-', '_'), $component.id))
    }
    $code.Add('end;')
    $code.Add('function AllExternalComponentNames(): String;')
    $code.Add('begin')
    $allNames = @($manifest.components | ForEach-Object { 'external\' + $_.group.ToLowerInvariant() + '\' + $_.id.Replace('-', '_') }) -join ','
    $code.Add(('  Result := ''{0}'';' -f $allNames))
    $code.Add('end;')
    $code.Add('procedure PrepareExternalComponentBundle();')
    $code.Add('begin')
    $code.Add('  if (ExternalOverlayComponentsCode() = '''') and not WizardIsComponentSelected(''sdk'') then Exit;')
    $code.Add('  ForceDirectories(ExpandConstant(''{tmp}\component-bundle\payloads''));')
    $code.Add('  ExtractComponentArtifact(''engram-overlay-components.json'', ''engram-overlay-components.json'');')
    $code.Add(('  if WizardIsComponentSelected(''sdk'') then ExtractComponentArtifact(''{0}'', ''{0}'');' -f $manifest.sdk.file))
    $code.Add('  if ExternalOverlayComponentsCode() = '''' then Exit;')
    $code.Add(('  ExtractComponentArtifact(''{0}'', ''{0}'');' -f $manifest.common.file))
    foreach ($payload in $manifest.payloads) {
        $requiring = @()
        foreach ($component in $manifest.components) {
            $single = Get-EngramComponentPlan -ManifestPath $manifestPath -Components $component.id
            if ($payload.id -in $single.InstalledPayloads) { $requiring += $component }
        }
        $conditions = @($requiring | ForEach-Object { "WizardIsComponentSelected('external\$($_.group.ToLowerInvariant())\$($_.id.Replace('-', '_'))') or ExistingExternalComponent('$($_.id)')" })
        $code.Add(('  if {0} then ExtractComponentArtifact(''{1}'', ''{2}'');' -f ($conditions -join ' or '), (Split-Path $payload.file -Leaf), $payload.file.Replace('/', '\')))
    }
    $code.Add('end;')
    $utf8 = [Text.UTF8Encoding]::new($true)
    [IO.File]::WriteAllLines((Join-Path $BundleRoot 'tree.iss'), $tree, $utf8)
    [IO.File]::WriteAllLines((Join-Path $BundleRoot 'files.iss'), $files, $utf8)
    [IO.File]::WriteAllLines((Join-Path $BundleRoot 'code.iss'), $code, $utf8)
}
