"""Opt-in N0 probe; synthetic state only, never a normal agent turn."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import Field, model_validator

from .behavior_wire import BackupId, HostedIdentity, WireModel
from .errors import Problem

PROBE_HEADER = "x-client-fa-n0-probe"
PROBE_PROFILE = "fa-foundry-native-n0-v1"
PROBE_STORE = "fa/v1/n0-probe"
PROBE_AGENT_ID = "00000000-0000-0000-0000-00000000004f"
PROBE_AGENT_NAME = "fa-agent-cc96a24795a872009197"
PROBE_PROJECT_ENDPOINT = "https://cog-fasample-memory-demo.services.ai.azure.com/api/projects/faexample"
AgentVersion = Annotated[str, Field(pattern=r"^[1-9][0-9]{0,15}$")]
Etag = Annotated[str, Field(min_length=1, max_length=256)]


class ProbeBaseline(WireModel):
    agent_version: AgentVersion
    etag: Etag


class ProbeRequest(WireModel):
    phase: Literal["write", "read"]
    probe_id: BackupId
    baseline: ProbeBaseline | None = None

    @model_validator(mode="after")
    def write_has_no_baseline(self):
        if self.phase == "write" and self.baseline is not None:
            raise ValueError("only a read can compare a previous version")
        return self


class ProbeValue(WireModel):
    schema_version: Literal["fa-foundry-native-n0-v1"] = PROBE_PROFILE
    probe_id: BackupId
    agent_id: Literal["00000000-0000-0000-0000-00000000004f"] = PROBE_AGENT_ID
    writer_version: AgentVersion
    stage: Literal["created", "updated"]


@dataclass(frozen=True)
class NativeStateProbe:
    identity: HostedIdentity

    @classmethod
    def from_environment(cls, identity: HostedIdentity) -> NativeStateProbe | None:
        profile = os.environ.get("FA_N0_STATE_PROBE", "")
        if not profile:
            return None
        if profile != PROBE_PROFILE:
            raise Problem(422, "N0_PROBE_PROFILE_INVALID", "unknown N0 probe profile")
        if (
            not os.environ.get("FOUNDRY_HOSTING_ENVIRONMENT")
            or identity.agent_id != PROBE_AGENT_ID
            or identity.agent_name != PROBE_AGENT_NAME
            or os.environ.get("FOUNDRY_PROJECT_ENDPOINT", "").rstrip("/") != PROBE_PROJECT_ENDPOINT
            or os.environ.get("AGENTSERVER_TASKS_BACKEND", "").strip().lower() not in {"", "hosted"}
        ):
            raise Problem(403, "N0_PROBE_TARGET_INVALID", "N0 requires the approved test agent and hosted backend")
        return cls(identity)

    async def execute(self, command: ProbeRequest) -> dict:
        from azure.ai.agentserver.core.storage import FoundryStateStore
        from azure.ai.agentserver.core.tasks import resilient_tasks_enabled
        from azure.identity.aio import ManagedIdentityCredential

        if not resilient_tasks_enabled():
            raise Problem(503, "N0_TASKS_DISABLED", "the SDK resilient task switch is not enabled")
        key = f"probe:{command.probe_id}"
        baseline = command.baseline
        if baseline is not None and int(self.identity.agent_version) <= int(baseline.agent_version):
            raise Problem(409, "N0_PROBE_VERSION_UNCHANGED", "the comparison requires a newer agent version")

        async with ManagedIdentityCredential() as credential:
            if command.phase == "write":
                store = await FoundryStateStore.get_or_create(
                    PROBE_STORE, credential=credential, user_isolation=False, item_ttl_seconds=-1,
                )
            else:
                # Do not recreate a missing store during recovery or cross-version verification.
                store = FoundryStateStore(PROBE_STORE, credential=credential)
            async with store:
                descriptor = await store.get()
                if descriptor.item_ttl_seconds != -1:
                    raise Problem(409, "N0_STATE_TTL_INVALID", "the existing probe store must never expire")
                if descriptor.name != PROBE_STORE or descriptor.user_isolation is not False:
                    raise Problem(409, "N0_STATE_SCOPE_INVALID", "the probe store must be agent-private")

                created_etag = None
                if command.phase == "write":
                    created = ProbeValue(
                        probe_id=command.probe_id, writer_version=self.identity.agent_version, stage="created",
                    )
                    reference = await store.create_item(key, created.model_dump(mode="json"))
                    item = await store.get_item(key)
                    self._readback(item, command.probe_id, expected=created, etag=reference.etag)
                    created_etag = item.etag
                    updated = created.model_copy(update={"stage": "updated"})
                    reference = await store.set_item(key, updated.model_dump(mode="json"), if_match=created_etag)
                    item = await store.get_item(key)
                    value = self._readback(item, command.probe_id, expected=updated, etag=reference.etag)
                else:
                    item = await store.get_item(key)
                    expected = None if baseline is None else ProbeValue(
                        probe_id=command.probe_id, writer_version=baseline.agent_version, stage="updated",
                    )
                    value = self._readback(
                        item, command.probe_id, expected=expected, etag=None if baseline is None else baseline.etag,
                    )

                return {
                    "profile": PROBE_PROFILE, "phase": command.phase,
                    "identity": self.identity.model_dump(mode="json"),
                    "store": PROBE_STORE, "key": key, "item_ttl_seconds": descriptor.item_ttl_seconds,
                    "resilient_tasks_enabled": True, "created_etag": created_etag,
                    "item": {"etag": item.etag, "value": value.model_dump(mode="json")},
                    "cross_version_verified": baseline is not None,
                }

    @staticmethod
    def _readback(item, probe_id: str, *, expected: ProbeValue | None = None, etag: str | None = None) -> ProbeValue:
        if item is None:
            raise Problem(404, "N0_PROBE_ITEM_MISSING", "the original probe key is missing; do not recreate it")
        try:
            value = ProbeValue.model_validate(item.value)
            ProbeBaseline(agent_version=value.writer_version, etag=item.etag)
        except (ValueError, TypeError):
            raise Problem(409, "N0_PROBE_READBACK_INVALID", "stored probe data has an unexpected shape") from None
        if (
            item.key != f"probe:{probe_id}" or value.probe_id != probe_id
            or expected is not None and value != expected
            or etag is not None and item.etag != etag
        ):
            raise Problem(409, "N0_PROBE_READBACK_CHANGED", "the original probe value or ETag does not match")
        return value


async def probe_response(request, probe: NativeStateProbe | None):
    from azure.ai.agentserver.core.storage import FoundryStorageError
    from azure.core.exceptions import ClientAuthenticationError, ServiceRequestError, ServiceResponseError
    from starlette.responses import JSONResponse

    try:
        if probe is None or request.headers.getlist(PROBE_HEADER) != [PROBE_PROFILE]:
            raise Problem(403, "N0_PROBE_DISABLED", "the N0 probe is not enabled for this request")
        if request.state.session_id != probe.identity.session_id:
            raise Problem(403, "N0_PROBE_SESSION_CHANGED", "the request is not in this hosted session")
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > 4096:
                raise Problem(413, "N0_PROBE_BODY_LIMIT", "the probe request exceeds its bound")
            raw.extend(chunk)
        try:
            def unique(pairs):
                value = dict(pairs)
                if len(value) != len(pairs):
                    raise ValueError("duplicate fields")
                return value

            command = ProbeRequest.model_validate(json.loads(raw, object_pairs_hook=unique))
        except (ValueError, TypeError):
            raise Problem(422, "N0_PROBE_REQUEST_INVALID", "only a typed synthetic probe selector is accepted") from None
        try:
            return JSONResponse(await probe.execute(command))
        except FoundryStorageError as exc:
            denied = exc.status_code in {401, 403}
            raise Problem(
                exc.status_code if exc.status_code in {400, 401, 403, 404, 409, 412, 429} else 502,
                "N0_STATE_ACCESS_DENIED" if denied else "N0_STATE_REQUEST_FAILED",
                "stop and report the missing access; do not add RBAC" if denied else
                "probe outcome is unresolved; read the original key before any further write",
                upstream_status=exc.status_code, phase=command.phase, key=f"probe:{command.probe_id}",
            ) from None
        except (ClientAuthenticationError, ServiceRequestError, ServiceResponseError, OSError):
            raise Problem(
                503, "N0_STATE_OUTCOME_UNKNOWN", "identity or transport failed; preserve the original probe key",
                phase=command.phase, key=f"probe:{command.probe_id}",
            ) from None
    except Problem as exc:
        return JSONResponse(exc.body, status_code=exc.status)
