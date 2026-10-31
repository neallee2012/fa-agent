"""Bounded assistant server composition, not a standalone API or activation path.

Select workload_version="assistant-v1.2" and an AssistantSettings JSON object:
  assistant: {profiles: [AssistantProfile], provider_endpoint:
    "<active target project_endpoint>/openai/v1/chat/completions",
    model: "gpt-4.1-mini", credential_binding: "server-api-uami",
    m3_context: {account_resource_id: "<table account>", endpoint: "<table endpoint>",
                 table_name: "<existing table name>"},
    artifact_container_url: "<sources.protection.container_url>"}

The existing Table and retained M3 container clients are reused, not provisioned.
Legacy work ownership is read only after installation validation. ContextService
is used for its existing M3 work/receipt APIs; legacy memory operations are not
enabled here. Runtime memory still uses the shared reviewed source authority.
No SDK/runtime witness, policy renewal or migrated-pool transition is produced.
"""

import logging
from dataclasses import replace
import httpx
from fastapi import HTTPException

from .behavior_assistant import AssistantAuthority
from .behavior_assistant_gateway import AssistantProvider
from .behavior_http_diagnostics import model_failure
from .behavior_assistant_deadlines import model_sync, check_server_model
from .errors import Problem
from .m3_artifacts import ArtifactService
from .m3_auth import ApprovalSettings
from .m3_store import BlobArtifactStore
from .service import ContextService
from .storage import TableRepository


class _DisabledLegacyMemory:
    def start(self, scope, items):
        raise Problem(403, "ASSISTANT_LEGACY_MEMORY_DISABLED", "use the reviewed runtime memory authority")

    def poll(self, update_id):
        raise Problem(403, "ASSISTANT_LEGACY_MEMORY_DISABLED", "legacy memory is not configured")

    def search(self, scope, query):
        raise Problem(403, "ASSISTANT_LEGACY_MEMORY_DISABLED", "use the reviewed runtime memory authority")


class _CurrentAssistantProvider(AssistantProvider):
    def __init__(self, composition, credential, *, transport=None):
        super().__init__(composition.config.assistant.provider_endpoint, credential, transport=transport)
        self.composition = composition

    async def send(self, body, *, before_send=None, text_reply=False, model=None, stream_response=False,
                   skill_access=False, user_turn_access=False, sop_copy_access=False,
                   skill_draft_access=False, work_memory_access=False, skill_resource_access=False,
                   user_file_access=False, assistant_reply_access=False, reviewed_note_access=False,
                   capability_tools=frozenset(), allow_images=False, model_provider=None, diagnostic_keys=()):
        if self.shared_http is None:
            self.shared_http=httpx.AsyncClient(transport=self.transport or httpx.AsyncHTTPTransport(retries=0),
                trust_env=False,follow_redirects=False,timeout=httpx.Timeout(60,connect=5))
        # Instruction egress relies on the model route's consume CAS gate; there is no second pre-send read.
        if text_reply and "reasoning_effort" in body:
            facts = self.composition.runtime.work_policy.models.select(model)
            if facts.supports_reasoning_effort is False:
                body = {name: value for name, value in body.items() if name != "reasoning_effort"}
                logging.getLogger(__name__).info("INSTRUCTION_REASONING_OPTION_OMITTED model_support=false")
        # A route dependency is not the last egress gate: model consumption may
        # take time, and the installation/catalog can change while it runs.
        async def current():
            try:
                if not text_reply:
                    await model_sync(AssistantServer.acquire_evidence, self.composition)
            except HTTPException as error:
                raise HTTPException(error.status_code, detail=error.detail, headers={
                    **(error.headers or {}), "x-behavior-model-upstream-error-phase": "provider_evidence",
                }) from None
            except Problem as error:
                raise Problem(error.status, error.body["error"]["code"],
                              "current provider egress evidence unavailable",
                              **model_failure(error, phase="provider_evidence")) from None
            if before_send is not None:
                await before_send()
            check_server_model()
        return await super().send(body, before_send=current, text_reply=text_reply, model=model,
                                  model_provider=model_provider, diagnostic_keys=diagnostic_keys,
                                  **({"stream_response":True} if stream_response else {}),
                                  **({"skill_access": True} if skill_access else {}),
                                  **({"user_turn_access": True} if user_turn_access else {}),
                                  **({"user_file_access": True} if user_file_access else {}),
                                  **({"assistant_reply_access": True} if assistant_reply_access else {}),
                                  **({"sop_copy_access": True} if sop_copy_access else {}),
                                  **({"skill_draft_access": True} if skill_draft_access else {}),
                                  **({"work_memory_access": True} if work_memory_access else {}),
                                  **({"reviewed_note_access": True} if reviewed_note_access else {}),
                                  **({"capability_tools": capability_tools} if capability_tools else {}),
                                  **({"allow_images": True} if allow_images else {}),
                                  **({"skill_resource_access": True} if skill_resource_access else {}))


