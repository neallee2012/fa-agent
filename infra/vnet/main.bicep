targetScope = 'resourceGroup'

// Adapted contract, not a copy of the default recipe:
// https://github.com/microsoft-foundry/foundry-samples/tree/main/infrastructure/infrastructure-setup-bicep/11-private-network-basic-vnet
// https://learn.microsoft.com/azure/foundry/agents/concepts/networking-options
// Network injection MUST be supplied on initial account creation. This is not an upgrade of the old account.
param location string = 'westus3'
param environmentName string = 'faexample'

@description('A NEW Foundry account name. Never pass the existing non-injected account name.')
param accountName string = 'cog-fasample-vnet-demo'

param projectName string = 'faexample'
param projectCapabilityHostName string = 'caphostproj'
param containerRegistryConnectionName string = 'poc-container-registry'

@description('Existing support resources are read-only references; only private endpoints and AcrPull are added.')
param existingKeyVaultResourceId string = resourceId('Microsoft.KeyVault/vaults', 'kv-faexample-de')
param existingStorageResourceId string = resourceId('Microsoft.Storage/storageAccounts', 'stfaexampledemo')
param existingContainerRegistryResourceId string = resourceId('Microsoft.ContainerRegistry/registries', 'crcontosodemo01')

param operatorObjectId string = '00000000-0000-0000-0000-00000000001b'
param vnetName string = 'vnet-fasample-westus3'
param vnetAddressPrefix string = '192.168.80.0/22'
param agentSubnetName string = 'snet-agents'
param agentSubnetPrefix string = '192.168.80.0/24'
param privateEndpointSubnetName string = 'snet-private-endpoints'
param privateEndpointSubnetPrefix string = '192.168.81.0/24'
param controllerSubnetName string = 'snet-controller'
param controllerSubnetPrefix string = '192.168.82.0/24'
param natGatewayName string = 'nat-fasample-westus3'
param natPublicIpName string = 'pip-fasample-egress'
param agentNsgName string = 'nsg-fasample-agents'
param controllerNsgName string = 'nsg-fasample-controller'
param foundryPrivateEndpointName string = 'pe-fasample-foundry'
param vaultPrivateEndpointName string = 'pe-fasample-vault'
param filePrivateEndpointName string = 'pe-fasample-file'
param registryPrivateEndpointName string = 'pe-fasample-acr'

@description('False omits only the controller VM, NIC and its project role assignment; Foundry and networking still deploy. This is not a dry-run switch.')
param enableController bool = false

@description('Required when enableController=true; the controller module enforces a nonempty public key. No private keys.')
@secure()
param controllerSshPublicKey string = ''

param controllerName string = 'vm-fasample-controller'
@description('WestUS3 availability probe selected this NVMe-only SKU; family quota still requires preflight.')
param controllerVmSize string = 'Standard_D2ls_v7'
param controllerAdminUsername string = 'pocadmin'
param controllerImageVersion string = 'latest'

@minValue(30)
param controllerOsDiskSizeGB int = 32

var tags = {
  workload: 'hosted-sandbox-poc'
  environment: 'poc'
  'azd-env-name': environmentName
  dataClassification: 'synthetic-only'
}
var vaultIdParts = split(existingKeyVaultResourceId, '/')
var storageIdParts = split(existingStorageResourceId, '/')
var registryIdParts = split(existingContainerRegistryResourceId, '/')

resource vault 'Microsoft.KeyVault/vaults@2023-07-01' existing = {
  scope: resourceGroup(vaultIdParts[2], vaultIdParts[4])
  name: last(vaultIdParts)
}

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' existing = {
  scope: resourceGroup(storageIdParts[2], storageIdParts[4])
  name: last(storageIdParts)
}

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = {
  scope: resourceGroup(registryIdParts[2], registryIdParts[4])
  name: last(registryIdParts)
}

module network './network.bicep' = {
  name: 'vnet-foundry-network'
  params: {
    location: location
    tags: tags
    vnetName: vnetName
    vnetAddressPrefix: vnetAddressPrefix
    agentSubnetName: agentSubnetName
    agentSubnetPrefix: agentSubnetPrefix
    privateEndpointSubnetName: privateEndpointSubnetName
    privateEndpointSubnetPrefix: privateEndpointSubnetPrefix
    controllerSubnetName: controllerSubnetName
    controllerSubnetPrefix: controllerSubnetPrefix
    natGatewayName: natGatewayName
    natPublicIpName: natPublicIpName
    agentNsgName: agentNsgName
    controllerNsgName: controllerNsgName
  }
}

resource account 'Microsoft.CognitiveServices/accounts@2025-04-01-preview' = {
  name: accountName
  location: location
  tags: tags
  kind: 'AIServices'
  sku: {
    name: 'S0'
  }
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    allowProjectManagement: true
    customSubDomainName: accountName
    disableLocalAuth: true
    publicNetworkAccess: 'Disabled'
    networkAcls: {
      defaultAction: 'Deny'
      ipRules: []
      virtualNetworkRules: []
    }
    networkInjections: [
      {
        scenario: 'agent'
        // RP preflight must resolve the subnet ID before deployment outputs exist.
        subnetArmId: resourceId('Microsoft.Network/virtualNetworks/subnets', vnetName, agentSubnetName)
        useMicrosoftManagedNetwork: false
      }
    ]
  }
  dependsOn: [
    network
  ]
}

// Match network.bicep's zone names without runtime outputs; regression tests enforce parity.
var privateDnsZoneNames = [
  'privatelink.cognitiveservices.azure.com'
  'privatelink.openai.azure.com'
  'privatelink.services.ai.azure.com'
  'privatelink.vaultcore.azure.net'
  'privatelink.file.${environment().suffixes.storage}'
  'privatelink.azurecr.io'
]

