targetScope = 'resourceGroup'

// GitHub Actions deploy identity for staging (ADR-0008, #415 W11). OIDC only; no client secret.
// Least privilege: push stage images, update the stage API app and sync job, read the stage RG and its logs.
// No Contributor, no role-assignment rights, nothing outside this RG.
// https://learn.microsoft.com/azure/developer/github/connect-from-azure-openid-connect
param location string = 'westus3'
param identityName string = 'id-fa-stage-deploy'
@description('Exact GitHub OIDC sub: repo (immutable form) + environment + ref, set by the repo sub-claim template (#415 Q4).')
param federatedSubject string
param registryName string = 'crfacontextstagedemo'
param apiAppName string = 'ca-fa-behavior-stage'
param syncJobName string = 'job-fa-agent-sync-stage'
param apiIdentityName string = 'id-facontext-api-stage'
@description('Extra Container Apps (for example the remote console) the pipeline may update; must already exist.')
param extraAppNames array = []
@description('User-assigned identities those extra apps use; updating an app needs Managed Identity Operator on them.')
param extraIdentityNames array = []
param tags object = {
  workload: 'hosted-sandbox-poc'
  environment: 'stage'
}

var roles = {
  acrPush: '8311e382-0749-4cb8-b61a-304f252e45ec'
  containerAppsContributor: '358470bc-b998-42bd-ab17-a7e34c199c0f'
  // Container Apps Contributor has no Microsoft.App/jobs/* actions; starting the job (with image/args
  // overrides) needs Microsoft.App/jobs/*/action from Container Apps Jobs Operator.
  containerAppsJobsOperator: 'b9a307c4-5aa3-4b52-ba60-2b17c136cd7b'
  managedIdentityOperator: 'f1a07417-d97a-45cb-824c-7a7467783830'
  reader: 'acdd72a7-3385-48ef-bd42-f606fba81ae7'
  logAnalyticsReader: '73c42c96-874c-492b-b04d-ab87d138a893'
}

module identity 'br/public:avm/res/managed-identity/user-assigned-identity:0.6.0' = {
  name: 'stage-deploy-identity'
  params: {
    name: identityName
    location: location
    tags: tags
    enableTelemetry: false
    federatedIdentityCredentials: [
      {
        name: 'github-staging-main'
        issuer: 'https://token.actions.githubusercontent.com'
        subject: federatedSubject
        audiences: ['api://AzureADTokenExchange']
      }
    ]
  }
}

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = {
  name: registryName
}
resource apiApp 'Microsoft.App/containerApps@2025-01-01' existing = {
  name: apiAppName
}
resource syncJob 'Microsoft.App/jobs@2025-01-01' existing = {
  name: syncJobName
}
resource apiIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' existing = {
  name: apiIdentityName
}
resource extraApps 'Microsoft.App/containerApps@2025-01-01' existing = [for name in extraAppNames: {
  name: name
}]
resource extraIdentities 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' existing = [for name in extraIdentityNames: {
  name: name
}]

resource push 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(registry.id, identityName, roles.acrPush)
  scope: registry
  properties: {
    principalId: identity.outputs.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.acrPush)
  }
}
resource apiUpdate 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(apiApp.id, identityName, roles.containerAppsContributor)
  scope: apiApp
  properties: {
    principalId: identity.outputs.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.containerAppsContributor)
  }
}
resource jobRun 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(syncJob.id, identityName, roles.containerAppsJobsOperator)
  scope: syncJob
  properties: {
    principalId: identity.outputs.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.containerAppsJobsOperator)
  }
}
resource apiIdentityUse 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(apiIdentity.id, identityName, roles.managedIdentityOperator)
  scope: apiIdentity
  properties: {
    principalId: identity.outputs.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.managedIdentityOperator)
  }
}
resource extraAppUpdate 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for (name, i) in extraAppNames: {
  name: guid(extraApps[i].id, identityName, roles.containerAppsContributor)
  scope: extraApps[i]
  properties: {
    principalId: identity.outputs.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.containerAppsContributor)
  }
}]
resource extraIdentityUse 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for (name, i) in extraIdentityNames: {
  name: guid(extraIdentities[i].id, identityName, roles.managedIdentityOperator)
  scope: extraIdentities[i]
  properties: {
    principalId: identity.outputs.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roles.managedIdentityOperator)
  }
}]
resource rgRead 'Microsoft.Authorization/roleAssignments@2022-04-01' = [for roleId in [roles.reader, roles.logAnalyticsReader]: {
  name: guid(resourceGroup().id, identityName, roleId)
  properties: {
    principalId: identity.outputs.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleId)
  }
}]

output clientId string = identity.outputs.clientId
output principalId string = identity.outputs.principalId
