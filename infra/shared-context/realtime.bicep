targetScope = 'resourceGroup'

// Web PubSub hint channel (v16): the API publishes ID-only hints to one group per Team and hands each owner a
// short-lived client URL; browsers get no role. Free_F1 is USD 0 but has no SLA, so the workbench keeps polling as
// its fallback. One resource per environment (dev, stage); deploy with realtime[.stage].parameters.json.
// https://learn.microsoft.com/azure/azure-web-pubsub/overview
// https://learn.microsoft.com/azure/azure-web-pubsub/concept-azure-ad-authorization
param location string = 'westus3'
@description('Globally unique; the endpoint is https://<name>.webpubsub.azure.com (FA_REALTIME_ENDPOINT).')
param name string
@description('Free_F1: 1 unit, 20 concurrent connections, 20,000 messages/day, no SLA, no private endpoint.')
@allowed(['Free_F1', 'Standard_S1'])
param sku string = 'Free_F1'
@description('The API user-assigned identity (also used by the deploy sync job); the only Web PubSub data-plane principal.')
param apiIdentityName string
param tags object = {
  workload: 'hosted-sandbox-poc'
}

// Built-in "Web PubSub Service Owner": generateToken + send for the API only (no narrower built-in role has both).
var webPubSubServiceOwner = '12cf5a90-567b-43ae-8102-96cf46c7d9b4'

resource apiIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' existing = {
  name: apiIdentityName
}

module webPubSub 'br/public:avm/res/signal-r-service/web-pub-sub:0.7.2' = {
  name: 'fa-realtime-webpubsub'
  params: {
    name: name
    location: location
    tags: tags
    enableTelemetry: false
    sku: sku
    capacity: 1
    // Entra only: no access key or connection string exists to leak; the API calls generateToken.
    disableLocalAuth: true
    disableAadAuth: false
    // Browsers connect directly; Free_F1 supports neither private endpoints nor network ACLs.
    publicNetworkAccess: 'Enabled'
    roleAssignments: [
      {
        principalId: apiIdentity.properties.principalId
        principalType: 'ServicePrincipal'
        roleDefinitionIdOrName: webPubSubServiceOwner
      }
    ]
  }
}

output endpoint string = 'https://${webPubSub.outputs.hostName}'
output resourceId string = webPubSub.outputs.resourceId
