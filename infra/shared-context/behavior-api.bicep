targetScope = 'resourceGroup'

param location string = resourceGroup().location
param appName string = 'ca-fa-behavior-poc'
param environmentName string = 'cae-facontext-private'
param identityName string = 'id-facontext-api'
param registryName string = 'crfacontextpocdemo'
@minLength(64)
@maxLength(64)
param imageSha256 string
param revisionSuffix string
param environmentVariables environmentVariableType[]
param commandArguments string[] = ['-m', 'shared_context.behavior_server', 'probe']
param tcpReadiness bool = false
@allowed([0, 1])
param minReplicas int = 0
@allowed(['minimal', 'assistant', 'assistant-large'])
param computeProfile string = 'minimal'
@description('Startup probe attempts at 5-second intervals (assistant profiles only).')
@minValue(1)
@maxValue(240)
param startupFailureThreshold int = 48
param tags object = {
  purpose: 'fa-six-gap-validation'
  budget: '100-usd-total-poc'
}

var containerResources = {
  minimal: {cpu: json('0.25'), memory: '0.5Gi'}
  assistant: {cpu: json('1.0'), memory: '2Gi'}
  'assistant-large': {cpu: json('2.0'), memory: '4Gi'}
}[computeProfile]

type environmentVariableType = {
  name: string
  value: string
}

resource environment 'Microsoft.App/managedEnvironments@2026-01-01' existing = {
  name: environmentName
}
resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' existing = {
  name: identityName
}
resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' existing = {
  name: registryName
}

module app 'br/public:avm/res/app/container-app:0.23.0' = {
  name: 'fa-behavior-api'
  params: {
    name: appName
    location: location
    environmentResourceId: environment.id
    workloadProfileName: 'Consumption'
    enableTelemetry: false
    tags: tags
    managedIdentities: {
      systemAssigned: false
      userAssignedResourceIds: [identity.id]
    }
    registries: [{server: registry.properties.loginServer, identity: identity.id}]
    ingressExternal: true
    ingressAllowInsecure: false
    ingressTransport: 'http'
    ingressTargetPort: 8080
    activeRevisionsMode: 'Single'
    revisionSuffix: revisionSuffix
    scaleSettings: {minReplicas: minReplicas, maxReplicas: 1}
    containers: [
      {
        name: 'behavior-api'
        image: '${registry.properties.loginServer}/fa-behavior-api@sha256:${imageSha256}'
        command: ['python']
        args: commandArguments
        resources: containerResources
        env: environmentVariables
        probes: concat(computeProfile != 'minimal' ? [
          {
            type: 'Startup'
            httpGet: {path: '/healthz', port: 8080, scheme: 'HTTP'}
            initialDelaySeconds: 1
            periodSeconds: 5
            timeoutSeconds: 3
            failureThreshold: startupFailureThreshold
          }
        ] : [], [
          (tcpReadiness ? {
            type: 'Readiness'
            tcpSocket: {port: 8080}
            initialDelaySeconds: 5
            periodSeconds: 10
            timeoutSeconds: 3
            failureThreshold: 3
          } : {
            type: 'Readiness'
            httpGet: {path: '/healthz', port: 8080, scheme: 'HTTP'}
            initialDelaySeconds: 5
            periodSeconds: 10
            timeoutSeconds: 3
            failureThreshold: 3
          })
        ])
      }
    ]
  }
}

output resourceId string = app.outputs.resourceId
output endpoint string = 'https://${app.outputs.fqdn}'
