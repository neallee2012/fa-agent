targetScope = 'resourceGroup'

param location string = resourceGroup().location
param appName string = 'ca-facontext-poc'
param environmentName string = 'cae-facontext-private'
param identityName string = 'id-facontext-api'
param registryName string = 'crfacontextpocdemo'

@minLength(64)
@maxLength(64)
param imageSha256 string

param revisionSuffix string
param targetPort int
param healthPath string
param environmentVariables environmentVariableType[]

type environmentVariableType = {
  name: string
  value: string
}

resource apiEnvironment 'Microsoft.App/managedEnvironments@2026-01-01' existing = {
  name: environmentName
}

resource apiIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' existing = {
  name: identityName
}

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = {
  name: registryName
}

module api 'br/public:avm/res/app/container-app:0.23.0' = {
  name: 'shared-context-api'
  params: {
    name: appName
    location: location
    environmentResourceId: apiEnvironment.id
    workloadProfileName: 'Consumption'
    enableTelemetry: false
    tags: {
      purpose: 'fa-hosted-shared-context-poc'
      budget: '100-usd-total-poc'
    }
    managedIdentities: {
      systemAssigned: false
      userAssignedResourceIds: [
        apiIdentity.id
      ]
    }
    registries: [
      {
        server: registry.properties.loginServer
        identity: apiIdentity.id
      }
    ]
    ingressExternal: true
    ingressAllowInsecure: false
    ingressTransport: 'http'
    ingressTargetPort: targetPort
    activeRevisionsMode: 'Single'
    revisionSuffix: revisionSuffix
    scaleSettings: {
      minReplicas: 0
      maxReplicas: 1
    }
    containers: [
      {
        name: 'context-api'
        image: '${registry.properties.loginServer}/fa-context-api@sha256:${imageSha256}'
        resources: {
          cpu: json('0.25')
          memory: '0.5Gi'
        }
        env: environmentVariables
        probes: [
          {
            type: 'Readiness'
            httpGet: {
              path: healthPath
              port: targetPort
              scheme: 'HTTP'
            }
            initialDelaySeconds: 5
            periodSeconds: 10
            timeoutSeconds: 3
            failureThreshold: 3
          }
        ]
      }
    ]
  }
}

output resourceId string = api.outputs.resourceId
output endpoint string = 'https://${api.outputs.fqdn}'
