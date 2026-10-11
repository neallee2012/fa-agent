"""Entra objects of an azd environment through Microsoft Graph v1.0 (docs/azd-new-environment.md, steps 7 and 15).

Why not Graph Bicep: extensible resources do not support what-if, so `azd provision --preview` would stop working
(https://learn.microsoft.com/graph/templates/limitations). Instead every write here is idempotent by an alternate key:

- applications: `PATCH /applications(uniqueName='...')` with `Prefer: create-if-missing` creates once and updates the
  same object afterwards (https://learn.microsoft.com/graph/api/application-upsert?view=graph-rest-1.0);
- service principals: read by `appId`, POST only when the tenant has none;
- delegated grants: read by client, then POST once or PATCH the scope string of that one grant;
- federated credentials: read by name, then POST once or PATCH that one credential.

What is created (the manual flow's D1/D2/E1 lessons):
- the FA API app: v1 access tokens (the API checks `aud` against the identifier URI), single tenant, only the
  delegated scopes Behavior.Read and Behavior.Approve;
- the Portal app: Web redirect `<origin>/.auth/login/aad/callback` with ID tokens (Container Apps Entra sign-in),
  no secret; its managed identity signs in through a federated credential;
- service principals for both, and for the Azure CLI first-party client when the tenant lacks one;
- `Principal` grants for the Team owner only (Azure CLI -> API, Portal -> API and Portal -> sign-in scopes).
  Granting needs DelegatedPermissionGrant.ReadWrite.All (an admin role); without it the caller gets
  ConsentRequired and the hook writes the three request bodies for an admin (fa_azd.admin_consent_files).
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Any, Callable

Graph = Callable[[str, str, Any, dict[str, str]], Any]

GRAPH_APP_ID = "00000003-0000-0000-c000-000000000000"  # Microsoft Graph (well-known, every tenant)
SCOPES = ("Behavior.Read", "Behavior.Approve")
SIGN_IN_SCOPES = ("openid", "profile", "offline_access")
UPSERT = {"Prefer": "create-if-missing"}
# Fixed namespace so the scope IDs of one API app never change between runs (Graph refuses to change the ID of an
# enabled scope).
SCOPE_NAMESPACE = uuid.UUID("6d2b4f7e-58c8-4f6e-9a65-3fa0c1d5a8b1")
FIC_ISSUER = "https://login.microsoftonline.com/{tenant}/v2.0"
FIC_AUDIENCE = "api://AzureADTokenExchange"


class GraphForbidden(Exception):
    """Graph refused the signed-in account (Forbidden / Authorization_RequestDenied); not a file-system error."""


class ConsentRequired(Exception):
    """The signed-in account cannot create delegated permission grants (needs an admin role)."""


def environment_key(subscription_id: str, resource_group: str) -> str:
    """Tenant-wide unique part of names: uniqueName and identifier URIs are unique per tenant, so two subscriptions
    using the same azd environment name must not update each other's app."""
    return hashlib.sha256(f"{subscription_id.lower()}/{resource_group.lower()}".encode()).hexdigest()[:10]


def names(environment: str, tenant_id: str, subscription_id: str, resource_group: str) -> dict[str, str]:
    key = environment_key(subscription_id, resource_group)
    return {
        "api_unique_name": f"fa-api-{environment}-{key}",
        "console_unique_name": f"fa-portal-{environment}-{key}",
        "api_audience": f"api://{tenant_id}/fa-{environment}-{key}",
    }


def scope_id(unique_name: str, scope: str) -> str:
    return str(uuid.uuid5(SCOPE_NAMESPACE, f"{unique_name}/{scope}"))


def api_app_body(environment: str, unique_name: str, audience: str) -> dict[str, Any]:
    return {
        "displayName": f"FA API ({environment})",
        "signInAudience": "AzureADMyOrg",
        "identifierUris": [audience],
        "api": {
            "requestedAccessTokenVersion": 1,
            "oauth2PermissionScopes": [{
                "id": scope_id(unique_name, scope),
                "value": scope,
                "type": "User",
                "isEnabled": True,
                "adminConsentDisplayName": f"FA {scope}",
                "adminConsentDescription": f"{scope} on the FA API of environment {environment}.",
                "userConsentDisplayName": f"FA {scope}",
                "userConsentDescription": f"{scope} on the FA API of environment {environment}.",
            } for scope in SCOPES],
        },
    }


