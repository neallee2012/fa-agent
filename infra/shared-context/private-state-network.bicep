targetScope = 'resourceGroup'

// The VNet, its subnets and the optional NAT egress. It is its own deployment so that azd (infra/azd/main.bicep) can
// create it before the Foundry account (ADR-0010 section 2); foundation.bicep deploys it itself when run on its own
// (createNetwork) and keeps the Table private endpoint and DNS, which need the storage account.

param location string
param tags object
param vnetName string = 'vnet-facontext-api'
@description('Opt-in NAT egress for snet-api; off keeps the stage/dev network (no NAT). It does not replace a missing ACA environment staticIp (2026-10-07: that was the AllowBringYourOwnPublicIpAddress feature). An environment deployed with it must keep passing true, or this VNet update detaches the NAT.')
param apiSubnetNatEgress bool = false
@allowed(['public', 'private'])
@description('ADR-0010 FA_NETWORK_MODE. private adds snet-agents (192.168.97.0/24, delegated to Microsoft.App/environments, its own NSG) for the Foundry Hosted Agents, NAT egress on every subnet, and the three Foundry private DNS zones linked to this VNet.')
param networkMode string = 'public'
@description('ADR-0010 phase 2b FA_VNET_RESOURCE_ID (private only): an existing VNet in another resource group. Set, this template creates no VNet, subnet, NSG, NAT or public IP and never writes that VNet; only the Foundry DNS zones (privateDnsZones) are linked to it.')
param vnetResourceId string = ''
@description('false (FA_DNS_MODE=existing): the user serves the private DNS names, so no Foundry zone or link is created.')
param privateDnsZones bool = true

var isPrivate = networkMode == 'private'
var byoVnet = vnetResourceId != ''
var natEgress = apiSubnetNatEgress || isPrivate
var natGatewayName = '${vnetName}-nat'
var natPublicIpName = '${vnetName}-egress-pip'
var agentsNsgName = '${vnetName}-agents-nsg'
// Only for the Foundry account's network injection: the platform puts each Hosted session (one IP each) and the
// project's data proxy here, so it shares no resource with Container Apps (ADR-0010 section 3).
var agentSubnet = {
  name: 'snet-agents'
  addressPrefix: '192.168.97.0/24'
  delegation: 'Microsoft.App/environments'
  natGatewayResourceId: resourceId('Microsoft.Network/natGateways', natGatewayName)
  networkSecurityGroupResourceId: resourceId('Microsoft.Network/networkSecurityGroups', agentsNsgName)
  defaultOutboundAccess: false
}
var foundryDnsZones = [
  'privatelink.cognitiveservices.azure.com'
  'privatelink.openai.azure.com'
  'privatelink.services.ai.azure.com'
]
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

module outboundPublicIp 'br/public:avm/res/network/public-ip-address:0.13.0' = if (natEgress && !byoVnet) {
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

module natGateway 'br/public:avm/res/network/nat-gateway:2.1.1' = if (natEgress && !byoVnet) {
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

// Default rules only: the place for a user's own outbound rules (ADR-0010 has no egress filter in its first version).
module agentsNsg 'br/public:avm/res/network/network-security-group:0.5.3' = if (isPrivate && !byoVnet) {
  name: 'shared-context-agents-nsg'
  params: {
    name: agentsNsgName
    location: location
    tags: tags
    enableTelemetry: false
  }
}

module network 'br/public:avm/res/network/virtual-network:0.10.2' = if (!byoVnet) {
  name: 'shared-context-vnet'
  params: {
    name: vnetName
    location: location
    tags: tags
    enableTelemetry: false
    addressPrefixes: isPrivate ? ['192.168.96.0/24', '192.168.97.0/24'] : ['192.168.96.0/24']
    // The AVM module PUTs the whole subnets array, so snet-agents is listed here, never added by another module.
    subnets: isPrivate ? concat(natEgressSubnets, [agentSubnet]) : apiSubnetNatEgress ? natEgressSubnets : baseSubnets
  }
  dependsOn: [
    natGateway
    agentsNsg
  ]
}

module foundryDns 'br/public:avm/res/network/private-dns-zone:0.8.1' = [for (zone, index) in foundryDnsZones: if (isPrivate && privateDnsZones) {
  name: 'shared-context-foundry-dns-${index}'
  params: {
    name: zone
    tags: tags
    enableTelemetry: false
    virtualNetworkLinks: [
      {
        name: '${vnetName}-link'
        virtualNetworkResourceId: byoVnet ? vnetResourceId : network!.outputs.resourceId
        registrationEnabled: false
      }
    ]
  }
}]

// Read by azd's foundry module, which therefore waits for this whole deployment (zones included). With an existing
// VNet main.bicep builds the subnet IDs from the user's names, so these two are empty.
output agentSubnetResourceId string = isPrivate && !byoVnet ? resourceId('Microsoft.Network/virtualNetworks/subnets', vnetName, 'snet-agents') : ''
output privateEndpointSubnetResourceId string = byoVnet ? '' : resourceId('Microsoft.Network/virtualNetworks/subnets', vnetName, 'snet-private-endpoints')
output foundryDnsZoneResourceIds array = isPrivate && privateDnsZones ? map(foundryDnsZones, zone => resourceId('Microsoft.Network/privateDnsZones', zone)) : []
