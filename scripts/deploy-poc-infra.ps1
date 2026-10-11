param(
    [Parameter(Mandatory = $true)]
    [string]$SubscriptionId,

    [Parameter(Mandatory = $true)]
    [string]$EnvironmentName,

    [string]$Location = "westus3"
)

$ErrorActionPreference = "Stop"

if (-not $env:AZURE_CONFIG_DIR) {
    throw "AZURE_CONFIG_DIR must point to the project-specific Azure CLI profile."
}

$deploymentName = "fa-poc-$EnvironmentName"
$template = Join-Path $PSScriptRoot "..\infra\poc\main.bicep"

$outputs = az deployment sub create `
    --subscription $SubscriptionId `
    --location $Location `
    --name $deploymentName `
    --template-file $template `
    --parameters environmentName=$EnvironmentName location=$Location `
    --query properties.outputs `
    --output json | ConvertFrom-Json

if ($LASTEXITCODE -ne 0) {
    throw "POC infrastructure deployment failed."
}

$values = @{
    AZURE_RESOURCE_GROUP = $outputs.AZURE_RESOURCE_GROUP.value
    POC_STORAGE_ACCOUNT_NAME = $outputs.POC_STORAGE_ACCOUNT_NAME.value
    POC_FILE_SHARE_NAME = $outputs.POC_FILE_SHARE_NAME.value
    POC_KEY_VAULT_NAME = $outputs.POC_KEY_VAULT_NAME.value
    POC_KEY_VAULT_URL = $outputs.POC_KEY_VAULT_URL.value
    POC_STORAGE_KEY_SECRET_NAME = $outputs.POC_STORAGE_KEY_SECRET_NAME.value
}

foreach ($entry in $values.GetEnumerator()) {
    azd env set $entry.Key $entry.Value
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to set azd environment value $($entry.Key)."
    }
}

$values | ConvertTo-Json
