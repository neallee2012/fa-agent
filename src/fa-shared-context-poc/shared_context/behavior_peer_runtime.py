"""Construct one registered peer's C1 runtime against the existing authority and root ledger."""

from contextlib import ExitStack
import logging
import re
import asyncio

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.routing import Mount

from .auth import Actor
from .behavior_assistant import AssistantRuntime
from .behavior_assistant_server import AssistantServer
from .behavior_delivery import DeliveryCore, RuntimeRegistration
from .behavior_effects import WorkStateEffectAdapter
from .behavior_foundry import FoundryExecutor, FoundryHttpClient, NativeJobRoute, NativeLaunchAuthority, native_key
from .behavior_inbox import InboxRegistration, PullInbox
from .behavior_operations import ToolBrokerCore
from .behavior_team_registry import agent_retired, registered_agent_ids, resolve_agent_scope, retired_agent_ids
from .behavior_runtime import RuntimeDependencies
from .behavior_work_policy import attach_standing_policy
from .behavior_work_policy import work_policy_key
from .behavior_work_target import refuse_retired_selection, selected_targets
from .behavior_workers import WorkerCore
from .errors import Problem


def build_peer(owner, policy, *, credential, native_transports, assistant_transport):
    from .behavior_server import NativeEvidenceCatalog, ServerComposition, _OperationCatalog
    config, original = owner.config, owner.runtime
    dep, store, clock = original.dep, original.store, original.clock
    targets = selected_targets(store, config, agent_id=policy.agent_id)
    if (config.workers.capacity != 1 or len(targets) != 1
            or targets[0].agent_oid != policy.template.owner.principal_id):
        raise Problem(503, "AGENT_RUNTIME_SCOPE_NOT_WIRED", "a peer requires one exact owning C1 target")
    # Peers are built here, not by the startup installation, so a leftover selection row stops this peer too.
    refuse_retired_selection(store, config, policy)
    resources = ExitStack()
    try:
        catalog = NativeEvidenceCatalog(config, clock=clock)
        inbox = PullInbox(store, InboxRegistration(config.authentication_version, dep.evidence.verify_access, catalog.inbox))
        workers = WorkerCore(store, config.workers, adapter=None, clock=clock, mode="native")
        delivery = DeliveryCore(store, clock=clock, registration=RuntimeRegistration(
            config.delivery, "native", catalog.delivery, dep.evidence.verify_ingress,
            workers.verify_work_result, inbox.verify_receipt, inbox))
        inbox.attach(delivery)
        binding = policy.template
        native = NativeLaunchAuthority(
            workers, team_id=binding.team_id, controller_id=config.controller_id, targets=targets,
            jobs=(NativeJobRoute(job_id=binding.job_id, api_binding_id=binding.binding_id,
                                grant_id=native_key("bootstrap-grant", binding.job_id)),),
            verifier_version=config.delivery.verifier_version, clock=clock)
        clients = []
        for target in targets:
            if native_transports is not None and target.executor_id not in native_transports:
                raise Problem(503, "AGENT_RUNTIME_TRANSPORT_MISSING", "the supplied local transport lacks the peer target")
            client = FoundryHttpClient(target, credential,
                transport=None if native_transports is None else native_transports[target.executor_id])
            resources.callback(client.close)
            clients.append(client)
        executor = FoundryExecutor(native, tuple(clients))
        workers.adapter = executor.adapter(verify_readiness=catalog.worker)
        aliases = _OperationCatalog(config)
        aliases.aliases = {key: value for key, value in aliases.aliases.items() if value.binding_id == binding.binding_id}
        effect = WorkStateEffectAdapter(delivery, allowed_work_ids=(binding.work.work_id,), references=aliases)
        broker = ToolBrokerCore(store, clock=clock, adapter=effect.registration, execution_adapter=effect)
        dependencies = RuntimeDependencies(store, dep.sources, dep.memory, broker, delivery, inbox, workers,
            dep.evidence, dep.claim_reviewer, native, dep.source_backups)
        runtime = AssistantRuntime(dependencies, (binding,), profiles=(policy.profile,), clock=clock,
                                   mode="native", controller_id=config.controller_id)
        catalog.runtime, aliases.runtime, aliases.adapter = runtime, runtime, effect
        peer = ServerComposition(config, runtime, owner.verifier, catalog,
                                 (*owner.clients[:3], *clients), resources, agent_id=policy.agent_id)
        peer._agent_message_owner = owner
        peer.hosted_versions = owner.hosted_versions
        peer.upgrade_snapshot_container_url = owner.upgrade_snapshot_container_url
        peer.work_cost_reader = owner.runtime.work_policy.cost_reader
        extra_actor = None
        if binding.agent_oid not in config.identity.auth().actors:
            scope = resolve_agent_scope(store, config, policy.agent_id)
            if scope.identity.oid != binding.agent_oid or scope.team_id != binding.team_id:
                raise Problem(503, "AGENT_RUNTIME_SCOPE_NOT_WIRED", "the registered agent differs from its binding")
            extra_actor = Actor(scope.identity.oid, scope.identity.actor, scope.team_id)
            peer.registered_actor = extra_actor
        peer.assistant = AssistantServer(peer, owner.clients[0], owner.clients[1], credential,
                                         transport=assistant_transport, extra_actor=extra_actor)
        peer.assistant.start()
        runtime.capability_grants = original.capability_grants
        runtime.capability_catalog = original.capability_catalog
        # Team-scoped services (grants keyed by this peer's own binding Team): onboarding S5 and server-side turns
        # (routines, Teams relay, agent messages) run before this peer's app build installs its own copies.
        runtime.team_mcp = getattr(original, "team_mcp", None)
        runtime.team_connectors = getattr(original, "team_connectors", None)
        attach_standing_policy(peer, agent_id=policy.agent_id)
        return peer
    except BaseException:
        resources.close()
        raise


