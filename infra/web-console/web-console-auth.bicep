targetScope = 'resourceGroup'

// Container Apps built-in Entra authentication for the remote console.
// Native resource instead of AVM: AVM container-app 0.23.0 and container-app/auth-config 0.1.0 pin
// Microsoft.App/containerApps/authConfigs@2026-01-01, whose token store accepts only a SAS URL secret.
// 2026-07-01 (GA) adds tokenStore.azureBlobStorage.managedIdentityResourceId, so no SAS or shared key is needed.

param appName string
param entraClientId string
param ficSecretName string
param ownerOid string
param apiAudience string
param tokenContainerUri string
param identityResourceId string
@description('The app\'s own https origin; approved so the sign-in layer does not treat its own POSTs as forgery.')
param publicOrigin string

resource app 'Microsoft.App/containerApps@2026-01-01' existing = {
  name: appName
}

resource auth 'Microsoft.App/containerApps/authConfigs@2026-07-01' = {
  parent: app
  name: 'current'
  properties: {
    platform: {
      enabled: true
    }
    globalValidation: {
      unauthenticatedClientAction: 'RedirectToLoginPage'
      redirectToProvider: 'azureactivedirectory'
    }
    httpSettings: {
      requireHttps: true
    }
    identityProviders: {
      azureActiveDirectory: {
        enabled: true
        registration: {
          clientId: entraClientId
          // Client assertion from the app's managed identity (federated credential), not a client secret.
          clientSecretSettingName: ficSecretName
          openIdIssuer: '${environment().authentication.loginEndpoint}${tenant().tenantId}/v2.0'
        }
        login: {
          loginParameters: [
            'scope=openid profile offline_access ${apiAudience}/Behavior.Read ${apiAudience}/Behavior.Approve'
          ]
        }
        validation: {
          defaultAuthorizationPolicy: {
            allowedPrincipals: {
              identities: [ownerOid]
            }
          }
        }
      }
    }
    login: {
      allowedExternalRedirectUrls: [
        publicOrigin
      ]
      tokenStore: {
        enabled: true
        azureBlobStorage: {
          blobContainerUri: tokenContainerUri
          managedIdentityResourceId: identityResourceId
        }
      }
    }
  }
}
