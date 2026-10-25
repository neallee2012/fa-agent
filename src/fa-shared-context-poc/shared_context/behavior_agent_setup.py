"""Setup a new Hosted Agent needs and Foundry does not do at creation (#306, #468); both steps default off.

- Endpoint protocols: a new agent object starts with only ``responses``; ``PATCH /agents/{name}`` replaces
  ``protocol_configuration``, so the full set is sent and read back
  (https://learn.microsoft.com/azure/foundry/agents/how-to/configure-agent). The API identity's Foundry User
  role already allows it (https://learn.microsoft.com/azure/foundry/agents/concepts/hosted-agent-permissions).
- Project roles: Foundry User and Cognitive Services OpenAI User on the agent identity, as private memory needs both
  (https://learn.microsoft.com/azure/foundry/agents/quickstarts/quickstart-memory-hosted-agent). No Foundry or Entra
  feature grants them at creation, so the API assigns them under an ABAC-constrained delegation (#468). Each
  assignment has a fixed name, so the same PUT repeated is the same assignment; ``principalType`` avoids the new
  principal's replication delay (https://learn.microsoft.com/azure/role-based-access-control/role-assignments-rest).
- Account role (``FA_ONBOARDING_ACCOUNT_ROLE=true``, set by deploy/azd/profile.py only when the Bicep output says the
  account-scope delegation exists; ADR-0011 Cosmos memory): Cosmos memory embeds at the account root
  (``/openai/v1/embeddings``, #971), so OpenAI User is also assigned on the Foundry account that owns the project,
  under the account-scope delegation (ADR-0006, Neal 2026-10-24).

Nothing here deletes. Logs carry fixed reasons, statuses and ARM error codes only, never a token or body.
"""

from __future__ import annotations

import logging
import os
import re
import uuid

import httpx

from .behavior_http_diagnostics import _exception_type

ENDPOINT_ENV = "FA_ONBOARDING_ENDPOINT_PROTOCOLS"
ROLE_SCOPE_ENV = "FA_ONBOARDING_ROLE_SCOPE"
ACCOUNT_ROLE_ENV = "FA_ONBOARDING_ACCOUNT_ROLE"
PROTOCOLS = ("responses", "invocations", "invocations_ws")
FOUNDRY_USER = "53ca6127-db72-4b80-b1b0-d745d6d5456d"
OPENAI_USER = "5e0bd9bd-7b93-4f28-af87-19fc36ad61bd"
ROLES = (FOUNDRY_USER, OPENAI_USER)
ARM = "https://management.azure.com"
ARM_TOKEN_SCOPE = "https://management.azure.com/.default"
ARM_API_VERSION = "2022-04-01"
_NAMESPACE = uuid.UUID("0b8f6c1e-3d3a-4b8e-9b1f-6a0c4e2f7d51")
_SCOPE = re.compile(
    r"/subscriptions/([0-9a-fA-F-]{36})/resourceGroups/[A-Za-z0-9._()-]{1,90}"
    r"/providers/Microsoft\.CognitiveServices/accounts/([A-Za-z0-9-]{2,64})/projects/([A-Za-z0-9][A-Za-z0-9_-]{0,63})")
_ENDPOINT = re.compile(
    r"https://([a-z0-9][a-z0-9-]{1,62})\.services\.ai\.azure\.com/api/projects/([A-Za-z0-9][A-Za-z0-9_-]{0,63})/?")
_CODE = re.compile(r"[A-Za-z][A-Za-z0-9_.]{0,63}")
_PRINCIPAL = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _arm_code(response):
    try:
        code = response.json().get("error", {}).get("code")
    except (ValueError, AttributeError):
        return "none"
    return code if isinstance(code, str) and _CODE.fullmatch(code) else "none"


def _granted(item, scope, role, principal_id):
    """One ARM role assignment that gives ``role`` to the principal at this scope or a parent of it."""
    properties = item.get("properties") if isinstance(item, dict) else None
    if not isinstance(properties, dict):
        return False
    granted = properties.get("scope")
    definition = properties.get("roleDefinitionId")
    return (isinstance(granted, str) and isinstance(definition, str)
            and str(properties.get("principalId", "")).lower() == principal_id
            and definition.lower().endswith("/roledefinitions/" + role)
            and (scope.lower() == granted.lower() or scope.lower().startswith(granted.lower().rstrip("/") + "/")))


def endpoint_pending(foundry, agent_name):
    """None once the agent endpoint serves every FA protocol (read back); otherwise a fixed reason."""
    from .behavior_agent_onboarding import FoundryCallError

    def protocols():
        agent = foundry.get_agent(agent_name)
        endpoint = agent.get("agent_endpoint") if isinstance(agent, dict) else False
        if endpoint is None:
            return {}
        current = endpoint.get("protocol_configuration") if isinstance(endpoint, dict) else False
        return {} if current is None else current

    try:
        current = protocols()
    except FoundryCallError as error:
        return f"reason=read_failed status={error.status}"
    if not isinstance(current, dict):
        return "reason=unreadable"
    if all(name in current for name in PROTOCOLS):
        return None
    # The PATCH replaces protocol_configuration: keep every protocol already there and add the missing ones.
    wanted = {**current, **{name: current.get(name, {}) for name in PROTOCOLS}}
    try:
        foundry.update_route(agent_name, {"protocol_configuration": wanted})
    except FoundryCallError as error:
        if error.status is not None:
            return f"reason=patch_refused status={error.status}"
    try:
        current = protocols()
    except FoundryCallError as error:
        return f"reason=read_failed status={error.status}"
    return None if isinstance(current, dict) and all(name in current for name in PROTOCOLS) else "reason=not_applied"


