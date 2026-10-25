targetScope = 'resourceGroup'

// ADR-0009 phase 5 (Neal 2026-10-17): idle-upgrade workspace snapshots live in their OWN storage account, never on
// the no-auto-gc backup account (backup-foundation.bicep), whose protection check refuses ANY enabled delete rule
// (source_materials.py verify(): SOURCE_PROTECTION_UNVERIFIED). So the 30-day lifecycle rule (5c) is safe here.
// Opt-in: with deployUpgradeSnapshots=false (the default) this template creates nothing.
// Creating the account, its delete lock and the role assignment are cloud and IAM writes: each deploy needs Neal's approval (dev too).
// Private and keyless like the backup account; reached through a private endpoint in the existing Blob private DNS
// zone (backup-foundation.bicep creates it and its VNet link), so apply this after that template.
// Real deletion is 30 days plus the 7-day blob soft delete below: about 37 days.
@description('false (default) deploys nothing; true creates the account, container, private endpoint and API role.')
param deployUpgradeSnapshots bool = false
param location string = resourceGroup().location
param storageName string
param containerName string = 'upgrade-snapshots'
param apiIdentityName string
param vnetName string
param privateEndpointSubnetName string = 'snet-private-endpoints'
param privateEndpointName string
param tags object = {}
@description('ADR-0010 phase 2b (an existing VNet): the private endpoint subnet ID. Empty uses vnetName and privateEndpointSubnetName in this resource group.')
param privateEndpointSubnetResourceId string = ''
@description('false (FA_DNS_MODE=existing): the private endpoint gets no DNS zone group; the user serves the name.')
param privateDnsZones bool = true

// prefixMatch starts with the container name (lifecycle-management-policy-structure); the root is SNAPSHOT_ROOT
// in shared_context/upgrade_snapshot_store.py.
var snapshotPrefix = '${containerName}/fa-upgrade-snapshots/'
var blobDnsZoneName = 'privatelink.blob.${environment().suffixes.storage}'

// The custom role backup-role.bicep creates (blob read and write, no delete): FA never deletes, the lifecycle rule does.
// Apply backup-role.bicep to this resource group first.
resource snapshotRole 'Microsoft.Authorization/roleDefinitions@2022-04-01' existing = {
  scope: subscription()
  name: guid(resourceGroup().id, 'fa-m1-backup-data-read-write-v1')
}

resource apiIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' existing = {
  name: apiIdentityName
}

resource vnet 'Microsoft.Network/virtualNetworks@2024-05-01' existing = {
  name: vnetName
}

resource subnet 'Microsoft.Network/virtualNetworks/subnets@2024-05-01' existing = {
  parent: vnet
  name: privateEndpointSubnetName
}

resource blobDns 'Microsoft.Network/privateDnsZones@2024-06-01' existing = {
  name: blobDnsZoneName
}

module storage 'br/public:avm/res/storage/storage-account:0.33.0' = if (deployUpgradeSnapshots) {
  name: 'fa-upgrade-snapshot-storage'
  params: {
    name: storageName
    location: location
    tags: tags
    enableTelemetry: false
    kind: 'StorageV2'
    skuName: 'Standard_LRS'
    accessTier: 'Hot'
    enableHierarchicalNamespace: false
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
    blobServices: {
      isVersioningEnabled: false
      deleteRetentionPolicyEnabled: true
      deleteRetentionPolicyDays: 7
      containerDeleteRetentionPolicyEnabled: true
      containerDeleteRetentionPolicyDays: 7
      containers: [
        {
          name: containerName
          publicAccess: 'None'
        }
      ]
    }
    lock: {
      kind: 'CanNotDelete'
      name: 'upgrade-snapshot-account-delete-protection'
    }
    // ADR-0009 5c: FA never deletes snapshots; the platform deletes them 30 days after they were written.
    managementPolicyRules: [
      {
        name: 'fa-upgrade-snapshots-30d'
        enabled: true
        type: 'Lifecycle'
        definition: {
          filters: {
            blobTypes: [
              'blockBlob'
            ]
            prefixMatch: [
              snapshotPrefix
            ]
          }
          actions: {
            baseBlob: {
              delete: {
                daysAfterCreationGreaterThan: 30
              }
            }
          }
        }
      }
    ]
  }
}

resource snapshotAccount 'Microsoft.Storage/storageAccounts@2025-01-01' existing = {
  name: storageName
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2025-01-01' existing = {
  parent: snapshotAccount
  name: 'default'
}

resource snapshotContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2025-01-01' existing = {
  parent: blobService
  name: containerName
}

// Only the API identity writes snapshots (ADR-0006: API thin forwarding); scoped to the one container.
resource apiSnapshotAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (deployUpgradeSnapshots) {
  scope: snapshotContainer
  name: guid(snapshotContainer.id, apiIdentity.id, snapshotRole.id)
  properties: {
    roleDefinitionId: snapshotRole.id
    principalId: apiIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
  dependsOn: [
    storage
  ]
}

module blobEndpoint 'br/public:avm/res/network/private-endpoint:0.12.1' = if (deployUpgradeSnapshots) {
  name: 'fa-upgrade-snapshot-blob-private-endpoint'
  params: {
    name: privateEndpointName
    location: location
    tags: tags
    enableTelemetry: false
    subnetResourceId: privateEndpointSubnetResourceId == '' ? subnet.id : privateEndpointSubnetResourceId
    privateLinkServiceConnections: [
      {
        name: 'fa-upgrade-snapshot-blob'
        properties: {
          privateLinkServiceId: snapshotAccount.id
          groupIds: [
            'blob'
          ]
        }
      }
    ]
    privateDnsZoneGroup: privateDnsZones ? {
      name: 'default'
      privateDnsZoneGroupConfigs: [
        {
          name: 'blob'
          privateDnsZoneResourceId: blobDns.id
        }
      ]
    } : null
  }
  dependsOn: [
    storage
  ]
}

// FA_UPGRADE_SNAPSHOT_CONTAINER_URL for the API; empty when nothing was deployed (phase 5 stays off).
output containerUrl string = deployUpgradeSnapshots ? 'https://${storageName}.blob.${environment().suffixes.storage}/${containerName}' : ''
