targetScope = 'resourceGroup'

param location string = resourceGroup().location
param appName string = 'ca-fa-behavior-controller'
param environmentName string = 'cae-facontext-private'
param identityName string = 'id-facontext-api'
param registryName string = 'crfacontextpocdemo'
@minLength(64)
@maxLength(64)
param imageSha256 string
param revisionSuffix string
param commandArguments string[]
param environmentVariables environmentVariableType[]
@allowed([0, 1])
param minimumReplicas int = 1

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

module controller 'br/public:avm/res/app/container-app:0.23.0' = {
  name: 'fa-behavior-controller'
  params: {
    name: appName
    location: location
    environmentResourceId: environment.id
    workloadProfileName: 'Consumption'
    enableTelemetry: false
    tags: {
      purpose: 'fa-six-gap-supervised-consumer'
      budget: '100-usd-total-poc'
    }
    managedIdentities: {
      systemAssigned: false
      userAssignedResourceIds: [identity.id]
    }
    registries: [{server: registry.properties.loginServer, identity: identity.id}]
    disableIngress: true
    ingressExternal: false
    activeRevisionsMode: 'Single'
    revisionSuffix: revisionSuffix
    scaleSettings: {minReplicas: minimumReplicas, maxReplicas: 1}
    containers: [
      {
        name: 'behavior-controller'
        image: '${registry.properties.loginServer}/fa-behavior-api@sha256:${imageSha256}'
        command: ['python']
        args: commandArguments
        resources: {cpu: json('0.25'), memory: '0.5Gi'}
        env: environmentVariables
      }
    ]
  }
}

output resourceId string = controller.outputs.resourceId
