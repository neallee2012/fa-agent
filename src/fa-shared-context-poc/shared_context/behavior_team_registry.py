"""One server-owned identity index; Team ownership cannot be reassigned."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, TypeAdapter, model_validator

from .behavior_agent_message_wire import MAX_TEAM_AGENTS
from .behavior_foundry import FoundryTarget
from .behavior_operations import _Model, _parse, _row, _updated, _write, control_key
from .behavior_store import BehaviorStore, ControlChanges, ControlRequest
from .errors import Problem
from .models import Identifier
from .records import Record
from .source_materials import digest

Oid = Annotated[str, Field(pattern=r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$")]
_OID = TypeAdapter(Oid)
_IDENTIFIER = TypeAdapter(Identifier)


class IdentityRegistration(_Model):
    schema_version: Literal["fa-team-identity-v1"] = "fa-team-identity-v1"
    kind: Literal["owner", "agent"]
    tenant_id: Oid
    oid: Oid
    team_id: Identifier
    agent_id: Identifier | None = Field(default=None, exclude_if=lambda value: value is None)
    active: bool = True
    deactivation_id: Identifier | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def exact_role(self):
        if (self.kind == "owner") != (self.agent_id is None):
            raise ValueError("only an agent identity has an agent_id")
        if self.active != (self.deactivation_id is None):
            raise ValueError("an inactive identity retains its original deactivation request")
        return self


class TeamOwnership(_Model):
    kind: Literal["team_ownership"] = "team_ownership"
    tenant_id: Oid
    team_id: Identifier
    owner_oid: Oid
    operator_oid: Oid
    request_id: Identifier


class TeamAgent(_Model):
    """The original registered Hosted target of one non-config Agent; never reassigned."""

    kind: Literal["team_agent"] = "team_agent"
    tenant_id: Oid
    team_id: Identifier
    agent_id: Identifier
    owner_oid: Oid
    request_id: Identifier
    target: FoundryTarget

    @model_validator(mode="after")
    def exact_target(self):
        if self.target.agent_id != self.agent_id:
            raise ValueError("a registered Agent ID is its original Hosted target agent_id")
        return self


class TeamAgents(_Model):
    """Registration-ordered roster of one Team's registered Agents (shared with the Team quota)."""

    kind: Literal["team_agents"] = "team_agents"
    tenant_id: Oid
    team_id: Identifier
    agent_ids: Annotated[tuple[Identifier, ...], Field(min_length=1, max_length=MAX_TEAM_AGENTS)]

    @model_validator(mode="after")
    def unique(self):
        if len(set(self.agent_ids)) != len(self.agent_ids):
            raise ValueError("each Agent appears once in its Team roster")
        return self


def identity_key(tenant_id, oid):
    return control_key("team-identity", tenant_id, oid)


def team_key(tenant_id, team_id):
    return control_key("team-ownership", tenant_id, team_id)


def agent_key(tenant_id, agent_id):
    return control_key("team-agent", tenant_id, agent_id)


def roster_key(tenant_id, team_id):
    return control_key("team-agents", tenant_id, team_id)


def _agent_request(value):
    return ControlRequest(
        request_id=control_key("team-agent-register", value.tenant_id, value.request_id),
        request_digest=digest(value.model_dump(mode="json")),
        read_set=(identity_key(value.tenant_id, value.target.agent_oid), agent_key(value.tenant_id, value.agent_id),
                  roster_key(value.tenant_id, value.team_id), identity_key(value.tenant_id, value.owner_oid)),
    )


def _agent_identity(value):
    return IdentityRegistration(kind="agent", tenant_id=value.tenant_id, oid=value.target.agent_oid,
                                team_id=value.team_id, agent_id=value.agent_id)


def _agent_result(value):
    return {"team_id": value.team_id, "agent_id": value.agent_id, "agent_oid": value.target.agent_oid}


def _config_conflict(target, configured):
    return any(target.agent_id == item.agent_id or target.agent_oid == item.agent_oid
               or target.executor_id == item.executor_id or target.session_id == item.session_id
               or (target.project_endpoint, target.agent_name) == (item.project_endpoint, item.agent_name)
               for item in configured)


def _registration_request(value):
    return ControlRequest(
        request_id=control_key("team-register", value.tenant_id, value.request_id),
        request_digest=digest(value.model_dump(mode="json")),
        read_set=(team_key(value.tenant_id, value.team_id), identity_key(value.tenant_id, value.owner_oid)),
    )


