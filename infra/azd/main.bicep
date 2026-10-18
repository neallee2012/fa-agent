targetScope = 'subscription'

// `azd provision` entry for a new FA environment (docs/azd-new-environment.md). It only composes the reviewed
// infra/shared-context modules in the order of the manual standard flow (deploy/README.md#new-environment-standard-flow):
// Foundry -> monitoring -> foundation -> Web PubSub -> backup role/storage -> M3 artifacts. The monitoring workspace is
// passed to the foundation as an output, so ARM orders and verifies it instead of a manual readback.
// Apps, the sync Job, the Portal and Hosted Agents need images and the Entra app; the postdeploy hook creates them.
// Nothing here names a tenant, subscription, principal or another environment's resource.

@minLength(2)
@maxLength(20)
@description('azd environment name (AZURE_ENV_NAME); lowercase letters, digits and hyphens. Prefix of the RG-scoped names.')
param environmentName string

// azd's region picker lists only these, and hides the ones without the embedding quota memory needs.
// The Hosted Agents regions of https://learn.microsoft.com/azure/foundry/agents/concepts/hosted-agents (2026-09-29).
@allowed([
  'australiaeast'
  'brazilsouth'
  'canadacentral'
  'canadaeast'
  'centralus'
  'eastus'
  'eastus2'
  'francecentral'
  'germanywestcentral'
  'italynorth'
  'japaneast'
  'japanwest'
  'koreacentral'
  'northcentralus'
  'norwayeast'
  'polandcentral'
  'southafricanorth'
  'southcentralus'
  'southindia'
  'southeastasia'
  'spaincentral'
  'swedencentral'
  'switzerlandnorth'
  'switzerlandwest'
  'uaenorth'
  'uksouth'
  'ukwest'
  'westcentralus'
  'westeurope'
  'westus'
  'westus3'
])
@metadata({
  azd: {
    type: 'location'
    usageName: [
      'OpenAI.GlobalStandard.text-embedding-3-small,10'
    ]
  }
})
@description('Region of every resource; it must offer Foundry Hosted Agents. The chat models are chosen per region (FA_MODELS).')
param location string

@description('Signed-in deployer (azd AZURE_PRINCIPAL_ID, a user): Foundry User and OpenAI User on the project, like the manual operator.')
param principalId string

@description('Empty uses rg-<environmentName>.')
param resourceGroupName string = ''

@description('JSON object deployment name -> capacity overriding src/fa-shared-context-poc/model-catalog.json; the preprovision hook rejects names outside the catalog.')
param modelCapacityJson string = ''

@description('Comma-separated catalog deployments to create (FA_MODEL_DEPLOYMENTS, written by the preprovision hook from FA_MODELS); empty creates every catalog entry.')
param modelDeploymentNames string = ''

@allowed(['NoAutoUpgrade', 'OnceNewDefaultVersionAvailable', 'OnceCurrentVersionExpired'])
param modelVersionUpgradeOption string = 'NoAutoUpgrade'

@description('Opt-in NAT egress for snet-api (private-state-network.bicep); keep true once deployed with it.')
param apiSubnetNatEgress bool = false

@description('Give the API identity the #468 constrained RBAC Administrator on the project so "+" can assign new Agent roles. On by default (Neal 2026-10-15); opt out with `azd env set FA_ENABLE_AGENT_ONBOARDING false`.')
param enableAgentOnboarding bool = true

@description('Give the API identity project-scope Contributor so Team MCP can create keyed connections (stage has it). On by default.')
param enableTeamMcpConnections bool = true

@description('ADR-0009 phase 5 (Neal 2026-10-17): a dedicated snapshot storage account, private endpoint and API role. Off by default; creating them is a cloud and IAM write that needs approval from Neal per deploy.')
param deployUpgradeSnapshots bool = false

