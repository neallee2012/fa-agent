"""#468: a new Agent's endpoint protocols and two project roles, set before registration; both default off."""

import json
import logging
from types import SimpleNamespace

import httpx
import pytest

import shared_context.behavior_agent_setup as setup
from shared_context.behavior_agent_onboarding import FoundryCallError

SUB = "00000000-0000-0000-0000-000000000012"
SCOPE = (f"/subscriptions/{SUB}/resourceGroups/rg-faexample/providers/Microsoft.CognitiveServices"
         "/accounts/cog-fasample-memory-demo/projects/faexample-stage")
ENDPOINT = "https://cog-fasample-memory-demo.services.ai.azure.com/api/projects/faexample-stage"
PRINCIPAL = "00000000-0000-0000-0000-000000000060"
TOKEN = "arm-token-must-not-be-logged"


class Endpoints:
    """The agent object's endpoint; ``apply`` False means a PATCH is accepted but not visible yet."""

    def __init__(self, protocols, *, refuse=None, unknown=False, apply=True):
        self.protocols, self.refuse, self.unknown, self.apply, self.patches = protocols, refuse, unknown, apply, []

    def get_agent(self, name):
        return {"name": name, "agent_endpoint": {"protocol_configuration": dict(self.protocols)}}

    def update_route(self, name, endpoint):
        self.patches.append(json.loads(json.dumps(endpoint)))
        if self.refuse is not None:
            raise FoundryCallError(self.refuse)
        if self.apply:
            self.protocols = endpoint["protocol_configuration"]
        if self.unknown:
            raise FoundryCallError(None)


def test_a_new_agent_endpoint_gets_every_protocol_in_one_replace_and_is_read_back():
    agent = Endpoints({"responses": {}, "a2a": {"x": 1}})
    assert setup.endpoint_pending(agent, "fa-agent-1") is None
    # The PATCH replaces protocol_configuration, so it keeps what was there and adds the missing ones.
    assert agent.patches == [{"protocol_configuration": {"responses": {}, "a2a": {"x": 1}, "invocations": {},
                                                         "invocations_ws": {}}}]
    assert setup.endpoint_pending(agent, "fa-agent-1") is None and len(agent.patches) == 1


@pytest.mark.parametrize("agent,expected", [
    (Endpoints({"responses": {}}, refuse=403), "reason=patch_refused status=403"),
    (Endpoints({"responses": {}}, apply=False), "reason=not_applied"),
    (Endpoints({"responses": {}}, unknown=True), None),            # unknown outcome: the read-back decides
    (Endpoints({"responses": {}}, unknown=True, apply=False), "reason=not_applied"),
])
def test_an_endpoint_patch_counts_only_when_read_back(agent, expected):
    assert setup.endpoint_pending(agent, "fa-agent-1") == expected


def arm(responses, seen):
    """ARM answering each request in order; records method, URL and body."""
    remaining = list(responses)

    def handle(request):
        seen.append((request.method, str(request.url), json.loads(request.content) if request.content else None,
                     request.headers.get("authorization")))
        status, value = remaining.pop(0)
        if isinstance(value, Exception):
            raise value
        return httpx.Response(status, json=value)
    return httpx.MockTransport(handle)


class Credential:
    def __init__(self):
        self.scopes = []

    def get_token(self, scope):
        self.scopes.append(scope)
        return SimpleNamespace(token=TOKEN)


def granted(role, principal=PRINCIPAL, scope=SCOPE):
    return {"properties": {"roleDefinitionId": f"/subscriptions/{SUB}/providers/Microsoft.Authorization/roleDefinitions/{role}",
                           "principalId": principal, "scope": scope}}


def test_both_roles_are_put_under_fixed_names_with_principal_type_and_an_arm_token():
    seen, credential = [], Credential()
    transport = arm([(201, granted(setup.FOUNDRY_USER)), (201, granted(setup.OPENAI_USER))], seen)
    assert setup.roles_pending(credential, SCOPE, PRINCIPAL.upper(), transport=transport) is None
    assert credential.scopes == ["https://management.azure.com/.default"]
    assert [method for method, *_ in seen] == ["PUT", "PUT"]
    for (_, url, body, auth), role in zip(seen, setup.ROLES):
        name = setup.assignment_name(SCOPE, PRINCIPAL, role)
        assert url == (f"https://management.azure.com{SCOPE}/providers/Microsoft.Authorization/roleAssignments/{name}"
                       "?api-version=2022-04-01")
        assert body == {"properties": {
            "roleDefinitionId": f"/subscriptions/{SUB}/providers/Microsoft.Authorization/roleDefinitions/{role}",
            "principalId": PRINCIPAL, "principalType": "ServicePrincipal"}}
        assert auth == "Bearer " + TOKEN
    # Repeating is the same two assignments, never new ones.
    again = []
    setup.roles_pending(credential, SCOPE, PRINCIPAL,
                        transport=arm([(200, granted(setup.FOUNDRY_USER)), (200, granted(setup.OPENAI_USER))], again))
    assert [url for _, url, *_ in again] == [url for _, url, *_ in seen]
    assert setup.ROLES == ("53ca6127-db72-4b80-b1b0-d745d6d5456d", "5e0bd9bd-7b93-4f28-af87-19fc36ad61bd")


