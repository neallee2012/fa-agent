targetScope = 'resourceGroup'

@description('Deploy only through the reviewed operator command in rg-faexample.')
@minLength(2)
@maxLength(64)
param botName string = 'fa-teams-personal-poc'

@allowed(['id-facontext-api'])
param existingIdentityName string = 'id-facontext-api'

@allowed(['00000000-0000-0000-0000-000000000013'])
param tenantId string = '00000000-0000-0000-0000-000000000013'

@description('Reviewed public HTTPS callback ending in /behavior/v1/teams/activities. Not a serviceUrl.')
@minLength(1)
param endpoint string

resource botIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' existing = {
  name: existingIdentityName
}

resource bot 'Microsoft.BotService/botServices@2022-09-15' = {
  name: botName
  location: 'global'
  kind: 'azurebot'
  sku: {name: 'F0'}
  tags: {
    purpose: 'fa-teams-personal-prerequisite'
    scope: 'designated-admin-personal-only'
    budget: 'within-existing-100-usd-poc-total'
  }
  properties: {
    displayName: 'FA Personal POC'
    endpoint: endpoint
    msaAppType: 'UserAssignedMSI'
    msaAppId: botIdentity.properties.clientId
    msaAppTenantId: tenantId
    msaAppMSIResourceId: botIdentity.id
    disableLocalAuth: true
    publicNetworkAccess: 'Enabled'
    isStreamingSupported: false
  }
}

resource teams 'Microsoft.BotService/botServices/channels@2022-09-15' = {
  parent: bot
  name: 'MsTeamsChannel'
  location: 'global'
  properties: {
    channelName: 'MsTeamsChannel'
    properties: {
      isEnabled: true
      enableCalling: false
    }
  }
}

output botResourceId string = bot.id
output botAppClientId string = botIdentity.properties.clientId
output botIdentityResourceId string = botIdentity.id
output nativeChannelEnabled bool = false
