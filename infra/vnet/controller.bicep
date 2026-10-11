targetScope = 'resourceGroup'

param location string = 'westus3'
param controllerName string = 'vm-fasample-controller'
param vmSize string = 'Standard_D2ls_v7'
param adminUsername string = 'pocadmin'

@description('Required SSH public key only. Generate outside the template; never supply or persist a private key.')
@minLength(32)
@secure()
param sshPublicKey string

param controllerSubnetId string
param accountName string
param projectName string

@description('Ubuntu 24.04 minimal x64 Gen2; westus3 image metadata confirms DiskControllerTypes=SCSI,NVMe. Pin a version after preflight if required.')
param imageVersion string = 'latest'

@minValue(30)
param osDiskSizeGB int = 32

param tags object = {
  workload: 'hosted-sandbox-poc'
  environment: 'poc'
  dataClassification: 'synthetic-only'
}

module vm 'br/public:avm/res/compute/virtual-machine:0.22.3' = {
  name: 'vnet-controller-vm'
  params: {
    name: controllerName
    location: location
    vmSize: vmSize
    diskControllerType: 'NVMe'
    tags: tags
    enableTelemetry: false
    osType: 'Linux'
    availabilityZone: -1
    adminUsername: adminUsername
    disablePasswordAuthentication: true
    provisionVMAgent: true
    allowExtensionOperations: true
    bootDiagnostics: false
    managedIdentities: {
      systemAssigned: true
    }
    imageReference: {
      publisher: 'Canonical'
      offer: 'ubuntu-24_04-lts'
      sku: 'minimal'
      version: imageVersion
    }
    osDisk: {
      name: '${controllerName}-os'
      createOption: 'FromImage'
      diskSizeGB: osDiskSizeGB
      caching: 'ReadWrite'
      managedDisk: {
        storageAccountType: 'StandardSSD_LRS'
      }
    }
    publicKeys: [
      {
        keyData: sshPublicKey
        path: '/home/${adminUsername}/.ssh/authorized_keys'
      }
    ]
    nicConfigurations: [
      {
        name: '${controllerName}-nic'
        enableAcceleratedNetworking: false
        enableIPForwarding: false
        ipConfigurations: [
          {
            name: 'ipconfig-private'
            subnetResourceId: controllerSubnetId
            privateIPAllocationMethod: 'Dynamic'
            privateIPAddressVersion: 'IPv4'
          }
        ]
      }
    ]
  }
}

module controllerProjectUser './project-access.bicep' = {
  name: 'vnet-controller-project-user'
  params: {
    accountName: accountName
    projectName: projectName
    principalId: vm.outputs.systemAssignedMIPrincipalId!
    principalType: 'ServicePrincipal'
  }
}

output controllerVmId string = vm.outputs.resourceId
output controllerVmName string = vm.outputs.name
output controllerPrincipalId string = vm.outputs.systemAssignedMIPrincipalId!
