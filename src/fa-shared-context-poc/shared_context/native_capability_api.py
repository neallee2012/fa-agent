"""Native capability scope (C2 skills, C3 mcp-api) for the existing readers and broker.

The verified Agent names its own running native turn; the API reads that original RID receipt back from the
Agent (as the T5 relay does) and serves only what its capture selected:

- approved skills: only the exact captured versions (name, version, hash, approved purpose), read through the
  existing ``ApprovedSkillSource``; a changed effective version is refused, never substituted;
- mcp-api tools: only the captured ``mcp-api`` grants, through the existing broker helpers; the secret is
  resolved on the server only, and a call is deduplicated per (Agent, RID, tool call id) in this process.

No job, ticket, ledger or durable write is created here.
"""

from __future__ import annotations

from fastapi import Depends, Request
from starlette.concurrency import run_in_threadpool

from .behavior_agent_template import TemplateSkill
from .behavior_api import _body, _principal_dependency, _response
from .behavior_capability_calls import call_grant_tool, checked_result, list_grant_tools
from .behavior_capability_prompt import skill_access
from .behavior_capabilities import skill_resources_granted
from .behavior_native_capability_wire import (
    NATIVE_CAPABILITY_PREFIX as PREFIX, NativeCapabilityCall, NativeSkillRead, NativeSkillResourceRead, NativeTurnScope,
)
from .behavior_native_turn_api import NativeTurnGateway
from .errors import Problem
from .native_agent_api import NativeAgentRelayGateway

CAPABILITY_RESPONSE_BYTES = 256 * 1024


class NativeCapabilityGateway:
    def __init__(self, composition, *, enabled):
        self.relay = NativeAgentRelayGateway(composition, enabled=enabled)

    def _turn(self, principal, command):
        composition, _client, receipt = self.current_turn(principal, command)
        return composition, receipt

    def current_turn(self, principal, command):
        scope = self.relay._sender_scope(principal)
        principal.require(scope.team_id, "execute")
        composition, client = self.relay._client(scope)
        NativeTurnGateway._legacy(composition.runtime.work_policy, command.conversation_id)
        receipt = self.relay._original(client, command.request_id)
        if (receipt is None or receipt.conversation_id != command.conversation_id
                or receipt.status != "started" or receipt.cancel_requested):
            raise Problem(403, "NATIVE_CAPABILITY_SCOPE",
                          "only the original running native turn may use its captured capabilities")
        NativeTurnGateway._same_session(client, receipt)
        return composition, client, receipt

    def _skills(self, composition, receipt):
        capture = receipt.capture
        if not capture.template.skills or not skill_access(capture.template.role.profile, capture.capabilities):
            raise Problem(403, "SKILL_ACCESS_DISABLED", "this native turn did not capture approved skills")
        authority = composition.assistant.authority
        binding = composition.runtime.work_policy.policy.template
        actor = authority.actors.get(binding.agent_oid)
        if (actor is None or actor.oid != binding.agent_oid or actor.team != binding.team_id
                or authority.artifacts.context.auth.tenant_id != binding.tenant_id):
            raise Problem(403, "SKILL_IDENTITY_DENIED", "the original server-bound Team identity is required")
        names = tuple(item.name for item in capture.template.skills)
        catalog = authority.skills.catalog(actor, binding.team_id, names, schema_version="fa-approved-skill-catalog-v2")
        refs = tuple(TemplateSkill(name=item.name, version_id=item.version_id, sha256=item.sha256,
                                  metadata_sha256=item.metadata_sha256) for item in catalog.entries)
        if refs != capture.template.skills:
            raise Problem(409, "SKILL_SOURCE_CHANGED", "the captured approved skill versions are no longer current")
        return authority, actor, binding.team_id, names, catalog

    def skill_catalog(self, principal, command: NativeTurnScope):
        command = NativeTurnScope.model_validate_json(command.model_dump_json())
        composition, receipt = self._turn(principal, command)
        return self._skills(composition, receipt)[-1]

    def skill_catalog_presentation(self, principal, command: NativeTurnScope):
        """SK-03/SK-04: the #75 budgeted read_skill view of this RID's captured catalog and declared capacity."""
        from .behavior_skill_catalog import present_catalog
        command = NativeTurnScope.model_validate_json(command.model_dump_json())
        composition, receipt = self._turn(principal, command)
        capture = receipt.capture
        if capture.template.settings.skill_catalog_budget_profile != "fa-skill-catalog-budget-v1":
            raise Problem(403, "SKILL_CATALOG_BUDGET_DISABLED", "the original capture does not enable a catalog budget")
        catalog = self._skills(composition, receipt)[-1]
        if capture.model_capacity is None:
            raise Problem(503, "SKILL_CATALOG_CAPACITY_UNKNOWN", "the original declared capacity is required")
        # The same server counter that validated this budget when the capture was made.
        return present_catalog(catalog, capture.model_capacity, receipt.command.content,
                               composition.runtime.work_policy.runtime.dep.memory._count)

    def read_skill(self, principal, command: NativeSkillRead):
        command = NativeSkillRead.model_validate_json(command.model_dump_json())
        composition, receipt = self._turn(principal, command)
        authority, actor, team, names, catalog = self._skills(composition, receipt)
        entry = next((item for item in catalog.entries if item.name == command.name), None)
        if entry is None:
            raise Problem(403, "SKILL_SOURCE_DENIED", "only a captured approved skill can be read")
        if skill_resources_granted(receipt.capture.capabilities):
            return authority.skills.read(actor, team, names, entry, include_resources=True, include_tool_requirements=True)
        return authority.skills.read(actor, team, names, entry)

    def read_skill_resource(self, principal, command: NativeSkillResourceRead):
        command = NativeSkillResourceRead.model_validate_json(command.model_dump_json())
        composition, receipt = self._turn(principal, command)
        if not skill_resources_granted(receipt.capture.capabilities):
            raise Problem(403, "SKILL_ACCESS_DISABLED", "this original turn did not select skill resources")
        authority, actor, team, _names, catalog = self._skills(composition, receipt)
        entry = next((item for item in catalog.entries if item.name == command.name), None)
        if entry is None:
            raise Problem(403, "SKILL_SOURCE_DENIED", "only a captured approved skill can supply resources")
        result = authority.artifacts.skill_resource(actor, team, entry.name, entry.version_id, command.resource)
        if result.skill_metadata_sha256 != entry.metadata_sha256:
            raise Problem(409, "SKILL_SOURCE_CHANGED", "the resource manifest differs from the captured version")
        self._skills(composition, receipt)
        return result

    @staticmethod
    def _grants(receipt):
        capture = receipt.capture.capabilities
        return () if capture is None else tuple(grant for grant in capture.grants if grant.kind == "mcp-api")

    def capability_catalog(self, principal, command: NativeTurnScope):
        command = NativeTurnScope.model_validate_json(command.model_dump_json())
        composition, receipt = self._turn(principal, command)
        return list_grant_tools(composition.runtime, self._grants(receipt), degrade=True)

    def capability_call(self, principal, command: NativeCapabilityCall):
        command = NativeCapabilityCall.model_validate_json(command.model_dump_json())
        composition, receipt = self._turn(principal, command)
        result, grant = call_grant_tool(
            composition.runtime, self._grants(receipt), tool_call_id=command.tool_call_id, tool=command.tool,
            arguments=command.arguments, input_schema_sha256=command.input_schema_sha256,
            key=("native", principal.oid, receipt.request_id, command.tool_call_id),
            log={"agent_id": receipt.agent_id, "request_id": receipt.request_id})
        return checked_result(result, grant)