def _deactivation_request(tenant_id, operator_oid, request_id, owner_oid, team_id):
    return ControlRequest(
        request_id=control_key("team-deactivate", tenant_id, request_id),
        request_digest=digest([tenant_id, operator_oid, owner_oid, team_id]),
        read_set=(identity_key(tenant_id, owner_oid), team_key(tenant_id, team_id)),
    )


class TeamRegistry:
    def __init__(self, store: BehaviorStore, *, tenant_id: str, operator_oid: str,
                 legacy_team_id: str, legacy_owner_oid: str, agent_oids: tuple[str, ...],
                 targets: tuple[FoundryTarget, ...] = ()):
        self.store = store
        self.tenant_id = _OID.validate_python(tenant_id, strict=True)
        self.operator_oid = _OID.validate_python(operator_oid, strict=True)
        self.legacy_owner = IdentityRegistration(
            kind="owner", tenant_id=tenant_id, oid=legacy_owner_oid, team_id=legacy_team_id,
        )
        self.agent_oids = frozenset(agent_oids)
        self.targets = tuple(targets)
        if legacy_owner_oid in self.agent_oids or operator_oid in self.agent_oids:
            raise ValueError("an operator or Team owner cannot be a configured Agent")
        if len(self.targets) >= MAX_TEAM_AGENTS:
            raise ValueError("configured Agents leave no room for a registered Agent")

    def lookup(self, oid: str) -> IdentityRegistration | None:
        """One physical point read. Caller claims never choose the Team or Table key."""
        try:
            _OID.validate_python(oid, strict=True)
        except ValueError:
            raise Problem(403, "TEAM_IDENTITY_DENIED", "one canonical verified oid is required") from None
        key = identity_key(self.tenant_id, oid)
        row = self.store.read_record(key)
        if row is None:
            return None
        value = _parse(row.payload["data"], IdentityRegistration)
        if value.tenant_id != self.tenant_id or value.oid != oid:
            raise Problem(403, "TEAM_IDENTITY_DENIED", "the stored identity is outside its original tenant or oid")
        return value

    def owner(self, oid: str) -> IdentityRegistration | None:
        value = self.lookup(oid)
        if value is None and oid == self.legacy_owner.oid:
            return self.legacy_owner
        return value

    def require_operator(self, principal, *, write: bool):
        from .behavior_auth import BehaviorPrincipal
        if (not isinstance(principal, BehaviorPrincipal) or principal.kind != "human"
                or principal.actor_id is not None or principal.tenant_id != self.tenant_id
                or principal.oid != self.operator_oid):
            raise Problem(403, "TEAM_OPERATOR_DENIED", "the verified original organization operator is required")
        principal.require(self.legacy_owner.team_id, "approve" if write else "read")

    def register_owner(self, principal, request_id, team_id, owner_oid, *, reconcile=False) -> Record:
        self.require_operator(principal, write=not reconcile)
        value = TeamOwnership(tenant_id=self.tenant_id, team_id=team_id, owner_oid=owner_oid,
                              operator_oid=principal.oid, request_id=request_id)
        if (owner_oid in self.agent_oids
                or (team_id == self.legacy_owner.team_id or owner_oid == self.legacy_owner.oid)
                and (team_id, owner_oid) != (self.legacy_owner.team_id, self.legacy_owner.oid)):
            raise Problem(409, "TEAM_OWNERSHIP_CONFLICT", "configured Team and identity ownership cannot be reassigned")
        request = _registration_request(value)
        if reconcile:
            return self.store.reconcile(request)
        tkey, ikey = request.read_set
        identity = IdentityRegistration(kind="owner", tenant_id=self.tenant_id, oid=owner_oid, team_id=team_id)

        def changes(rows):
            if rows[tkey] is not None or rows[ikey] is not None:
                raise Problem(409, "TEAM_OWNERSHIP_CONFLICT", "each Team and each owner may be registered only once")
            return ControlChanges(
                writes=(_write(tkey, value, "create"), _write(ikey, identity, "create")),
                result={"team_id": team_id, "owner_oid": owner_oid},
            )

        return self.store.transact(request, changes)

    def register_agent(self, principal, request_id, agent_id, target: FoundryTarget, *,
                       reconcile=False) -> Record:
        """The active Team owner's approval registers one Agent oid/target; first version: served Team only."""
        from .behavior_auth import BehaviorPrincipal
        if (not isinstance(principal, BehaviorPrincipal) or principal.kind != "human"
                or principal.actor_id is not None or principal.tenant_id != self.tenant_id):
            raise Problem(403, "TEAM_OWNER_DENIED", "the verified original Team owner is required")
        principal.require(principal.team, "read" if reconcile else "approve")
        try:
            target = FoundryTarget.model_validate(target.model_dump(mode="json"))
            value = TeamAgent(tenant_id=self.tenant_id, team_id=principal.team, agent_id=agent_id,
                              owner_oid=principal.oid, request_id=request_id, target=target)
        except (AttributeError, ValueError):
            raise Problem(422, "TEAM_AGENT_INVALID", "one exact Agent ID and its original Hosted target are required") from None
        oid = target.agent_oid
        if (oid in self.agent_oids or oid in {self.operator_oid, self.legacy_owner.oid, principal.oid}
                or _config_conflict(target, self.targets)):
            raise Problem(409, "TEAM_AGENT_CONFLICT", "a configured identity or Hosted target cannot be registered again")
        request = _agent_request(value)
        if reconcile:
            return self.store.reconcile(request)
        if value.team_id != self.legacy_owner.team_id:
            raise Problem(409, "TEAM_RUNTIME_NOT_SERVED", "this runtime serves only its configured Team")
        owner = self.owner(principal.oid)
        if owner is None or not owner.active or owner.kind != "owner" or owner.team_id != value.team_id:
            raise Problem(403, "TEAM_OWNER_DENIED", "an active registered owner of this Team is required")
        ikey, akey, rkey, okey = request.read_set
        limit = MAX_TEAM_AGENTS - len(self.targets)

        def changes(rows):
            stored_owner = None if rows[okey] is None else _row(rows, okey, IdentityRegistration)
            if (stored_owner is None and principal.oid != self.legacy_owner.oid
                    or stored_owner is not None and (stored_owner.kind != "owner" or not stored_owner.active
                                                     or stored_owner.team_id != value.team_id)):
                raise Problem(403, "TEAM_OWNER_DENIED", "an active registered owner of this Team is required")
            if rows[ikey] is not None or rows[akey] is not None:
                raise Problem(409, "TEAM_AGENT_CONFLICT", "each Agent identity and Agent ID may be registered only once")
            roster = None if rows[rkey] is None else _row(rows, rkey, TeamAgents)
            current = () if roster is None else roster.agent_ids
            if (roster is not None and (roster.tenant_id, roster.team_id) != (self.tenant_id, value.team_id)
                    or value.agent_id in current):
                raise Problem(409, "TEAM_AGENT_CONFLICT", "the original Team roster differs")
            if len(current) >= limit:
                raise Problem(409, "TEAM_AGENT_LIMIT", "the Team has no remaining Agent capacity")
            updated = TeamAgents(tenant_id=self.tenant_id, team_id=value.team_id,
                                 agent_ids=(*current, value.agent_id))
            return ControlChanges(
                writes=(_write(ikey, _agent_identity(value), "create"), _write(akey, value, "create"),
                        _write(rkey, updated, "create" if roster is None else "replace")),
                result=_agent_result(value),
            )

        return self.store.transact(request, changes)

    def deactivate_owner(self, principal, request_id, owner_oid, *, reconcile=False) -> Record:
        self.require_operator(principal, write=not reconcile)
        try:
            _IDENTIFIER.validate_python(request_id, strict=True)
        except ValueError:
            raise Problem(422, "TEAM_OPERATOR_COMMAND", "retain one valid original deactivation request ID") from None
        identity = self.lookup(owner_oid)
        if identity is None or identity.kind != "owner":
            raise Problem(403, "TEAM_IDENTITY_DENIED", "an explicitly registered Team owner is required")
        request = _deactivation_request(self.tenant_id, principal.oid, request_id, owner_oid, identity.team_id)
        if reconcile:
            return self.store.reconcile(request)
        ikey, tkey = request.read_set

        def changes(rows):
            current = _row(rows, ikey, IdentityRegistration)
            ownership = _row(rows, tkey, TeamOwnership)
            if (current.oid != owner_oid or current.team_id != identity.team_id or current.kind != "owner"
                    or current.tenant_id != self.tenant_id or ownership.tenant_id != self.tenant_id
                    or ownership.owner_oid != owner_oid or ownership.team_id != current.team_id):
                raise Problem(409, "TEAM_OWNERSHIP_CONFLICT", "the original owner mapping differs")
            if not current.active:
                raise Problem(409, "TEAM_IDENTITY_INACTIVE", "the original deactivation is already retained")
            return ControlChanges(
                writes=(_write(ikey, _updated(current, active=False, deactivation_id=request_id),
                               "replace"),),
                result={"team_id": current.team_id, "owner_oid": owner_oid, "active": False},
            )

        return self.store.transact(request, changes)


