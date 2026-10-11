"""Strict, non-secret operator configuration and independently captured evidence.

Only local server-owned files/environment are inputs. Mount configuration and
catalogs read-only; do not expose them as HTTP uploads. Hashes bind observations,
not authenticate them: the operator-controlled catalog is the trust boundary.
The schema/sample CLI deliberately produces no native readiness witnesses.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_serializer, model_validator

from .auth import Actor, AuthSettings
from .behavior_agent_message_wire import MAX_TEAM_AGENTS
from .behavior_assistant import AssistantProfile, EXECUTOR_VERSION, PROTECTION_VERSION
from .behavior_assistant_wire import CHECKS as ASSISTANT_CHECKS
from .behavior_delivery import RuntimeContract, ReadinessEvidence, WorkDefinition
from .behavior_effects import Goal
from .behavior_foundry import FoundryTarget, NativeExecutionGrant, SUPPORTED_CHECKS
from .behavior_inbox import InboxDeploymentEvidence
from .behavior_operations import _Model, _utc
from .behavior_runtime import RuntimeBinding, RuntimePolicy
from .behavior_store import AuthorityScope, Digest
from .behavior_workers import WorkerContract, WorkerReadiness
from .models import Identifier
from .memory_projection import ProjectionSetup
from .source_backup import RecoveryAuthorization, SourceBackupReference
from .source_materials import (
    NativeConfigurationEvidence, NativeProtectionState, NoAutoGCConfiguration, digest,
)

MAX_CONFIG_BYTES = 512 * 1024
UUID = Annotated[str, Field(pattern=r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$")]
LocalPath = Annotated[str, Field(min_length=1, max_length=1024)]
Version = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")]
ACCOUNT = re.compile(
    r"^/subscriptions/[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}/resourceGroups/[A-Za-z0-9_.()-]+"
    r"/providers/Microsoft\.Storage/storageAccounts/([a-z0-9]{3,24})$"
)


def _pairs(pairs):
    value = dict(pairs)
    if len(value) != len(pairs):
        raise ValueError("duplicate configuration key")
    return value


def parse_document(raw: bytes, model):
    if len(raw) > MAX_CONFIG_BYTES:
        raise ValueError("configuration exceeds its bound")
    json.loads(raw, object_pairs_hook=_pairs, parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite")))
    return model.model_validate_json(raw)


def read_document(path: str | Path, model):
    path = Path(path)
    if any(node.is_symlink() or getattr(node, "is_junction", lambda: False)() for node in (path, *path.parents)):
        raise ValueError("configuration/catalog symlinks are not supported")
    if not path.is_file():
        raise ValueError("required operator file is missing")
    with path.open("rb") as stream:
        return parse_document(stream.read(MAX_CONFIG_BYTES + 1), model)


def _path(value):
    if "\x00" in value or "://" in value or ".." in Path(value).parts:
        raise ValueError("a fixed local operator path is required")
    return value


def _account(value):
    if ACCOUNT.fullmatch(value) is None:
        raise ValueError("exact public Azure Storage ARM account resource required")
    return value


def _storage_url(value, kind, account, *, container=False):
    parts = urlsplit(value)
    expected = account.rsplit("/", 1)[-1] + f".{kind}.core.windows.net"
    path_ok = bool(re.fullmatch(r"/[a-z0-9](?:[a-z0-9-]{1,61})[a-z0-9]", parts.path)) if container else parts.path == ""
    if (parts.scheme != "https" or parts.netloc != expected or not path_ok
            or parts.query or parts.fragment or parts.username or parts.password
            or container and "--" in parts.path):
        raise ValueError("exact public Azure endpoint/container without SAS, alias or credentials required")
    return value


class AgentIdentity(_Model):
    oid: UUID
    actor: Identifier


class IdentityConfiguration(_Model):
    tenant_id: UUID
    audience: str
    team_id: Identifier
    human_oid: UUID
    admin_client_id: UUID
    managed_identity_client_id: UUID | None
    agents: Annotated[tuple[AgentIdentity, ...], Field(min_length=4, max_length=4)]

    def auth(self):
        return AuthSettings(
            self.tenant_id, self.audience,
            (f"https://sts.windows.net/{self.tenant_id}/",
             f"https://login.microsoftonline.com/{self.tenant_id}/v2.0"),
            {agent.oid: Actor(agent.oid, agent.actor, self.team_id) for agent in self.agents},
        )

    @model_validator(mode="after")
    def exact(self):
        self.auth()
        if (self.human_oid in {a.oid for a in self.agents} or len({a.oid for a in self.agents}) != 4
                or any(a.actor.startswith("human-") for a in self.agents)):
            raise ValueError("four distinct agent identities and one separate human are required")
        return self


class ServerAgentScope(_Model):
    """Server-resolved ownership metadata, not a live identity or an ACL grant."""

    schema_version: Literal["fa-server-agent-scope-v1"] = "fa-server-agent-scope-v1"
    configuration_sha256: Digest
    authority_domain: Identifier
    control_binding: Digest
    tenant_id: UUID
    team_id: Identifier
    human_oid: UUID
    identity: AgentIdentity
    target: FoundryTarget
    legacy_keys: bool

    @model_validator(mode="after")
    def paired(self):
        if self.identity.oid != self.target.agent_oid:
            raise ValueError("agent identity and Hosted target must have the same original oid")
        return self


class TableConfiguration(_Model):
    account_resource_id: str
    endpoint: str
    scope: AuthorityScope

    _arm = field_validator("account_resource_id")(_account)

    @model_validator(mode="after")
    def exact(self):
        _storage_url(self.endpoint, "table", self.account_resource_id)
        return self


class BlobConfiguration(_Model):
    account_resource_id: str
    protection: NoAutoGCConfiguration

    _arm = field_validator("account_resource_id")(_account)

    @model_validator(mode="after")
    def exact(self):
        _storage_url(self.protection.container_url, "blob", self.account_resource_id, container=True)
        return self


class RootBudget(_Model):
    root_budget_id: Identifier
    root_request_id: Identifier
    attempts: Annotated[int, Field(ge=1, le=1000000)]
    units: Annotated[int, Field(ge=1, le=1000000)]
    control_units: Annotated[int, Field(ge=1, le=1000000)]
    scans: Annotated[int, Field(ge=1, le=1000)]
    pending_limit: Annotated[int, Field(ge=1, le=32)]


class ServerRuntimePolicy(RuntimePolicy):
    """Known deployed intake policy; consent/review still run in the core."""

    human_text_enabled: bool = False
    text_policy_version: Literal["human-text-policy-v1"] | None = None


class PolicyConfiguration(_Model):
    binding_id: Identifier
    policy: ServerRuntimePolicy


class OperationAlias(_Model):
    input_ref: Identifier
    binding_id: Identifier
    goal: Goal


class EvidenceCatalogConfiguration(_Model):
    path: LocalPath
    catalog_id: Identifier
    observer_oid: UUID
    minimum_revision: Annotated[int, Field(ge=1)]
    max_age_seconds: Annotated[int, Field(ge=1, le=86400)]
    inventory_scopes: Annotated[tuple[Identifier, ...], Field(min_length=1, max_length=16)]

    _local = field_validator("path")(_path)

    @model_validator(mode="after")
    def unique(self):
        if len(set(self.inventory_scopes)) != len(self.inventory_scopes):
            raise ValueError("inventory coverage must be unique")
        return self


class SupervisorLimits(_Model):
    ticks: Annotated[int, Field(ge=1, le=100)] = 1
    page_limit: Annotated[int, Field(ge=1, le=16)] = 4
    interval_seconds: Annotated[float, Field(ge=0, le=30)] = 1.0
    max_run_seconds: Annotated[float, Field(gt=0, le=3600)] = 120.0


class TokenizerConfiguration(_Model):
    version: Literal["0.11.0"]
    encoding: Literal["o200k_base"]
    cache_directory: LocalPath
    accounting_scope: Literal["context-text-sections-only"] = "context-text-sections-only"

    _local = field_validator("cache_directory")(_path)


class M3ContextConfiguration(_Model):
    """Existing legacy Table, not a new behavior authority or workspace."""

    account_resource_id: str
    endpoint: str
    table_name: Annotated[str, Field(pattern=r"^[A-Za-z][A-Za-z0-9]{2,62}$")]

    _arm = field_validator("account_resource_id")(_account)

    @model_validator(mode="after")
    def exact(self):
        _storage_url(self.endpoint, "table", self.account_resource_id)
        return self


class AssistantSettings(_Model):
    """Opt-in JSON; all profiles/backends are pinned by the server config digest."""

    profiles: Annotated[tuple[AssistantProfile, ...], Field(min_length=1, max_length=16)]
    provider_endpoint: Annotated[str, Field(min_length=1, max_length=2048)]
    model: Literal["gpt-4.1-mini"] = "gpt-4.1-mini"
    credential_binding: Literal["server-api-uami"] = "server-api-uami"
    m3_context: M3ContextConfiguration
    artifact_container_url: Annotated[str, Field(min_length=1, max_length=2048)]

    @model_validator(mode="after")
    def exact(self):
        if (len({p.binding_id for p in self.profiles}) != len(self.profiles)
                or len({p.artifact_id for p in self.profiles}) != len(self.profiles)):
            raise ValueError("assistant profiles and result artifacts must be distinct")
        return self


# The organization model ceiling minus its safety reserve (behavior_work_policy.SteadyWorkPolicy).
MAX_TEAM_MODEL_CAP_MICROUSD = 100_000_000


class TeamDefaults(_Model):
    """Organization defaults a new Team starts from (ADR-0006 T6b, Neal 2026-10-02 A/A/A/A/B).

    The operator initializing a Team and its owner afterwards can only keep or lower these values; the Team
    inherits the organization's approved connectors unless a smaller subset is chosen. Raising a value is a
    separate operator revision, not part of these commands."""

    model_cap_microusd: Annotated[int, Field(ge=1, le=MAX_TEAM_MODEL_CAP_MICROUSD)]
    max_agents: Annotated[int, Field(ge=1, le=MAX_TEAM_AGENTS)]
    connectors: Annotated[tuple[Identifier, ...], Field(max_length=16)] = ()

    @model_validator(mode="after")
    def distinct(self):
        if len(set(self.connectors)) != len(self.connectors):
            raise ValueError("duplicate organization connector")
        return self


class ServerConfiguration(_Model):
    schema_version: Literal["fa-behavior-server-v1"] = "fa-behavior-server-v1"
    deployment_id: Identifier
    identity: IdentityConfiguration
    table: TableConfiguration
    sources: BlobConfiguration
    backups: BlobConfiguration
    delivery: RuntimeContract
    workers: WorkerContract
    bindings: Annotated[tuple[RuntimeBinding, ...], Field(min_length=1, max_length=16)]
    policies: Annotated[tuple[PolicyConfiguration, ...], Field(min_length=1, max_length=16)]
    roots: Annotated[tuple[RootBudget, ...], Field(min_length=1, max_length=16)]
    operations: Annotated[tuple[OperationAlias, ...], Field(min_length=1, max_length=16)]
    targets: Annotated[tuple[FoundryTarget, ...], Field(min_length=4, max_length=4)]
    controller_id: Identifier
    controller_resource_id: Annotated[str, Field(min_length=1, max_length=2048)]
    api_resource_id: Annotated[str, Field(min_length=1, max_length=2048)]
    authentication_version: Literal["behavior-jwt-v1"] = "behavior-jwt-v1"
    review_policy_version: Version
    evidence: EvidenceCatalogConfiguration
    tokenizer: TokenizerConfiguration
    supervisor: SupervisorLimits = SupervisorLimits()
    team_pending_limit: Annotated[int, Field(ge=5, le=32)] = 32
    entry_pending_limit: Annotated[int, Field(ge=5, le=32)] = 32
    max_context_tokens: Annotated[int, Field(ge=1, le=8192)] = 8192
    max_context_bytes: Annotated[int, Field(ge=1, le=1048576)] = 1048576
    workload_version: Literal["checksum-count-v1", "assistant-v1.2"] = "checksum-count-v1"
    automatic_model_repairs: Literal[0] = 0
    total_poc_budget_usd: Literal[100] = 100
    memory_projections: Annotated[tuple[ProjectionSetup, ...], Field(max_length=16)] = ()
    assistant: AssistantSettings | None = None
    # Absent from configurations written before T6b, so their digest and installation stay unchanged.
    team_defaults: TeamDefaults | None = None

    @model_serializer(mode="wrap")
    def omit_unused_assistant(self, handler):
        value = handler(self)
        if self.assistant is None:
            value.pop("assistant", None)
        if self.team_defaults is None:
            value.pop("team_defaults", None)
        return value

    @property
    def sha256(self):
        return digest(self.model_dump(mode="json"))

    @property
    def active_targets(self):
        return self.targets[:self.workers.capacity]

    def agent_scope(self, agent_id):
        from .errors import Problem
        targets = tuple(target for target in self.targets if target.agent_id == agent_id)
        identities = tuple(agent for agent in self.identity.agents
                           if targets and agent.oid == targets[0].agent_oid)
        if len(targets) != 1 or len(identities) != 1:
            raise Problem(403, "AGENT_SCOPE_DENIED", "one configured agent identity/target pair is required")
        return ServerAgentScope(
            configuration_sha256=self.sha256, authority_domain=self.table.scope.authority_domain,
            control_binding=self.workers.control_binding, tenant_id=self.identity.tenant_id,
            team_id=self.identity.team_id, human_oid=self.identity.human_oid,
            identity=identities[0], target=targets[0], legacy_keys=targets[0] == self.targets[0],
        )

    @property
    def registered_jobs(self):
        return tuple((binding, job) for binding in self.bindings for job in binding.registered_jobs)

    def resource_bindings(self):
        resources = {
            "table_account": self.table.account_resource_id, "table_endpoint": self.table.endpoint,
            "table": self.table.scope.table_name, "partition": self.table.scope.partition_key,
            "authority_domain": self.table.scope.authority_domain,
            "sources_account": self.sources.account_resource_id,
            "sources_container": self.sources.protection.container_url,
            "backups_account": self.backups.account_resource_id,
            "backups_container": self.backups.protection.container_url,
            "controller": self.controller_resource_id, "api": self.api_resource_id,
            "targets_sha256": digest([target.model_dump(mode="json") for target in self.targets]),
            "memory_projections_sha256": digest([setup.model_dump(mode="json") for setup in self.memory_projections]),
        }
        if self.assistant is not None:
            resources.update(
                assistant_sha256=digest(self.assistant.model_dump(mode="json")),
                assistant_identity_sha256=digest(self.identity.model_dump(mode="json")),
                assistant_bindings_sha256=digest([binding.model_dump(mode="json") for binding in self.bindings]),
                assistant_credential_client_id=self.identity.managed_identity_client_id,
                m3_context_account=self.assistant.m3_context.account_resource_id,
                m3_context_endpoint=self.assistant.m3_context.endpoint,
                m3_context_table=self.assistant.m3_context.table_name,
                m3_context_partition=self.identity.team_id,
                m3_artifact_partition="m3-" + self.identity.tenant_id + "-" + self.identity.team_id,
                m3_artifact_container=self.assistant.artifact_container_url,
                assistant_provider_endpoint=self.assistant.provider_endpoint,
            )
        return resources

    @model_validator(mode="after")
    def exact(self):
        ids, oid = self.identity, {a.oid for a in self.identity.agents}
        jobs = self.registered_jobs
        assistant = self.workload_version == "assistant-v1.2"
        checks = set(ASSISTANT_CHECKS) if assistant else SUPPORTED_CHECKS
        if assistant != (self.assistant is not None):
            raise ValueError("assistant settings require explicit assistant-v1.2 workload selection")
        if assistant:
            settings = self.assistant
            if (self.workers.capacity != 1 or any(b.child_jobs for b in self.bindings)
                    or self.workers.executor_version != EXECUTOR_VERSION
                    or self.workers.protection_version != PROTECTION_VERSION
                    or self.delivery.protection_version != PROTECTION_VERSION
                    or ids.managed_identity_client_id is None
                    or settings.provider_endpoint != self.active_targets[0].project_endpoint + "/openai/v1/chat/completions"
                    or settings.m3_context.account_resource_id != self.table.account_resource_id
                    or settings.m3_context.endpoint != self.table.endpoint
                    or settings.m3_context.table_name != self.table.scope.table_name
                    or settings.artifact_container_url != self.sources.protection.container_url):
                raise ValueError("assistant requires C1, new readiness versions and exact existing API-UAMI/M3/provider resources")
            profiles = {p.binding_id: p for p in settings.profiles}
            if set(profiles) != {b.binding_id for b in self.bindings}:
                raise ValueError("every assistant binding requires its own profile")
            for binding in self.bindings:
                profile = profiles[binding.binding_id]
                if (binding.agent_oid != self.active_targets[0].agent_oid
                        or profile.claim_id not in binding.claim_ids
                        or profile.source.source_id not in binding.source_ids
                        or profile.legacy_source_work in {job.work.work_id for _, job in jobs}
                        or tuple(binding.work.required_checks) != tuple(ASSISTANT_CHECKS)):
                    raise ValueError("assistant identity, exact checks, source or legacy-work mapping differs")
        if (self.workers.capacity == 4 and len(self.bindings) < 5
                or len(jobs) > min(32, self.workers.pending_limit)
                or any(binding.child_jobs for binding in self.bindings) and self.workers.max_generations < 2
                or len({job.job_id for _, job in jobs}) != len(jobs)
                or len({job.work.work_id for _, job in jobs}) != len(jobs)):
            raise ValueError("C1/C4 require distinct bounded jobs; C4 retains four slots and a fifth root job")
        for _, job in jobs:
            if (set(job.work.required_checks) != checks or job.work.human_checks
                    or job.work.result_kind != "answer"):
                raise ValueError("every registered job needs its own supported workload definition")
        if (self.delivery.team_id != ids.team_id or self.delivery.control_binding != self.workers.control_binding
                or self.delivery.protection_version != self.workers.protection_version
                or self.delivery.model_repair_contract is not None):
            raise ValueError("worker/delivery/native model-repair configuration differs")
        if (len({t.agent_oid for t in self.targets}) != 4 or {t.agent_oid for t in self.targets} != oid
                or len({t.agent_id for t in self.targets}) != 4 or len({t.executor_id for t in self.targets}) != 4
                or len({t.session_id for t in self.targets}) != 4
                or len({(t.project_endpoint, t.agent_name) for t in self.targets}) != 4
                or any(not urlsplit(t.project_endpoint).hostname.endswith(".services.ai.azure.com") for t in self.targets)):
            raise ValueError("four distinct exact Foundry agent/MI/session targets required")
        if self.evidence.observer_oid in oid:
            raise ValueError("Hosted agents cannot be their own readiness observer")
        for storage, prefix in ((self.sources, "m3/v1/"), (self.backups, "v1/")):
            if (storage.protection.authority_domain != self.table.scope.authority_domain
                    or storage.protection.protected_prefix != prefix + ids.tenant_id + "/"):
                raise ValueError("source/backup authority, tenant or retained namespace differs")
        if self.sources.protection.container_url == self.backups.protection.container_url:
            raise ValueError("source and backup containers must be separate")
        bindings = {b.binding_id: b for b in self.bindings}
        roots = {r.root_budget_id: r for r in self.roots}
        if (len(bindings) != len(self.bindings) or len(roots) != len(self.roots)
                or len({p.binding_id for p in self.policies}) != len(self.policies)
                or {p.binding_id for p in self.policies} != set(bindings)
                or len({a.input_ref for a in self.operations}) != len(self.operations)):
            raise ValueError("duplicate/missing server bindings, roots, aliases or policies")
        used_roots = set()
        for binding in self.bindings:
            if (binding.tenant_id != ids.tenant_id or binding.team_id != ids.team_id
                    or binding.route.channel_id != "authenticated-pull"
                    or binding.human_oid != ids.human_oid or binding.agent_oid not in oid
                    or binding.owner.root_budget_id not in roots
                    or roots[binding.owner.root_budget_id].root_request_id != binding.root_request_id
                    or set(binding.work.required_checks) != checks
                    or binding.work.human_checks or binding.work.result_kind != "answer"):
                raise ValueError("fixed identity/root/deterministic work contract differs")
            used_roots.add(binding.owner.root_budget_id)
            aliases = {a.input_ref for a in self.operations if a.binding_id == binding.binding_id}
            if aliases != set(binding.allowed_input_refs):
                raise ValueError("operation aliases must resolve only their own fixed work")
        if set(roots) != used_roots or {a.binding_id for a in self.operations} != set(bindings):
            raise ValueError("unused root or operation binding")
        for name in ("owner_record_id", "policy_id"):
            if len({getattr(b.owner, name) for b in self.bindings}) != len(bindings):
                raise ValueError("owner/policy identities must be unambiguous")
        for policy in self.policies:
            binding = bindings[policy.binding_id]
            expected = digest(["fa-behavior-api-v1", "native", binding.model_dump(mode="json")])
            if policy.policy.binding_digest != expected:
                raise ValueError("runtime policy is not bound to this native binding")
        policies = {item.binding_id: item.policy for item in self.policies}
        if (len({setup.binding_id for setup in self.memory_projections}) != len(self.memory_projections)
                or len({(setup.endpoint, setup.store, setup.projection_namespace) for setup in self.memory_projections})
                != len(self.memory_projections)):
            raise ValueError("memory projection binding and namespace must be unique")
        for setup in self.memory_projections:
            binding = bindings.get(setup.binding_id)
            if binding is None:
                raise ValueError("memory projection references an unknown runtime binding")
            policy = policies[binding.binding_id]
            actor = next(agent.actor for agent in ids.agents if agent.oid == binding.agent_oid)
            if (setup.control_binding != self.workers.control_binding
                    or setup.endpoint not in {target.project_endpoint for target in self.targets}
                    or setup.scope.tenant_id != ids.tenant_id or setup.scope.team_id != ids.team_id
                    or setup.scope.visibility != policy.source_visibility
                    or setup.scope.owner_actor is not None and setup.scope.owner_actor not in {actor, "human-" + ids.human_oid}
                    or setup.subject != binding.subject or not set(setup.claim_ids) <= set(binding.claim_ids)
                    or setup.versions != binding.context_versions):
                raise ValueError("memory projection must use the exact project/reviewed source/binding/context authority")
        versions = [
            self.review_policy_version, self.workers.executor_version, self.delivery.protection_version,
            self.delivery.consumer_version, self.delivery.timer_version, self.delivery.verifier_version,
            self.sources.protection.configuration_version, self.backups.protection.configuration_version,
            self.sources.protection.generation, self.backups.protection.generation,
        ]
        for binding in self.bindings:
            versions.extend((binding.work.criteria_version, binding.context_versions.release_version,
                             binding.context_versions.catalog_version, binding.context_versions.memory_schema_version))
            versions.extend(child.work.criteria_version for child in binding.child_jobs)
        if any(v.lower() in {"latest", "current", "head", "unknown"} for v in versions):
            raise ValueError("versions must be pinned")
        for value in (self.api_resource_id, self.controller_resource_id):
            if not value.startswith("/subscriptions/") or "*" in value or "?" in value:
                raise ValueError("exact deployed API/controller ARM resources required")
        return self


from .behavior_channels import TeamsChannelConfiguration


class ServerConfigurationV2(ServerConfiguration):
    """Explicit extension; v1 serialization and every original contract stay intact."""
    schema_version: Literal["fa-behavior-server-v2"]
    channels: TeamsChannelConfiguration

    @model_validator(mode="after")
    def exact_channels(self):
        setup = self.channels
        if (self.workload_version != "assistant-v1.2" or self.workers.capacity != 1
                or self.delivery.channel.channel_id != "authenticated-pull"
                or self.delivery.channel.supported_levels != ("client_received",)
                or self.delivery.channel.max_confirmation_level != "client_received"
                or len(self.bindings) != 1 or len(self.roots) != 1
                or setup.template_binding_id != self.bindings[0].binding_id
                or setup.personal.team_id != self.identity.team_id
                or setup.personal.tenant_id != self.identity.tenant_id
                or setup.personal.intended_user_oid != self.identity.human_oid
                or setup.personal.uami_client_id != self.identity.managed_identity_client_id
                or setup.native_max_generations > self.workers.max_generations
                or setup.max_model_requests > self.assistant.profiles[0].max_model_requests
                or self.assistant.profiles[0].require_operation):
            raise ValueError("Teams metrics requires the original C1/root, designated personal identity and reviewed SDK template")
        _path(setup.native_evidence_path)
        return self

    def predecessor(self):
        value = self.model_dump(mode="json")
        value.pop("channels")
        value["schema_version"] = "fa-behavior-server-v1"
        return ServerConfiguration.model_validate_json(json.dumps(value))

    def resource_bindings(self):
        return {**super().resource_bindings(),
                "channels_sha256": digest(self.channels.model_dump(mode="json"))}


class ServerConfigurationDocument:
    @staticmethod
    def model_validate_json(raw):
        value = json.loads(raw, object_pairs_hook=_pairs)
        model = ServerConfigurationV2 if value.get("schema_version") == "fa-behavior-server-v2" else ServerConfiguration
        return model.model_validate_json(raw)


class ProtectionObservation(_Model):
    configuration: NoAutoGCConfiguration
    observed_at: datetime
    valid_until: datetime
    blob_service: NativeConfigurationEvidence
    management_policy: NativeConfigurationEvidence
    automatic_delete_consumers: tuple[Identifier, ...] | None
    inventory_version: Identifier | None
    inventory_coverage: tuple[Identifier, ...] | None
    inventory_observed_at: datetime | None
    inventory_valid_until: datetime | None

    _times = field_validator("observed_at", "valid_until")(_utc)

    @model_validator(mode="after")
    def kinds(self):
        if self.blob_service.resource_kind != "blob-service" or self.management_policy.resource_kind != "management-policy":
            raise ValueError("native evidence kinds must match their storage configuration lanes")
        return self

    def state(self):
        return NativeProtectionState(
            **self.configuration.model_dump(exclude={"mode"}), observed_at=self.observed_at, valid_until=self.valid_until,
            blob_service_etag=self.blob_service.etag, management_policy_etag=self.management_policy.etag,
            versioning_enabled=self.blob_service.properties.get("isVersioningEnabled") is True,
            management_policy=self.management_policy.management_policy(),
            automatic_delete_consumers=self.automatic_delete_consumers, inventory_version=self.inventory_version,
            blob_service_evidence=self.blob_service, management_policy_evidence=self.management_policy,
        )


class ProjectionInstallationEvidence(_Model):
    schema_version: Literal["fa-projection-installation-evidence-v1"] = "fa-projection-installation-evidence-v1"
    binding_id: Identifier
    setup_sha256: Digest
    evidence_sha256: Digest
    observed_at: datetime
    expires_at: datetime
    verified_contracts: tuple[Literal[
        "existing_store", "namespace_isolation", "same_authority", "exact_authority_fallback",
    ], ...]
    proof: Annotated[bytes, Field(min_length=1, max_length=8192)]

    _times = field_validator("observed_at", "expires_at")(_utc)


class NativeEvidenceBundle(_Model):
    """Current catalog snapshot; nullable entries mean unknown, never ready."""

    schema_version: Literal["fa-native-evidence-bundle-v1"] = "fa-native-evidence-bundle-v1"
    catalog_id: Identifier
    revision: Annotated[int, Field(ge=1)]
    observer_oid: UUID
    configuration_sha256: Digest
    deployment_id: Identifier
    resource_bindings: dict[str, str]
    captured_at: datetime
    expires_at: datetime
    sources: ProtectionObservation | None
    backups: ProtectionObservation | None
    worker: WorkerReadiness | None
    inbox: InboxDeploymentEvidence | None
    delivery: ReadinessEvidence | None
    memory_projections: Annotated[tuple[ProjectionInstallationEvidence, ...], Field(max_length=16)] = ()

    _times = field_validator("captured_at", "expires_at")(_utc)


class BootstrapAuthorization(_Model):
    schema_version: Literal["fa-bootstrap-authorization-v1"] = "fa-bootstrap-authorization-v1"
    decision_id: Identifier
    authorizing_human_oid: UUID
    configuration_sha256: Digest
    purpose: Literal["authorized-six-gap-validation"]
    not_before: datetime
    expires_at: datetime
    native_grants: Annotated[tuple[NativeExecutionGrant, ...], Field(min_length=1, max_length=32)]

    _times = field_validator("not_before", "expires_at")(_utc)


class RecoveryConfiguration(_Model):
    schema_version: Literal["fa-recovery-server-v1"] = "fa-recovery-server-v1"
    deployment_id: Identifier
    identity: IdentityConfiguration
    authority_domain: Identifier
    backup_account_resource_id: str
    backup_container_url: str
    catalog: EvidenceCatalogConfiguration

    _arm = field_validator("backup_account_resource_id")(_account)

    @model_validator(mode="after")
    def exact(self):
        _storage_url(self.backup_container_url, "blob", self.backup_account_resource_id, container=True)
        return self

    @property
    def sha256(self):
        return digest(self.model_dump(mode="json"))


class RecoveryCatalog(_Model):
    schema_version: Literal["fa-current-recovery-catalog-v1"] = "fa-current-recovery-catalog-v1"
    catalog_id: Identifier
    observer_oid: UUID
    revision: Annotated[int, Field(ge=1)]
    configuration_sha256: Digest
    observed_at: datetime
    expires_at: datetime
    reference: SourceBackupReference
    decision: RecoveryAuthorization
    active: bool

    _times = field_validator("observed_at", "expires_at")(_utc)


def load_configuration(path=None, *, recovery=False, environ=None):
    env = os.environ if environ is None else environ
    prefix = "BEHAVIOR_RECOVERY_CONFIG" if recovery else "BEHAVIOR_SERVER_CONFIG"
    selected = path if path is not None else env.get(prefix)
    raw = env.get(prefix + "_JSON")
    if bool(selected) == bool(raw):
        raise ValueError("supply exactly one trusted configuration path or JSON environment value")
    model = RecoveryConfiguration if recovery else ServerConfigurationDocument
    return read_document(selected, model) if selected else parse_document(raw.encode("utf-8"), model)


def synthetic_configuration():
    """Non-runnable synthetic config. No evidence/authorization is synthesized."""
    from .behavior_delivery import ChannelContract, Route
    from .behavior_operations import ExecutionContext
    from .memory_governance import ContextVersions

    tenant, human, client = ("11111111-1111-1111-1111-111111111111",
                             "99999999-9999-9999-9999-999999999999",
                             "33333333-3333-3333-3333-333333333333")
    identity = IdentityConfiguration(
        tenant_id=tenant, audience="api://22222222-2222-2222-2222-222222222222",
        team_id="team-one", human_oid=human, admin_client_id=client, managed_identity_client_id=None,
        agents=tuple(AgentIdentity(oid=f"{n * 8}-{n * 4}-{n * 4}-{n * 4}-{n * 12}",
                                  actor=f"agent-{i}") for i, n in enumerate("abcd", 1)),
    )
    account = f"/subscriptions/{tenant}/resourceGroups/synthetic/providers/Microsoft.Storage/storageAccounts/synthetic"
    scope = AuthorityScope(authority_domain="synthetic-behavior", table_name="Behavior", partition_key="authority")
    table = TableConfiguration(account_resource_id=account, endpoint="https://synthetic.table.core.windows.net", scope=scope)
    control = digest({"layout": 1, "endpoint": ["https", "synthetic.table.core.windows.net", ""],
                      "domain": scope.authority_domain, "table": scope.table_name, "partition": scope.partition_key})
    delivery = RuntimeContract(
        control_binding=control, team_id=identity.team_id, entry_id="behavior-api", protection_version="retention-v1",
        consumer_version="behavior-controller-v1", timer_version="bounded-tick-v1", verifier_version="checksum-verifier-v1",
        channel=ChannelContract(channel_id="authenticated-pull", version="fa-pull-inbox-v1",
                                supported_levels=("client_received",), max_confirmation_level="client_received"),
    )
    worker = WorkerContract(workspace_id="workspace", control_binding=control, executor_id="native-pool",
                            executor_version="checksum-count-v1", protection_version="retention-v1", capacity=4)
    bindings, policies, aliases = [], [], []
    expiry = datetime(2026, 9, 16, 8, 0, tzinfo=timezone.utc)
    for index in range(1, 6):
        owner = ExecutionContext(owner_record_id=f"owner-{index}", principal_id=identity.agents[(index - 1) % 4].oid,
                                 epoch=1, conversation_id=f"conversation-{index}", policy_id=f"policy-{index}", root_budget_id="root")
        binding = RuntimeBinding(
            binding_id=f"binding-{index}", tenant_id=tenant, team_id=identity.team_id, human_oid=human,
            agent_oid=owner.principal_id, owner=owner, ingress_id="authenticated-api",
            source_ids=(f"metrics-{index}",), claim_ids=(f"claim-{index}",), subject=f"subject-{index}",
            event_id=f"event-{index}", root_request_id="six-gap-validation", job_id=f"job-{index}",
            work=WorkDefinition(work_id=f"work-{index}", goal="Run the authorized deterministic checksum validation.",
                                criteria_version="checksum-criteria-v1", result_kind="answer", required_checks=tuple(sorted(SUPPORTED_CHECKS))),
            route=Route(tenant_id=tenant, account_id="synthetic-account", recipient_id=human,
                        channel_id="authenticated-pull", thread_id=f"thread-{index}"),
            operation_id=f"operation-{index}", attempt_id=f"attempt-{index}", grant_id=f"operation-grant-{index}",
            allowed_input_refs=(f"review-goal-{index}",),
            context_versions=ContextVersions(work_revision=1, release_version="release-v1", catalog_version="catalog-v1", memory_schema_version="memory-v1"),
        )
        bindings.append(binding)
        policies.append(PolicyConfiguration(binding_id=binding.binding_id, policy=ServerRuntimePolicy(
            binding_digest=digest(["fa-behavior-api-v1", "native", binding.model_dump(mode="json")]),
            operation_policy_revision=1, active=True, source_visibility="team", purpose="metrics",
            allowed_purposes=("metrics",), valid_until=expiry,
        )))
        aliases.append(OperationAlias(input_ref=binding.allowed_input_refs[0], binding_id=binding.binding_id,
                                      goal="Review the prepared result against the pinned work requirements."))
    def blob(container, prefix):
        return BlobConfiguration(account_resource_id=account, protection=NoAutoGCConfiguration(
            authority_domain=scope.authority_domain, generation="retention-generation-1",
            container_url=f"https://synthetic.blob.core.windows.net/{container}",
            protected_prefix=prefix + tenant + "/", configuration_version="retention-v1", evidence_sha256="0" * 64,
        ))
    return ServerConfiguration(
        deployment_id="synthetic-deployment", identity=identity, table=table, sources=blob("sources", "m3/v1/"),
        backups=blob("source-backups", "v1/"), delivery=delivery, workers=worker,
        bindings=tuple(bindings), policies=tuple(policies), operations=tuple(aliases),
        roots=(RootBudget(root_budget_id="root", root_request_id="six-gap-validation", attempts=100,
                          units=1000, control_units=1000, scans=64, pending_limit=32),),
        targets=tuple(FoundryTarget(executor_id=f"executor-{i}", project_endpoint="https://synthetic.services.ai.azure.com/api/projects/project",
                                   agent_id=f"{i:08d}-1111-1111-1111-111111111111", agent_name=f"checksum-{i}",
                                   agent_version="1", session_id=f"session-{i}", agent_oid=agent.oid)
                      for i, agent in enumerate(identity.agents, 1)),
        controller_id="controller", controller_resource_id=f"/subscriptions/{tenant}/resourceGroups/synthetic/providers/Microsoft.App/containerApps/controller",
        api_resource_id=f"/subscriptions/{tenant}/resourceGroups/synthetic/providers/Microsoft.App/containerApps/api",
        review_policy_version="human-review-v1",
        evidence=EvidenceCatalogConfiguration(path="operator-native-evidence.json", catalog_id="native-catalog",
            observer_oid="88888888-8888-8888-8888-888888888888", minimum_revision=1, max_age_seconds=3600,
            inventory_scopes=("storage-lifecycle", "scheduled-jobs", "application-gc", "operator-automation")),
        tokenizer=TokenizerConfiguration(version="0.11.0", encoding="o200k_base", cache_directory="/opt/fa-tokenizer"),
    )
