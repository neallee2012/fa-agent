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
    publicNetworkAccess: 'Enabled'
    restrictOutboundNetworkAccess: false
    dynamicThrottlingEnabled: false
    networkAcls: {
      defaultAction: 'Allow'
      ipRules: []
      virtualNetworkRules: []
    }
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
