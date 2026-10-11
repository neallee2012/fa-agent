$ErrorActionPreference = 'Stop'
$source = Join-Path (Split-Path $PSScriptRoot -Parent) 'scripts\invoke-private-controller.ps1'
$ast = [System.Management.Automation.Language.Parser]::ParseFile($source, [ref]$null, [ref]$null)
$function = $ast.Find({
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -eq 'Invoke-ControllerScript'
}, $true)
. ([scriptblock]::Create($function.Extent.Text))
$localState = Join-Path ([IO.Path]::GetTempPath()) ('fasample-transport-' + [guid]::NewGuid())
New-Item -ItemType Directory -Path $localState | Out-Null
$utf8 = New-Object System.Text.UTF8Encoding($false)

function az {
    $global:LASTEXITCODE = 0
    $script:response | ConvertTo-Json -Depth 5
}

function Assert-Result([string]$Label, [string]$Expected, [bool]$MustFail = $false) {
    try {
        $actual = Invoke-ControllerScript 'true' $Label
    } catch {
        if ($MustFail) { return }
        throw
    }
    if ($MustFail) { throw "$Label accepted an invalid result" }
    if ($actual -ne $Expected) { throw "$Label returned unexpected output: $actual" }
}

try {
    $script:response = @{ value = @(@{
        code = 'ProvisioningState/succeeded'
        message = "Enable succeeded: `n[stdout]`n{`"bootstrap`":`"ready`"}`n`nFA_POC_EXIT=0`n`n[stderr]`n"
    }) }
    Assert-Result 'combined' '{"bootstrap":"ready"}'

    $script:response = @{ value = @(
        @{ code = 'ComponentStatus/StdOut/succeeded'; message = "ready`nFA_POC_EXIT=0" },
        @{ code = 'ComponentStatus/StdErr/succeeded'; message = '' }
    ) }
    Assert-Result 'separate' 'ready'

    $script:response = @{ value = @(@{
        code = 'ProvisioningState/succeeded'
        message = "Enable succeeded: `n[stdout]`nFA_POC_EXIT=7`n`n[stderr]`nprobe failed"
    }) }
    Assert-Result 'nonzero' '' $true

    $script:response = @{ value = @(@{
        code = 'ProvisioningState/succeeded'; message = "Enable succeeded: `n[stdout]`nready`n[stderr]`n"
    }) }
    Assert-Result 'missing-marker' '' $true
    Write-Output 'PASS: 4 private controller transport cases'
} finally {
    Get-ChildItem -LiteralPath $localState -File | Remove-Item
    Remove-Item -LiteralPath $localState
}