def _agent_bindings(owner, agent_id, *, retired=False):
    """One peer's binding IDs (its template and every admission), or None before its registry row exists."""
    from .behavior_agent_registry import AgentScopeRegistry, read_agent_registry, registry_key
    from .behavior_operations import _row
    from .behavior_work_registry import read_registry
    store = owner.runtime.store
    key = registry_key(owner.config.table.scope.authority_domain, agent_id)
    rows = store.read_snapshot((key,))
    if rows[key] is None:
        return None
    # A retired Agent's scope is 410 by design; its stored row only names the bindings to answer as retired.
    policy = (_row(rows, key, AgentScopeRegistry) if retired
              else read_agent_registry(store, owner.config, agent_id)).policy
    index = read_registry(store, policy)
    return frozenset((policy.template.binding_id,
                      *(work_policy_key("binding", admission) for admission in index.entries)))


def _binding_agent(owner, binding_id):
    """Scan the served Team's peers for the one holding ``binding_id``; A's own bindings stay with A."""
    if binding_id in owner.runtime.bindings:
        return None
    for agent_id in _peer_agent_ids(owner):
        if binding_id in (_agent_bindings(owner, agent_id) or ()):
            return agent_id
    # After a restart the map is empty: a retired Agent's binding answers as retired, not as A's unknown binding.
    for agent_id in retired_agent_ids(owner.runtime.store, owner.config):
        if binding_id in (_agent_bindings(owner, agent_id, retired=True) or ()):
            raise agent_retired()
    return None


def _peer_agent_ids(owner):
    """Configured peers, then the served Team's registered Agents (one roster read)."""
    configured = tuple(target.agent_id for target in owner.config.targets[1:])
    return configured + tuple(agent_id for agent_id in registered_agent_ids(owner.runtime.store, owner.config)
                              if agent_id not in configured)


