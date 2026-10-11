targetScope = 'resourceGroup'

@description('Repair entrypoint: existing Foundry/network/ACR prerequisites only. Creates the registry connection and the absent controller.')
param location string = 'westus3'
param environmentName string = 'faexample'
param accountName string = 'cog-fasample-vnet-demo'
param projectName string = 'faexample'
param vnetName string = 'vnet-fasample-westus3'
param controllerSubnetName string = 'snet-controller'
param existingContainerRegistryResourceId string = resourceId('Microsoft.ContainerRegistry/registries', 'crcontosodemo01')
param containerRegistryConnectionName string = 'poc-container-registry'

@description('Supply the same approved public key from the existing deployment overlay. Never supply a private key.')
@minLength(32)
@secure()
param controllerSshPublicKey string

param controllerName string = 'vm-fasample-controller'
param controllerVmSize string = 'Standard_D2ls_v7'
param controllerAdminUsername string = 'pocadmin'
param controllerImageVersion string = '24.04.202608270'

@minValue(30)
param controllerOsDiskSizeGB int = 32

var tags = {
  workload: 'hosted-sandbox-poc'
  environment: 'poc'
  'azd-env-name': environmentName
  dataClassification: 'synthetic-only'
}
var registryIdParts = split(existingContainerRegistryResourceId, '/')

resource account 'Microsoft.CognitiveServices/accounts@2025-04-01-preview' existing = {
  name: accountName
}

resource project 'Microsoft.CognitiveServices/accounts/projects@2025-04-01-preview' existing = {
  parent: account
  name: projectName
}

resource vnet 'Microsoft.Network/virtualNetworks@2025-05-01' existing = {
  name: vnetName
}

resource controllerSubnet 'Microsoft.Network/virtualNetworks/subnets@2025-05-01' existing = {
  parent: vnet
  name: controllerSubnetName
}

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = {
  scope: resourceGroup(registryIdParts[2], registryIdParts[4])
  name: last(registryIdParts)
}

module registryConnection './registry-connection.bicep' = {
  name: 'vnet-project-registry-connection'
  params: {
    accountName: account.name
    projectName: project.name
    registryResourceId: registry.id
    connectionName: containerRegistryConnectionName
  }
}

module controller './controller.bicep' = {
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
    controllerSubnetId: controllerSubnet.id
    accountName: account.name
    projectName: project.name
  }
  dependsOn: [
    registryConnection
  ]
}

output accountId string = account.id
output projectId string = project.id
output projectEndpoint string = 'https://${account.name}.services.ai.azure.com/api/projects/${project.name}'
output projectPrincipalId string = project.identity.principalId
output controllerSubnetId string = controllerSubnet.id
output existingContainerRegistryId string = registry.id
output containerRegistryConnectionId string = registryConnection.outputs.resourceId
output containerRegistryEndpoint string = registry.properties.loginServer
output controllerVmId string = controller.outputs.controllerVmId
output controllerVmName string = controller.outputs.controllerVmName
output controllerPrincipalId string = controller.outputs.controllerPrincipalId
