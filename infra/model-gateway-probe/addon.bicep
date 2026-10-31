// Throwaway model-gateway probe, deployed into the template-15 resource group after it, built from AVM modules.
// The template-15 agent subnet is not redeclared here (that would reset its delegation and NSG);
// the operator attaches rt-agent-blackhole to it with `az network vnet subnet update --route-table` during T6.

param location string = resourceGroup().location
param vnetAName string = 'vnet-fa-stdpriv'
param apimSubnetPrefix string = '10.251.2.0/27'
param vnetBPrefix string = '10.252.0.0/16'
param vnetBSubnetPrefix string = '10.252.0.0/24'
param foundryAccountName string
param tenantId string = tenant().tenantId
@description('Placeholder at deploy; after T0 prints the appid claim, set it with a PUT on the API policy (docs/model-gateway-probe.md step 3), not a redeploy')
param agentAppId string = '00000000-0000-0000-0000-000000000000'
param apimName string
param publisherEmail string
param publisherName string = 'FA gateway probe'
param adminUsername string = 'probe'
param sshPublicKey string
param deployNva bool = false
@description('VM size for the mock and NVA; B-series and D v5/v6 had no westus3 capacity on the first run')
param vmSize string = 'Standard_D2ls_v7'
@secure()
@description('Dummy key shared with the hosted agent; not a real secret')
param mockApiKey string

var cognitiveAudience = 'https://cognitiveservices.azure.com'
var openAiUserRole = '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'
var probeDir = '../../scripts/model-gateway-probe'

resource vnetA 'Microsoft.Network/virtualNetworks@2024-05-01' existing = {
  name: vnetAName
}

resource foundry 'Microsoft.CognitiveServices/accounts@2025-06-01' existing = {
  name: foundryAccountName
}

// Rules from Learn "Deploy your Azure API Management instance to a virtual network - internal mode".
module apimNsg 'br/public:avm/res/network/network-security-group:0.5.3' = {
  name: 'gwprobe-nsg-apim'
  params: {
    name: 'nsg-apim'
    location: location
    enableTelemetry: false
    securityRules: [
      { name: 'in-management', properties: { priority: 100, direction: 'Inbound', access: 'Allow', protocol: 'Tcp', sourceAddressPrefix: 'ApiManagement', sourcePortRange: '*', destinationAddressPrefix: 'VirtualNetwork', destinationPortRange: '3443' } }
      { name: 'in-load-balancer', properties: { priority: 110, direction: 'Inbound', access: 'Allow', protocol: 'Tcp', sourceAddressPrefix: 'AzureLoadBalancer', sourcePortRange: '*', destinationAddressPrefix: 'VirtualNetwork', destinationPortRange: '6390' } }
      { name: 'out-storage', properties: { priority: 100, direction: 'Outbound', access: 'Allow', protocol: 'Tcp', sourceAddressPrefix: 'VirtualNetwork', sourcePortRange: '*', destinationAddressPrefix: 'Storage', destinationPortRange: '443' } }
      { name: 'out-sql', properties: { priority: 110, direction: 'Outbound', access: 'Allow', protocol: 'Tcp', sourceAddressPrefix: 'VirtualNetwork', sourcePortRange: '*', destinationAddressPrefix: 'Sql', destinationPortRange: '1433' } }
      { name: 'out-key-vault', properties: { priority: 120, direction: 'Outbound', access: 'Allow', protocol: 'Tcp', sourceAddressPrefix: 'VirtualNetwork', sourcePortRange: '*', destinationAddressPrefix: 'AzureKeyVault', destinationPortRange: '443' } }
      { name: 'out-monitor', properties: { priority: 130, direction: 'Outbound', access: 'Allow', protocol: 'Tcp', sourceAddressPrefix: 'VirtualNetwork', sourcePortRange: '*', destinationAddressPrefix: 'AzureMonitor', destinationPortRanges: ['1886', '443'] } }
      { name: 'out-internet-http', properties: { priority: 140, direction: 'Outbound', access: 'Allow', protocol: 'Tcp', sourceAddressPrefix: 'VirtualNetwork', sourcePortRange: '*', destinationAddressPrefix: 'Internet', destinationPortRange: '80' } }
    ]
  }
}

