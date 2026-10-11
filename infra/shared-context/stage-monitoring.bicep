targetScope = 'resourceGroup'

// Stage monitoring (ADR-0008): Container Apps logs (job/API failures must be readable from CI) and
// Hosted agent traces through the project App Insights connection. Deploy before foundation.bicep
// so the Container Apps environment can point its app logs here. dev reuses this file with
// dev-monitoring.parameters.json; the defaults below stay the stage values.
param location string = 'westus3'
param workspaceName string = 'log-fa-stage'
param appInsightsName string = 'appi-fa-stage'
param foundryResourceGroupName string = 'rg-faexample'
param foundryAccountName string = 'cog-fasample-memory-demo'
param projectName string = 'faexample-stage'
@description('Project connection name; must be unique across the shared account.')
param connectionName string = 'stage-appinsights'
@description('Prefix for the nested deployment written into the shared account resource group.')
param foundryDeploymentPrefix string = 'stage'
@description('Daily ingestion cap in GB; a cost guard for the non-production environment.')
param dailyQuotaGb string = '0.5'
param tags object = {
  workload: 'hosted-sandbox-poc'
  environment: 'stage'
}

module workspace 'br/public:avm/res/operational-insights/workspace:0.16.1' = {
  name: 'stage-log-analytics'
  params: {
    name: workspaceName
    location: location
    tags: tags
    enableTelemetry: false
    skuName: 'PerGB2018'
    dataRetention: 30
    dailyQuotaGb: dailyQuotaGb
  }
}

module appInsights 'br/public:avm/res/insights/component:0.8.0' = {
  name: 'stage-app-insights'
  params: {
    name: appInsightsName
    location: location
    tags: tags
    enableTelemetry: false
    workspaceResourceId: workspace.outputs.resourceId
    kind: 'web'
    applicationType: 'web'
    retentionInDays: 30
    disableLocalAuth: false
  }
}

module projectConnection './project-appinsights-connection.bicep' = {
  name: '${foundryDeploymentPrefix}-project-appinsights-connection'
  scope: resourceGroup(foundryResourceGroupName)
  params: {
    accountName: foundryAccountName
    projectName: projectName
    appInsightsResourceId: appInsights.outputs.resourceId
    appInsightsConnectionString: appInsights.outputs.connectionString
    connectionName: connectionName
  }
}

output workspaceResourceId string = workspace.outputs.resourceId
output appInsightsResourceId string = appInsights.outputs.resourceId
