targetScope = 'resourceGroup'

// Remote web test console (ADR-0008 §3): Container Apps + built-in Entra authentication, one owner, no anonymous path.
// The Entra app (E1-E5) and its federated credential (E4) are created outside this template (Microsoft Graph).
// AVM modules are pinned to the versions FA already uses; the one native resource is explained in web-console-auth.bicep.

param location string = resourceGroup().location
param environmentName string = 'cae-facontext-stage'
param workloadProfileName string = 'Consumption'
param registryName string = 'crfacontextstagedemo'
param vnetName string = 'vnet-facontext-api'
param privateEndpointSubnetName string = 'snet-private-endpoints'
@description('ADR-0010 phase 2b (an existing VNet in another resource group): the private endpoint subnet ID. Empty uses vnetName and privateEndpointSubnetName here.')
param privateEndpointSubnetResourceId string = ''
@description('false (FA_DNS_MODE=existing): the private endpoint gets no DNS zone group; the user serves the name.')
param privateDnsZones bool = true
param appName string = 'ca-fa-console-stage'
param identityName string = 'id-fa-console-stage'
@minLength(3)
@maxLength(24)
param storageName string
param journalContainerName string = 'journal'
param tokenContainerName string = 'easyauth-tokens'
@description('Resource environment tag; defaults to the existing stage deployment.')
param environmentTag string = 'stage'

@description('Entra app registration (E1) used for sign-in; never the Azure CLI client.')
param entraClientId string
@description('The single signed-in human allowed in; the FA API still checks oid→Team owner.')
param ownerOid string
@description('FA API application ID URI; the console requests only Behavior.Read and Behavior.Approve on it.')
param apiAudience string

@minLength(64)
@maxLength(64)
param imageSha256 string
param revisionSuffix string = ''

@description('Stage keeps one replica so a message after idle does not wait for the console to start (Neal 2026-10-07).')
@allowed([0, 1])
param minReplicas int = 1

@description('False on the first deployment of a new app: its sign-in config (module auth) exists only after the app, so until then the console is reachable only inside the environment. azd sets true in the second deployment.')
param ingressExternal bool = true

@description('Exact console configuration files (file name → content), mounted read-only at /config.')
@secure()
param configFiles object
@minLength(64)
@maxLength(64)
param consoleConfigSha256 string
@minLength(64)
@maxLength(64)
param instructionsSha256 string
@minLength(64)
@maxLength(64)
param agentsSha256 string
@description('The console container ENV: the profile console_environment, the one source shared with the CI console step.')
param environmentVariables environmentVariableType[]

type environmentVariableType = {
  name: string
  value: string
}

var tags = {
  workload: 'hosted-sandbox-poc'
  architecture: 'remote-web-console'
  environment: environmentTag
}
var blobDnsZoneName = 'privatelink.blob.${environment().suffixes.storage}'
var storageBlobDataContributor = 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'
var acrPull = '7f951dda-4ed3-4680-a7ca-43fe172d538d'
var ficSecretName = 'override-use-mi-fic-assertion-client-id'
var configEntries = items(configFiles)

resource cae 'Microsoft.App/managedEnvironments@2026-01-01' existing = {
  name: environmentName
}
resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = {
  name: registryName
}
resource vnet 'Microsoft.Network/virtualNetworks@2024-05-01' existing = {
  name: vnetName
}
resource peSubnet 'Microsoft.Network/virtualNetworks/subnets@2024-05-01' existing = {
  parent: vnet
  name: privateEndpointSubnetName
}
resource blobDns 'Microsoft.Network/privateDnsZones@2024-06-01' existing = {
  name: blobDnsZoneName
}

var publicOrigin = 'https://${appName}.${cae.properties.defaultDomain}'

module identity 'br/public:avm/res/managed-identity/user-assigned-identity:0.6.0' = {
  name: 'fa-web-console-identity'
  params: {
    name: identityName
    location: location
    tags: tags
    enableTelemetry: false
  }
}

module storage 'br/public:avm/res/storage/storage-account:0.33.0' = {
  name: 'fa-web-console-storage'
  params: {
    name: storageName
    location: location
    kind: 'StorageV2'
    skuName: 'Standard_LRS'
    accessTier: 'Hot'
    enableTelemetry: false
    tags: tags
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
      containers: [for container in [journalContainerName, tokenContainerName]: {
        name: container
        publicAccess: 'None'
        roleAssignments: [
          {
            principalId: identity.outputs.principalId
            principalType: 'ServicePrincipal'
            roleDefinitionIdOrName: storageBlobDataContributor
          }
        ]
      }]
    }
    privateEndpoints: [
      {
        service: 'blob'
        subnetResourceId: privateEndpointSubnetResourceId == '' ? peSubnet.id : privateEndpointSubnetResourceId
        privateDnsZoneGroup: privateDnsZones ? {
          privateDnsZoneGroupConfigs: [
            {
              privateDnsZoneResourceId: blobDns.id
            }
          ]
        } : null
      }
    ]
  }
}

