targetScope = 'resourceGroup'

// 先套用 backup-role.bicep，再僅對新備份帳號執行此 additive 模板。
// 既有 VNet、ACA environment、Table 與 Table DNS 不在此模板的修改範圍。
param location string = 'westus3'
param storageName string = 'stfam1backupdemo'
param containerName string = 'm1-environment-backups'
param apiIdentityName string = 'id-facontext-api'
param vnetName string = 'vnet-facontext-api'
param privateEndpointSubnetName string = 'snet-private-endpoints'
param privateEndpointName string = 'pe-fam1-backup-blob'

@description('僅在本 RG 尚無 Blob private DNS zone 時設為 true；既有 zone 不重新部署。')
param createBlobPrivateDnsZone bool = true

@description('僅在 Blob zone 尚無此 VNet 連結時設為 true；已有連結時設為 false。')
param createBlobDnsVnetLink bool = true
param blobDnsVnetLinkName string = 'facontext-api-link'

var tags = {
  workload: 'hosted-sandbox-poc'
  architecture: 'm1-private-environment-backup'
  dataClassification: 'synthetic-only'
}
var blobDnsZoneName = 'privatelink.blob.${environment().suffixes.storage}'

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

resource backupRole 'Microsoft.Authorization/roleDefinitions@2022-04-01' existing = {
  scope: subscription()
  name: guid(resourceGroup().id, 'fa-m1-backup-data-read-write-v1')
}

module storage 'br/public:avm/res/storage/storage-account:0.33.0' = {
  name: 'm1-backup-storage'
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
      isVersioningEnabled: true
      deleteRetentionPolicyEnabled: true
      deleteRetentionPolicyDays: 7
      deleteRetentionPolicyAllowPermanentDelete: false
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
      name: 'm1-backup-account-delete-protection'
    }
  }
}

resource backupAccount 'Microsoft.Storage/storageAccounts@2025-01-01' existing = {
  name: storageName
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2025-01-01' existing = {
  parent: backupAccount
  name: 'default'
}

resource backupContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2025-01-01' existing = {
  parent: blobService
  name: containerName
}

resource apiBackupAccess 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: backupContainer
  // 使用既有 UAMI resource ID 作穩定種子；若 MI 曾重建，須先人工檢查舊指派。
  name: guid(backupContainer.id, apiIdentity.id, backupRole.id)
  properties: {
    roleDefinitionId: backupRole.id
    principalId: apiIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
  dependsOn: [
    storage
  ]
}

module blobDns 'br/public:avm/res/network/private-dns-zone:0.8.1' = if (createBlobPrivateDnsZone) {
  name: 'm1-backup-blob-dns'
  params: {
    name: blobDnsZoneName
    tags: tags
    enableTelemetry: false
  }
}

resource existingBlobDns 'Microsoft.Network/privateDnsZones@2024-06-01' existing = {
  name: blobDnsZoneName
}

module blobDnsLink 'br/public:avm/res/network/private-dns-zone/virtual-network-link:0.1.0' = if (createBlobDnsVnetLink) {
  name: 'm1-backup-blob-dns-link'
  params: {
    privateDnsZoneName: blobDnsZoneName
    name: blobDnsVnetLinkName
    location: 'global'
    tags: tags
    enableTelemetry: false
    registrationEnabled: false
    virtualNetworkResourceId: vnet.id
  }
  dependsOn: [
    blobDns
  ]
}

module blobEndpoint 'br/public:avm/res/network/private-endpoint:0.12.1' = {
  name: 'm1-backup-blob-private-endpoint'
  params: {
    name: privateEndpointName
    location: location
    tags: tags
    enableTelemetry: false
    subnetResourceId: subnet.id
    privateLinkServiceConnections: [
      {
        name: 'm1-backup-blob'
        properties: {
          privateLinkServiceId: storage.outputs.resourceId
          groupIds: [
            'blob'
          ]
        }
      }
    ]
    privateDnsZoneGroup: {
      name: 'default'
      privateDnsZoneGroupConfigs: [
        {
          name: 'blob'
          privateDnsZoneResourceId: existingBlobDns.id
        }
      ]
    }
  }
  dependsOn: [
    blobDns
  ]
}

output backupBlobEndpoint string = 'https://${storageName}.blob.${environment().suffixes.storage}'
output backupContainerName string = containerName
output backupStorageResourceId string = storage.outputs.resourceId
output backupContainerResourceId string = backupContainer.id
output backupRoleDefinitionId string = backupRole.id
output backupRoleAssignmentId string = apiBackupAccess.id
output apiIdentityPrincipalId string = apiIdentity.properties.principalId
output backupPrivateEndpointId string = blobEndpoint.outputs.resourceId