// The subnet child module adds one subnet to the existing VNet A without redeclaring the VNet.
module apimSubnet 'br/public:avm/res/network/virtual-network/subnet:0.2.0' = {
  name: 'gwprobe-apim-subnet'
  params: {
    name: 'apim-subnet'
    virtualNetworkName: vnetA.name
    addressPrefix: apimSubnetPrefix
    networkSecurityGroupResourceId: apimNsg.outputs.resourceId
    enableTelemetry: false
  }
}

module apim 'br/public:avm/res/api-management/service:0.15.0' = {
  name: 'gwprobe-apim'
  params: {
    name: apimName
    location: location
    sku: 'Developer'
    managedIdentities: { systemAssigned: true }
    publisherEmail: publisherEmail
    publisherName: publisherName
    virtualNetworkType: 'Internal'
    subnetResourceId: apimSubnet.outputs.resourceId
    enableTelemetry: false
    apis: [
      {
        name: 'foundry-openai-v1'
        displayName: 'Foundry OpenAI v1'
        path: 'foundry/openai/v1'
        serviceUrl: 'https://${foundryAccountName}.openai.azure.com/openai/v1'
        protocols: ['https']
        subscriptionRequired: false
        operations: [
          { name: 'chat-completions', displayName: 'chat-completions', method: 'POST', urlTemplate: '/chat/completions' }
          { name: 'responses', displayName: 'responses', method: 'POST', urlTemplate: '/responses' }
        ]
        // buffer-response="false" keeps the backend SSE stream flowing to the caller instead of arriving at the end.
        policies: [
          {
            format: 'rawxml'
            value: format('''<policies>
  <inbound>
    <base />
    <validate-azure-ad-token tenant-id="{0}" header-name="Authorization" failed-validation-httpcode="401" failed-validation-error-message="Unauthorized">
      <client-application-ids>
        <application-id>{1}</application-id>
      </client-application-ids>
      <audiences>
        <audience>{2}</audience>
      </audiences>
    </validate-azure-ad-token>
    <authentication-managed-identity resource="{2}" />
  </inbound>
  <backend>
    <forward-request buffer-response="false" />
  </backend>
  <outbound>
    <base />
  </outbound>
  <on-error>
    <base />
  </on-error>
</policies>''', tenantId, agentAppId, cognitiveAudience)
          }
        ]
      }
    ]
  }
}

// The APIM module does not output gatewayUrl or the private IP; this read-back creates nothing.
resource apimRead 'Microsoft.ApiManagement/service@2024-05-01' existing = {
  name: apimName
  dependsOn: [apim]
}

module apimOpenAiUser 'br/public:avm/ptn/authorization/resource-role-assignment:0.1.2' = {
  name: 'gwprobe-apim-openai-user'
  params: {
    resourceId: foundry.id
    name: guid(foundry.id, apim.outputs.resourceId, openAiUserRole)
    roleDefinitionId: openAiUserRole
    principalId: apim.outputs.systemAssignedMIPrincipalId!
    principalType: 'ServicePrincipal'
    enableTelemetry: false
  }
}

module vnetBNsg 'br/public:avm/res/network/network-security-group:0.5.3' = {
  name: 'gwprobe-nsg-onprem-sim'
  params: {
    name: 'nsg-onprem-sim'
    location: location
    enableTelemetry: false
    securityRules: [
      // Any VNet source, so the mock logs whatever address the micro-VM really uses (T5) instead of dropping it.
      { name: 'in-mock-from-vnet', properties: { priority: 100, direction: 'Inbound', access: 'Allow', protocol: 'Tcp', sourceAddressPrefix: 'VirtualNetwork', sourcePortRange: '*', destinationAddressPrefix: '*', destinationPortRange: '8000' } }
      // The default AllowVnetInBound would otherwise admit every port from the peered VNet, SSH included.
      { name: 'in-deny-vnet', properties: { priority: 4000, direction: 'Inbound', access: 'Deny', protocol: '*', sourceAddressPrefix: 'VirtualNetwork', sourcePortRange: '*', destinationAddressPrefix: '*', destinationPortRange: '*' } }
    ]
  }
}

