targetScope = 'resourceGroup'

// The AVM storage-account module owns the account and blob-service configuration.
// This additive deployment owns only a new container and its role assignment.
// Do not redeploy M1's AVM parent or change its versioning, network, or backups.
param storageName string = 'stfam1backupdemo'
param containerName string = 'm3-artifacts'
param apiIdentityName string = 'id-facontext-api'
param readWriteRoleDefinitionGuid string = '00000000-0000-0000-0000-000000000057'

resource storage 'Microsoft.Storage/storageAccounts@2025-01-01' existing = {
  name: storageName
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2025-01-01' existing = {
  parent: storage
  name: 'default'
}

resource apiIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' existing = {
  name: apiIdentityName
}

resource artifacts 'Microsoft.Storage/storageAccounts/blobServices/containers@2025-01-01' = {
  parent: blobService
  name: containerName
  properties: {
    publicAccess: 'None'
  }
}

resource apiAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(artifacts.id, apiIdentity.id, readWriteRoleDefinitionGuid)
  scope: artifacts
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', readWriteRoleDefinitionGuid)
    principalId: apiIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

output containerId string = artifacts.id
output roleAssignmentId string = apiAccess.id
