// ADR-0006 (Neal 2026-10-24): with Cosmos memory (ADR-0011) the API identity may also ADD Cognitive Services OpenAI
// User for service principals on the Foundry account, so a "+" Agent can embed at the account root
// (/openai/v1/embeddings, #971; the project path 404s). Only that one role; the project-scope #468 delegation
// (stage-onboarding-delegation.bicep) is unchanged.
//
// The same native resource and condition format as stage-onboarding-delegation.bicep (see its notes on AVM and the
// official condition examples): write only OpenAI User, only principalType ServicePrincipal; delete refused for every
// assignment (compared with the all-zero GUID, which no role definition has).
targetScope = 'resourceGroup'

param accountName string
@description('Principal (object) ID of the API user-assigned identity.')
param principalId string
param assignmentDescription string

var rbacAdministrator = 'f58310d9-a9f6-439a-9e8d-f62e7b41a168'
var openAiUser = '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'
var never = '00000000-0000-0000-0000-000000000000'

var condition = '((!(ActionMatches{\'Microsoft.Authorization/roleAssignments/write\'})) OR (@Request[Microsoft.Authorization/roleAssignments:RoleDefinitionId] ForAnyOfAnyValues:GuidEquals {${openAiUser}} AND @Request[Microsoft.Authorization/roleAssignments:PrincipalType] ForAnyOfAnyValues:StringEqualsIgnoreCase {\'ServicePrincipal\'})) AND ((!(ActionMatches{\'Microsoft.Authorization/roleAssignments/delete\'})) OR (@Resource[Microsoft.Authorization/roleAssignments:RoleDefinitionId] ForAnyOfAnyValues:GuidEquals {${never}}))'

resource account 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = {
  name: accountName
}

resource delegation 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(account.id, principalId, rbacAdministrator)
  scope: account
  properties: {
    principalId: principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', rbacAdministrator)
    condition: condition
    conditionVersion: '2.0'
    description: assignmentDescription
  }
}

output roleAssignmentId string = delegation.id
output condition string = condition