module vnetB 'br/public:avm/res/network/virtual-network:0.10.2' = {
  name: 'gwprobe-vnet-onprem-sim'
  params: {
    name: 'vnet-onprem-sim'
    location: location
    addressPrefixes: [vnetBPrefix]
    subnets: [
      { name: 'llm-subnet', addressPrefix: vnetBSubnetPrefix, networkSecurityGroupResourceId: vnetBNsg.outputs.resourceId }
    ]
    // Only B->A here: the module's remote peering cannot wait for APIM, so A->B is its own module below.
    peerings: [
      { name: 'to-foundry', remoteVirtualNetworkResourceId: vnetA.id, allowVirtualNetworkAccess: true, allowForwardedTraffic: true, doNotVerifyRemoteGateways: false }
    ]
    enableTelemetry: false
  }
}

// dependsOn apimSubnet and apim: concurrent writes to VNet A (APIM injection links the subnet) fail with AnotherOperationInProgress.
module peerAB 'br/public:avm/res/network/virtual-network/virtual-network-peering:0.2.0' = {
  name: 'gwprobe-peer-to-onprem-sim'
  dependsOn: [apimSubnet, apim]
  params: {
    name: 'to-onprem-sim'
    localVnetName: vnetA.name
    remoteVirtualNetworkResourceId: vnetB.outputs.resourceId
    allowVirtualNetworkAccess: true
    allowForwardedTraffic: true
    doNotVerifyRemoteGateways: false
    enableTelemetry: false
  }
}

var mockUnit = join([
  '[Unit]'
  'Description=OpenAI-compatible mock for the FA gateway probe'
  'After=network-online.target'
  '[Service]'
  'EnvironmentFile=/etc/mock-openai.env'
  'ExecStart=/usr/bin/python3 /opt/probe/mock_openai.py --host 0.0.0.0 --port 8000 --log /var/log/mock-openai.jsonl'
  'Restart=always'
  '[Install]'
  'WantedBy=multi-user.target'
], '\n')

// T3 runs on the mock VM (APIM is reachable only inside the VNets): a wrong identity and no token, both expected 401.
var t3Url = 'https://${apimName}.azure-api.net/foundry/openai/v1/chat/completions'
var t3Targets = [
  { id: 'T3mi', url: t3Url, wire: 'chat', model: 'gpt-5-mini', auth: { type: 'mi', audience: cognitiveAudience } }
  { id: 'T3none', url: t3Url, wire: 'chat', model: 'gpt-5-mini', auth: { type: 'none' } }
]

var mockCloudInit = join([
  '#cloud-config'
  'write_files:'
  '  - path: /opt/probe/mock_openai.py'
  '    encoding: b64'
  '    content: ${base64(loadTextContent('${probeDir}/mock_openai.py'))}'
  '  - path: /opt/probe/probe.py'
  '    encoding: b64'
  '    content: ${base64(loadTextContent('${probeDir}/probe.py'))}'
  '  - path: /opt/probe/targets.t3.json'
  '    encoding: b64'
  '    content: ${base64(string(t3Targets))}'
  '  - path: /etc/mock-openai.env'
  '    permissions: "0600"'
  '    encoding: b64'
  '    content: ${base64('MOCK_API_KEY=${mockApiKey}\n')}'
  '  - path: /etc/systemd/system/mock-openai.service'
  '    encoding: b64'
  '    content: ${base64(mockUnit)}'
  'runcmd:'
  '  - [systemctl, daemon-reload]'
  '  - [systemctl, enable, --now, mock-openai.service]'
], '\n')

