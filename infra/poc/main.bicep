targetScope = 'subscription'

@minLength(3)
@maxLength(24)
param environmentName string

param location string = 'westus3'

param storageAccountName string = take(
  replace(toLower('st${environmentName}${uniqueString(subscription().id, location)}'), '-', ''),
  24
)

param keyVaultName string = take(
  toLower('kv-${environmentName}-${uniqueString(subscription().id, location)}'),
  24
)

param fileShareName string = 'team-poc'
param storageKeySecretName string = 'azure-files-account-key'

var resourceGroupName = 'rg-${environmentName}'
var tags = {
  'azd-env-name': environmentName
  component: 'shared-poc-support'
  workload: 'hosted-sandbox-poc'
  dataClassification: 'synthetic-only'
}

resource resourceGroup 'Microsoft.Resources/resourceGroups@2024-11-01' = {
  name: resourceGroupName
  location: location
  tags: tags
}

module keyVault 'br/public:avm/res/key-vault/vault:0.14.0' = {
  name: 'poc-key-vault'
  scope: resourceGroup
  params: {
    name: keyVaultName
    location: location
    tags: tags
    enablePurgeProtection: false
    enableRbacAuthorization: true
    enableSoftDelete: true
    enableVaultForDeployment: false
    enableVaultForDiskEncryption: false
    enableVaultForTemplateDeployment: true
    networkAcls: {
      bypass: 'AzureServices'
      defaultAction: 'Allow'
    }
    publicNetworkAccess: 'Enabled'
    sku: 'standard'
    softDeleteRetentionInDays: 7
  }
}

module storage 'br/public:avm/res/storage/storage-account:0.33.0' = {
  name: 'poc-storage'
  scope: resourceGroup
  params: {
    name: storageAccountName
    location: location
    tags: tags
    allowBlobPublicAccess: false
    allowSharedKeyAccess: true
    kind: 'StorageV2'
    minimumTlsVersion: 'TLS1_2'
    networkAcls: {
      bypass: 'AzureServices'
      defaultAction: 'Allow'
    }
    publicNetworkAccess: 'Enabled'
    requireInfrastructureEncryption: true
    skuName: 'Standard_LRS'
    fileServices: {
      shares: [
        {
          accessTier: 'Hot'
          name: fileShareName
          shareQuota: 5
        }
      ]
    }
    secretsExportConfiguration: {
      accessKey1Name: storageKeySecretName
      keyVaultResourceId: keyVault.outputs.resourceId
    }
  }
}

output AZURE_RESOURCE_GROUP string = resourceGroup.name
output POC_STORAGE_ACCOUNT_NAME string = storageAccountName
output POC_FILE_SHARE_NAME string = fileShareName
output POC_KEY_VAULT_NAME string = keyVaultName
output POC_KEY_VAULT_URL string = 'https://${keyVaultName}${environment().suffixes.keyvaultDns}'
output POC_STORAGE_KEY_SECRET_NAME string = storageKeySecretName
output POC_STORAGE_ACCOUNT_ID string = storage.outputs.resourceId
output POC_KEY_VAULT_ID string = keyVault.outputs.resourceId
