param(
    [Parameter(Mandatory = $true)]
    [string]$SubscriptionId,

    [Parameter(Mandatory = $true)]
    [string]$ResourceGroupName,

    [Parameter(Mandatory = $true)]
    [string]$KeyVaultName,

    [Parameter(Mandatory = $true)]
    [string]$AgentPrincipalId
)

$ErrorActionPreference = "Stop"

if (-not $env:AZURE_CONFIG_DIR) {
    throw "AZURE_CONFIG_DIR must point to the project-specific Azure CLI profile."
}

$vaultId = az keyvault show `
    --subscription $SubscriptionId `
    --resource-group $ResourceGroupName `
    --name $KeyVaultName `
    --query id `
    --output tsv

if ($LASTEXITCODE -ne 0 -or -not $vaultId) {
    throw "Unable to resolve Key Vault resource ID."
}

az role assignment create `
    --subscription $SubscriptionId `
    --assignee-object-id $AgentPrincipalId `
    --assignee-principal-type ServicePrincipal `
    --role "Key Vault Secrets User" `
    --scope $vaultId `
    --output none

if ($LASTEXITCODE -ne 0) {
    throw "Failed to grant Key Vault Secrets User to the Hosted agent identity."
}

Write-Output "Granted Key Vault Secrets User on $vaultId."