def validate_team_registry_history(inventory, config):
    """Recognize only exact owner/index pairs and their original create/deactivate receipts."""
    from .behavior_recovery_history import _Inventory

    view = _Inventory(inventory, config)
    classified = {}
    for key, row in inventory.rows.items():
        if row.payload["data"].get("kind") != "team_ownership":
            continue
        value = view.row(key, TeamOwnership)
        if (key != team_key(value.tenant_id, value.team_id) or value.tenant_id != config.identity.tenant_id
                or value.operator_oid != config.identity.human_oid
                or value.owner_oid in config.identity.auth().actors
                or (value.team_id == config.identity.team_id or value.owner_oid == config.identity.human_oid)
                and (value.team_id, value.owner_oid) != (config.identity.team_id, config.identity.human_oid)):
            raise Problem(409, "TEAM_REGISTRY_INVALID", "retained Team ownership differs from its original scope")
        ikey = identity_key(value.tenant_id, value.owner_oid)
        identity = view.row(ikey, IdentityRegistration)
        original = IdentityRegistration(kind="owner", tenant_id=value.tenant_id,
                                        oid=value.owner_oid, team_id=value.team_id)
        if identity.model_copy(update={"active": True, "deactivation_id": None}) != original or ikey in classified:
            raise Problem(409, "TEAM_REGISTRY_INVALID", "retained owner/index pairs must be one-to-one")
        view.receipt(_registration_request(value), (key, ikey),
                     exact={key: view.hash(key), ikey: view.image_hash(ikey, original)},
                     result={"team_id": value.team_id, "owner_oid": value.owner_oid})
        if not identity.active:
            request = _deactivation_request(value.tenant_id, value.operator_oid, identity.deactivation_id,
                                            value.owner_oid, value.team_id)
            view.receipt(request, (ikey,), exact={ikey: view.hash(ikey)},
                         result={"team_id": value.team_id, "owner_oid": value.owner_oid, "active": False})
        classified.update({key: value, ikey: identity})
    _agent_history(view, inventory, config, classified)
    from .behavior_agent_onboarding import validate_onboarding_history
    validate_onboarding_history(view, inventory, config, classified)
    for key, row in inventory.rows.items():
        if row.payload["data"].get("schema_version") == "fa-team-identity-v1" and key not in classified:
            raise Problem(409, "TEAM_REGISTRY_INVALID", "an identity has no supported original registration")
    return classified


