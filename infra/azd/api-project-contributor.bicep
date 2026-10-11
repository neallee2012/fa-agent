targetScope = 'resourceGroup'

// Team MCP "create a keyed connection": the API identity PUTs {project}/connections/{name} with its own token
// (src/fa-shared-context-poc/README.md, Team MCP; stage got this project-scope Contributor by hand, Neal 2026-10-06).
// Scoped to the one project, not the account or resource group.
//
// Native roleAssignment, not avm/ptn/authorization/resource-role-assignment (used for ACR pull in infra/feed-mcp and
// infra/web-console). The 0.1.2 module has optional `name` and `description` parameters (read from its main.json),
// but its default name is guid(resourceId, principalId, roleDefinitionId-as-full-ID), which differs from the name below,
// so a switch would need an explicit name to keep existing environments' assignment unchanged, and the module only
// adds a nested deployment with no behavior gain. The same native pattern as
// infra/shared-context/project-service-access.bicep, which also scopes to this project.
param accountName string
param projectName string
@description('Principal (object) ID of the API user-assigned identity.')
param principalId string

var contributor = 'b24988ac-6180-42a0-ab88-20f7382dd24c'

resource account 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = {
  name: accountName
}

resource project 'Microsoft.CognitiveServices/accounts/projects@2025-06-01' existing = {
  parent: account
  name: projectName
}

resource access 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(project.id, principalId, contributor)
  scope: project
  properties: {
    principalId: principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', contributor)
    description: 'FA Team MCP: the API identity creates keyed project connections'
  }
}