def console_app_body(environment: str, api_app_id: str, api_unique_name: str, redirect_uri: str) -> dict[str, Any]:
    return {
        "displayName": f"FA Portal ({environment})",
        "signInAudience": "AzureADMyOrg",
        "web": {
            "redirectUris": [redirect_uri],
            "implicitGrantSettings": {"enableIdTokenIssuance": True, "enableAccessTokenIssuance": False},
        },
        "requiredResourceAccess": [{
            "resourceAppId": api_app_id,
            "resourceAccess": [{"id": scope_id(api_unique_name, scope), "type": "Scope"} for scope in SCOPES],
        }],
    }


def fic_body(name: str, tenant_id: str, principal_id: str) -> dict[str, Any]:
    return {"name": name, "issuer": FIC_ISSUER.format(tenant=tenant_id), "subject": principal_id,
            "audiences": [FIC_AUDIENCE],
            "description": "Container Apps Entra sign-in: client assertion from the Portal managed identity"}


def upsert_application(graph: Graph, unique_name: str, body: dict[str, Any]) -> dict[str, Any]:
    path = f"/applications(uniqueName='{unique_name}')"
    graph("PATCH", path, body, UPSERT)
    return graph("GET", path, None, {})


def ensure_service_principal(graph: Graph, app_id: str) -> dict[str, Any]:
    """Read first; create only when the tenant has none (D2: the Azure CLI client can be missing). An existing
    principal, including Microsoft's first-party ones, is never written."""
    path = f"/servicePrincipals(appId='{app_id}')"
    try:
        return graph("GET", path, None, {})
    except LookupError:
        pass
    graph("POST", "/servicePrincipals", {"appId": app_id}, {})
    return graph("GET", path, None, {})


def ensure_grant(graph: Graph, client_sp: str, resource_sp: str, principal_id: str, scopes: tuple[str, ...]) -> str:
    """One `Principal` grant of exactly these scopes added for the owner; other scopes on it stay as they are."""
    listed = graph("GET", f"/oauth2PermissionGrants?$filter=clientId eq '{client_sp}'", None, {}) or {}
    existing = [item for item in listed.get("value", [])
                if item.get("resourceId") == resource_sp and item.get("consentType") == "Principal"
                and item.get("principalId") == principal_id]
    try:
        if existing:
            have = (existing[0].get("scope") or "").split()
            missing = [scope for scope in scopes if scope not in have]
            if not missing:
                return "already"
            graph("PATCH", f"/oauth2PermissionGrants/{existing[0]['id']}", {"scope": " ".join([*have, *missing])}, {})
            return "updated"
        graph("POST", "/oauth2PermissionGrants", {"clientId": client_sp, "consentType": "Principal",
                                                   "principalId": principal_id, "resourceId": resource_sp,
                                                   "scope": " ".join(scopes)}, {})
        return "created"
    except GraphForbidden as error:
        raise ConsentRequired(str(error)) from None


def ensure_fic(graph: Graph, app_object_id: str, body: dict[str, Any]) -> str:
    base = f"/applications/{app_object_id}/federatedIdentityCredentials"
    listed = (graph("GET", base, None, {}) or {}).get("value", [])
    current = next((item for item in listed if item.get("name") == body["name"]), None)
    if current is None:
        graph("POST", base, body, {})
        return "created"
    if all(current.get(key) == body[key] for key in ("issuer", "subject", "audiences")):
        return "already"
    graph("PATCH", f"{base}/{current['id']}", {key: body[key] for key in ("issuer", "subject", "audiences")}, {})
    return "updated"


def ensure_api(graph: Graph, environment: str, unique_name: str, audience: str) -> dict[str, str]:
    app = upsert_application(graph, unique_name, api_app_body(environment, unique_name, audience))
    if audience not in (app.get("identifierUris") or []):
        raise SystemExit(f"API app {unique_name} did not read back identifier URI {audience}")
    scopes = {item.get("value") for item in (app.get("api") or {}).get("oauth2PermissionScopes") or []}
    if not set(SCOPES) <= scopes or (app.get("api") or {}).get("requestedAccessTokenVersion") != 1:
        raise SystemExit(f"API app {unique_name} did not read back v1 tokens with {', '.join(SCOPES)}")
    sp = ensure_service_principal(graph, app["appId"])
    return {"app_id": app["appId"], "object_id": app["id"], "sp_id": sp["id"]}


def ensure_console(graph: Graph, environment: str, unique_name: str, api_app_id: str, api_unique_name: str,
                   redirect_uri: str) -> dict[str, str]:
    app = upsert_application(graph, unique_name,
                             console_app_body(environment, api_app_id, api_unique_name, redirect_uri))
    if redirect_uri not in ((app.get("web") or {}).get("redirectUris") or []):
        raise SystemExit(f"Portal app {unique_name} did not read back redirect URI {redirect_uri}")
    sp = ensure_service_principal(graph, app["appId"])
    return {"app_id": app["appId"], "object_id": app["id"], "sp_id": sp["id"]}
