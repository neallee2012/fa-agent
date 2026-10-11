[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('build', 'identity', 'verify')]
    [string]$Action,
    [string]$Config,
    [string]$OutputDirectory,
    [string]$AzConfigDirectory,
    [string]$SchemaFile,
    [string]$Python = 'python'
)

$ErrorActionPreference = 'Stop'
$root = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$source = Join-Path $root 'src\fa-shared-context-poc'
$oldPythonPath = $env:PYTHONPATH
try {
    $env:PYTHONPATH = $source
    if ($oldPythonPath) { $env:PYTHONPATH += [IO.Path]::PathSeparator + $oldPythonPath }
    $arguments = @('-m', 'shared_context.teams_operator', $Action)
    if ($Config) { $arguments += @('--config', $Config) }
    if ($OutputDirectory) { $arguments += @('--output', $OutputDirectory) }
    if ($AzConfigDirectory) { $arguments += @('--az-config-dir', $AzConfigDirectory) }
    if ($SchemaFile) { $arguments += @('--schema-file', $SchemaFile) }
    & $Python @arguments
    if ($LASTEXITCODE -ne 0) { throw 'Teams operator action failed; no cloud write fallback is available.' }
} finally {
    $env:PYTHONPATH = $oldPythonPath
}
