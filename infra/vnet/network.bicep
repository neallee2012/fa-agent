targetScope = 'resourceGroup'

param location string = 'westus3'
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
param tags object = {
  workload: 'hosted-sandbox-poc'
  environment: 'poc'
  dataClassification: 'synthetic-only'
}

var privateDnsZoneNames = [
  'privatelink.cognitiveservices.azure.com'
  'privatelink.openai.azure.com'
  'privatelink.services.ai.azure.com'
  'privatelink.vaultcore.azure.net'
  'privatelink.file.${environment().suffixes.storage}'
  'privatelink.azurecr.io'
]

module outboundPublicIp 'br/public:avm/res/network/public-ip-address:0.13.0' = {
  name: 'vnet-outbound-public-ip'
  params: {
    name: natPublicIpName
    location: location
    tags: tags
    enableTelemetry: false
    skuName: 'Standard'
    publicIPAllocationMethod: 'Static'
    publicIPAddressVersion: 'IPv4'
    availabilityZones: []
  }
}

module natGateway 'br/public:avm/res/network/nat-gateway:2.1.1' = {
  name: 'vnet-outbound-nat'
  params: {
    name: natGatewayName
    location: location
    tags: tags
    enableTelemetry: false
    natGatewaySku: 'Standard'
    availabilityZone: -1
    idleTimeoutInMinutes: 4
    publicIpResourceIds: [
      resourceId('Microsoft.Network/publicIPAddresses', natPublicIpName)
    ]
  }
  dependsOn: [
    outboundPublicIp
  ]
}

// Keep the default VNet/platform and internet egress rules for Foundry provisioning,
// Entra, DNS and package downloads. NAT provides explicit egress, not an egress firewall.
module agentNsg 'br/public:avm/res/network/network-security-group:0.5.3' = {
  name: 'vnet-agent-nsg'
  params: {
    name: agentNsgName
    location: location
    tags: tags
    enableTelemetry: false
    securityRules: []
  }
}

module controllerNsg 'br/public:avm/res/network/network-security-group:0.5.3' = {
  name: 'vnet-controller-nsg'
  params: {
    name: controllerNsgName
    location: location
    tags: tags
    enableTelemetry: false
    securityRules: [
      {
        name: 'DenyAllInbound'
        properties: {
          description: 'ARM Run Command uses the outbound VM agent; no inbound SSH is needed.'
          priority: 4096
          access: 'Deny'
          direction: 'Inbound'
          protocol: '*'
          sourceAddressPrefix: '*'
          sourcePortRange: '*'
          destinationAddressPrefix: '*'
          destinationPortRange: '*'
        }
      }
    ]
  }
}

module vnet 'br/public:avm/res/network/virtual-network:0.10.2' = {
  name: 'vnet-network'
  params: {
    name: vnetName
    location: location
    tags: tags
    enableTelemetry: false
    addressPrefixes: [
      vnetAddressPrefix
    ]
    subnets: [
      {
        name: agentSubnetName
        addressPrefix: agentSubnetPrefix
        delegation: 'Microsoft.App/environments'
        natGatewayResourceId: resourceId('Microsoft.Network/natGateways', natGatewayName)
        networkSecurityGroupResourceId: resourceId('Microsoft.Network/networkSecurityGroups', agentNsgName)
        defaultOutboundAccess: false
      }
      {
        name: privateEndpointSubnetName
        addressPrefix: privateEndpointSubnetPrefix
        privateEndpointNetworkPolicies: 'Disabled'
        defaultOutboundAccess: false
      }
      {
        name: controllerSubnetName
        addressPrefix: controllerSubnetPrefix
        natGatewayResourceId: resourceId('Microsoft.Network/natGateways', natGatewayName)
        networkSecurityGroupResourceId: resourceId('Microsoft.Network/networkSecurityGroups', controllerNsgName)
        defaultOutboundAccess: false
      }
    ]
  }
  dependsOn: [
    natGateway
    agentNsg
    controllerNsg
  ]
}

// All six zones are owned by this deployment's RG. Never link or update another project's zones.
module privateDnsZones 'br/public:avm/res/network/private-dns-zone:0.8.1' = [
  for zoneName in privateDnsZoneNames: {
    name: 'dns-${uniqueString(zoneName)}'
    params: {
      name: zoneName
      tags: tags
      enableTelemetry: false
      virtualNetworkLinks: [
        {
          name: '${vnetName}-link'
          virtualNetworkResourceId: resourceId('Microsoft.Network/virtualNetworks', vnetName)
          registrationEnabled: false
          tags: tags
        }
      ]
    }
    dependsOn: [
      vnet
    ]
  }
]

output vnetId string = vnet.outputs.resourceId
output vnetName string = vnet.outputs.name
output agentSubnetId string = vnet.outputs.subnetResourceIds[0]
output privateEndpointSubnetId string = vnet.outputs.subnetResourceIds[1]
output controllerSubnetId string = vnet.outputs.subnetResourceIds[2]
output natGatewayId string = natGateway.outputs.resourceId
output outboundPublicIpId string = outboundPublicIp.outputs.resourceId
output privateDnsZoneIds array = [
  for (zoneName, index) in privateDnsZoneNames: privateDnsZones[index].outputs.resourceId
]
