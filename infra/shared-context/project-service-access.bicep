targetScope = 'resourceGroup'

param accountName string
param projectName string
param principalId string

var roleIds = [
  '53ca6127-db72-4b80-b1b0-d745d6d5456d'
  '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'
]

resource account 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = {
  name: accountName
}

resource project 'Microsoft.CognitiveServices/accounts/projects@2025-06-01' existing = {
  parent: account
  name: projectName
}

resource access 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for roleId in roleIds: {
  name: guid(project.id, principalId, roleId)
  scope: project
  properties: {
    principalId: principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleId)
  }
}]