class AssistantServer:
    def __init__(self, composition, table, container, credential, *, transport=None, extra_actor=None):
        config = composition.config
        self.config = config
        self.runtime = composition.runtime
        auth = config.identity.auth()
        if extra_actor is not None:
            # A Team-registry Agent: verified by resolve_agent_scope, never in the static allowlist.
            if extra_actor.oid in auth.actors:
                raise Problem(503, "ASSISTANT_CONFIGURATION", "a registered agent cannot reuse a configured actor")
            auth = replace(auth, actors={**auth.actors, extra_actor.oid: extra_actor})
        context = ContextService(TableRepository(table), _DisabledLegacyMemory(), auth)
        self.artifacts = ArtifactService(
            context, BlobArtifactStore(container, config.identity.tenant_id),
            ApprovalSettings(config.identity.human_oid, config.identity.admin_client_id, config.identity.team_id),
        )
        self.actors = tuple(auth.actors.values())
        self.authority = None
        self.provider = _CurrentAssistantProvider(composition, credential, transport=transport)

    def start(self):
        if self.authority is None:
            fresh = getattr(self.runtime.store, "_verified_fresh_initialization", None) == self.config.sha256
            self.authority = AssistantAuthority(self.runtime, self.artifacts, self.actors, require_legacy_sources=not fresh)
        return self.authority

    @staticmethod
    def bounded_route(composition, request):
        binding = composition.runtime.bindings.get(request.path_params.get("binding_id"))
        if (request.method != "POST" or binding is None
                or binding.binding_id not in composition.runtime.profiles):
            return False
        path = getattr(request.scope.get("route"), "path", None)
        root = "/behavior/v1/bindings/{binding_id}"
        return path in {
            root + "/hosted/launches/{launch_id}/claim", root + "/assistant/launches/{launch_id}/claim",
            *(root + "/assistant/sessions/{session_id}/" + action for action in (
                "acquire", "status", "context", "reserve-model", "model", "save", "reconcile", "finish",
                "stopped", "checkpoint", "restore",
                "skill-catalog", "skill-catalog-presentation", "read-skill", "read-skill-resource", "attached-text",
            )),
        }

    @staticmethod
    def acquire_evidence(composition):
        """Re-read only registered assistant routes' side-effect-free evidence gate."""
        from .behavior_assistant_api import acquire_http_error
        for attempt in range(3):
            try:
                composition.require_current_native_evidence()
                return
            except Problem as error:
                if error.status != 412 or error.body["error"]["code"] != "BEHAVIOR_CONFLICT":
                    raise acquire_http_error(error, "native_evidence") from None
                logging.getLogger(__name__).warning(
                    "ASSISTANT_ACQUIRE_CONFLICT phase=native_evidence attempt=%s maximum=3", attempt + 1)
                if attempt == 2:
                    raise acquire_http_error(error, "native_evidence") from None