def _agent_history(view, inventory, config, classified):
    """Each registered Agent: exact index/target pair, its roster position and one original receipt."""
    identity = config.identity
    configured = identity.auth().actors
    rosters, members = {}, {}
    for key, row in inventory.rows.items():
        if row.payload["data"].get("kind") != "team_agent":
            continue
        value = view.row(key, TeamAgent)
        owner = classified.get(identity_key(value.tenant_id, value.owner_oid))
        oid = value.target.agent_oid
        if (key != agent_key(value.tenant_id, value.agent_id) or value.tenant_id != identity.tenant_id
                or value.team_id != identity.team_id or oid in configured or oid == identity.human_oid
                or _config_conflict(value.target, config.targets)
                or value.owner_oid != identity.human_oid
                and not (isinstance(owner, IdentityRegistration) and owner.kind == "owner"
                         and owner.team_id == value.team_id)):
            raise Problem(409, "TEAM_REGISTRY_INVALID", "a retained Agent registration differs from its original scope")
        ikey, rkey = identity_key(value.tenant_id, oid), roster_key(value.tenant_id, value.team_id)
        if view.row(ikey, IdentityRegistration) != _agent_identity(value) or ikey in classified:
            raise Problem(409, "TEAM_REGISTRY_INVALID", "retained Agent identity/target pairs must be one-to-one")
        roster = rosters.get(rkey) or view.row(rkey, TeamAgents)
        if (rkey != roster_key(roster.tenant_id, roster.team_id) or roster.team_id != value.team_id
                or value.agent_id not in roster.agent_ids):
            raise Problem(409, "TEAM_REGISTRY_INVALID", "a retained Agent is missing from its Team roster")
        position = roster.agent_ids.index(value.agent_id)
        image = TeamAgents(tenant_id=roster.tenant_id, team_id=roster.team_id,
                           agent_ids=roster.agent_ids[:position + 1])
        view.receipt(_agent_request(value), (ikey, key, rkey),
                     exact={ikey: view.hash(ikey), key: view.hash(key), rkey: view.image_hash(rkey, image)},
                     result=_agent_result(value))
        rosters[rkey] = roster
        members.setdefault(rkey, set()).add(value.agent_id)
        classified.update({key: value, ikey: _agent_identity(value)})
    for key, row in inventory.rows.items():
        if row.payload["data"].get("kind") != "team_agents":
            continue
        if key not in rosters or set(rosters[key].agent_ids) != members[key]:
            raise Problem(409, "TEAM_REGISTRY_INVALID", "a retained Team roster lists an unregistered Agent")
        classified[key] = rosters[key]


