"""Production ASGI/controller composition; no startup provisioning or daemons.

Operator sequence (each command is a separate supervised process):
  python -m shared_context.behavior_server bootstrap --config config.json
      --evidence evidence.json --stage records --authorization approval.json
  python -m shared_context.behavior_server probe --config config.json
  python -m shared_context.behavior_server bootstrap --config config.json
      --evidence evidence.json --stage worker --authorization approval.json
  python -m shared_context.behavior_server controller --config config.json --preactivation
  python -m shared_context.behavior_server bootstrap --config config.json
      --evidence evidence.json --stage activate --authorization approval.json
  python -m shared_context.behavior_server serve --config config.json
  python -m shared_context.behavior_server controller --config config.json

The preactivation API has ONLY assigned native callbacks and an authenticated,
read-only configuration probe. No user admission/source/approval routes exist.
An independent operator captures protection + worker proof first, then observes
the supervised controller/pool/probe API and supplies inbox/delivery evidence.
Use an explicit ACA Job in the same private environment for bootstrap; a laptop
cannot reach private Table/Blob. /healthz is process liveness ONLY, has no data
access and is not evidence of storage, authorization, consumers or native agents.
No command invents those observations or converts local tests into native PASS.
Human text is disabled by default. Enable only in the exact approved deployment
policy with human_text_enabled=true and text_policy_version=human-text-policy-v1.
This allows bounded signed-human retention consent, not untrusted tool content,
general secret detection, automatic claim approval or tool execution permission.
Optional memory_projections use the same reviewed SourceMaterials/MemoryGovernance
and owned AIProjectClient instances. Existing-store/namespace/setup evidence must
be present in the current catalog before activation; no store creation, update,
poll or search happens at startup. Native completion receipts are never minted
by this factory. Memory remains a retrieval projection, not an authority.
Token counts use pinned tiktoken 0.11.0/o200k_base verified local ranks. Prewarm
TIKTOKEN_CACHE_DIR during the image build, not at native service startup. These
counts cover context text sections only, never full model chat-envelope usage;
provider usage remains the native SDK receipt. Invalid UTF-8 is an unknown count.
For C1, explicitly set workers.capacity=1; the four-identity catalog is retained
but only targets[0] is registered with the native executor. Register leaf jobs in
RuntimeBinding.child_jobs, give each its own exact NativeExecutionGrant, and give
the parent a partial checkpoint_after_bytes plus at least two launch uses. A
manual controller tick parks an active parent with registered pending children
before normal WorkerConsumer scheduling. Only verified idle/fence/checkpoint
evidence releases the slot; parent resume uses its original checkpoint. C4 with
empty child_jobs preserves the deployed configuration and binding digests.
assistant-v1.2 instead requires AssistantSettings, C1/no children and independent
SDK/M3 readiness. The existing Table/container clients compose ArtifactService;
legacy ownership reads are deferred to installation, never build_server. See
behavior_assistant_server for the exact opt-in JSON and disabled capabilities.

Required pre-existing resources: exact Table, versioned source/backup containers,
four Foundry agent identities/versions/sessions, API and supervised controller.
One immutable installation binds the authority to one configured global pool.
Changing configuration requires a separately reviewed drained migration, NOT
bootstrap in another workspace/partition to evade existing capacity or counters.
MI needs only the specific Table entity, source/backup Blob and Foundry invocation
data access for its process. Recovery uses a separate MI with backup READ only.
If memory_projections is configured, the API MI additionally needs data access
to its explicitly configured existing Foundry Memory store, never M4 scopes.
Observer inventory permissions are separate; this module never calls ARM or
creates resources/RBAC. Delegated Behavior.Read/Approve/Backup/Restore stay
distinct from app-only agent tokens, which are admitted by oid alone (ADR-0006
gate C, no app role); Artifacts.Approve grants nothing here.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
import base64
from collections.abc import Mapping
from contextlib import ExitStack, asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from importlib.metadata import version as package_version
from pathlib import Path
from os import environ, getpid
from types import MappingProxyType

from azure.ai.projects import AIProjectClient
from azure.data.tables import TableClient
from azure.identity import ManagedIdentityCredential
from azure.storage.blob import ContainerClient
from fastapi import APIRouter, Depends, FastAPI, Request

from . import behavior_wire as wire
from .behavior_api import (
    _install_error_handlers, _principal_dependency, _response, behavior_router,
    create_source_recovery_app,
)
from .behavior_auth import BehaviorAuthSettings, BehaviorJwtVerifier
from .behavior_delivery import DeliveryCore, DeliveryConsumer, PendingIndexRecord, QuotaRecord, RuntimeRegistration
from .behavior_delivery_pending import counts as pending_counts, head_key as pending_head_key
from .behavior_effects import WorkGoalUpdate, WorkStateEffectAdapter
from .behavior_foundry import FoundryExecutor, FoundryHttpClient, NativeJobRoute, NativeLaunchAuthority, NativeRecoveryBinding, native_key
from .behavior_inbox import InboxRegistration, PullInbox
from .behavior_operations import BudgetRecord, OwnerRecord, PolicyRecord, ToolBrokerCore, control_key
from .behavior_runtime import BehaviorRuntime, RuntimeDependencies, RuntimeEvidenceAuthority, executor_job_input
from .behavior_server_config import (
    BootstrapAuthorization, NativeEvidenceBundle, RecoveryCatalog, RecoveryConfiguration,
    ServerConfiguration, SupervisorLimits, TokenizerConfiguration, load_configuration, read_document,
)
from .behavior_store import BehaviorStore, ControlChanges, ControlRequest, ControlWrite
from .behavior_workers import WorkerCore
from .errors import Problem
from .m3_models import MAX_ARTIFACT_BYTES
from .m3_store import BlobArtifactStore
from .memory_governance import MemoryGovernance
from .memory_projection import NativeMemoryProjection
from .source_backup import SourceBackupService, SourceBackupSettings, make_blob_backup_store
from .source_materials import InventoryProtectionVerifier, SourceMaterials, digest
from .source_reviews import DurableHumanReviewVerifier, HumanReviewPolicy

SDK_OPTIONS = {
    "retry_total": 0, "retry_connect": 0, "retry_read": 0, "retry_status": 0,
    "retry_to_secondary": False, "logging_enable": False,
    "connection_timeout": 10, "read_timeout": 20,
}
COVERAGE = {"atomic_control", "inline_retention", "durable_consumer", "timer_redrive", "bounded_scan", "ownership_recovery"}
TOKENIZER_URL = "https://openaipublic.blob.core.windows.net/encodings/o200k_base.tiktoken"
TOKENIZER_SHA256 = "446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d"
TOKENIZER_CACHE_KEY = hashlib.sha1(TOKENIZER_URL.encode()).hexdigest()
# Longest wait between read-only session checks of an instruction whose Hosted stopped answering.
LOST_WAIT_MAX_SECONDS = 900


@lru_cache(maxsize=1)
def _o200k_base(data):
    import tiktoken

    # The pinned 0.11.0 o200k_base constructor parameters. Loading verified local
    # ranks directly avoids get_encoding()'s implicit network/cache fallback.
    pattern = "|".join((
        r"[^\r\n\p{L}\p{N}]?[\p{Lu}\p{Lt}\p{Lm}\p{Lo}\p{M}]*[\p{Ll}\p{Lm}\p{Lo}\p{M}]+(?i:'s|'t|'re|'ve|'m|'ll|'d)?",
        r"[^\r\n\p{L}\p{N}]?[\p{Lu}\p{Lt}\p{Lm}\p{Lo}\p{M}]+[\p{Ll}\p{Lm}\p{Lo}\p{M}]*(?i:'s|'t|'re|'ve|'m|'ll|'d)?",
        r"\p{N}{1,3}", r" ?[^\s\p{L}\p{N}]+[\r\n/]*", r"\s*[\r\n]+", r"\s+(?!\S)", r"\s+",
    ))
    ranks = {base64.b64decode(token, validate=True): int(rank)
             for token, rank in (line.split() for line in data.splitlines() if line)}
    return tiktoken.Encoding(
        name="o200k_base", pat_str=pattern, mergeable_ranks=ranks,
        special_tokens={"<|endoftext|>": 199999, "<|endofprompt|>": 200018},
    )


class ServerTokenizer:
    """Text-section BPE counts, NOT provider usage or full chat-envelope tokens."""

    def __init__(self, config: TokenizerConfiguration):
        config = TokenizerConfiguration.model_validate_json(config.model_dump_json())
        try:
            if package_version("tiktoken") != config.version:
                raise ValueError("tokenizer package version differs")
            path = Path(config.cache_directory) / TOKENIZER_CACHE_KEY
            if not path.is_file() or any(node.is_symlink() or getattr(node, "is_junction", lambda: False)() for node in (path, *path.parents)):
                raise ValueError("tokenizer cache must not use symlinks")
            with path.open("rb") as stream:
                data = stream.read(5 * 1024 * 1024 + 1)
            if len(data) > 5 * 1024 * 1024 or hashlib.sha256(data).hexdigest() != TOKENIZER_SHA256:
                raise ValueError("tokenizer cache is missing or differs from the pinned upstream encoding")
            self.encoding = _o200k_base(data)
        except (OSError, ValueError, ImportError):
            raise Problem(503, "BEHAVIOR_TOKENIZER_UNAVAILABLE", "the pinned offline tokenizer cache is required") from None

    def count(self, data: bytes) -> int | None:
        if not isinstance(data, bytes):
            return None
        try:
            text = data.decode("utf-8", errors="strict")
            return len(self.encoding.encode(text, allowed_special=set(), disallowed_special=()))
        except (UnicodeError, ValueError):
            return None


def utc_now():
    return datetime.now(timezone.utc)


def _denied(message):
    return Problem(503, "BEHAVIOR_NATIVE_EVIDENCE_REQUIRED", message)


def _fresh(start, end, now, max_age):
    if (start.tzinfo is None or end.tzinfo is None or start.utcoffset().total_seconds() != 0
            or end.utcoffset().total_seconds() != 0 or not start <= now < end
            or (now - start).total_seconds() > max_age):
        raise _denied("native observation is missing, expired or outside its freshness bound")


class NativeEvidenceCatalog:
    """Reload the current trusted file on every gate; no HTTP-selected loaders."""

    def __init__(self, configuration: ServerConfiguration, *, clock=utc_now):
        self.config, self.clock = configuration, clock
        self.runtime = None

    def load(self):
        try:
            bundle = read_document(self.config.evidence.path, NativeEvidenceBundle)
        except (ValueError, OSError):
            raise _denied("current operator evidence catalog is unavailable") from None
        config, catalog, now = self.config, self.config.evidence, self.clock()
        if (bundle.catalog_id != catalog.catalog_id or bundle.observer_oid != catalog.observer_oid
                or bundle.revision < catalog.minimum_revision or bundle.configuration_sha256 != config.sha256
                or bundle.deployment_id != config.deployment_id or bundle.resource_bindings != config.resource_bindings()):
            raise _denied("native catalog resource, configuration or observer binding differs")
        _fresh(bundle.captured_at, bundle.expires_at, now, catalog.max_age_seconds)
        return bundle

    def protection(self, name, bundle=None):
        bundle = self.load() if bundle is None else bundle
        observation = getattr(bundle, name)
        expected = getattr(self.config, name)
        if observation is None or observation.configuration != expected.protection:
            raise _denied("exact source/backup retention observation is missing")
        now, settings = self.clock(), self.config.evidence
        if (observation.inventory_coverage is None
                or set(observation.inventory_coverage) != set(settings.inventory_scopes)
                or len(observation.inventory_coverage) != len(settings.inventory_scopes)
                or observation.inventory_observed_at is None or observation.inventory_valid_until is None):
            raise _denied("other automatic-deletion inventory remains unverified")
        _fresh(observation.observed_at, observation.valid_until, now, settings.max_age_seconds)
        _fresh(observation.inventory_observed_at, observation.inventory_valid_until, now, settings.max_age_seconds)
        for evidence in (observation.blob_service, observation.management_policy):
            if evidence.account_resource_id != expected.account_resource_id:
                raise _denied("observed ARM storage resource differs")
            _fresh(evidence.observed_at, evidence.valid_until, now, settings.max_age_seconds)
        state = observation.state()
        InventoryProtectionVerifier(lambda: state, clock=self.clock).verify(expected.protection)
        return state

    def worker(self, evidence_id=None, bundle=None):
        bundle = self.load() if bundle is None else bundle
        self.protection("sources", bundle)
        self.protection("backups", bundle)
        value, config = bundle.worker, self.config
        if self.runtime is None or value is None:
            raise _denied("independent native worker/supervisor observation is missing")
        contract = config.workers
        if (evidence_id is not None and value.evidence_id != evidence_id
                or value.configuration_digest != self.runtime.dep.workers.configuration_digest()
                or value.control_binding != contract.control_binding
                or value.protection_version != contract.protection_version
                or value.executor_id != contract.executor_id or value.executor_version != contract.executor_version
                or value.native_binding_reference != config.deployment_id
                or value.supervisor_reference != config.controller_id
                or value.protection_reference != config.sources.protection.generation):
            raise _denied("native worker proof is not bound to this deployment/supervisor/protection")
        _fresh(value.observed_at, value.expires_at, self.clock(), config.evidence.max_age_seconds)
        return value

    def delivery(self, bundle=None):
        bundle = self.load() if bundle is None else bundle
        self.worker(bundle=bundle)
        value, config = bundle.delivery, self.config.delivery
        if value is None:
            raise _denied("independent durable-consumer/timer observation is missing")
        if (value.configuration_digest != self.runtime.dep.delivery.configuration_digest()
                or value.control_binding != config.control_binding
                or value.protection_version != config.protection_version
                or value.consumer_version != config.consumer_version or value.timer_version != config.timer_version
                or set(value.coverage) != COVERAGE or len(value.coverage) != len(COVERAGE)):
            raise _denied("delivery configuration or independently verified coverage differs")
        _fresh(value.observed_at, value.expires_at, self.clock(), self.config.evidence.max_age_seconds)
        return value

    def inbox(self, evidence_id=None):
        bundle = self.load()
        readiness = self.delivery(bundle)
        value, config = bundle.inbox, self.config
        worker = bundle.worker
        if value is None:
            raise _denied("independent authenticated-inbox observation is missing")
        if (evidence_id is not None and value.evidence_id != evidence_id
                or value.readiness != readiness or value.configuration_digest != readiness.configuration_digest
                or value.control_binding != config.delivery.control_binding
                or value.authentication_version != config.authentication_version
                or value.protection_scope != config.table.scope.partition_key
                or value.protection_evidence_id != config.sources.protection.generation
                or value.worker_evidence_id != worker.evidence_id
                or value.worker_workspace_id != config.workers.workspace_id
                or value.worker_configuration_digest != worker.configuration_digest):
            raise _denied("inbox proof does not bind this authenticated API/worker pool")
        return value

    def require_activation(self):
        self.inbox()
        self.projections()

    def projections(self):
        if not self.config.memory_projections:
            return
        bundle = self.load()
        entries = {item.binding_id: item for item in bundle.memory_projections}
        if (len(entries) != len(bundle.memory_projections)
                or set(entries) != {setup.binding_id for setup in self.config.memory_projections}):
            raise _denied("current optional memory projection installation evidence is missing")
        contracts = {"existing_store", "namespace_isolation", "same_authority", "exact_authority_fallback"}
        for setup in self.config.memory_projections:
            proof = entries[setup.binding_id]
            if (proof.setup_sha256 != setup.sha256 or proof.evidence_sha256 != setup.evidence_sha256
                    or set(proof.verified_contracts) != contracts or len(proof.verified_contracts) != len(contracts)):
                raise _denied("memory projection evidence differs from the configured existing store/namespace/authority")
            _fresh(proof.observed_at, proof.expires_at, self.clock(), self.config.evidence.max_age_seconds)


class _OperationCatalog(Mapping):
    """Resolve an exact configured alias against the CURRENT native work ETag."""

    def __init__(self, config):
        self.aliases = {alias.input_ref: alias for alias in config.operations}
        self.runtime = self.adapter = None

    def __len__(self):
        return len(self.aliases)

    def __iter__(self):
        return iter(self.aliases)

    def __getitem__(self, key):
        alias = self.aliases[key]
        binding = self.runtime.bindings[alias.binding_id]
        context = self.runtime.resolve_owner(binding.owner.owner_record_id)
        current = self.adapter.inspect(binding.work.work_id, context)
        return WorkGoalUpdate(
            work_id=binding.work.work_id, obligation_id=binding.obligation_id,
            expected_etag=current.etag, expected_definition_digest=current.definition_digest, goal=alias.goal,
        )


@dataclass(frozen=True)
class _VerifiedOrigin:
    process_id: int
    configuration: ServerConfiguration
    store: object
    origin: ServerConfiguration
    installation_fence: tuple[str, str] | None


class ServerComposition:
    def __init__(self, config, runtime, verifier, catalog, clients, resources, *, probe_transport=None, agent_id=None):
        self.config, self.runtime, self.verifier, self.catalog = config, runtime, verifier, catalog
        if runtime is not None:
            runtime.agent_message_composition = self
        self.clients, self._resources, self.closed = clients, resources, False
        self._probe_transport = probe_transport
        self.assistant = None
        self.teams = None
        self._instruction_tasks = {}
        self._instruction_wakes = {}
        # Bindings whose original Hosted did not answer; the keepalive must not wake their session.
        self._instruction_lost = set()
        self._instruction_stopping = False
        self.agent_id = agent_id
        # behavior_agent_versions.HostedVersions in production; None keeps each target's configured version offline.
        self.hosted_versions = None
        # ADR-0009 phase 5: None keeps it off; build_server sets it and build_peer copies it, so every composition has it.
        self.upgrade_snapshot_container_url = None
        # A Team-registry peer's resolve_agent_scope-verified actor; never set for configured agents.
        self.registered_actor = None
        self._peer_factory = None
        self._peers = {}
        from threading import Lock, RLock
        self._peer_lock = RLock()
        self._agent_message_consumer_lock = Lock()
        self._peer_router = None
        self._verified_origin: _VerifiedOrigin | None = None

    def close(self):
        with self._peer_lock:
            if not self.closed:
                self.closed = True
                for task in self._instruction_tasks.values():task.cancel()
                for peer in self._peers.values():peer.close()
                self._resources.close()

    def for_agent(self, agent_id):
        """Server-only composition lookup; the selected runtime still verifies every principal and work."""
        from .behavior_agent_registry import resolve_agent_policy
        from .behavior_team_registry import resolve_agent_scope
        with self._peer_lock:
            if self.closed:
                raise _denied("server composition is closed")
            scope = resolve_agent_scope(self.runtime.store, self.config, agent_id)
            if scope.legacy_keys and self.agent_id is None or agent_id == self.agent_id:
                return self
            policy = resolve_agent_policy(self.runtime.store, self.config, agent_id)
            if self._peer_factory is None or scope.legacy_keys:
                raise Problem(503, "AGENT_RUNTIME_SCOPE_NOT_WIRED", "this composition cannot substitute another agent")
            peer = self._peers.get(agent_id)
            if peer is not None and peer.closed:
                del self._peers[agent_id]
                peer = None
            if peer is None:
                try:
                    peer = self._peer_factory(policy)
                except Exception as error:
                    import logging
                    logging.getLogger(__name__).error("AGENT_RUNTIME_UNAVAILABLE agent_id=%s exception_type=%s",
                                                       agent_id, type(error).__name__)
                    raise Problem(503, "AGENT_RUNTIME_UNAVAILABLE", "the selected peer runtime could not be initialized") from None
                owned = set(self.runtime.bindings)
                for previous in self._peers.values():
                    owned.update(previous.runtime.bindings)
                if owned.intersection(peer.runtime.bindings):
                    peer.close()
                    raise Problem(409, "AGENT_SCOPE_INVALID", "two agents cannot share a binding identity")
                self._peers[agent_id] = peer
            elif peer.runtime.work_policy.policy != policy:
                raise Problem(409, "AGENT_SCOPE_INVALID", "the registered peer policy changed")
            return peer

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def installation(self):
        from .behavior_migration import installed_origin

        if self.closed:
            raise _denied("server composition is closed")
        store = self.runtime.store
        try:
            if self.agent_id is None:
                original = installed_origin(store, self.config)
            else:
                owner = getattr(self, "_agent_message_owner", None)
                verified = getattr(owner, "_verified_origin", None)
                if (not isinstance(owner, ServerComposition) or owner.closed or owner.agent_id is not None
                        or not isinstance(verified, _VerifiedOrigin) or verified.process_id != getpid()
                        or verified.configuration is not self.config or owner.config is not self.config
                        or verified.store is not store or owner.runtime.store is not store
                        or verified.installation_fence is None
                        or verified.installation_fence != store._installation_fence):
                    raise Problem(503, "AGENT_ORIGIN_UNAVAILABLE",
                                  "the peer requires its live root's verified configuration and store")
                store._check_installation()
                original = verified.origin
            if store._verified_fresh_initialization != original.sha256:
                _authority_probe_readback(store, original)
            if self.assistant is not None:
                self.assistant.start()
            if self.teams is not None:
                self.teams.start()
            from .behavior_work_policy import attach_standing_policy
            attach_standing_policy(self, agent_id=self.agent_id)
            from .behavior_work_target import activate_target
            activate_target(self)
            if self.agent_id is None:
                self._verified_origin = _VerifiedOrigin(
                    getpid(), self.config, store, original, store._installation_fence)
        except BaseException:
            if self.agent_id is None:
                self._verified_origin = None
            raise

    def require_current_native_evidence(self):
        if self.closed:
            raise _denied("server composition is closed")
        self.catalog.require_activation()
        self.installation()

    def instruction_request(self,request):
        """Live identity/owner/ticket/CAS gates remain in the actual handler."""
        from .behavior_assistant import _read_conflicts
        binding_id=request.path_params.get("binding_id")
        control=self.runtime.work_policy
        cached=None if control is None else control.records.get(binding_id)
        # Route classification only: every handler still authorizes, refreshes or reads the registry itself.
        if cached is not None and cached.profile.instruction is not None and binding_id in self.runtime.bindings:
            return True
        def read():
            self.runtime.refresh_bindings(binding_id)
            control=self.runtime.work_policy
            record=None if control is None else control.records.get(binding_id)
            return record is not None and record.profile.instruction is not None
        return _read_conflicts(read, phase="instruction_route")

    def _instruction_target(self,binding_id):
        control=self.runtime.work_policy
        record=None if control is None else control.records.get(binding_id)
        task=None if record is None else record.profile.instruction
        if task is None or task.peer_work is None:
            return self,binding_id
        from .behavior_agent_message_api import _read_root
        owner=getattr(self,"_agent_message_owner",None)
        if owner is None:
            raise Problem(503,"AGENT_MESSAGE_UNAVAILABLE","the original peer controller is unavailable")
        root=control.peer_roots.get(task.peer_work.root_id)
        if root is None:root=_read_root(self.runtime.store,task.peer_work.root_id)
        return owner,root.origin.binding_id

    def launch_instruction(self,binding_id):
        import asyncio
        target,original=self._instruction_target(binding_id)
        if target is not self:return target.launch_instruction(original)
        if self.closed or self._instruction_stopping:raise _denied("server instruction observer is stopping")
        prior=self._instruction_tasks.get(binding_id)
        if prior is not None and not prior.done():return
        interval=self.runtime.dep.delivery.contract.review_interval_seconds
        wake=asyncio.Event()
        self._instruction_wakes[binding_id]=wake
        from .behavior_session_keepalive import InstructionSessionKeepalive
        keepalive=InstructionSessionKeepalive(self,binding_id)
        lost_codes={"WORKER_QUERY_UNKNOWN","WORKER_STOP_UNKNOWN"}
        def session_stopped():
            check=getattr(self.runtime.dep.workers.adapter,"stopped_session",None)
            record=self.runtime.work_policy.records.get(binding_id)
            if check is None or record is None:return None
            job=self.runtime.dep.workers.inspect(record.binding.job_id)
            return None if job.binding is None else check(job.binding)
        async def lost_wait():
            """The original Hosted did not answer: read only its session until it is proven stopped or woken.

            Reads back off from the review interval to LOST_WAIT_MAX_SECONDS, so a turn that never closes on its
            own (its session status unreadable, never stopped) costs a few reads an hour; a cancel still wakes it.
            """
            from starlette.concurrency import run_in_threadpool
            self._instruction_lost.add(binding_id)
            delay=interval
            while not self.closed and not self._instruction_stopping:
                if wake.is_set():return "woken"
                try:await asyncio.wait_for(wake.wait(),timeout=delay)
                except TimeoutError:pass
                if wake.is_set():return "woken"
                try:stopped=await run_in_threadpool(session_stopped)
                except Exception:stopped=None
                if stopped is not None:return "stopped"
                delay=min(delay*2,max(interval,LOST_WAIT_MAX_SECONDS))
            return None
        async def follow():
            from starlette.concurrency import run_in_threadpool
            import logging
            proven=False
            try:
                while not self.closed and not self._instruction_stopping:
                    wake.clear()
                    try:
                        await run_in_threadpool(keepalive.tick)
                        status=await run_in_threadpool(self.runtime.controller.step,binding_id)
                    except Problem as error:
                        if error.status==412 and error.body["error"]["code"]=="BEHAVIOR_CONFLICT":
                            logging.getLogger(__name__).warning("INSTRUCTION_OBSERVATION_CONFLICT original_work=true")
                            await asyncio.sleep(1)
                            continue
                        if error.body["error"]["code"] in lost_codes and not proven:
                            logging.getLogger(__name__).warning("INSTRUCTION_OBSERVATION_LOST_PENDING code=%s",
                                                                error.body["error"]["code"])
                            outcome=await lost_wait()
                            if outcome is None:return
                            proven=outcome=="stopped"
                            continue
                        raise
                    if status.state in {"succeeded","failed","cancelled"} and status.result_forwarded:return
                    if status.state == "capacity_uncertain" and not wake.is_set():
                        if binding_id in self._instruction_lost and not proven:
                            outcome=await lost_wait()
                            if outcome is None:return
                            proven=outcome=="stopped"
                            continue
                        logging.getLogger(__name__).warning("INSTRUCTION_OBSERVATION_PAUSED original_work=true state=capacity_uncertain")
                        return
                    try:await asyncio.wait_for(wake.wait(),timeout=interval)
                    except TimeoutError:pass
            except Problem as error:
                logging.getLogger(__name__).warning("INSTRUCTION_OBSERVATION_UNRESOLVED code=%s",error.body["error"]["code"])
            except Exception as error:
                logging.getLogger(__name__).error("INSTRUCTION_OBSERVATION_FAILED exception_type=%s",type(error).__name__)
                raise
        task=asyncio.create_task(follow())
        self._instruction_tasks[binding_id]=task
        def finished(done):
            if self._instruction_tasks.get(binding_id) is done:
                self._instruction_tasks.pop(binding_id,None)
                self._instruction_wakes.pop(binding_id,None)
                self._instruction_lost.discard(binding_id)
            if not done.cancelled():done.exception()
        task.add_done_callback(finished)

    def instruction_ready(self,binding_id):
        target,original=self._instruction_target(binding_id)
        target.launch_instruction(original)
        target._instruction_wakes[original].set()

    async def stop_instruction_observers(self):
        import asyncio
        self._instruction_stopping=True
        tasks=tuple(self._instruction_tasks.values())
        for task in tasks:task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
        for peer in self._peers.values():
            await peer.stop_instruction_observers()

    def resume_instructions(self):
        from .behavior_workers import worker_key,JobRecord
        from .behavior_operations import _parse
        control=self.runtime.work_policy
        if control is None:return
        control.refresh()
        if getattr(self,"_agent_message_owner",None) is None:
            from .behavior_agent_message_runtime import resume_peer_messages
            resume_peer_messages(self)
        for record in control.records.values():
            if record.profile.instruction is None:continue
            key=worker_key("job",record.binding.job_id)
            row=self.runtime.store.read_snapshot((key,))[key]
            if row is None:continue
            job=_parse(row.payload["data"],JobRecord)
            if job.state in {"succeeded","failed","cancelled"} and job.result_forwarded:
                from .behavior_agent_message_runtime import complete_origin
                complete_origin(self.runtime,record.binding)
                continue
            self.launch_instruction(record.binding.binding_id)


def _installation(config):
    from .behavior_migration import installation_record

    return installation_record(config)


def _credential(identity, resources, credential):
    if credential is None:
        credential = ManagedIdentityCredential(client_id=identity.managed_identity_client_id, retry_total=0)
        resources.callback(credential.close)
    return credential


REMOTE_CONSOLE_CLIENT_ENV = "FA_REMOTE_CONSOLE_CLIENT_ID"


def _verifier(identity, key_client, store=None, targets=()):
    from .behavior_team_registry import TeamRegistry
    teams = None if store is None else TeamRegistry(
        store, tenant_id=identity.tenant_id, operator_oid=identity.human_oid,
        legacy_team_id=identity.team_id, legacy_owner_oid=identity.human_oid,
        agent_oids=tuple(agent.oid for agent in identity.agents), targets=tuple(targets),
    )
    # Unset or empty keeps the single configured client; a malformed value stops startup instead of failing every login.
    return BehaviorJwtVerifier(
        identity.auth(), BehaviorAuthSettings(identity.human_oid, identity.admin_client_id, identity.team_id,
                                              environ.get(REMOTE_CONSOLE_CLIENT_ENV) or None), key_client,
        teams=teams,
    )


def build_server(
    config: ServerConfiguration, *, credential=None, table_transport=None,
    source_transport=None, backup_transport=None, native_transports=None, memory_transports=None,
    probe_transport=None, assistant_transport=None, key_client=None, clock=utc_now, hosted_versions=None,
):
    """No I/O or row creation. Injected transports/credentials are offline test seams.

    Created SDK clients are always owned/closed here; an injected credential is
    caller-owned. Production's default is ManagedIdentityCredential ONLY.
    """
    from .behavior_server_config import ServerConfigurationDocument, ServerConfigurationV2
    config = ServerConfigurationDocument.model_validate_json(config.model_dump_json())
    from .upgrade_snapshot_store import snapshot_container_url
    upgrade_snapshot_container_url = snapshot_container_url(config)  # before any client: a bad value stops startup
    resources = ExitStack()
    try:
        credential = _credential(config.identity, resources, credential)
        options = {} if table_transport is None else {"transport": table_transport}
        table = TableClient(config.table.endpoint, config.table.scope.table_name, credential=credential, **SDK_OPTIONS, **options)
        resources.callback(table.close)
        from .behavior_migration import MigrationStore, installation_record

        store = MigrationStore(table, config.table.scope,
                               expected_installation=(installation_record(config)[0], config.sha256))
        if store._binding != config.workers.control_binding:
            raise ValueError("configured control_binding does not match the exact Table SDK endpoint")
        options = {} if source_transport is None else {"transport": source_transport}
        source = ContainerClient.from_container_url(
            config.sources.protection.container_url, credential=credential, **SDK_OPTIONS, **options,
            max_single_put_size=MAX_ARTIFACT_BYTES, max_single_get_size=MAX_ARTIFACT_BYTES,
        )
        resources.callback(source.close)
        if source.url != config.sources.protection.container_url:
            raise ValueError("source SDK container endpoint differs")
        backup = make_blob_backup_store(SourceBackupSettings(
            tenant_id=config.identity.tenant_id, authority_domain=config.table.scope.authority_domain,
            container_url=config.backups.protection.container_url,
        ), credential, transport=backup_transport)
        resources.callback(backup.container.close)
        catalog = NativeEvidenceCatalog(config, clock=clock)
        source_verifier = InventoryProtectionVerifier(lambda: catalog.protection("sources"), clock=clock)
        backup_verifier = InventoryProtectionVerifier(lambda: catalog.protection("backups"), clock=clock)
        sources = SourceMaterials(
            store, blob_store=BlobArtifactStore(source, config.identity.tenant_id),
            protection=config.sources.protection, protection_verifier=source_verifier,
        )
        backups = SourceBackupService(
            backup, SourceBackupSettings(
                tenant_id=config.identity.tenant_id, authority_domain=config.table.scope.authority_domain,
                container_url=config.backups.protection.container_url,
            ), sources=sources, protection=config.backups.protection, protection_verifier=backup_verifier, clock=clock,
        )
        evidence = RuntimeEvidenceAuthority(store, clock=clock, authentication_version=config.authentication_version)
        inbox = PullInbox(store, InboxRegistration(config.authentication_version, evidence.verify_access, catalog.inbox))
        workers = WorkerCore(store, config.workers, adapter=None, clock=clock, mode="native")
        delivery = DeliveryCore(store, clock=clock, registration=RuntimeRegistration(
            config.delivery, "native", catalog.delivery, evidence.verify_ingress, workers.verify_work_result,
            inbox.verify_receipt, inbox,
        ))
        inbox.attach(delivery)
        native = NativeLaunchAuthority(
            workers, team_id=config.identity.team_id, controller_id=config.controller_id, targets=config.active_targets,
            jobs=tuple(NativeJobRoute(job_id=job.job_id, api_binding_id=binding.binding_id,
                                     grant_id=native_key("bootstrap-grant", job.job_id))
                       for binding, job in config.registered_jobs),
            verifier_version=config.delivery.verifier_version, clock=clock,
            recovery_binding=NativeRecoveryBinding(
                configuration_sha256=config.sha256, operator_oid=config.identity.human_oid,
                credential_client_id=config.identity.managed_identity_client_id,
            ) if config.assistant is not None and config.identity.managed_identity_client_id is not None else None,
        )
        if native_transports is not None and set(native_transports) not in (
            {t.executor_id for t in config.targets}, {t.executor_id for t in config.active_targets},
        ):
            raise ValueError("offline transports must cover the exact native target set")
        native_clients = []
        for target in config.active_targets:
            client = FoundryHttpClient(target, credential, transport=None if native_transports is None else native_transports[target.executor_id])
            resources.callback(client.close)
            native_clients.append(client)
        executor = FoundryExecutor(native, tuple(native_clients))
        workers.adapter = executor.adapter(verify_readiness=catalog.worker)
        reviewer = DurableHumanReviewVerifier(sources, HumanReviewPolicy(policy_version=config.review_policy_version), clock=clock)
        tokenizer = ServerTokenizer(config.tokenizer)
        memory = MemoryGovernance(
            sources, clock=clock, token_counter=tokenizer.count, review_verifier=reviewer,
            max_context_tokens=config.max_context_tokens, max_context_bytes=config.max_context_bytes,
        )
        if memory_transports is not None and set(memory_transports) != {s.binding_id for s in config.memory_projections}:
            raise ValueError("offline memory transports must cover the exact configured projection bindings")
        projections, project_clients = {}, []
        for setup in config.memory_projections:
            options = {} if memory_transports is None else {"transport": memory_transports[setup.binding_id]}
            project = AIProjectClient(
                endpoint=setup.endpoint, credential=credential, api_version="v1",
                credential_scopes=["https://ai.azure.com/.default"], retry_total=0, retry_connect=0,
                retry_read=0, retry_status=0, redirect_max=0, logging_enable=False,
                connection_timeout=10, read_timeout=20, **options,
            )
            resources.callback(project.close)
            project_clients.append(project)
            projections[setup.binding_id] = NativeMemoryProjection(memory, project, setup)
        aliases = _OperationCatalog(config)
        effect = WorkStateEffectAdapter(delivery, allowed_work_ids=tuple(b.work.work_id for b in config.bindings), references=aliases)
        broker = ToolBrokerCore(store, clock=clock, adapter=effect.registration, execution_adapter=effect)
        dependencies = RuntimeDependencies(
            store, sources, memory, broker, delivery, inbox, workers, evidence, reviewer, native, backups,
            memory_projections=None if not projections else MappingProxyType(projections),
        )
        if config.assistant is None:
            runtime = BehaviorRuntime(dependencies, config.bindings, clock=clock, mode="native", controller_id=config.controller_id)
        else:
            from .behavior_assistant import AssistantRuntime

            runtime = AssistantRuntime(
                dependencies, config.bindings, profiles=config.assistant.profiles,
                clock=clock, mode="native", controller_id=config.controller_id,
            )
        catalog.runtime, aliases.runtime, aliases.adapter = runtime, runtime, effect
        composition = ServerComposition(config, runtime, _verifier(config.identity, key_client, store, config.targets), catalog,
                                        (table, source, backup.container, *native_clients, *project_clients), resources,
                                        probe_transport=probe_transport)
        if hosted_versions is None and table_transport is None and native_transports is None:
            from .behavior_agent_onboarding import ProjectAgentFoundry
            from .behavior_agent_versions import HostedVersions
            hosted_versions = HostedVersions(lambda endpoint: ProjectAgentFoundry(endpoint, credential))
        composition.hosted_versions = hosted_versions
        composition.upgrade_snapshot_container_url = upgrade_snapshot_container_url
        if config.assistant is not None:
            from .behavior_assistant_server import AssistantServer

            composition.assistant = AssistantServer(
                composition, table, source, credential, transport=assistant_transport,
            )
            from .behavior_capabilities import load_runtime_capabilities
            runtime.capability_grants, runtime.capability_catalog = load_runtime_capabilities()
            from .behavior_peer_runtime import build_peer
            composition._peer_factory = lambda policy: build_peer(
                composition, policy, credential=credential, native_transports=native_transports,
                assistant_transport=assistant_transport)
        if type(config) is ServerConfigurationV2:
            from .teams_metrics import TeamsMetrics
            composition.teams = TeamsMetrics(composition)
        return composition
    except BaseException:
        resources.close()
        raise


def _lifespan(app, composition, validate):
    @asynccontextmanager
    async def lifespan(_app):
        clock = getattr(_app.state, "native_routine_clock", None)
        # The upgrade reconciler (ADR-0009 phase 6a) starts only behind its flags; the daily patrol is off from 2d-ii.
        patrols = [task for task in (getattr(_app.state, "native_upgrade_reconciler", None),) if task is not None]
        try:
            validate()
            if getattr(_app.state,"instruction_runner",False):composition.resume_instructions()
            if composition._peer_router is not None:
                await composition._peer_router.prepare()
            if clock is not None:
                clock.start()
            for patrol in patrols:
                patrol.start()
            yield
        finally:
            for patrol in patrols:
                await patrol.stop()
            if clock is not None:
                await clock.stop()
            if getattr(_app.state,"instruction_runner",False):await composition.stop_instruction_observers()
            # ADR-0009 2d-i background cuts: a running files fence is withdrawn before the clients close.
            from .native_cut import close_cut_drivers
            await asyncio.to_thread(close_cut_drivers, composition)
            if composition.assistant is not None:
                await composition.assistant.provider.aclose()
            for peer in composition._peers.values():
                if peer.assistant is not None:
                    await peer.assistant.provider.aclose()
            composition.close()
    app.router.lifespan_context = lifespan
    app.state.composition = composition
    return app


def _health(app, *, preactivation):
    @app.get("/healthz")
    def healthz():
        return _response({
            "status": "alive", "admissions": "CLOSED" if preactivation else "EVIDENCE_GATED",
            "operational_readiness": "NOT_EVALUATED", "actual_environment": "NOT_RUN",
        })


def application(composition: ServerComposition, *, preactivation=False):
    try:
        if not preactivation:
            composition.installation()
            if composition.runtime.work_policy is None:
                composition.catalog.require_activation()
            composition.runtime.validate_startup()
            from .native_binding import idle_upgrade_enabled
            idle_upgrade_enabled()  # refuses FA_NATIVE_IDLE_UPGRADE=1 without the 2d-i switch
            identity = _principal_dependency(composition.verifier)

            def current_native_evidence(request: Request, _principal=Depends(identity)):
                if composition.instruction_request(request):
                    return
                if (composition.runtime.work_policy is not None
                        and composition.runtime.work_policy.result_route(request)):
                    composition.installation()
                elif composition.assistant is not None and composition.assistant.bounded_route(composition, request):
                    composition.assistant.acquire_evidence(composition)
                else:
                    composition.require_current_native_evidence()

            app = FastAPI(title="FA bounded native behavior API", docs_url=None, redoc_url=None, openapi_url=None)
            from .behavior_timing import RouteTiming
            app.add_middleware(RouteTiming)
            _install_error_handlers(app)
            app.include_router(behavior_router(composition.runtime, composition.verifier,
                                               registered_actor=composition.registered_actor),
                               dependencies=[Depends(current_native_evidence)])
            if composition.assistant is not None:
                from .behavior_assistant_api import install_assistant_routes

                def current_assistant_evidence(request: Request, _principal=Depends(identity)):
                    if composition.instruction_request(request):
                        return
                    if composition.assistant.bounded_route(composition, request):
                        composition.assistant.acquire_evidence(composition)
                    else:
                        composition.require_current_native_evidence()

                routes = APIRouter(dependencies=[Depends(current_assistant_evidence)])
                install_assistant_routes(
                    routes, composition.assistant.authority, composition.verifier,
                    provider=composition.assistant.provider,
                    on_result_ready=composition.instruction_ready,
                )
                app.include_router(routes)
                from .team_data_relay import TeamDataRelay, install_team_data_routes
                artifacts = composition.assistant.artifacts
                app.state.team_data = TeamDataRelay(
                    artifacts.repository, artifacts.store, composition.config.identity.tenant_id)
                install_team_data_routes(app, app.state.team_data, composition.verifier)
            if composition.teams is not None:
                composition.teams.install_routes(app, composition.verifier)
            if composition.runtime.work_policy is not None:
                from .behavior_work_policy_api import install_work_policy_routes
                from .memory_note_proposal_api import install_note_proposal_routes
                from .memory_bridge_settings_api import install_memory_bridge_settings_routes
                from .memory_candidate_api import install_memory_candidate_routes
                install_work_policy_routes(app,composition,start_work=composition.launch_instruction,
                                           wake_work=composition.instruction_ready)
                install_note_proposal_routes(app, composition.runtime, composition.verifier)
                install_memory_bridge_settings_routes(app, composition.runtime, composition.verifier)
                install_memory_candidate_routes(app, composition.runtime, composition.verifier)
                if composition.assistant is not None:
                    from .assistant_image_api import install_image_routes
                    install_image_routes(app, composition.assistant.authority, composition.verifier)
            _health(app, preactivation=False)
            from .native_hitl_api import install_native_hitl_routes
            install_native_hitl_routes(app, composition)
            from .team_skills_api import install_team_skills_routes
            install_team_skills_routes(app, composition)
            from .team_mcp_api import install_team_mcp_routes
            install_team_mcp_routes(app, composition)
            from .team_connector_api import install_team_connector_routes
            install_team_connector_routes(app, composition)
            if composition.agent_id is None:
                from .native_teams_relay import install_teams_relay_routes
                install_teams_relay_routes(app, composition)
            from .native_group_api import install_native_group_routes
            install_native_group_routes(app, composition)
            from .behavior_desktop_view_api import install_desktop_view_routes
            install_desktop_view_routes(app, composition)
            if composition.agent_id is None and composition.runtime.work_policy is not None:
                from .behavior_peer_runtime import install_peer_routing
                install_peer_routing(app, composition, application)
                # ADR-0009 M1 step 3 (b): the daily patrol is off from 2d-ii (no v1 row is left to find); the
                # operator one-shot ``agent-versions patrol --once`` stays.
                from .native_upgrade_reconciler import NativeUpgradeReconciler
                app.state.native_upgrade_reconciler = NativeUpgradeReconciler(composition)
            return _lifespan(app, composition, lambda: (
                composition.installation(),
                composition.catalog.require_activation() if composition.runtime.work_policy is None else None,
                composition.runtime.validate_startup(),
            ))
        app = FastAPI(title="FA operator preactivation (admissions CLOSED)", docs_url=None, redoc_url=None, openapi_url=None)
        _install_error_handlers(app)
        _health(app, preactivation=True)
        callbacks = behavior_router(composition.runtime, composition.verifier)
        callbacks.routes[:] = [route for route in callbacks.routes if "/hosted/launches/" in route.path]
        app.include_router(callbacks)
        identity = _principal_dependency(composition.verifier)

        @app.get("/behavior/v1/operator/prerequisites")
        @app.get("/behavior/v1/operator/config-check")
        def prerequisites(principal=Depends(identity)):
            principal.require(composition.config.identity.team_id, "read")
            if principal.kind != "human" or principal.oid != composition.config.identity.human_oid:
                raise Problem(403, "BEHAVIOR_ACCESS_DENIED", "only the configured operator may inspect prerequisites")
            return _response({
                "admissions": "CLOSED", "configuration_sha256": composition.config.sha256,
                "control_binding": composition.runtime.store._binding,
                "worker_configuration_digest": composition.runtime.dep.workers.configuration_digest(),
                "delivery_configuration_digest": composition.runtime.dep.delivery.configuration_digest(),
                "prerequisite_verification": "OPERATOR_EVIDENCE_REQUIRED", "actual_environment": "NOT_RUN",
            })
        return _lifespan(app, composition, lambda: None)
    except BaseException:
        composition.close()
        raise


def create_production_app():
    return application(build_server(load_configuration()))


def create_probe_app():
    return application(build_server(load_configuration()), preactivation=True)


def _validate_approval(composition, approval):
    config, now = composition.config, composition.runtime.clock()
    approval = BootstrapAuthorization.model_validate_json(approval.model_dump_json())
    if approval.authorizing_human_oid != config.identity.human_oid or approval.configuration_sha256 != config.sha256:
        raise Problem(403, "BEHAVIOR_BOOTSTRAP_DENIED", "operator approval is not for this exact configuration")
    _fresh(approval.not_before, approval.expires_at, now, 86400)
    grants = {g.job_id: g for g in approval.native_grants}
    if len(grants) != len(approval.native_grants) or set(grants) != {job.job_id for _, job in config.registered_jobs}:
        raise Problem(403, "BEHAVIOR_BOOTSTRAP_DENIED", "every exact job needs separate launch authorization")
    policies = {p.binding_id: p.policy for p in config.policies}
    for binding, job in config.registered_jobs:
        grant = grants[job.job_id]
        body = (composition.runtime.job_input(binding) if job.job_id == binding.job_id
                else executor_job_input(job.job_id, job.work))
        if (grant.grant_id != native_key("bootstrap-grant", job.job_id)
                or grant.controller_id != config.controller_id or grant.owner != binding.owner
                or grant.policy_revision != policies[binding.binding_id].operation_policy_revision
                or grant.definition != job.work or grant.input_digest != hashlib.sha256(body).hexdigest()
                or grant.workload_version != config.workload_version
                or config.workload_version == "assistant-v1.2" and grant.checkpoint_after_bytes is not None
                or grant.targets != config.active_targets or not grant.active
                or not 1 <= grant.remaining_uses <= config.workers.max_generations
                or now >= policies[binding.binding_id].valid_until
                or grant.not_before < approval.not_before or grant.expires_at > approval.expires_at
                or not grant.not_before <= now < grant.expires_at
                or grant.checkpoint_after_bytes is not None and grant.checkpoint_after_bytes > len(body)):
            raise Problem(403, "BEHAVIOR_BOOTSTRAP_DENIED", "native grant exceeds the exact workload/input/agent authorization")
        if binding.child_jobs and job.job_id == binding.job_id and (
            grant.checkpoint_after_bytes is None or grant.checkpoint_after_bytes >= len(body)
            or grant.remaining_uses < 2
        ):
            raise Problem(403, "BEHAVIOR_BOOTSTRAP_DENIED", "registered-child parent requires a partial checkpoint and a resume grant")
        if job.job_id != binding.job_id and grant.checkpoint_after_bytes is not None:
            raise Problem(403, "BEHAVIOR_BOOTSTRAP_DENIED", "registered leaf child cannot park without its own dependency contract")
    return grants


def _probe_layout(config):
    keys = {kind: control_key("server-authority-probe", config.sha256, kind)
            for kind in ("intent", "budget", "winner", "loser", "proof")}
    winner = ControlRequest(
        request_id=control_key("server-authority-probe-winner", config.sha256),
        request_digest=digest(["authority-probe-winner", config.sha256]), read_set=(keys["budget"],),
    )
    loser = ControlRequest(
        request_id=control_key("server-authority-probe-loser", config.sha256),
        request_digest=digest(["authority-probe-loser", config.sha256]), read_set=(keys["budget"],),
    )
    return keys, winner, loser


def _authority_probe_readback(store, config, *, require_proof=True):
    keys, winner, loser = _probe_layout(config)
    rows = store.read_snapshot(tuple(keys.values()))
    winner_receipt = store._receipt(winner)
    if (winner_receipt is None or store._receipt(loser) is not None or rows[keys["loser"]] is not None
            or rows[keys["budget"]] is None or rows[keys["winner"]] is None
            or rows[keys["budget"]].payload["data"] != {"remaining": 0, "configuration_sha256": config.sha256}
            or rows[keys["winner"]].payload["data"] != {"winner": True, "control_binding": store._binding}):
        raise Problem(503, "BEHAVIOR_AUTHORITY_PROBE_REQUIRED", "this application authority lacks a verified atomic probe")
    actual = {
        key: hashlib.sha256(json.dumps(rows[key].payload, ensure_ascii=False, allow_nan=False,
                                      separators=(",", ":")).encode()).hexdigest()
        for key in (keys["budget"], keys["winner"])
    }
    if dict(winner_receipt.payload["writes"]) != actual:
        raise Problem(503, "BEHAVIOR_AUTHORITY_PROBE_REQUIRED", "winner business rows differ from the committed receipt fingerprints")
    expected = {
        "configuration_sha256": config.sha256, "control_binding": store._binding, "stale_batch_status": 412,
        "winner_request_id": winner.request_id, "loser_request_id": loser.request_id,
        "winner_receipt_sha256": digest(winner_receipt.payload), "business_payload_sha256": actual,
    }
    if require_proof and (rows[keys["proof"]] is None or rows[keys["proof"]].payload["data"] != expected):
        raise Problem(503, "BEHAVIOR_AUTHORITY_PROBE_REQUIRED", "own-authority stale-412 observation is missing")
    return expected


def _probe_authority(composition):
    """One consented probe, two SDK clients, same actual app authority.

    Only probe-prefixed records and the existing EGT guard are touched. Failure
    or lost acknowledgement consumes the attempt; no automatic probe restart.
    The winning receipt/business rows are read back through the second client.
    This is installation capability evidence, not a full native scenario PASS.
    """
    config, store = composition.config, composition.runtime.store
    keys, winner, loser = _probe_layout(config)
    if store.read_snapshot((keys["proof"],))[keys["proof"]] is not None:
        return _authority_probe_readback(store, config)
    fresh = False
    request = ControlRequest(
        request_id=control_key("server-authority-probe-reserve", config.sha256),
        request_digest=digest(["authority-probe-reserve", config.sha256]),
    )

    def reserve(_rows):
        nonlocal fresh
        fresh = True
        return ControlChanges(writes=(
            ControlWrite(row_id=keys["intent"], mode="create",
                         data={"configuration_sha256": config.sha256, "attempt_consumed": True}),
            ControlWrite(row_id=keys["budget"], mode="create",
                         data={"remaining": 1, "configuration_sha256": config.sha256}),
        ), result={"attempt_consumed": True})

    store.transact(request, reserve)
    if not fresh:
        raise Problem(503, "BEHAVIOR_AUTHORITY_PROBE_UNKNOWN", "original own-authority probe needs operator reconciliation; never restart")
    options = {} if composition._probe_transport is None else {"transport": composition._probe_transport}
    with TableClient(config.table.endpoint, config.table.scope.table_name,
                     credential=composition.clients[0].credential, **SDK_OPTIONS, **options) as client:
        observer = BehaviorStore(client, config.table.scope)
        if observer._binding != store._binding:
            raise Problem(503, "BEHAVIOR_AUTHORITY_PROBE_REQUIRED", "independent SDK client authority differs")

        def compete(rows):
            if rows[keys["budget"]].payload["data"]["remaining"] != 1:
                raise Problem(409, "BEHAVIOR_AUTHORITY_PROBE_UNKNOWN", "probe reservation already consumed")
            observer.transact(winner, lambda _rows: ControlChanges(writes=(
                ControlWrite(row_id=keys["budget"], mode="replace",
                             data={"remaining": 0, "configuration_sha256": config.sha256}),
                ControlWrite(row_id=keys["winner"], mode="create",
                             data={"winner": True, "control_binding": store._binding}),
            ), result={"winner": True}))
            return ControlChanges(writes=(
                ControlWrite(row_id=keys["loser"], mode="create", data={"unexpected": True}),
            ), result={"unexpected": True})

        try:
            store.transact(loser, compete)
        except Problem as error:
            if error.status != 412 or error.body["error"]["code"] != "BEHAVIOR_CONFLICT":
                raise
        else:
            raise Problem(503, "BEHAVIOR_AUTHORITY_PROBE_REQUIRED", "stale batch was not rejected with 412")
        proof = _authority_probe_readback(observer, config, require_proof=False)
        observer.transact(
            ControlRequest(request_id=control_key("server-authority-probe-proof", config.sha256),
                           request_digest=digest(proof)),
            lambda _: ControlChanges(writes=(ControlWrite(row_id=keys["proof"], mode="create", data=proof),),
                                     result={"own_authority_probe": "VERIFIED"}),
        )
        return _authority_probe_readback(observer, config)


def bootstrap_records(config, grants=None):
    """Initial configured counters/owners; absent grants never authorize legacy launches."""
    policies = {p.binding_id: p.policy for p in config.policies}
    rows = []
    configuration_digest = digest({"contract": config.delivery.model_dump(mode="json"), "mode": "native"})
    for binding in config.bindings:
        rows.extend((
            (control_key("owner", binding.owner.owner_record_id),
             OwnerRecord(**binding.owner.model_dump(), active=True, operation_ids=(binding.operation_id,))),
            (control_key("policy", binding.owner.policy_id),
             PolicyRecord(policy_id=binding.owner.policy_id, revision=policies[binding.binding_id].operation_policy_revision, active=True)),
        ))
        if grants is not None:
            rows.extend((native_key("grant", grants[job.job_id].grant_id), grants[job.job_id])
                        for job in binding.registered_jobs)
    for root in config.roots:
        rows.extend((
            (control_key("budget", root.root_budget_id),
             BudgetRecord(root_budget_id=root.root_budget_id, active=True, remaining_attempts=root.attempts, remaining_units=root.units)),
            (control_key("delivery-index", root.root_budget_id),
             PendingIndexRecord(configuration_digest=configuration_digest, evidence_mode="native", root_budget_id=root.root_budget_id)),
            (control_key("delivery-quota", "root", root.root_budget_id), QuotaRecord(
                configuration_digest=configuration_digest, evidence_mode="native", scope_kind="root", scope_id=root.root_budget_id,
                root_request_id=root.root_request_id, pending_limit=root.pending_limit,
                available_control_units=root.control_units, scans_remaining=root.scans,
            )),
        ))
    for kind, identifier, limit in (("team", config.identity.team_id, config.team_pending_limit),
                                     ("entry", config.delivery.entry_id, config.entry_pending_limit)):
        rows.append((control_key("delivery-quota", kind, identifier), QuotaRecord(
            configuration_digest=configuration_digest, evidence_mode="native",
            scope_kind=kind, scope_id=identifier, pending_limit=limit,
        )))
    return rows


def bootstrap(composition, approval: BootstrapAuthorization, *, stage="records"):
    """Explicit operator-only CLI operation. No human operation grants are minted."""
    if composition.config.schema_version == "fa-behavior-server-v2":
        raise Problem(403, "CHANNELS_BOOTSTRAP_DISABLED", "v2 只能走原 authority 的明確遷移，不可 bootstrap root/pool")
    if stage not in {"records", "worker", "activate"}:
        raise ValueError("unknown bootstrap stage")
    grants = _validate_approval(composition, approval)
    runtime, config = composition.runtime, composition.config
    if stage != "records":
        composition.catalog.worker()
        if stage == "activate":
            composition.catalog.require_activation()
    from .behavior_migration import MigrationStore

    installer = MigrationStore(composition.clients[0], config.table.scope)
    installer.initialize()
    installation_key, installation = _installation(config)
    from .behavior_migration import migration_key

    migration_pointer = migration_key(config.table.scope.authority_domain, "current")
    rotation_pointer = control_key("runtime-rotation-current", config.table.scope.authority_domain)
    from .behavior_fresh_installation import fresh_key
    fresh_pointer = fresh_key(config.table.scope.authority_domain)
    installation_rows = installer.read_snapshot((installation_key, migration_pointer, rotation_pointer, fresh_pointer))
    if any(installation_rows[key] is not None for key in (migration_pointer, rotation_pointer, fresh_pointer)):
        raise Problem(409, "MIGRATION_BOOTSTRAP_DISABLED", "migrated authorities never re-bootstrap; publish fresh readiness explicitly")
    installed = installation_rows[installation_key]
    if installed is not None and installed.payload["data"] != installation:
        raise Problem(409, "MIGRATION_REQUIRED", "ordinary bootstrap cannot replace an installed configuration")
    installer.transact(
        ControlRequest(request_id=control_key("server-installation-init", config.table.scope.authority_domain),
                       request_digest=digest(installation)),
        lambda _: ControlChanges(writes=(ControlWrite(row_id=installation_key, mode="create", data=installation),),
                                 result={"deployment_id": config.deployment_id}),
    )
    runtime.store.bind_installation(installation_key, config.sha256, protected_rows=(migration_pointer,))
    policies = {p.binding_id: p.policy for p in config.policies}
    rows = bootstrap_records(config, grants)
    decision_key = control_key("server-bootstrap-authorization", approval.decision_id)
    decision = {
        **approval.model_dump(mode="json", exclude={"native_grants"}),
        "native_grant_digests": [digest(grant.model_dump(mode="json")) for grant in approval.native_grants],
    }
    runtime.store.transact(
        ControlRequest(request_id=control_key("server-bootstrap-decision", approval.decision_id),
                       request_digest=digest(decision)),
        lambda _: ControlChanges(writes=(ControlWrite(row_id=decision_key, mode="create", data=decision),),
                                 result={"decision_id": approval.decision_id}),
    )
    # Stable immutable receipts retain original seeds. Replays do not compare or
    # rewrite mutable counters, owners, quotas, revocations or generation rows.
    for key, value in rows:
        request = ControlRequest(
            request_id=control_key("server-bootstrap", key),
            request_digest=digest([config.sha256, approval.decision_id, value.model_dump(mode="json")]),
        )
        runtime.store.transact(request, lambda _, k=key, v=value: ControlChanges(
            writes=(ControlWrite(row_id=k, mode="create", data=v.model_dump(mode="json")),),
            result={"created_application_record": k},
        ))
    runtime.bootstrap(policies)
    _probe_authority(composition)
    if stage != "records":
        runtime.dep.sources.initialize_protection()
        runtime.dep.source_backups.initialize_protection()
        runtime.dep.workers.publish_readiness(composition.catalog.worker().evidence_id)
        runtime.dep.workers.initialize()
        if composition.assistant is not None:
            composition.installation()
    if stage == "activate":
        runtime.dep.inbox.publish_readiness(composition.catalog.inbox().evidence_id)
        runtime.validate_startup()
    return {"stage": stage, "configuration_sha256": config.sha256,
            "own_authority_probe": "VERIFIED",
            "prerequisite_verification": "RECORDED" if stage == "activate" else "INCOMPLETE",
            "actual_environment": "NOT_RUN", "full_scenario_pass": False}


class CurrentRecoveryCatalog:
    def __init__(self, config, *, clock=utc_now):
        self.config, self.clock = config, clock

    def load(self):
        config, settings = self.config, self.config.catalog
        try:
            value = read_document(settings.path, RecoveryCatalog)
        except (OSError, ValueError):
            raise Problem(503, "SOURCE_BACKUP_RECOVERY_UNKNOWN", "current independent recovery catalog is unavailable") from None
        if (value.catalog_id != settings.catalog_id or value.observer_oid != settings.observer_oid
                or value.revision < settings.minimum_revision or value.configuration_sha256 != config.sha256
                or not value.active or value.decision.principal_oid != config.identity.human_oid
                or value.decision.reference_sha256 != value.reference.sha256
                or value.decision.scope != value.reference.description.scope
                or value.reference.description.owner_oid != config.identity.human_oid
                or value.reference.description.storage != SourceBackupSettings(
                    tenant_id=config.identity.tenant_id, authority_domain=config.authority_domain,
                    container_url=config.backup_container_url)
                or value.decision.scope.tenant_id != config.identity.tenant_id
                or value.decision.scope.team_id != config.identity.team_id):
            raise Problem(403, "SOURCE_BACKUP_RECOVERY_DENIED", "current exact recovery decision/catalog differs")
        _fresh(value.observed_at, value.expires_at, self.clock(), settings.max_age_seconds)
        _fresh(value.observed_at, value.decision.expires_at, self.clock(), settings.max_age_seconds)
        return value

    def authorize(self, principal, reference):
        principal.require(self.config.identity.team_id, "restore")
        catalog = self.load()
        if (principal.kind != "human" or principal.oid != catalog.decision.principal_oid
                or principal.tenant_id != self.config.identity.tenant_id or reference != catalog.reference):
            raise Problem(403, "SOURCE_BACKUP_RECOVERY_DENIED", "backup is not the CURRENT independently authorized reference")
        return catalog.decision


def recovery_application(config: RecoveryConfiguration, *, credential=None, backup_transport=None, key_client=None, clock=utc_now):
    config = RecoveryConfiguration.model_validate_json(config.model_dump_json())
    resources = ExitStack()
    try:
        catalog = CurrentRecoveryCatalog(config, clock=clock)
        catalog.load()
        credential = _credential(config.identity, resources, credential)
        settings = SourceBackupSettings(tenant_id=config.identity.tenant_id, authority_domain=config.authority_domain,
                                        container_url=config.backup_container_url)
        backup = make_blob_backup_store(settings, credential, transport=backup_transport)
        resources.callback(backup.container.close)
        service = SourceBackupService(backup, settings, sources=None, recovery_authorizer=catalog, clock=clock)
        app = create_source_recovery_app(service, _verifier(config.identity, key_client))
        composition = ServerComposition(config, None, None, catalog, (backup.container,), resources)
        app.state.recovery_service = service
        return _lifespan(app, composition, catalog.load)
    except BaseException:
        resources.close()
        raise


def create_recovery_app():
    return recovery_application(load_configuration(recovery=True))


def run_controller(composition, *, limits=None, preactivation=False, stop_requested=lambda: False,
                   monotonic=time.monotonic, sleep=time.sleep):
    """Foreground bounded ticks. No threads, models, HTTP-selected code or blind resend.

    RuntimeController.drain uses the existing WorkerConsumer query/stop-before-
    start and completion generation lanes. Delivery pages reserve durable scans.
    An exact native assistant C1 pull binding may defer only a freshly verified
    nonready new delivery page; query/stop order and all loop bounds are unchanged.
    The wall limit is checked between ticks; an in-flight bounded SDK operation
    is not killed/retried. A service supervisor owns termination/restart.
    """
    try:
        limits = composition.config.supervisor if limits is None else SupervisorLimits.model_validate_json(limits.model_dump_json())
        runtime = composition.runtime
        if runtime.controller is None or runtime.dep.native_launches is None or runtime.mode != "native":
            raise Problem(403, "BEHAVIOR_CONTROLLER_REQUIRED", "native internal controller is required")
        began, ticks, worker_items, delivery_items = monotonic(), 0, 0, 0
        problems = {}

        def count_problems(results):
            for item in results:
                code = item.get("problem")
                if code is None:
                    continue
                if not isinstance(code, str) or re.fullmatch(r"[A-Z0-9_]{1,96}", code) is None:
                    code = "BEHAVIOR_DEPENDENCY_UNAVAILABLE"
                if code not in problems and len(problems) >= 32:
                    code = "OTHER_PROBLEMS"
                problems[code] = problems.get(code, 0) + 1

        composition.installation()
        if preactivation:
            composition.catalog.worker()
            runtime.dep.workers._ready()
            runtime.dep.workers.pending()
        else:
            composition.catalog.require_activation()
            runtime.validate_startup()
        for tick in range(limits.ticks):
            if stop_requested() or monotonic() - began >= limits.max_run_seconds:
                break
            composition.catalog.worker() if preactivation else composition.catalog.require_activation()
            result = runtime.controller.drain(limit=limits.page_limit)
            worker_items += len(result)
            count_problems(result)
            if not preactivation:
                for root in composition.config.roots:
                    if stop_requested() or monotonic() - began >= limits.max_run_seconds:
                        break
                    index_key = control_key("delivery-index", root.root_budget_id)
                    rows = runtime.store.read_snapshot((index_key, pending_head_key(root.root_budget_id)))
                    if not pending_counts(runtime.dep.delivery, rows, root.root_budget_id)[0]:
                        continue
                    pending_binding = None
                    if (composition.config.workload_version == "assistant-v1.2"
                            and composition.assistant is not None and composition.config.workers.capacity == 1
                            and getattr(composition, "teams", None) is None and len(runtime.bindings) == 1):
                        candidate = next(iter(runtime.bindings.values()))
                        if (candidate.binding_id in runtime.profiles and candidate.route.channel_id == "authenticated-pull"
                                and candidate.owner.root_budget_id == root.root_budget_id):
                            pending_binding = candidate
                    page = DeliveryConsumer(runtime.dep.delivery).drain(
                        root.root_budget_id, runtime.resolve_owner, composition.config.controller_id, limit=limits.page_limit,
                        select_core=runtime.delivery_for_obligation if getattr(composition, "teams", None) is not None else None,
                        **({"pending_binding": pending_binding} if pending_binding is not None else {}),
                    )
                    if getattr(composition, "teams", None) is not None:
                        page = composition.teams.deliver_page(page)
                    delivery_items += len(page)
                    count_problems(page)
            ticks += 1
            if tick + 1 < limits.ticks and not stop_requested():
                remaining = limits.max_run_seconds - (monotonic() - began)
                if remaining > 0:
                    sleep(min(limits.interval_seconds, remaining))
        return {"ticks": ticks, "worker_items": worker_items, "delivery_items": delivery_items,
                "problem_counts": problems, "preactivation": preactivation,
                "actual_environment": "NOT_RUN", "full_scenario_pass": False}
    finally:
        composition.close()


if __name__ == "__main__":
    from behavior_server_main import main

    main()