def test_an_existing_assignment_under_another_name_is_confirmed_by_reading_it_back():
    seen = []
    exists = (409, {"error": {"code": "RoleAssignmentExists", "message": TOKEN}})
    transport = arm([exists, (200, {"value": [granted(setup.FOUNDRY_USER)]}),
                     exists, (200, {"value": [granted(setup.FOUNDRY_USER)]})], seen)
    reason = setup.roles_pending(Credential(), SCOPE, PRINCIPAL, transport=transport)
    assert reason == f"reason=exists_unconfirmed role={setup.OPENAI_USER} status=200"
    assert [method for method, *_ in seen] == ["PUT", "GET", "PUT", "GET"]
    assert "principalId+eq" in seen[1][1] or "principalId%20eq" in seen[1][1]


@pytest.mark.parametrize("responses,expected", [
    ([(403, {"error": {"code": "AuthorizationFailed", "message": TOKEN}})],
     f"reason=refused role={setup.FOUNDRY_USER} status=403 code=AuthorizationFailed"),
    ([(400, {"error": {"code": "PrincipalNotFound"}})],
     f"reason=refused role={setup.FOUNDRY_USER} status=400 code=PrincipalNotFound"),
    ([(201, granted(setup.FOUNDRY_USER, principal="00000000-0000-4000-8000-000000000000"))],
     f"reason=response_invalid role={setup.FOUNDRY_USER}"),
    ([(0, httpx.ConnectError("x"))], f"reason=transport role={setup.FOUNDRY_USER} exception_type=ConnectError"),
])
def test_a_role_failure_is_a_fixed_reason_and_never_deletes(responses, expected):
    seen = []
    assert setup.roles_pending(Credential(), SCOPE, PRINCIPAL, transport=arm(responses, seen)) == expected
    assert all(method in {"PUT", "GET"} for method, *_ in seen)


def test_environment_enables_each_step_separately_and_refuses_a_scope_of_another_project(caplog):
    credential = Credential()
    off = setup.NewAgentSetup.from_environment(ENDPOINT, credential, environ={})
    assert (off.endpoint, off.role_scope) == (False, None)
    endpoint_only = setup.NewAgentSetup.from_environment(ENDPOINT, credential, environ={setup.ENDPOINT_ENV: "true"})
    assert (endpoint_only.endpoint, endpoint_only.role_scope) == (True, None)
    roles_only = setup.NewAgentSetup.from_environment(ENDPOINT, credential, environ={setup.ROLE_SCOPE_ENV: SCOPE})
    assert (roles_only.endpoint, roles_only.role_scope) == (False, SCOPE)
    with caplog.at_level(logging.WARNING):
        other = setup.NewAgentSetup.from_environment(
            ENDPOINT, credential, environ={setup.ROLE_SCOPE_ENV: SCOPE.replace("faexample-stage", "other"),
                                           setup.ENDPOINT_ENV: "yes"})
    assert (other.endpoint, other.role_scope) == (False, None)
    assert "FA_AGENT_SETUP_INACTIVE step=roles reason=config_invalid" in caplog.text
    assert "FA_AGENT_SETUP_INACTIVE step=endpoint reason=config_invalid" in caplog.text


def test_pending_runs_endpoint_then_roles_and_off_does_nothing():
    agent = Endpoints({"responses": {}})
    assert setup.NewAgentSetup().pending(agent, "fa-agent-1", PRINCIPAL) is None and agent.patches == []
    seen = []
    both = setup.NewAgentSetup(endpoint=True, role_scope=SCOPE, credential=Credential(),
                               transport=arm([(403, {"error": {"code": "AuthorizationFailed"}})], seen))
    assert both.pending(agent, "fa-agent-1", PRINCIPAL) == (
        f"step=roles reason=refused role={setup.FOUNDRY_USER} status=403 code=AuthorizationFailed")
    assert len(agent.patches) == 1
    blocked = setup.NewAgentSetup(endpoint=True, role_scope=SCOPE, credential=Credential(), transport=arm([], []))
    assert blocked.pending(Endpoints({"responses": {}}, refuse=403), "fa-agent-1", PRINCIPAL) == (
        "step=endpoint reason=patch_refused status=403")