def _scope(config, key, value):
    from .behavior_server_config import AgentIdentity, ServerAgentScope
    identity = config.identity
    if (key != agent_key(identity.tenant_id, value.agent_id) or value.tenant_id != identity.tenant_id
            or value.team_id != identity.team_id or value.target.agent_oid in identity.auth().actors
            or value.target.agent_oid == identity.human_oid or _config_conflict(value.target, config.targets)):
        raise Problem(403, "AGENT_SCOPE_DENIED", "the registered Agent is outside this runtime's original Team")
    return ServerAgentScope(
        configuration_sha256=config.sha256, authority_domain=config.table.scope.authority_domain,
        control_binding=config.workers.control_binding, tenant_id=identity.tenant_id, team_id=value.team_id,
        human_oid=identity.human_oid, identity=AgentIdentity(oid=value.target.agent_oid, actor=value.agent_id),
        target=value.target, legacy_keys=False,
    )


def _registered_key(config, agent_id):
    if any(target.agent_id == agent_id for target in config.targets):
        return None
    try:
        _IDENTIFIER.validate_python(agent_id, strict=True)
    except ValueError:
        raise Problem(403, "AGENT_SCOPE_DENIED", "one configured or registered Agent ID is required") from None
    return agent_key(config.identity.tenant_id, agent_id)


def resolve_agent_scope(store, config, agent_id):
    """Config targets: 0 reads. A registered Agent: one read of its original create-only target row."""
    key = _registered_key(config, agent_id)
    if key is None:
        return config.agent_scope(agent_id)
    row = store.read_record(key)
    if row is None:
        raise Problem(403, "AGENT_SCOPE_DENIED", "one configured or registered Agent ID is required")
    return _scope(config, key, _parse(row.payload["data"], TeamAgent))


def inventory_agent_scope(inventory, config, agent_id):
    """History readers resolve from the retained startup inventory, never from live rows."""
    key = _registered_key(config, agent_id)
    if key is None:
        return config.agent_scope(agent_id)
    if key not in inventory.rows:
        raise Problem(409, "AGENT_SCOPE_INVALID", "retained work names an Agent without its original registration")
    return _scope(config, key, _parse(inventory.rows[key].payload["data"], TeamAgent))


def registered_agent_ids(store, config):
    """The served Team's registered Agents in registration order (one read)."""
    key = roster_key(config.identity.tenant_id, config.identity.team_id)
    row = store.read_record(key)
    if row is None:
        return ()
    value = _parse(row.payload["data"], TeamAgents)
    if (value.tenant_id, value.team_id) != (config.identity.tenant_id, config.identity.team_id):
        raise Problem(409, "TEAM_REGISTRY_INVALID", "the stored roster is outside its original Team")
    return value.agent_ids


