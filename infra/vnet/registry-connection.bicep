targetScope = 'resourceGroup'

param accountName string
param projectName string
param registryResourceId string
param connectionName string = 'poc-container-registry'

var registryIdParts = split(registryResourceId, '/')

resource account 'Microsoft.CognitiveServices/accounts@2025-04-01-preview' existing = {
  name: accountName
}

resource project 'Microsoft.CognitiveServices/accounts/projects@2025-04-01-preview' existing = {
  parent: account
  name: projectName
}

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = {
  scope: resourceGroup(registryIdParts[2], registryIdParts[4])
  name: last(registryIdParts)
}

resource connection 'Microsoft.CognitiveServices/accounts/projects/connections@2025-10-01-preview' = {
  parent: project
  name: connectionName
  properties: {
    category: 'ContainerRegistry'
    authType: 'ManagedIdentity'
    target: registry.properties.loginServer
    useWorkspaceManagedIdentity: true
    // Official azd RegistryIdentity mapping, not an inferred Entra app ID or UAI reference:
    // https://github.com/Azure/azure-dev/blob/4ecd0bd827e3b3c7805eef7d2def2270b9bb19d0/cli/azd/extensions/azure.ai.agents/internal/synthesis/templates/modules/acr.bicep
    credentials: {
      clientId: project.identity.principalId
      resourceId: registry.id
    }
    metadata: {
      ApiType: 'Azure'
      ResourceId: registry.id
    }
  }
}

output resourceId string = connection.id