var endpointDefinitions = [
  {
    name: foundryPrivateEndpointName
    resourceId: account.id
    groupId: 'account'
    dnsZoneIndexes: [
      0
      1
      2
    ]
  }
  {
    name: vaultPrivateEndpointName
    resourceId: vault.id
    groupId: 'vault'
    dnsZoneIndexes: [
      3
    ]
  }
  {
    name: filePrivateEndpointName
    resourceId: storage.id
    groupId: 'file'
    dnsZoneIndexes: [
      4
    ]
  }
  {
    name: registryPrivateEndpointName
    resourceId: registry.id
    groupId: 'registry'
    dnsZoneIndexes: [
      5
    ]
  }
]

module privateEndpoints 'br/public:avm/res/network/private-endpoint:0.12.1' = [
  for endpoint in endpointDefinitions: {
    name: 'deploy-${endpoint.name}'
    params: {
      name: endpoint.name
      location: location
      tags: tags
      enableTelemetry: false
      subnetResourceId: resourceId('Microsoft.Network/virtualNetworks/subnets', vnetName, privateEndpointSubnetName)
      privateLinkServiceConnections: [
        {
          name: '${endpoint.name}-connection'
          properties: {
            privateLinkServiceId: endpoint.resourceId
            groupIds: [
              endpoint.groupId
            ]
          }
        }
      ]
      privateDnsZoneGroup: {
        name: 'default'
        privateDnsZoneGroupConfigs: [
          for zoneIndex in endpoint.dnsZoneIndexes: {
            name: 'zone-${zoneIndex}'
            privateDnsZoneResourceId: resourceId('Microsoft.Network/privateDnsZones', privateDnsZoneNames[zoneIndex])
          }
        ]
      }
    }
    dependsOn: [
      network
    ]
  }
]

resource project 'Microsoft.CognitiveServices/accounts/projects@2025-04-01-preview' = {
  parent: account
  name: projectName
  location: location
  tags: tags
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    displayName: 'Hosted Sandbox POC - VNet'
    description: 'Basic platform-managed agent state; team Files is independent application data.'
  }
  dependsOn: [
    privateEndpoints
  ]
}

// Account creation implicitly provisions its capability host. A second account host causes 409.
// Only the Basic PROJECT host from official template 11 is explicitly declared.
resource projectCapabilityHost 'Microsoft.CognitiveServices/accounts/projects/capabilityHosts@2025-04-01-preview' = {
  parent: project
  name: projectCapabilityHostName
  properties: {
    // Official template 11 sends this field; the published ARM/Bicep schema omits it.
    #disable-next-line BCP037
    capabilityHostKind: 'Agents'
  }
}

module operatorProjectUser './project-access.bicep' = {
  name: 'vnet-operator-project-user'
  params: {
    accountName: account.name
    projectName: project.name
    principalId: operatorObjectId
    principalType: 'User'
  }
}

module registryAccess './registry-access.bicep' = {
  name: 'vnet-project-acr-pull'
  scope: resourceGroup(registryIdParts[2], registryIdParts[4])
  params: {
    registryName: registry.name
    projectPrincipalId: project.identity.principalId
  }
}

module registryConnection './registry-connection.bicep' = {
  name: 'vnet-project-registry-connection'
  params: {
    accountName: account.name
    projectName: project.name
    registryResourceId: registry.id
    connectionName: containerRegistryConnectionName
  }
  dependsOn: [
    registryAccess
  ]
}

module controller './controller.bicep' = if (enableController) {
  name: 'vnet-private-controller'
  params: {
    location: location
    tags: tags
    controllerName: controllerName
    vmSize: controllerVmSize
    adminUsername: controllerAdminUsername
    sshPublicKey: controllerSshPublicKey
    imageVersion: controllerImageVersion
    osDiskSizeGB: controllerOsDiskSizeGB
    controllerSubnetId: resourceId('Microsoft.Network/virtualNetworks/subnets', vnetName, controllerSubnetName)
    accountName: account.name
    projectName: project.name
  }
  dependsOn: [
    network
    projectCapabilityHost
    registryConnection
  ]
}

output accountId string = account.id
output accountName string = account.name
output accountEndpoint string = account.properties.endpoint
output accountPrincipalId string = account.identity.principalId
output projectId string = project.id
output projectName string = project.name
output projectEndpoint string = 'https://${account.name}.services.ai.azure.com/api/projects/${project.name}'
output projectPrincipalId string = project.identity.principalId
output projectCapabilityHostId string = projectCapabilityHost.id
output containerRegistryConnectionId string = registryConnection.outputs.resourceId
output containerRegistryEndpoint string = registry.properties.loginServer
output vnetId string = network.outputs.vnetId
output agentSubnetId string = network.outputs.agentSubnetId
output privateEndpointSubnetId string = network.outputs.privateEndpointSubnetId
output controllerSubnetId string = network.outputs.controllerSubnetId
output privateDnsZoneIds array = network.outputs.privateDnsZoneIds
output natGatewayId string = network.outputs.natGatewayId
output outboundPublicIpId string = network.outputs.outboundPublicIpId
output controllerVmId string = enableController ? controller!.outputs.controllerVmId : ''
output controllerVmName string = enableController ? controller!.outputs.controllerVmName : ''
output controllerPrincipalId string = enableController ? controller!.outputs.controllerPrincipalId : ''
output privateEndpointNames array = [for endpoint in endpointDefinitions: endpoint.name]
output privateEndpointIds array = [
  for (endpoint, index) in endpointDefinitions: privateEndpoints[index].outputs.resourceId
]
output existingKeyVaultId string = vault.id
output existingStorageId string = storage.id
output existingContainerRegistryId string = registry.id