var env = toLower(environmentName)
// Globally unique names (storage, registry, Foundry subdomain, Web PubSub) use a per subscription/env/region token.
var token = toLower(uniqueString(subscription().id, env, location))
var rgName = empty(resourceGroupName) ? 'rg-${env}' : resourceGroupName
var tags = {
  'azd-env-name': environmentName
  workload: 'foundry-agents'
  architecture: 'shared-context'
  environment: env
}
var names = {
  foundryAccount: 'cog-${token}'
  project: 'fa-${env}'
  registry: 'cr${token}'
  containerAppsEnvironment: 'cae-${env}'
  apiIdentity: 'id-fa-api-${env}'
  storage: 'st${token}'
  backupStorage: 'stbk${token}'
  vnet: 'vnet-${env}'
  apiApp: 'ca-api-${env}'
  syncJob: 'job-sync-${env}'
  webpubsub: 'wps-${token}'
  logAnalytics: 'log-${env}'
  appInsights: 'appi-${env}'
  backupPrivateEndpoint: 'pe-${env}-backup-blob'
  consoleApp: 'ca-console-${env}'
  consoleIdentity: 'id-fa-console-${env}'
  consoleStorage: 'stcon${token}'
  snapshotStorage: 'stsnap${token}'
  snapshotPrivateEndpoint: 'pe-${env}-upgrade-snapshot-blob'
}
var tableName = 'workstate'
var backupContainer = 'm1-environment-backups'
var artifactsContainer = 'm3-artifacts'

// One deployment per chosen catalog entry, the same rule as fa_azd.model_deployments.
var catalog = loadJsonContent('../../src/fa-shared-context-poc/model-catalog.json')
var chosen = empty(modelDeploymentNames) ? map(catalog, item => item.deployment) : split(modelDeploymentNames, ',')
var chosenCatalog = filter(catalog, item => contains(chosen, item.deployment))
var capacity = empty(modelCapacityJson) ? {} : json(modelCapacityJson)
var modelDeployments = [for item in chosenCatalog: {
  name: item.deployment
  model: {
    format: item.model.format
    name: item.model.name
    version: item.model.version
  }
  sku: {
    name: item.sku.name
    capacity: capacity[?item.deployment] ?? item.sku.capacity
  }
  versionUpgradeOption: modelVersionUpgradeOption
}]

// Native, not AVM resource-group: every module below needs `scope: rg` from a resource symbol, and the RG carries
// only a name, location and tags (no lock or role assignment that the AVM module would add value for).
resource rg 'Microsoft.Resources/resourceGroups@2025-04-01' = {
  name: rgName
  location: location
  tags: tags
}

module foundry '../shared-context/foundry.bicep' = {
  name: 'fa-${env}-foundry'
  scope: rg
  params: {
    location: location
    accountName: names.foundryAccount
    projectName: names.project
    operatorObjectId: principalId
    modelDeployments: modelDeployments
  }
}

module monitoring '../shared-context/stage-monitoring.bicep' = {
  name: 'fa-${env}-monitoring'
  scope: rg
  params: {
    location: location
    workspaceName: names.logAnalytics
    appInsightsName: names.appInsights
    foundryResourceGroupName: rgName
    foundryAccountName: names.foundryAccount
    projectName: names.project
    connectionName: '${env}-appinsights'
    foundryDeploymentPrefix: env
    dailyQuotaGb: '0.5'
    tags: tags
  }
  dependsOn: [foundry]
}

module foundation '../shared-context/foundation.bicep' = {
  name: 'fa-${env}-foundation'
  scope: rg
  params: {
    location: location
    environmentName: names.containerAppsEnvironment
    apiIdentityName: names.apiIdentity
    storageName: names.storage
    tableName: tableName
    registryName: names.registry
    foundryAccountName: names.foundryAccount
    projectName: names.project
    foundryResourceGroupName: rgName
    logAnalyticsWorkspaceResourceId: monitoring.outputs.workspaceResourceId
    foundryDeploymentPrefix: env
    registryConnectionName: '${env}-container-registry'
    vnetName: names.vnet
    apiSubnetNatEgress: apiSubnetNatEgress
    tags: tags
  }
  dependsOn: [foundry]
}

module realtime '../shared-context/realtime.bicep' = {
  name: 'fa-${env}-realtime'
  scope: rg
  params: {
    location: location
    name: names.webpubsub
    sku: 'Free_F1'
    apiIdentityName: names.apiIdentity
    tags: tags
  }
  dependsOn: [foundation]
}

module backupRole '../shared-context/backup-role.bicep' = {
  name: 'fa-${env}-backup-role'
  params: {
    backupResourceGroupName: rgName
    // Custom role names are unique per tenant: the token keeps two subscriptions' "dev" apart.
    roleName: 'FA M1 Backup Data Read Write (${env}-${token})'
  }
  dependsOn: [rg]
}

