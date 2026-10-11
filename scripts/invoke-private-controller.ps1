param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('Bootstrap', 'Deploy', 'G0', 'G3', 'All', 'Collect')]
    [string]$Action,
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[a-zA-Z0-9-]+$')]
    [string]$VmName,
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^https://[a-z0-9-]+\.services\.ai\.azure\.com/api/projects/[a-zA-Z0-9_-]+$')]
    [string]$ProjectEndpoint,
    [string]$SubscriptionId = '00000000-0000-0000-0000-000000000012',
    [string]$ResourceGroup = 'rg-faexample',
    [ValidatePattern('^[0-9]+$')]
    [string]$AgentVersion,
    [ValidatePattern('^[a-zA-Z0-9_-]+$')]
    [string]$RunId = ('vnet-' + [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ')),
    [ValidatePattern('^[a-zA-Z0-9_-]+$')]
    [string]$CollectRunId,
    [ValidateSet('account_key', 'managed_identity')]
    [string]$SmbAuthMode = 'account_key',
    [ValidatePattern('^[a-z0-9]+\.azurecr\.io/[a-zA-Z0-9_./-]+@sha256:[0-9a-f]{64}$')]
    [string]$Image = 'crcontosodemo01.azurecr.io/fa-hosted-sandbox-poc/fa-hosted-sandbox-poc-faexample@sha256:d6f8f7134abc4b2cf5089ecc2c5e1de2a6cf1b189bd962afb547f668c51bab01'
)

$ErrorActionPreference = 'Stop'
if (-not $env:AZURE_CONFIG_DIR) {
    throw 'Set the project-isolated AZURE_CONFIG_DIR before using this script.'
}
$workspace = Split-Path $PSScriptRoot -Parent
$localState = Join-Path $workspace '.azure\vnet-controller'
New-Item -ItemType Directory -Path $localState -Force | Out-Null
$utf8 = New-Object System.Text.UTF8Encoding($false)
$accountRaw = az account show --subscription $SubscriptionId -o json
if ($LASTEXITCODE -ne 0) { throw 'Unable to read the isolated Azure account.' }
$account = $accountRaw | ConvertFrom-Json
if ($account.tenantId -ne '00000000-0000-0000-0000-000000000013') {
    throw 'The Azure tenant differs from the approved POC tenant.'
}

function Invoke-ControllerScript([string]$Body, [string]$Label) {
    $prefix = @'
#!/bin/sh
set -eu
umask 077
trap 'code=$?; printf "\nFA_POC_EXIT=%s\n" "$code"' EXIT
'@
    $scriptPath = Join-Path $localState ($Label + '.sh')
    [IO.File]::WriteAllText($scriptPath, ($prefix + "`n" + $Body).Replace("`r`n", "`n"), $utf8)
    $raw = az vm run-command invoke --subscription $SubscriptionId --resource-group $ResourceGroup `
        --name $VmName --command-id RunShellScript --scripts "@$scriptPath" -o json
    if ($LASTEXITCODE -ne 0) { throw "ARM Run Command failed: $Label" }
    [IO.File]::WriteAllText((Join-Path $localState ($Label + '-response.json')), ($raw -join "`n"), $utf8)
    $result = $raw | ConvertFrom-Json
    $stdout = ($result.value | Where-Object { $_.code -like '*StdOut*' } | ForEach-Object { $_.message }) -join "`n"
    $stderr = ($result.value | Where-Object { $_.code -like '*StdErr*' } | ForEach-Object { $_.message }) -join "`n"
    if (-not ($result.value | Where-Object { $_.code -like '*StdOut*' })) {
        foreach ($entry in $result.value) {
            if ($entry.message -match '(?s)\[stdout\]\r?\n(?<stdout>.*?)\r?\n\[stderr\]\r?\n(?<stderr>.*)\z') {
                $stdout += $Matches['stdout']
                $stderr += $Matches['stderr']
            }
        }
    }
    if ($stdout -notmatch '(?m)^FA_POC_EXIT=(\d+)\s*$') {
        throw "Run Command output is incomplete ($Label): $stderr"
    }
    if ([int]$Matches[1] -ne 0) {
        throw "Private controller failed ($Label): $stdout`n$stderr"
    }
    if ($stderr) { Write-Warning $stderr }
    return ($stdout -replace '(?m)^FA_POC_EXIT=\d+\s*$', '').Trim()
}

if ($Action -eq 'Bootstrap') {
    $archive = Join-Path $localState 'controller-upload.zip'
    $files = @('run_poc.py', 'private_controller.py', 'g3_only.py', 'requirements.txt') |
        ForEach-Object { Join-Path $workspace ('controller\' + $_) }
    Compress-Archive -Path $files -DestinationPath $archive -Force
    $encoded = [Convert]::ToBase64String([IO.File]::ReadAllBytes($archive))
    $body = @'
root=/opt/fasample-controller
mkdir -p "$root/app" "$root/evidence"
printf '%s' '__ARCHIVE__' | base64 -d > "$root/controller-upload.zip"
if ! command -v python3 >/dev/null 2>&1; then
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3
fi
if ! python3 -c 'import ensurepip' >/dev/null 2>&1; then
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3-venv
fi
python3 -m zipfile -e "$root/controller-upload.zip" "$root/app"
if [ ! -x "$root/venv/bin/python" ]; then
    python3 -m venv "$root/venv"
fi
if ! "$root/venv/bin/python" -c 'import importlib.metadata as m; from pathlib import Path; rows=Path("/opt/fasample-controller/app/requirements.txt").read_text().splitlines(); assert all(m.version(name)==version for name,version in (row.split("==") for row in rows if row and not row.startswith("#")))' >/dev/null 2>&1; then
    "$root/venv/bin/python" -m pip install --quiet -r "$root/app/requirements.txt"
fi
"$root/venv/bin/python" -m compileall -q "$root/app"
printf '{"bootstrap":"ready"}\n'
'@
    Invoke-ControllerScript $body.Replace('__ARCHIVE__', $encoded) 'bootstrap'
    exit 0
}

if ($Action -in @('Deploy', 'G0', 'G3', 'All')) {
    if ($Action -ne 'Deploy' -and -not $AgentVersion) { throw 'G0/G3/All requires -AgentVersion.' }
    $verb = $Action.ToLowerInvariant()
    $command = "/opt/fasample-controller/venv/bin/python /opt/fasample-controller/app/private_controller.py $verb --project-endpoint '$ProjectEndpoint' --image '$Image' --smb-auth-mode '$SmbAuthMode' --run-id '$RunId' --output /opt/fasample-controller/evidence"
    if ($AgentVersion) { $command += " --agent-version '$AgentVersion'" }
    Invoke-ControllerScript $command ($verb + '-' + $RunId)
    exit 0
}

$export = @'
python3 - <<'PY'
import hashlib, json, re, zipfile
from pathlib import Path
root = Path("/opt/fasample-controller")
evidence = root / "evidence"
selected_run = "__COLLECT_RUN_ID__"
if selected_run:
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", selected_run):
        raise ValueError("Invalid collection run ID")
    selected = evidence / selected_run
    if selected.is_symlink() or not selected.is_dir():
        raise ValueError("Requested evidence run is missing or is a symlink")
    paths = list(evidence.glob("*")) + list(selected.rglob("*"))
else:
    paths = list(evidence.rglob("*"))
with zipfile.ZipFile(root / "evidence-export.zip", "w", zipfile.ZIP_DEFLATED) as archive:
    for path in sorted(paths):
        if path.is_file() and not path.is_symlink():
            archive.write(path, path.relative_to(evidence))
data = (root / "evidence-export.zip").read_bytes()
print(json.dumps({"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}))
PY
'@
$export = $export.Replace('__COLLECT_RUN_ID__', $CollectRunId)
$metadata = (Invoke-ControllerScript $export ('collect-' + $RunId)) | ConvertFrom-Json
if ($metadata.size -gt 10485760) { throw 'Evidence archive exceeds the 10 MiB bounded transfer limit.' }
$buffer = New-Object System.IO.MemoryStream
try {
    $chunkSize = 2100
    for ($offset = 0; $offset -lt $metadata.size; $offset += $chunkSize) {
        $chunkCommand = "python3 -c `"import base64; f=open('/opt/fasample-controller/evidence-export.zip','rb'); f.seek($offset); print(base64.b64encode(f.read($chunkSize)).decode())`""
        $chunk = Invoke-ControllerScript $chunkCommand ('chunk-' + $RunId + '-' + $offset)
        $bytes = [Convert]::FromBase64String($chunk)
        $expected = [Math]::Min($chunkSize, $metadata.size - $offset)
        if ($bytes.Length -ne $expected) { throw "Incomplete evidence chunk at $offset." }
        $buffer.Write($bytes, 0, $bytes.Length)
    }
    $data = $buffer.ToArray()
} finally {
    $buffer.Dispose()
}
$sha = [Security.Cryptography.SHA256]::Create()
try { $actualHash = ([BitConverter]::ToString($sha.ComputeHash($data))).Replace('-', '').ToLowerInvariant() }
finally { $sha.Dispose() }
if ($actualHash -ne $metadata.sha256) { throw 'Evidence archive SHA-256 differs from the private controller.' }
$destination = Join-Path $workspace ('evidence\private-controller-' + $RunId)
if (Test-Path $destination) { throw "Refusing to overwrite prior collected evidence: $destination" }
New-Item -ItemType Directory -Path $destination | Out-Null
$localArchive = Join-Path $destination 'evidence-export.zip'
[IO.File]::WriteAllBytes($localArchive, $data)
Add-Type -AssemblyName System.IO.Compression.FileSystem
$zip = [IO.Compression.ZipFile]::OpenRead($localArchive)
try {
    foreach ($entry in $zip.Entries) {
        $target = [IO.Path]::GetFullPath((Join-Path $destination $entry.FullName.Replace('/', '\')))
        if (-not $target.StartsWith($destination + '\', [StringComparison]::OrdinalIgnoreCase)) {
            throw "Archive entry escapes the evidence directory: $($entry.FullName)"
        }
    }
} finally { $zip.Dispose() }
Expand-Archive -Path $localArchive -DestinationPath $destination
Write-Output "Collected evidence: $destination; SHA-256: $actualHash"