class _PeerRouting:
    def __init__(self, app, *, owner, factory, error_handler):
        from .behavior_api import _principal_dependency
        self.app, self.owner, self.factory, self.error_handler = app, owner, factory, error_handler
        self.identity = _principal_dependency(owner.verifier)
        self.apps = {}
        # binding ID -> peer agent ID, filled at prepare() and on a scan; only peers of this owner's Team.
        # The peer's own binding check still runs on every request; an entry it does not confirm is dropped.
        self.binding_agents = {}
        self.lock = asyncio.Lock()
        owner._peer_router = self

    async def application_for(self, peer):
        async with self.lock:
            if peer.agent_id not in self.apps:
                app = await run_in_threadpool(self.factory, peer)
                self.apps[peer.agent_id] = app
                if getattr(app.state, "instruction_runner", False):
                    peer.resume_instructions()
            return self.apps[peer.agent_id]

    async def prepare(self):
        from .behavior_agent_registry import registry_key
        store = self.owner.runtime.store
        try:
            agent_ids = await run_in_threadpool(_peer_agent_ids, self.owner)
        except Exception as error:
            logging.getLogger(__name__).error("AGENT_RUNTIME_UNAVAILABLE roster exception_type=%s", type(error).__name__)
            agent_ids = tuple(target.agent_id for target in self.owner.config.targets[1:])
        for agent_id in agent_ids:
            key = registry_key(self.owner.config.table.scope.authority_domain, agent_id)
            try:
                rows = await run_in_threadpool(store.read_snapshot, (key,))
                if rows[key] is not None:
                    peer = await run_in_threadpool(self.owner.for_agent, agent_id)
                    await self.application_for(peer)
                    bindings = await run_in_threadpool(_agent_bindings, self.owner, agent_id)
                    self.binding_agents.update(dict.fromkeys(bindings or (), agent_id))
            except Problem as error:
                logging.getLogger(__name__).error("AGENT_RUNTIME_UNAVAILABLE agent_id=%s code=%s",
                    agent_id, error.body["error"]["code"])
            except Exception as error:
                logging.getLogger(__name__).error("AGENT_RUNTIME_UNAVAILABLE agent_id=%s exception_type=%s",
                    agent_id, type(error).__name__)

    async def _binding_agent(self, binding_id):
        """A human binding path's peer: the map first, else one registry scan (as before the map)."""
        if binding_id in self.owner.runtime.bindings:
            return None
        agent_id = self.binding_agents.get(binding_id)
        if agent_id is None:
            agent_id = await run_in_threadpool(_binding_agent, self.owner, binding_id)
            if agent_id is not None:
                self.binding_agents[binding_id] = agent_id
        return agent_id

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        from starlette._utils import get_route_path
        path = get_route_path(scope)
        binding = re.match(r"^/behavior/v1/bindings/([A-Za-z0-9][A-Za-z0-9_-]{0,63})(?:/|$)", path)
        prefix = re.match(r"^/agents/([A-Za-z0-9][A-Za-z0-9_-]{0,63})(?=/|$)", path)
        if binding is None and prefix is None:
            return await self.app(scope, receive, send)
        request = Request(scope, receive)
        selected = self.app
        try:
            principal = await run_in_threadpool(self.identity, request)
            if prefix is not None:
                agent_id = prefix.group(1)
                target = await run_in_threadpool(
                    resolve_agent_scope, self.owner.runtime.store, self.owner.config, agent_id)
                if (principal.tenant_id != target.tenant_id or principal.team != target.team_id
                        or principal.oid != (target.identity.oid if principal.kind == "agent" else target.human_oid)):
                    raise Problem(403, "BEHAVIOR_BINDING_DENIED", "credential and selected agent scope differ")
            elif principal.kind == "agent":
                target = next((target for target in self.owner.config.targets if target.agent_oid == principal.oid), None)
                if target is not None:
                    agent_id = target.agent_id
                else:
                    registered = await run_in_threadpool(
                        resolve_agent_scope, self.owner.runtime.store, self.owner.config, principal.actor_id)
                    if (registered.identity.oid != principal.oid or registered.team_id != principal.team
                            or registered.tenant_id != principal.tenant_id):
                        raise Problem(403, "BEHAVIOR_BINDING_DENIED", "agent credential has no registered target")
                    agent_id = registered.target.agent_id
            else:
                agent_id = await self._binding_agent(binding.group(1))
            if agent_id is None or agent_id == self.owner.config.targets[0].agent_id:
                selected = self.app
            else:
                peer = await run_in_threadpool(self.owner.for_agent, agent_id)
                if binding is not None:
                    await run_in_threadpool(peer.runtime.refresh_bindings, binding.group(1))
                    assigned = peer.runtime.bindings.get(binding.group(1))
                    if assigned is None and self.binding_agents.get(binding.group(1)) == agent_id:
                        del self.binding_agents[binding.group(1)]  # the next request scans the registry again
                    if (assigned is None or principal.oid != (
                            assigned.agent_oid if principal.kind == "agent" else assigned.human_oid)):
                        raise Problem(403, "BEHAVIOR_BINDING_DENIED", "credential and original binding path differ")
                selected = await self.application_for(peer)
            if prefix is not None:
                mount = Mount(prefix.group(), app=selected)
                _, child_scope = mount.matches(scope)
                scope, selected = {**scope, **child_scope}, mount.handle
        except Problem as error:
            logging.getLogger(__name__).warning("AGENT_ROUTE_REFUSED code=%s", error.body["error"]["code"])
            response = await self.error_handler(request, error)
            return await response(scope, receive, send)
        except Exception as error:
            logging.getLogger(__name__).error("AGENT_ROUTE_UNAVAILABLE exception_type=%s", type(error).__name__)
            response = await self.error_handler(request, Problem(
                503, "AGENT_RUNTIME_UNAVAILABLE", "the selected peer runtime could not be initialized"))
            return await response(scope, receive, send)
        return await selected(scope, receive, send)


def install_peer_routing(app, composition, factory):
    """Keep A's paths; route B through its original credential and ordinary endpoint/ticket checks."""
    app.add_middleware(_PeerRouting, owner=composition, factory=factory, error_handler=app.exception_handlers[Problem])
