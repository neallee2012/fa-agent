targetScope = 'resourceGroup'

// ADR-0011 section 5: the long-term memory of FA_NETWORK_MODE=private (the Foundry Memory Store fails on a Deny
// account, ADR-0010 Q4). A serverless Cosmos DB for NoSQL account with key auth and the public network off, reached
// only through a private endpoint in snet-private-endpoints. infra/azd/main.bicep deploys it only for private with
// memory on (FA_MEMORY_BACKEND=cosmos); public never has it. Only the API identity gets a data role, and that role
// cannot delete (memory goes through the API relay; Hosted Agents never connect to Cosmos).

param location string
param tags object
@description('Globally unique account name (3-44 lowercase letters, digits and hyphens).')
param accountName string
param vnetName string
param privateEndpointSubnetResourceId string
@description('Object ID of the API user-assigned identity: the only principal with a data role.')
param apiPrincipalId string
@description('ADR-0010 phase 2b: an existing VNet to link the zone to. Empty uses vnetName in this resource group.')
param vnetResourceId string = ''
@description('false (FA_DNS_MODE=existing): no zone, VNet link or zone group; the user serves both documents.azure.com names.')
param privateDnsZones bool = true

var databaseName = 'fa-memory'
var containerName = 'memories'
var containerResourceId = resourceId('Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers', accountName, databaseName, containerName)
// A fixed name (Cosmos needs a GUID) so the account-level assignment below can name the definition.
var memoryRoleName = guid(resourceGroup().id, accountName, 'fa-memory-data')
// Exactly these five (ADR-0011 section 4): no items/delete, items/upsert or readChangeFeed. items/replace is what a
// patch needs; it also allows a whole replace, which only the API code refuses.
var memoryDataActions = [
  'Microsoft.DocumentDB/databaseAccounts/readMetadata'
  'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers/items/create'
  'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers/items/read'
  'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers/items/replace'
  'Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers/executeQuery'
]

module dns 'br/public:avm/res/network/private-dns-zone:0.8.1' = if (privateDnsZones) {
  name: 'fa-memory-cosmos-dns'
  params: {
    name: 'privatelink.documents.azure.com'
    tags: tags
    enableTelemetry: false
    virtualNetworkLinks: [
      {
        name: '${vnetName}-link'
        virtualNetworkResourceId: vnetResourceId == '' ? resourceId('Microsoft.Network/virtualNetworks', vnetName) : vnetResourceId
        registrationEnabled: false
      }
    ]
  }
}

// AVM 0.21.1 (ARM 2026-04-01-preview): the version and settings the ADR-0011 phase 0c probe deployed. The AVM
// defaults for local auth, metadata writes and the network are written out so the contract does not rest on them.
module account 'br/public:avm/res/document-db/database-account:0.21.1' = {
  name: 'fa-memory-cosmos-account'
  params: {
    name: accountName
    location: location
    tags: tags
    enableTelemetry: false
    capacityMode: 'Serverless'
    capabilitiesToAdd: [
      'EnableNoSQLVectorSearch'
    ]
    disableLocalAuthentication: true
    disableKeyBasedMetadataWriteAccess: true
    networkRestrictions: {
      publicNetworkAccess: 'Disabled'
      ipRules: []
      virtualNetworkRules: []
    }
    // Serverless offers zones only in some regions; 7-day continuous backup is the free tier (30 days is billed).
    zoneRedundant: false
    backupPolicyContinuousTier: 'Continuous7Days'
    privateEndpoints: [
      {
        service: 'Sql'
        subnetResourceId: privateEndpointSubnetResourceId
        privateDnsZoneGroup: privateDnsZones ? {
          privateDnsZoneGroupConfigs: [
            {
              privateDnsZoneResourceId: dns!.outputs.resourceId
            }
          ]
        } : null
        tags: tags
      }
    ]
    sqlDatabases: [
      {
        name: databaseName
        containers: [
          {
            name: containerName
            // One logical partition per memory scope (fap1-/fat1-); every query names it.
            paths: [
              '/scope'
            ]
            indexingPolicy: {
              indexingMode: 'consistent'
              automatic: true
              includedPaths: [
                {
                  path: '/*'
                }
              ]
              excludedPaths: [
                {
                  path: '/embedding/*'
                }
                {
                  path: '/"_etag"/?'
                }
              ]
              vectorIndexes: [
                {
                  path: '/embedding'
                  type: 'quantizedFlat'
                }
              ]
            }
            // text-embedding-3-small; a vector policy cannot be changed after the container exists.
            vectorEmbeddingPolicy: {
              vectorEmbeddings: [
                {
                  path: '/embedding'
                  dataType: 'float32'
                  distanceFunction: 'cosine'
                  dimensions: 1536
                }
              ]
            }
          }
        ]
      }
    ]
    sqlRoleDefinitions: [
      {
        name: memoryRoleName
        roleName: 'FA memory data without delete'
        dataActions: memoryDataActions
      }
    ]
    // Not the definition's nested assignments: AVM 0.21.1 drops their scope, so the role would cover the whole
    // account. This list passes it; the module rewrites the ARM ID to the data-plane form (/dbs/<db>/colls/<coll>).
    sqlRoleAssignments: [
      {
        roleDefinitionId: memoryRoleName
        principalId: apiPrincipalId
        scope: containerResourceId
      }
    ]
  }
}

// documentEndpoint (https://<name>.documents.azure.com:443/), the form the API's FA_COSMOS_ENDPOINT check accepts.
output endpoint string = account.outputs.endpoint
