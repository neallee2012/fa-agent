targetScope = 'resourceGroup'

param location string = resourceGroup().location
param identityName string = 'id-fa-behavior-recovery'
param registryName string = 'crfacontextpocdemo'
param backupAccountName string = 'stfam1backupdemo'
param backupContainerName string = 'm1-environment-backups'

module identity 'br/public:avm/res/managed-identity/user-assigned-identity:0.6.0' = {
  name: 'fa-behavior-recovery-mi'
  params: {
    name: identityName
    location: location
    enableTelemetry: false
    tags: {purpose: 'fa-quarantined-source-recovery', budget: '100-usd-total-poc'}
  }
}

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = {
  name: registryName
}
resource account 'Microsoft.Storage/storageAccounts@2023-05-01' existing = {
  name: backupAccountName
}
resource service 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' existing = {
  parent: account
  name: 'default'
}
resource container 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' existing = {
  parent: service
  name: backupContainerName
}
resource pull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, identityName, 'AcrPull')
  scope: registry
  properties: {
    principalId: identity.outputs.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d')
  }
}
resource readBackup 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(container.id, identityName, 'BlobReader')
  scope: container
  properties: {
    principalId: identity.outputs.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '2a2b9908-6ea1-4ae2-8e65-a410df84e7d1')
  }
}

output clientId string = identity.outputs.clientId
output principalId string = identity.outputs.principalId
output resourceId string = identity.outputs.resourceId