module backupFoundation '../shared-context/backup-foundation.bicep' = {
  name: 'fa-${env}-backup-foundation'
  scope: rg
  params: {
    location: location
    storageName: names.backupStorage
    containerName: backupContainer
    apiIdentityName: names.apiIdentity
    vnetName: names.vnet
    privateEndpointSubnetName: 'snet-private-endpoints'
    privateEndpointName: names.backupPrivateEndpoint
    createBlobPrivateDnsZone: true
    createBlobDnsVnetLink: true
    blobDnsVnetLinkName: '${env}-link'
  }
  dependsOn: [backupRole, foundation]
}

module artifacts '../shared-context/m3-artifacts.bicep' = {
  name: 'fa-${env}-m3-artifacts'
  scope: rg
  params: {
    storageName: names.backupStorage
    containerName: artifactsContainer
    apiIdentityName: names.apiIdentity
    // The backup role above: backup-role.bicep names it guid(<RG id>, 'fa-m1-backup-data-read-write-v1').
    readWriteRoleDefinitionGuid: guid(rg.id, 'fa-m1-backup-data-read-write-v1')
  }
  dependsOn: [backupFoundation]
}

module upgradeSnapshots '../shared-context/upgrade-snapshots.bicep' = if (deployUpgradeSnapshots) {
  name: 'fa-${env}-upgrade-snapshots'
  scope: rg
  params: {
    deployUpgradeSnapshots: deployUpgradeSnapshots
    location: location
    storageName: names.snapshotStorage
    apiIdentityName: names.apiIdentity
    vnetName: names.vnet
    privateEndpointName: names.snapshotPrivateEndpoint
    tags: tags
  }
  // The private endpoint uses the Blob private DNS zone and VNet link that the backup foundation creates.
  dependsOn: [backupFoundation]
}

module onboarding '../shared-context/stage-onboarding-delegation.bicep' = if (enableAgentOnboarding) {
  name: 'fa-${env}-onboarding-delegation'
  scope: rg
  params: {
    accountName: names.foundryAccount
    projectName: names.project
    principalId: foundation.outputs.apiIdentityPrincipalId
    assignmentDescription: 'FA #468: ${env} onboarding may only add Foundry User / OpenAI User for service principals; no deletes'
  }
}

module teamMcpConnections 'api-project-contributor.bicep' = if (enableTeamMcpConnections) {
  name: 'fa-${env}-team-mcp-connections'
  scope: rg
  params: {
    accountName: names.foundryAccount
    projectName: names.project
    principalId: foundation.outputs.apiIdentityPrincipalId
  }
}

output AZURE_RESOURCE_GROUP string = rg.name
output FA_ENVIRONMENT string = env
output FA_FOUNDRY_ACCOUNT string = names.foundryAccount
output FA_PROJECT string = names.project
output FA_PROJECT_ENDPOINT string = foundry.outputs.projectEndpoint
output FA_PROJECT_RESOURCE_ID string = foundry.outputs.projectId
output FA_REGISTRY_NAME string = names.registry
output FA_REGISTRY string = '${names.registry}${environment().suffixes.acrLoginServer}'
output FA_CONTAINER_APPS_ENVIRONMENT string = names.containerAppsEnvironment
output FA_API_IDENTITY string = names.apiIdentity
output FA_API_IDENTITY_CLIENT_ID string = foundation.outputs.apiIdentityClientId
output FA_API_IDENTITY_PRINCIPAL_ID string = foundation.outputs.apiIdentityPrincipalId
output FA_TABLE_ACCOUNT string = names.storage
output FA_TABLE_NAME string = foundation.outputs.tableName
output FA_BACKUP_STORAGE string = names.backupStorage
output FA_BACKUP_CONTAINER string = backupContainer
output FA_ARTIFACTS_CONTAINER string = artifactsContainer
output FA_VNET string = names.vnet
output FA_REALTIME_ENDPOINT string = realtime.outputs.endpoint
output FA_LOG_ANALYTICS string = names.logAnalytics
output FA_API_APP string = names.apiApp
output FA_SYNC_JOB string = names.syncJob
output FA_CONSOLE_APP string = names.consoleApp
output FA_CONSOLE_IDENTITY string = names.consoleIdentity
output FA_CONSOLE_STORAGE string = names.consoleStorage
output FA_AGENT_ONBOARDING bool = enableAgentOnboarding
output FA_UPGRADE_SNAPSHOT_CONTAINER_URL string = deployUpgradeSnapshots ? upgradeSnapshots!.outputs.containerUrl : ''
output FA_BACKUP_ROLE_ID string = backupRole.outputs.backupRoleDefinitionId
