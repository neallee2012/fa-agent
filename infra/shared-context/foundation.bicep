targetScope = 'resourceGroup'

param location string = 'westus3'
param environmentName string = 'cae-facontext-private'
param apiIdentityName string = 'id-facontext-api'
param storageName string = 'stfacontextpocdemo'
param tableName string = 'workstate'
param registryName string = 'crfacontextpocdemo'
param foundryAccountName string = 'cog-fasample-memory-demo'
param projectName string = 'faexample'
param vnetName string = 'vnet-facontext-api'
@description('RG of the shared Foundry account; stage resources live in another RG (ADR-0008).')
param foundryResourceGroupName string = resourceGroup().name
@description('Empty keeps the dev environment unchanged (no app log destination).')
param logAnalyticsWorkspaceResourceId string = ''
param tags object = {
  workload: 'hosted-sandbox-poc'
  architecture: 'shared-context'
  dataClassification: 'synthetic-only'
}
@description('Name prefix of the nested deployments written into the shared Foundry account RG; stage must not reuse dev names, or it overwrites dev deployment history.')
param foundryDeploymentPrefix string = 'shared-context'
@description('Connection names are unique across the whole account, so stage needs its own name.')
param registryConnectionName string = 'poc-container-registry'
@description('Opt-in NAT egress for snet-api (see private-state-network.bicep); keep true where it was deployed.')
param apiSubnetNatEgress bool = false
@description('false: the VNet (private-state-network.bicep) was deployed before this template, as infra/azd/main.bicep does to create it before the Foundry account (ADR-0010). The default deploys it here, as the manual flow and stage always have.')
param createNetwork bool = true
@allowed(['public', 'private'])
@description('ADR-0010 FA_NETWORK_MODE of the VNet this template deploys (createNetwork). Run alone on an azd private environment it must be private, or this VNet update drops snet-agents and 192.168.97.0/24.')
param networkMode string = 'public'
@description('ADR-0010 phase 2b (an existing VNet, infra/azd/main.bicep): the subnet IDs and the VNet ID to use instead of vnetName. Empty keeps vnetName and the FA subnet names.')
param apiSubnetResourceId string = ''
param privateEndpointSubnetResourceId string = ''
param vnetResourceId string = ''
@description('false (FA_DNS_MODE=existing): no Table private DNS zone, VNet link or zone group; the user serves the name.')
param privateDnsZones bool = true

resource foundry 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = {
  scope: resourceGroup(foundryResourceGroupName)
  name: foundryAccountName
}

resource project 'Microsoft.CognitiveServices/accounts/projects@2025-06-01' existing = {
  parent: foundry
  name: projectName
}

module apiIdentity 'br/public:avm/res/managed-identity/user-assigned-identity:0.6.0' = {
  name: 'shared-context-api-identity'
  params: {
    name: apiIdentityName
    location: location
    tags: tags
    enableTelemetry: false
  }
}

module apiEnvironment 'br/public:avm/res/app/managed-environment:0.16.0' = {
  name: 'shared-context-api-environment'
  params: {
    name: environmentName
    location: location
    tags: tags
    enableTelemetry: false
    publicNetworkAccess: 'Enabled'
    zoneRedundant: false
    infrastructureSubnetResourceId: apiSubnetResourceId == '' ? resourceId('Microsoft.Network/virtualNetworks/subnets', vnetName, 'snet-api') : apiSubnetResourceId
    // Keyless Azure Monitor route: the legacy 'log-analytics' destination ingests with the workspace shared key
    // (HTTP Data Collector API, support ended 2026-09-14), which a workspace with local auth disabled rejects.
    // https://learn.microsoft.com/azure/container-apps/migrate-logs-azure-monitor
    appLogsConfiguration: empty(logAnalyticsWorkspaceResourceId) ? null : {
      destination: 'azure-monitor'
    }
    diagnosticSettings: empty(logAnalyticsWorkspaceResourceId) ? null : [
      {
        name: 'to-log-analytics'
        workspaceResourceId: logAnalyticsWorkspaceResourceId
        logAnalyticsDestinationType: 'Dedicated'
        logCategoriesAndGroups: [
          {
            categoryGroup: 'allLogs'
          }
        ]
        metricCategories: []
      }
    ]
    workloadProfiles: [
      {
        name: 'Consumption'
        workloadProfileType: 'Consumption'
      }
    ]
  }
  dependsOn: [
    privateStateNetwork
  ]
}

