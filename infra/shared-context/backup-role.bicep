targetScope = 'subscription'

@description('角色僅可在此既有 RG 及其子資源指派；此模板不建立 RG，也不建立任何 role assignment。')
param backupResourceGroupName string = 'rg-faexample'
@description('Custom role names are unique per tenant; stage uses its own name.')
param roleName string = 'FA M1 Backup Data Read Write'

resource backupResourceGroup 'Microsoft.Resources/resourceGroups@2025-04-01' existing = {
  name: backupResourceGroupName
}
var backupResourceGroupId = backupResourceGroup.id

// 角色定義採 subscription 級資源；可指派範圍仍僅限單一 RG。
// read 可能涵蓋列舉；此角色不抵銷其他直接或繼承授權中的 delete。
// https://learn.microsoft.com/azure/role-based-access-control/custom-roles
resource backupRole 'Microsoft.Authorization/roleDefinitions@2022-04-01' = {
  name: guid(backupResourceGroupId, 'fa-m1-backup-data-read-write-v1')
  properties: {
    roleName: roleName
    description: 'M1 私人備份 Blob 讀寫；不授予刪除、管理或 delegation key。'
    type: 'CustomRole'
    assignableScopes: [
      backupResourceGroupId
    ]
    permissions: [
      {
        actions: []
        notActions: []
        dataActions: [
          'Microsoft.Storage/storageAccounts/blobServices/containers/blobs/read'
          'Microsoft.Storage/storageAccounts/blobServices/containers/blobs/write'
        ]
        notDataActions: []
      }
    ]
  }
}

output backupRoleDefinitionId string = backupRole.id
output backupAssignableScope string = backupResourceGroupId
