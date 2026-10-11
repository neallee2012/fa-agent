targetScope = 'resourceGroup'

// Feed reader MCP server (src/fa-shared-context-poc/README.md "Feed reader MCP"): one small Container App in the
// existing Container Apps environment. It has no Azure role except AcrPull for its own image, no Team data and no
// secret; its only setting is the FA API's Ed25519 public key. Foundry calls it over the public ingress with the
// connection's x-feed-url and x-fa-feed-signature headers (official custom-keys connection; the server checks the
// signature, https://learn.microsoft.com/azure/foundry/mcp/build-your-own-mcp-server#secure-your-mcp-server-endpoint).
// AVM modules are pinned to the versions FA already uses (infra/web-console/web-console.bicep).

param location string = resourceGroup().location
param environmentName string = 'cae-facontext-stage'
param workloadProfileName string = 'Consumption'
param registryName string = 'crfacontextstagedemo'
param appName string = 'ca-fa-feed-stage'
param identityName string = 'id-fa-feed-stage'
param environmentTag string = 'stage'

@minLength(64)
@maxLength(64)
param imageSha256 string
param revisionSuffix string = ''

@description('FA API Ed25519 public key (standard base64, 44 characters); the private key stays in the API secret.')
@minLength(44)
@maxLength(44)
param publicKey string

@description('Stage keeps one replica (Neal 2026-10-07): with 0 the first tools/list after idle waits ~20 s for a start. Dev passes 0.')
@allowed([0, 1])
param minReplicas int = 1

var tags = {
  workload: 'hosted-sandbox-poc'
  architecture: 'feed-reader-mcp'
  environment: environmentTag
}
var acrPull = '7f951dda-4ed3-4680-a7ca-43fe172d538d'

resource cae 'Microsoft.App/managedEnvironments@2026-01-01' existing = {
  name: environmentName
}
resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = {
  name: registryName
}

// Its own pull identity (official preference for image pulls:
// https://learn.microsoft.com/azure/container-apps/managed-identity-image-pull); never the API's identity.
module identity 'br/public:avm/res/managed-identity/user-assigned-identity:0.6.0' = {
  name: 'fa-feed-mcp-identity'
  params: {
    name: identityName
    location: location
    tags: tags
    enableTelemetry: false
  }
}

module registryPull 'br/public:avm/ptn/authorization/resource-role-assignment:0.1.2' = {
  name: 'fa-feed-mcp-acr-pull'
  params: {
    resourceId: registry.id
    roleDefinitionId: acrPull
    principalId: identity.outputs.principalId
    principalType: 'ServicePrincipal'
    enableTelemetry: false
  }
}

module app 'br/public:avm/res/app/container-app:0.23.0' = {
  name: 'fa-feed-mcp-app'
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
    ingressExternal: true
    ingressAllowInsecure: false
    ingressTransport: 'http'
    ingressTargetPort: 8080
    activeRevisionsMode: 'Single'
    revisionSuffix: empty(revisionSuffix) ? null : revisionSuffix
    // One replica keeps the short feed cache in one place. With minReplicas 0 an idle app costs near nothing, but the
    // first Team toolbox tools/list after idle waits for the app to start (dev 10-07: about 20 s of a cold turn).
    scaleSettings: {minReplicas: minReplicas, maxReplicas: 1}
    containers: [
      {
        name: 'feed-mcp'
        image: '${registry.properties.loginServer}/fa-feed-mcp@sha256:${imageSha256}'
        env: [{name: 'FEED_MCP_PUBLIC_KEY', value: publicKey}]
        resources: {cpu: json('0.25'), memory: '0.5Gi'}
        probes: [
          {
            type: 'Startup'
            httpGet: {path: '/healthz', port: 8080}
            initialDelaySeconds: 1
            periodSeconds: 5
            failureThreshold: 24
          }
          {
            type: 'Readiness'
            httpGet: {path: '/healthz', port: 8080}
            periodSeconds: 10
            failureThreshold: 3
          }
        ]
      }
    ]
  }
}

output appResourceId string = app.outputs.resourceId
output mcpUrl string = 'https://${app.outputs.fqdn}/mcp'
output identityPrincipalId string = identity.outputs.principalId
