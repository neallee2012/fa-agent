targetScope = 'resourceGroup'

param location string = resourceGroup().location
param jobName string
param environmentName string = 'cae-facontext-private'
param identityName string = 'id-facontext-api'
param registryName string = 'crfacontextpocdemo'
@allowed(['fa-behavior-api'])
param imageRepository string = 'fa-behavior-api'
@minLength(64)
@maxLength(64)
param imageSha256 string
param commandArguments string[]
param environmentVariables environmentVariableType[]
@minValue(60)
@maxValue(3600)
param timeoutSeconds int = 600
param containerResources object = {cpu: '0.25', memory: '0.5Gi'}
param tags object = {
  purpose: 'fa-six-gap-explicit-bootstrap-or-supervisor'
  budget: '100-usd-total-poc'
}

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

module job 'br/public:avm/res/app/job:0.7.0' = {
  name: 'fa-behavior-manual-job'
  params: {
    name: jobName
    location: location
    environmentResourceId: environment.id
    workloadProfileName: 'Consumption'
    enableTelemetry: false
    triggerType: 'Manual'
    manualTriggerConfig: {parallelism: 1, replicaCompletionCount: 1}
    replicaRetryLimit: 0
    replicaTimeout: timeoutSeconds
    tags: tags
    managedIdentities: {
      systemAssigned: false
      userAssignedResourceIds: [identity.id]
    }
    registries: [{server: registry.properties.loginServer, identity: identity.id}]
    containers: [
      {
        name: 'behavior-controller'
        image: '${registry.properties.loginServer}/${imageRepository}@sha256:${imageSha256}'
        command: ['python']
        args: commandArguments
        resources: containerResources
        env: environmentVariables
      }
    ]
  }
}

output resourceId string = job.outputs.resourceId