def assignment_name(scope, principal_id, role):
    """The fixed ARM name of one assignment, so a repeated PUT is the same assignment, never a second one."""
    return str(uuid.uuid5(_NAMESPACE, f"{scope.lower()}|{principal_id}|{role}"))


def roles_pending(credential, scope, principal_id, *, account=False, transport=None):
    """None once both project roles (and with ``account`` OpenAI User on the project's account) are confirmed for the
    agent identity; otherwise a fixed reason. Never deletes."""
    principal_id = principal_id.lower()
    if _PRINCIPAL.fullmatch(principal_id) is None:
        return "reason=principal_invalid"
    subscription = _SCOPE.fullmatch(scope).group(1)
    targets = [(scope, role, role) for role in ROLES]
    if account:  # the account that owns the project; its reasons say role=<id>@account
        targets.append((scope.rsplit("/projects/", 1)[0], OPENAI_USER, OPENAI_USER + "@account"))
    try:
        token = credential.get_token(ARM_TOKEN_SCOPE).token
    except Exception as error:
        return f"reason=token_unavailable exception_type={_exception_type(error)}"
    headers = {"Authorization": "Bearer " + token}
    with httpx.Client(transport=transport or httpx.HTTPTransport(retries=0), trust_env=False,
                      follow_redirects=False, timeout=httpx.Timeout(30, connect=5)) as client:
        for scope, role, label in targets:
            base = ARM + scope + "/providers/Microsoft.Authorization/roleAssignments"
            body = {"properties": {
                "roleDefinitionId": f"/subscriptions/{subscription}/providers/Microsoft.Authorization/roleDefinitions/{role}",
                "principalId": principal_id, "principalType": "ServicePrincipal"}}
            try:
                response = client.put(f"{base}/{assignment_name(scope, principal_id, role)}",
                                      params={"api-version": ARM_API_VERSION}, json=body, headers=headers)
                if response.status_code in {200, 201}:
                    try:
                        confirmed = _granted(response.json(), scope, role, principal_id)
                    except ValueError:
                        confirmed = False
                    if confirmed:
                        continue
                    return f"reason=response_invalid role={label}"
                code = _arm_code(response)
                if response.status_code != 409 or code != "RoleAssignmentExists":
                    return f"reason=refused role={label} status={response.status_code} code={code}"
                # The same role is already on this principal under another name: read it back instead.
                existing = client.get(base, params={"api-version": ARM_API_VERSION,
                                                    "$filter": f"principalId eq '{principal_id}'"}, headers=headers)
                try:
                    found = existing.status_code == 200 and any(
                        _granted(item, scope, role, principal_id) for item in existing.json().get("value", ()))
                except (ValueError, AttributeError):
                    found = False
                if not found:
                    return f"reason=exists_unconfirmed role={label} status={existing.status_code}"
            except httpx.HTTPError as error:
                return f"reason=transport role={label} exception_type={_exception_type(error)}"
    return None


class NewAgentSetup:
    """The configured steps; ``pending`` returns None only after every enabled step is read back done."""

    def __init__(self, *, endpoint=False, role_scope=None, account_role=False, credential=None, transport=None):
        self.endpoint, self.role_scope, self.credential, self.transport = endpoint, role_scope, credential, transport
        self.account_role = account_role

    @classmethod
    def from_environment(cls, project_endpoint, credential, *, environ=None, transport=None):
        environ = os.environ if environ is None else environ
        log = logging.getLogger(__name__)
        endpoint = environ.get(ENDPOINT_ENV)
        if endpoint not in {None, "", "true"}:
            log.warning("FA_AGENT_SETUP_INACTIVE step=endpoint reason=config_invalid")
        scope = environ.get(ROLE_SCOPE_ENV) or None
        if scope is not None:
            matched, project = _SCOPE.fullmatch(scope), _ENDPOINT.fullmatch(project_endpoint or "")
            # Only the project this API serves: a scope for another account or project is a configuration error.
            if (matched is None or project is None or matched.group(2).lower() != project.group(1)
                    or matched.group(3).lower() != project.group(2).lower()):
                log.warning("FA_AGENT_SETUP_INACTIVE step=roles reason=config_invalid")
                scope = None
        return cls(endpoint=endpoint == "true", role_scope=scope,
                   account_role=environ.get(ACCOUNT_ROLE_ENV) == "true", credential=credential, transport=transport)

    def pending(self, foundry, agent_name, principal_id):
        if self.endpoint:
            reason = endpoint_pending(foundry, agent_name)
            if reason is not None:
                return "step=endpoint " + reason
        if self.role_scope is not None:
            reason = roles_pending(self.credential, self.role_scope, principal_id, account=self.account_role,
                                   transport=self.transport)
            if reason is not None:
                return "step=roles " + reason
        return None