module registryPull 'br/public:avm/ptn/authorization/resource-role-assignment:0.1.2' = {
  name: 'fa-web-console-acr-pull'
  params: {
    resourceId: registry.id
    roleDefinitionId: acrPull
    principalId: identity.outputs.principalId
    principalType: 'ServicePrincipal'
    enableTelemetry: false
  }
}

module app 'br/public:avm/res/app/container-app:0.23.0' = {
  name: 'fa-web-console-app'
  dependsOn: [registryPull]
  params: {
    name: appName
    location: location
    environmentResourceId: cae.id
    workloadProfileName: workloadProfileName
    enableTelemetry: false
    tags: tags
    managedIdentities: {
      systemAssigned: false
      userAssignedResourceIds: [identity.outputs.resourceId]
    }
    registries: [{server: registry.properties.loginServer, identity: identity.outputs.resourceId}]
    ingressExternal: ingressExternal
    ingressAllowInsecure: false
    ingressTransport: 'http'
    ingressTargetPort: 8080
    activeRevisionsMode: 'Single'
    revisionSuffix: empty(revisionSuffix) ? null : revisionSuffix
    // One replica keeps one journal writer; stage keeps it running (minReplicas, Neal 2026-10-07).
    scaleSettings: {minReplicas: minReplicas, maxReplicas: 1}
    secrets: concat([{name: ficSecretName, value: identity.outputs.clientId}], map(configEntries, file => {
      name: 'cfg-${toLower(replace(file.key, '.', '-'))}'
      value: file.value
    }))
    volumes: [
      {
        name: 'console-config'
        storageType: 'Secret'
        secrets: map(configEntries, file => {
          secretRef: 'cfg-${toLower(replace(file.key, '.', '-'))}'
          path: file.key
        })
      }
    ]
    containers: [
      {
        name: 'web-console'
        image: '${registry.properties.loginServer}/fa-web-console@sha256:${imageSha256}'
        args: [
          'serve-remote'
          '--config'
          '/config/console.json'
          '--config-sha256'
          consoleConfigSha256
          '--instructions'
          '/config/instructions.json'
          '--instructions-sha256'
          instructionsSha256
          '--agents'
          '/config/agents.json'
          '--agents-sha256'
          agentsSha256
          '--journal'
          '/data/state/console.sqlite3'
          '--state-root'
          '/data/state'
          '--state-blob-url'
          '${storage.outputs.primaryBlobEndpoint}${journalContainerName}/${ownerOid}/state.tar'
          '--public-origin'
          publicOrigin
          '--owner-oid'
          ownerOid
          '--managed-identity-client-id'
          identity.outputs.clientId
        ]
        resources: {cpu: json('0.25'), memory: '0.5Gi'}
        env: environmentVariables
        volumeMounts: [{volumeName: 'console-config', mountPath: '/config'}]
        // Probes reach the container directly, without the sign-in headers, so only the port is checked.
        probes: [
          {
            type: 'Startup'
            tcpSocket: {port: 8080}
            initialDelaySeconds: 1
            periodSeconds: 5
            failureThreshold: 48
          }
          {
            type: 'Readiness'
            tcpSocket: {port: 8080}
            periodSeconds: 10
            failureThreshold: 3
          }
        ]
      }
    ]
  }
}

module auth './web-console-auth.bicep' = {
  name: 'fa-web-console-auth'
  params: {
    appName: app.outputs.name
    entraClientId: entraClientId
    ficSecretName: ficSecretName
    ownerOid: ownerOid
    apiAudience: apiAudience
    tokenContainerUri: '${storage.outputs.primaryBlobEndpoint}${tokenContainerName}'
    identityResourceId: identity.outputs.resourceId
    publicOrigin: publicOrigin
  }
}

output appResourceId string = app.outputs.resourceId
output publicOrigin string = publicOrigin
output redirectUri string = '${publicOrigin}/.auth/login/aad/callback'
output identityPrincipalId string = identity.outputs.principalId
output identityClientId string = identity.outputs.clientId
