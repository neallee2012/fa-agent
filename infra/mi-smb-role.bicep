targetScope = 'resourceGroup'

@allowed(['stfaexampledemo'])
param storageAccountName string = 'stfaexampledemo'

@allowed(['00000000-0000-0000-0000-000000000011'])
param runtimePrincipalId string = '00000000-0000-0000-0000-000000000011'

var miAdminRoleId = 'a235d3ee-5935-4cfb-8cc5-a3303ad5995e'

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' existing = {
  name: storageAccountName
}

resource runtimeSmbRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, runtimePrincipalId, miAdminRoleId)
  scope: storage
  properties: {
    principalId: runtimePrincipalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', miAdminRoleId)
  }
}

output roleAssignmentId string = runtimeSmbRole.id