def same_team_recipient(scope, *, sender_oid, team_id):
    """The lane-independent recipient rule: any Agent of the sender's Team except the sender.

    `sender_oid` and `team_id` come only from server bindings (ticket / verified principal), never from the
    model. The original A is a valid recipient (results and new requests back to A); a lane that cannot
    receive at A keeps that restriction in its own adapter. Hop, message and per-turn limits stay with the
    relay that records the message."""
    if scope.team_id != team_id or scope.identity.oid == sender_oid:
        raise Problem(403, "AGENT_MESSAGE_ROUTE_DENIED", "the recipient must be another Agent of the same Team")
    return scope


def resolve_team_recipient(store, config, recipient_agent_id, *, sender_oid, team_id):
    """Resolve a model-chosen recipient ID: configured Agents with no read, a registered Agent with one.

    Only an unknown, malformed or other-Team ID becomes ROUTE_DENIED; store failures (5xx, conflicts)
    propagate unchanged so a caller never mistakes an outage for a final refusal."""
    try:
        scope = resolve_agent_scope(store, config, recipient_agent_id)
    except Problem as error:
        if error.status != 403 or error.body["error"]["code"] != "AGENT_SCOPE_DENIED":
            raise
        raise Problem(403, "AGENT_MESSAGE_ROUTE_DENIED", "the recipient is not a configured or registered Agent") from None
    return same_team_recipient(scope, sender_oid=sender_oid, team_id=team_id)


def _ready_display_name(store, config, agent_id, scope):
    """The display name when this resolved recipient is ready, else None: an onboarded Agent must be ready."""
    from .behavior_agent_onboarding import agent_onboarding
    if any(target.agent_id == agent_id for target in config.targets):
        return scope.target.agent_name
    onboarding = agent_onboarding(store, config, agent_id)
    if onboarding is None:
        return scope.target.agent_name
    return onboarding.display_name if onboarding.state == "ready" else None


def team_recipient_ids(store, config, *, sender_oid, team_id):
    """Ready same-Team recipients as (agent_id, display_name), configured first then registered.

    Ready means the original A, or an Agent whose runtime scope is registered; an onboarded Agent must
    also have reached ready. Reads are bounded by the Team size: the roster, one snapshot of scope rows,
    then per registered Agent its target row and onboarding record. One recipient: team_recipient_ready."""
    from .behavior_agent_registry import registry_key
    configured = tuple(target.agent_id for target in config.targets)
    candidates = configured + tuple(agent_id for agent_id in registered_agent_ids(store, config)
                                    if agent_id not in configured)
    domain = config.table.scope.authority_domain
    peers = candidates[1:]
    rows = store.read_snapshot(tuple(registry_key(domain, agent_id) for agent_id in peers)) if peers else {}
    ready = []
    for agent_id in candidates:
        if agent_id != configured[0] and rows[registry_key(domain, agent_id)] is None:
            continue
        try:
            scope = resolve_team_recipient(store, config, agent_id, sender_oid=sender_oid, team_id=team_id)
        except Problem as error:
            if error.body["error"]["code"] != "AGENT_MESSAGE_ROUTE_DENIED":
                raise
            continue
        display_name = _ready_display_name(store, config, agent_id, scope)
        if display_name is not None:
            ready.append((agent_id, display_name))
    return tuple(ready)


def team_recipient_ready(store, config, agent_id, *, sender_oid, team_id):
    """One model-chosen recipient's scope when ``team_recipient_ids`` would list it, read for that Agent only.

    Same rule and errors as resolving then checking membership in the full list: an unknown, other-Team or
    self recipient is 403 AGENT_MESSAGE_ROUTE_DENIED; a resolved one that is not ready is 409
    AGENT_MESSAGE_RECIPIENT_NOT_READY. Reads: its target row, the roster for a registered Agent, its scope
    row and its onboarding record, never the rest of the Team."""
    from .behavior_agent_registry import registry_key
    scope = resolve_team_recipient(store, config, agent_id, sender_oid=sender_oid, team_id=team_id)
    configured = tuple(target.agent_id for target in config.targets)
    ready = agent_id in configured or agent_id in registered_agent_ids(store, config)
    if ready and agent_id != configured[0]:
        key = registry_key(config.table.scope.authority_domain, agent_id)
        ready = store.read_snapshot((key,))[key] is not None
    if not ready or _ready_display_name(store, config, agent_id, scope) is None:
        raise Problem(409, "AGENT_MESSAGE_RECIPIENT_NOT_READY", "the resolved receiver is not ready")
    return scope
