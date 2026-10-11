targetScope = 'resourceGroup'

param location string
param tags object
param storageResourceId string
param vnetName string = 'vnet-facontext-api'
@description('Opt-in NAT egress for snet-api; off keeps the stage/dev network (no NAT). It does not replace a missing ACA environment staticIp (2026-10-07: that was the AllowBringYourOwnPublicIpAddress feature). An environment deployed with it must keep passing true, or this VNet update detaches the NAT.')
param apiSubnetNatEgress bool = false

var natGatewayName = '${vnetName}-nat'
var natPublicIpName = '${vnetName}-egress-pip'
var baseSubnets = [
  {
    name: 'snet-api'
    addressPrefix: '192.168.96.0/26'
    delegation: 'Microsoft.App/environments'
  }
  {
    name: 'snet-private-endpoints'
    addressPrefix: '192.168.96.64/27'
    privateEndpointNetworkPolicies: 'Disabled'
  }
]
var natEgressSubnets = [
  {
    name: 'snet-api'
    addressPrefix: '192.168.96.0/26'
    delegation: 'Microsoft.App/environments'
    natGatewayResourceId: resourceId('Microsoft.Network/natGateways', natGatewayName)
    defaultOutboundAccess: false
  }
  {
    name: 'snet-private-endpoints'
    addressPrefix: '192.168.96.64/27'
    privateEndpointNetworkPolicies: 'Disabled'
    defaultOutboundAccess: false
  }
]

module outboundPublicIp 'br/public:avm/res/network/public-ip-address:0.13.0' = if (apiSubnetNatEgress) {
  name: 'shared-context-outbound-public-ip'
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

module natGateway 'br/public:avm/res/network/nat-gateway:2.1.1' = if (apiSubnetNatEgress) {
  name: 'shared-context-outbound-nat'
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

module network 'br/public:avm/res/network/virtual-network:0.10.2' = {
  name: 'shared-context-vnet'
  params: {
    name: vnetName
    location: location
    tags: tags
    enableTelemetry: false
    addressPrefixes: [
      '192.168.96.0/24'
    ]
    subnets: apiSubnetNatEgress ? natEgressSubnets : baseSubnets
  }
  dependsOn: [
    natGateway
  ]
}

module tableDns 'br/public:avm/res/network/private-dns-zone:0.8.1' = {
  name: 'shared-context-table-dns'
  params: {
    name: 'privatelink.table.${environment().suffixes.storage}'
    tags: tags
    enableTelemetry: false
    virtualNetworkLinks: [
      {
        name: 'facontext-api-link'
        virtualNetworkResourceId: network.outputs.resourceId
        registrationEnabled: false
      }
    ]
  }
}

module tableEndpoint 'br/public:avm/res/network/private-endpoint:0.12.1' = {
  name: 'shared-context-table-private-endpoint'
  params: {
    name: 'pe-facontext-table'
    location: location
    tags: tags
    enableTelemetry: false
    subnetResourceId: resourceId('Microsoft.Network/virtualNetworks/subnets', vnetName, 'snet-private-endpoints')
    privateLinkServiceConnections: [
      {
        name: 'facontext-table'
        properties: {
          privateLinkServiceId: storageResourceId
          groupIds: [
            'table'
          ]
        }
      }
    ]
    privateDnsZoneGroup: {
      name: 'default'
      privateDnsZoneGroupConfigs: [
        {
          name: 'table'
          privateDnsZoneResourceId: tableDns.outputs.resourceId
        }
      ]
    }
  }
  dependsOn: [
    network
  ]
}

output apiSubnetId string = resourceId('Microsoft.Network/virtualNetworks/subnets', vnetName, 'snet-api')
output privateEndpointId string = tableEndpoint.outputs.resourceId
