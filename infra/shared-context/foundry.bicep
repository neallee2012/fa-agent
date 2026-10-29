targetScope = 'resourceGroup'

param location string = 'westus3'
param accountName string = 'cog-fasample-memory-demo'
param projectName string = 'faexample'
param operatorObjectId string
@description('Model deployments in the AVM account shape. deploy/shared-context/prepare_environment.py renders them from src/fa-shared-context-poc/model-catalog.json so every model the Portal offers has a deployment (2026-10-07: a catalog model without one fails only at the first turn). The default keeps the original two.')
param modelDeployments array = [
  {
    name: 'gpt-4.1-mini'
    model: {
      format: 'OpenAI'
      name: 'gpt-4.1-mini'
      version: '2025-04-14'
    }
    sku: {
      name: 'GlobalStandard'
      capacity: 10
    }
    versionUpgradeOption: 'NoAutoUpgrade'
  }
  {
    name: 'text-embedding-3-small'
    model: {
      format: 'OpenAI'
      name: 'text-embedding-3-small'
      version: '1'
    }
    sku: {
      name: 'GlobalStandard'
      capacity: 10
    }
    versionUpgradeOption: 'NoAutoUpgrade'
  }
]

@allowed(['public', 'private'])
@description('ADR-0010 FA_NETWORK_MODE. private: Hosted Agents injected into agentSubnetResourceId, public network access Disabled (Deny, AzureServices bypass), and a private endpoint in privateEndpointSubnetResourceId with the given DNS zones. Injection can only be set when the account is created.')
param networkMode string = 'public'
param agentSubnetResourceId string = ''
param privateEndpointSubnetResourceId string = ''
param privateDnsZoneResourceIds array = []
@description('false (ADR-0010 phase 2b FA_DNS_MODE=existing): the private endpoint gets no DNS zone group; the user serves the three names.')
param privateDnsZones bool = true

var isPrivate = networkMode == 'private'
var foundryUserRoleId = '53ca6127-db72-4b80-b1b0-d745d6d5456d'
var openAiUserRoleId = '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'

module foundryAccount 'br/public:avm/res/cognitive-services/account:0.19.0' = {
  name: 'shared-context-foundry-account'
  params: {
    name: accountName
    location: location
    kind: 'AIServices'
    sku: 'S0'
    allowProjectManagement: true
    customSubDomainName: accountName
    disableLocalAuth: true
    publicNetworkAccess: isPrivate ? 'Disabled' : 'Enabled'
    restrictOutboundNetworkAccess: false
    dynamicThrottlingEnabled: false
    networkAcls: isPrivate ? {
      defaultAction: 'Deny'
      bypass: 'AzureServices'
      ipRules: []
      virtualNetworkRules: []
    } : {
      defaultAction: 'Allow'
      ipRules: []
      virtualNetworkRules: []
    }
    // The platform creates the account capability host with the injection (ADR-0010 phase 0, Q3); the project one is
    // projectCapabilityHost below.
    networkInjections: isPrivate ? {
      scenario: 'agent'
      subnetResourceId: agentSubnetResourceId
      useMicrosoftManagedNetwork: false
    } : null
    privateEndpoints: isPrivate ? [
      {
        subnetResourceId: privateEndpointSubnetResourceId
        privateDnsZoneGroup: privateDnsZones ? {
          privateDnsZoneGroupConfigs: map(privateDnsZoneResourceIds, id => { privateDnsZoneResourceId: id })
        } : null
      }
    ] : null
    managedIdentities: {
      systemAssigned: true
    }
    enableTelemetry: false
    tags: {
      workload: 'hosted-sandbox-poc'
      architecture: 'shared-context'
      dataClassification: 'synthetic-only'
    }
    deployments: modelDeployments
  }
}

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
    description: 'Non-VNet shared-context and Memory Store POC'
  }
  dependsOn: [
    foundryAccount
  ]
}

// ADR-0010 2026-10-28: tools outside Foundry (MCP such as WebIQ, OpenAPI, A2A) reached through the VNet need a project
// capability host, or fail with `name or host unknown` (Learn: toolbox-network-isolation). Basic: platform-managed data,
// no BYO connections (template 11). It needs the account one, which the injected account gets; the same PUT again is a
// no-op (Learn: capability-hosts). deploy/azd/fa_azd.py lists the project's capability hosts and deletes them before
// the project.
resource projectCapabilityHost 'Microsoft.CognitiveServices/accounts/projects/capabilityHosts@2025-06-01' = if (isPrivate) {
  parent: project
  name: 'caphostproj'
  properties: {
    // In the Learn capability-hosts PUT and template 11, missing from Bicep's ProjectCapabilityHostProperties type and
    // the REST spec. Should the PUT answer 400 on it, try `properties: {}` before another version (ADR-0010 section 4).
    #disable-next-line BCP037
    capabilityHostKind: 'Agents'
  }
}

module operatorFoundryAccess '../vnet/project-access.bicep' = {
  name: 'shared-context-operator-foundry-user'
  params: {
    accountName: accountName
    projectName: project.name
    principalId: operatorObjectId
    principalType: 'User'
  }
}

resource operatorOpenAiAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(project.id, operatorObjectId, openAiUserRoleId)
  scope: project
  properties: {
    principalId: operatorObjectId
    principalType: 'User'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', openAiUserRoleId)
  }
}

module projectMemoryAccess './account-access.bicep' = {
  name: 'shared-context-project-memory-access'
  params: {
    accountName: accountName
    principalId: project.identity.principalId
    roleId: foundryUserRoleId
  }
}

output accountId string = account.id
output projectId string = project.id
output projectPrincipalId string = project.identity.principalId
output projectEndpoint string = 'https://${accountName}.services.ai.azure.com/api/projects/${projectName}'
