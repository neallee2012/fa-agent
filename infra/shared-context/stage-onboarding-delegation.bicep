// #468 (Neal 2026-10-03 00:12): the stage API UAMI may only ADD Foundry User / Cognitive Services OpenAI User
// assignments for service principals on the stage project, so "+" can give a new Agent identity its #306 roles.
// Deploy to the Foundry account's resource group (rg-faexample) with a `stage-` deployment name; the
// stage project is the scope, nothing else in that resource group changes.
//
// Not AVM: avm/ptn/authorization/role-assignment takes `condition` but only at management group, subscription or
// resource group scope; avm/ptn/authorization/resource-role-assignment takes a resource scope but no `condition`.
// The same native resource as project-service-access.bicep.
//
// Condition, official format and the "constrain roles and principal types" example
// (https://learn.microsoft.com/azure/role-based-access-control/delegate-role-assignments-examples#example-constrain-roles-and-principal-types,
// https://learn.microsoft.com/azure/role-based-access-control/conditions-format):
// - write: only the two roles, only principalType ServicePrincipal.
// - delete: refused for every assignment. Each clause needs an expression, so the delete clause compares the
//   target's role with the all-zero GUID, which no role definition has.
targetScope = 'resourceGroup'

param accountName string
param projectName string
@description('Principal (object) ID of the stage API user-assigned identity.')
param principalId string

var rbacAdministrator = 'f58310d9-a9f6-439a-9e8d-f62e7b41a168'
var foundryUser = '53ca6127-db72-4b80-b1b0-d745d6d5456d'
var openAiUser = '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'
var never = '00000000-0000-0000-0000-000000000000'

var condition = '((!(ActionMatches{\'Microsoft.Authorization/roleAssignments/write\'})) OR (@Request[Microsoft.Authorization/roleAssignments:RoleDefinitionId] ForAnyOfAnyValues:GuidEquals {${foundryUser}, ${openAiUser}} AND @Request[Microsoft.Authorization/roleAssignments:PrincipalType] ForAnyOfAnyValues:StringEqualsIgnoreCase {\'ServicePrincipal\'})) AND ((!(ActionMatches{\'Microsoft.Authorization/roleAssignments/delete\'})) OR (@Resource[Microsoft.Authorization/roleAssignments:RoleDefinitionId] ForAnyOfAnyValues:GuidEquals {${never}}))'

resource account 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = {
  name: accountName
}

resource project 'Microsoft.CognitiveServices/accounts/projects@2025-06-01' existing = {
  parent: account
  name: projectName
}

resource delegation 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(project.id, principalId, rbacAdministrator)
  scope: project
  properties: {
    principalId: principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', rbacAdministrator)
    condition: condition
    conditionVersion: '2.0'
    description: 'FA #468: stage onboarding may only add Foundry User / OpenAI User for service principals; no deletes'
  }
}

output roleAssignmentId string = delegation.id
output condition string = condition