def install_native_capability_routes(app, composition, *, enabled):
    gateway = NativeCapabilityGateway(composition, enabled=enabled)
    identity = _principal_dependency(composition.verifier)

    async def reply(call, *, max_bytes=None):
        result = await run_in_threadpool(call)
        response = _response(result, **({} if max_bytes is None else {"max_bytes": max_bytes}))
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.post(PREFIX + "/skill-catalog")
    async def skill_catalog(request: Request, principal=Depends(identity)):
        command = await _body(request, NativeTurnScope)
        return await reply(lambda: gateway.skill_catalog(principal, command))

    @app.post(PREFIX + "/skill-catalog-presentation")
    async def skill_catalog_presentation(request: Request, principal=Depends(identity)):
        command = await _body(request, NativeTurnScope)
        return await reply(lambda: gateway.skill_catalog_presentation(principal, command))

    @app.post(PREFIX + "/read-skill")
    async def read_skill(request: Request, principal=Depends(identity)):
        command = await _body(request, NativeSkillRead)
        return await reply(lambda: gateway.read_skill(principal, command), max_bytes=CAPABILITY_RESPONSE_BYTES)

    @app.post(PREFIX + "/read-skill-resource")
    async def read_skill_resource(request: Request, principal=Depends(identity)):
        command = await _body(request, NativeSkillResourceRead)
        return await reply(lambda: gateway.read_skill_resource(principal, command), max_bytes=CAPABILITY_RESPONSE_BYTES)

    @app.post(PREFIX + "/catalog")
    async def capability_catalog(request: Request, principal=Depends(identity)):
        command = await _body(request, NativeTurnScope)
        return await reply(lambda: gateway.capability_catalog(principal, command), max_bytes=CAPABILITY_RESPONSE_BYTES)

    @app.post(PREFIX + "/call")
    async def capability_call(request: Request, principal=Depends(identity)):
        command = await _body(request, NativeCapabilityCall, max_body_bytes=32 * 1024)
        return await reply(lambda: gateway.capability_call(principal, command), max_bytes=CAPABILITY_RESPONSE_BYTES)