var nvaCloudInit = join([
  '#cloud-config'
  'write_files:'
  '  - path: /etc/sysctl.d/99-ip-forward.conf'
  '    encoding: b64'
  '    content: ${base64('net.ipv4.ip_forward = 1\n')}'
  'runcmd:'
  '  - [sysctl, --system]'
], '\n')

var vms = concat([{ name: 'vm-llm-mock', customData: mockCloudInit, ipForwarding: false }],
  deployNva ? [{ name: 'vm-nva', customData: nvaCloudInit, ipForwarding: true }] : [])

// The module base64-encodes customData itself. Accelerated networking stays off (the module defaults it on)
// so vmSize can still be a size without it, such as B1s.
module machines 'br/public:avm/res/compute/virtual-machine:0.22.3' = [for vm in vms: {
  name: 'gwprobe-${vm.name}'
  params: {
    name: vm.name
    location: location
    vmSize: vmSize
    availabilityZone: -1
    osType: 'Linux'
    imageReference: { publisher: 'Canonical', offer: 'ubuntu-24_04-lts', sku: 'server', version: 'latest' }
    osDisk: { createOption: 'FromImage', deleteOption: 'Delete', caching: 'ReadWrite', managedDisk: { storageAccountType: 'Standard_LRS' } }
    adminUsername: adminUsername
    disablePasswordAuthentication: true
    publicKeys: [{ path: '/home/${adminUsername}/.ssh/authorized_keys', keyData: sshPublicKey }]
    customData: vm.customData
    managedIdentities: { systemAssigned: true }
    nicConfigurations: [
      {
        name: 'nic-${vm.name}'
        deleteOption: 'Delete'
        enableIPForwarding: vm.ipForwarding
        enableAcceleratedNetworking: false
        ipConfigurations: [
          { name: 'ipconfig1', privateIPAllocationMethod: 'Dynamic', subnetResourceId: vnetB.outputs.subnetResourceIds[0] }
        ]
      }
    ]
    enableTelemetry: false
  }
}]

var mockVmPrivateIp = machines[0].outputs.nicConfigurations[0].ipConfigurations[0].privateIP!

// Only this APIM's FQDN, never the azure-api.net apex.
module apimZone 'br/public:avm/res/network/private-dns-zone:0.8.1' = {
  name: 'gwprobe-dns-apim'
  params: {
    name: '${apimName}.azure-api.net'
    a: [{ name: '@', ttl: 300, aRecords: [{ ipv4Address: apimRead.properties.privateIPAddresses[0] }] }]
    virtualNetworkLinks: [
      { name: 'link-vnet-a', virtualNetworkResourceId: vnetA.id, registrationEnabled: false }
      { name: 'link-vnet-b', virtualNetworkResourceId: vnetB.outputs.resourceId, registrationEnabled: false }
    ]
    enableTelemetry: false
  }
}

module onpremZone 'br/public:avm/res/network/private-dns-zone:0.8.1' = {
  name: 'gwprobe-dns-onprem-sim'
  params: {
    name: 'onprem-sim.test'
    a: [{ name: 'llm', ttl: 300, aRecords: [{ ipv4Address: mockVmPrivateIp }] }]
    virtualNetworkLinks: [
      { name: 'link-vnet-a', virtualNetworkResourceId: vnetA.id, registrationEnabled: false }
    ]
    enableTelemetry: false
  }
}

module blackhole 'br/public:avm/res/network/route-table:0.5.0' = {
  name: 'gwprobe-rt-agent-blackhole'
  params: {
    name: 'rt-agent-blackhole'
    location: location
    disableBgpRoutePropagation: false
    routes: [{ name: 'onprem-sim-blackhole', properties: { addressPrefix: vnetBPrefix, nextHopType: 'None' } }]
    enableTelemetry: false
  }
}

output apimGatewayUrl string = apimRead.properties.gatewayUrl
output apimPrivateIp string = apimRead.properties.privateIPAddresses[0]
output mockVmPrivateIp string = mockVmPrivateIp
output mockUrl string = 'http://llm.${onpremZone.outputs.name}:8000/v1'
output routeTableId string = blackhole.outputs.resourceId