module storage 'br/public:avm/res/storage/storage-account:0.33.0' = {
  name: 'shared-context-state-storage'
  params: {
    name: storageName
    location: location
    kind: 'StorageV2'
    skuName: 'Standard_LRS'
    enableTelemetry: false
    tags: tags
    publicNetworkAccess: 'Disabled'
    networkAcls: {
      defaultAction: 'Deny'
      bypass: 'None'
      ipRules: []
      virtualNetworkRules: []
    }
    allowSharedKeyAccess: false
    defaultToOAuthAuthentication: true
    allowBlobPublicAccess: false
    requireInfrastructureEncryption: true
    supportsHttpsTrafficOnly: true
    minimumTlsVersion: 'TLS1_2'
    blobServices: {}
    tableServices: {
      tables: [
        {
          name: tableName
          roleAssignments: [
            {
              principalId: apiIdentity.outputs.principalId
              principalType: 'ServicePrincipal'
              roleDefinitionIdOrName: '0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3'
            }
          ]
        }
      ]
    }
  }
}

module privateStateNetwork './private-state-network.bicep' = if (createNetwork) {
  name: 'shared-context-private-state-network'
  params: {
    location: location
    tags: tags
    vnetName: vnetName
    apiSubnetNatEgress: apiSubnetNatEgress
    networkMode: networkMode
  }
}

module tableDns 'br/public:avm/res/network/private-dns-zone:0.8.1' = if (privateDnsZones) {
  name: 'shared-context-table-dns'
  params: {
    name: 'privatelink.table.${environment().suffixes.storage}'
    tags: tags
    enableTelemetry: false
    virtualNetworkLinks: [
      {
        name: 'facontext-api-link'
        virtualNetworkResourceId: vnetResourceId == '' ? resourceId('Microsoft.Network/virtualNetworks', vnetName) : vnetResourceId
        registrationEnabled: false
      }
    ]
  }
  dependsOn: [
    privateStateNetwork
  ]
}

module tableEndpoint 'br/public:avm/res/network/private-endpoint:0.12.1' = {
  name: 'shared-context-table-private-endpoint'
  params: {
    name: 'pe-facontext-table'
    location: location
    tags: tags
    enableTelemetry: false
    subnetResourceId: privateEndpointSubnetResourceId == '' ? resourceId('Microsoft.Network/virtualNetworks/subnets', vnetName, 'snet-private-endpoints') : privateEndpointSubnetResourceId
    privateLinkServiceConnections: [
      {
        name: 'facontext-table'
        properties: {
          privateLinkServiceId: storage.outputs.resourceId
          groupIds: [
            'table'
          ]
        }
      }
    ]
    privateDnsZoneGroup: privateDnsZones ? {
      name: 'default'
      privateDnsZoneGroupConfigs: [
        {
          name: 'table'
          privateDnsZoneResourceId: tableDns!.outputs.resourceId
        }
      ]
    } : null
  }
}

module registry 'br/public:avm/res/container-registry/registry:0.13.0' = {
  name: 'shared-context-registry'
  params: {
    name: registryName
    location: location
    tags: tags
    enableTelemetry: false
    acrSku: 'Basic'
    acrAdminUserEnabled: false
    anonymousPullEnabled: false
    publicNetworkAccess: 'Enabled'
    networkRuleSetDefaultAction: 'Allow'
    roleAssignmentMode: 'LegacyRegistryPermissions'
    azureADAuthenticationAsArmPolicyStatus: 'enabled'
    retentionPolicyStatus: 'disabled'
    zoneRedundancy: 'Disabled'
    roleAssignments: [
      {
        principalId: apiIdentity.outputs.principalId
        principalType: 'ServicePrincipal'
        roleDefinitionIdOrName: 'AcrPull'
      }
      {
        principalId: project.identity.principalId
        principalType: 'ServicePrincipal'
        roleDefinitionIdOrName: 'AcrPull'
      }
    ]
  }
}

module apiMemoryAccess './project-service-access.bicep' = {
  name: '${foundryDeploymentPrefix}-api-memory-access'
  scope: resourceGroup(foundryResourceGroupName)
  params: {
    accountName: foundryAccountName
    projectName: projectName
    principalId: apiIdentity.outputs.principalId
  }
}

module registryConnection '../vnet/registry-connection.bicep' = {
  name: '${foundryDeploymentPrefix}-registry-connection'
  scope: resourceGroup(foundryResourceGroupName)
  params: {
    accountName: foundryAccountName
    projectName: projectName
    registryResourceId: registry.outputs.resourceId
    connectionName: registryConnectionName
  }
}

output apiIdentityResourceId string = apiIdentity.outputs.resourceId
output apiIdentityPrincipalId string = apiIdentity.outputs.principalId
output apiIdentityClientId string = apiIdentity.outputs.clientId
output environmentResourceId string = apiEnvironment.outputs.resourceId
output registryResourceId string = registry.outputs.resourceId
output tableEndpoint string = 'https://${storageName}.table.${environment().suffixes.storage}'
output tableName string = tableName
