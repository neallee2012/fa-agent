// For foundry-samples commit ce52cffdbd91716723b06eb1bce39f964a23ed45, template
// infrastructure/infrastructure-setup-bicep/15-private-network-standard-agent-setup/main.bicep.
// Copy this file into that folder of a local clone (or edit `using` to the clone path) before deploying.
// The template has no SKU parameters; docs/model-gateway-probe.md lists the two edits to
// modules-network-secured/standard-dependent-resources.bicep (Cosmos serverless, Search basic) the cost assumes.
using './main.bicep'

param location = 'westus3'
param aiServices = 'fagwprobe'
param skipModelDeployment = false
param modelName = 'gpt-5-mini'
param modelFormat = 'OpenAI'
param modelVersion = '2025-08-07'
param modelSkuName = 'GlobalStandard'
param modelCapacity = 10
param firstProjectName = 'probe'
param projectDescription = 'FA probe: hosted agent calling models behind an enterprise AI gateway'
param displayName = 'probe'
param vnetName = 'vnet-fa-stdpriv'
param agentSubnetName = 'agent-subnet'
param peSubnetName = 'pe-subnet'
param vnetAddressPrefix = '10.251.0.0/16'
param agentSubnetPrefix = '10.251.0.0/24'
param peSubnetPrefix = '10.251.1.0/24'
param enableContainerRegistry = true
param developerIpCidr = '<operator public IP>/32'
param existingDnsZones = {
  'privatelink.services.ai.azure.com': ''
  'privatelink.openai.azure.com': ''
  'privatelink.cognitiveservices.azure.com': ''
  'privatelink.search.windows.net': ''
  'privatelink.blob.core.windows.net': ''
  'privatelink.documents.azure.com': ''
  'privatelink.azurecr.io': ''
}
param dnsZoneNames = [
  'privatelink.services.ai.azure.com'
  'privatelink.openai.azure.com'
  'privatelink.cognitiveservices.azure.com'
  'privatelink.search.windows.net'
  'privatelink.blob.core.windows.net'
  'privatelink.documents.azure.com'
]
