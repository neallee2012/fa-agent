targetScope = 'resourceGroup'

// Deploy into the shared account's RG. Creates only the stage project and its role assignments (ADR-0008).
// Never redeploy foundry.bicep for this: it declares the whole shared account (models, capacity, settings).
// Not AVM: avm/res/cognitive-services/account/project is still "Proposed" in the AVM index, so the
// project uses the native resource exactly like foundry.bicep.
param location string = 'westus3'
param accountName string = 'cog-fasample-memory-demo'
param projectName string = 'faexample-stage'
param operatorObjectId string

var foundryUserRoleId = '53ca6127-db72-4b80-b1b0-d745d6d5456d'
var openAiUserRoleId = '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'

resource account 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = {
  name: accountName
}

resource project 'Microsoft.CognitiveServices/accounts/projects@2025-06-01' = {
  parent: account
  name: projectName
  location: location
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    displayName: projectName
    description: 'Stage environment (ADR-0008); data never shared with the dev project'
  }
}

// Operator: create agents, memory stores and the toolbox in this project only. Foundry User is enough for
// toolbox versions (https://learn.microsoft.com/azure/foundry/agents/how-to/tools/toolbox, Prerequisites);
// control-plane writes such as connections use the operator's existing subscription role.
resource operatorAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for roleId in [foundryUserRoleId, openAiUserRoleId]: {
  name: guid(project.id, operatorObjectId, roleId)
  scope: project
  properties: {
    principalId: operatorObjectId
    principalType: 'User'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleId)
  }
}]

// Same as dev foundry.bicep projectMemoryAccess: the project identity needs Foundry User on the account for Memory.
module projectMemoryAccess './account-access.bicep' = {
  name: 'stage-project-memory-access'
  params: {
    accountName: accountName
    principalId: project.identity.principalId
    roleId: foundryUserRoleId
  }
}

output projectId string = project.id
output projectPrincipalId string = project.identity.principalId
output projectEndpoint string = 'https://${accountName}.services.ai.azure.com/api/projects/${projectName}'
