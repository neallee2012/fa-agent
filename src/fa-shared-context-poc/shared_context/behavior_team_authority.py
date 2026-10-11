"""Per-Team settings and usage ledger rows (ADR-0006 T6a/T6b); no runtime use yet.

A Team other than the configured original Team gets two create-only rows in the original
authority partition, keyed by Team: its settings and an empty usage ledger. One Table
transaction can only cover one PartitionKey, so keeping them next to the organization
ledger lets later admission/usage writes check and reserve both in a single CAS.
The original Team keeps its original keys and rows; nothing is moved or rewritten.

T6b (Neal 2026-10-02 08:28, A/A/A/A/B): the operator initializes a Team from the organization
defaults in the server config and can only keep or lower them; connectors default to the
organization's approved list. The Team owner can afterwards only lower its Team's values. Each
owner step stays in the settings row (`narrowings`) so history can rebuild every original receipt.
Admission/usage wiring, root budgets and raising a value are later slices.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, TypeAdapter, model_validator

from .behavior_agent_message_wire import MAX_TEAM_AGENTS
from .behavior_operations import _Model, _parse, _row, _write, control_key
from .behavior_server_config import MAX_TEAM_MODEL_CAP_MICROUSD
from .behavior_store import ControlChanges, ControlRequest
from .behavior_team_registry import IdentityRegistration, Oid, TeamOwnership, identity_key, team_key
from .behavior_work_policy import work_policy_key
from .errors import Problem
from .models import Identifier
from .records import Record
from .source_materials import digest

Money = Annotated[int, Field(ge=0, le=2**63 - 1)]
ModelCap = Annotated[int, Field(ge=1, le=MAX_TEAM_MODEL_CAP_MICROUSD)]
AgentLimit = Annotated[int, Field(ge=1, le=MAX_TEAM_AGENTS)]
Connectors = Annotated[tuple[Identifier, ...], Field(max_length=16)]
MAX_TEAM_NARROWINGS = 16
_IDENTIFIER = TypeAdapter(Identifier)


def team_authority_key(domain, team_id):
    return work_policy_key("team", domain, team_id)


def team_ledger_key(domain, team_id):
    return work_policy_key("team-cost", domain, team_id)


def team_root_budget_id(tenant_id, team_id):
    return control_key("team-root", tenant_id, team_id)


class TeamLimits(_Model):
    model_cap_microusd: ModelCap
    max_agents: AgentLimit
    connectors: Connectors = ()

    def allows(self, other: "TeamLimits") -> bool:
        """True when `other` keeps or lowers every value: the only direction these commands may move."""
        return (other.model_cap_microusd <= self.model_cap_microusd and other.max_agents <= self.max_agents
                and set(other.connectors) <= set(self.connectors))


class TeamNarrowing(_Model):
    """One owner command, kept as requested; `None` keeps that value unchanged."""

    request_id: Identifier
    owner_oid: Oid
    model_cap_microusd: ModelCap | None = None
    max_agents: AgentLimit | None = None
    connectors: Connectors | None = None

    @model_validator(mode="after")
    def changes_something(self):
        if (self.model_cap_microusd, self.max_agents, self.connectors) == (None, None, None):
            raise ValueError("a narrowing names at least one value")
        if self.connectors is not None and len(set(self.connectors)) != len(self.connectors):
            raise ValueError("duplicate connector")
        return self

    def applied(self, limits: TeamLimits) -> TeamLimits:
        return TeamLimits(
            model_cap_microusd=(limits.model_cap_microusd if self.model_cap_microusd is None
                                else self.model_cap_microusd),
            max_agents=limits.max_agents if self.max_agents is None else self.max_agents,
            connectors=limits.connectors if self.connectors is None else self.connectors,
        )


class TeamAuthority(_Model):
    kind: Literal["team_authority"] = "team_authority"
    schema_version: Literal["fa-team-authority-v1"] = "fa-team-authority-v1"
    tenant_id: Oid
    team_id: Identifier
    owner_oid: Oid
    authority_domain: Identifier
    root_budget_id: Identifier
    revision: Literal[1] = 1
    # The operator's original grant; the owner's later steps never rewrite it.
    model_cap_microusd: ModelCap
    max_agents: AgentLimit
    connectors: Connectors = ()
    operator_oid: Oid
    request_id: Identifier
    narrowings: Annotated[tuple[TeamNarrowing, ...], Field(max_length=MAX_TEAM_NARROWINGS,
                                                           exclude_if=lambda value: not value)] = ()

    @model_validator(mode="after")
    def exact(self):
        if self.root_budget_id != team_root_budget_id(self.tenant_id, self.team_id):
            raise ValueError("the Team root budget is derived from its Team")
        if len(set(self.connectors)) != len(self.connectors):
            raise ValueError("duplicate connector")
        limits = self.granted
        for step in self.narrowings:
            narrowed = step.applied(limits)
            if step.owner_oid != self.owner_oid or not limits.allows(narrowed):
                raise ValueError("an owner step can only lower its own Team's values")
            limits = narrowed
        if len({step.request_id for step in self.narrowings}) != len(self.narrowings):
            raise ValueError("each owner step keeps its own request ID")
        return self

    @property
    def granted(self) -> TeamLimits:
        return TeamLimits(model_cap_microusd=self.model_cap_microusd, max_agents=self.max_agents,
                          connectors=self.connectors)

    @property
    def limits(self) -> TeamLimits:
        """The values in force: the operator's grant after every owner step."""
        limits = self.granted
        for step in self.narrowings:
            limits = step.applied(limits)
        return limits

    def created(self) -> "TeamAuthority":
        return self.model_copy(update={"narrowings": ()})

    def through(self, count: int) -> "TeamAuthority":
        return self.model_copy(update={"narrowings": self.narrowings[:count]})


class TeamUsageLedger(_Model):
    kind: Literal["team_usage_ledger"] = "team_usage_ledger"
    tenant_id: Oid
    team_id: Identifier
    authority_domain: Identifier
    unit: Literal["USD-micro"] = "USD-micro"
    work_reservations_microusd: Money = 0
    reservation_count: Annotated[int, Field(ge=0)] = 0
    model_pending_microusd: Money = 0
    model_metered_microusd: Money = 0


def _initial_ledger(value):
    return TeamUsageLedger(tenant_id=value.tenant_id, team_id=value.team_id, authority_domain=value.authority_domain)


def _request(value):
    return ControlRequest(
        request_id=control_key("team-authority-init", value.tenant_id, value.request_id),
        request_digest=digest(value.model_dump(mode="json")),
        read_set=(team_key(value.tenant_id, value.team_id), identity_key(value.tenant_id, value.owner_oid),
                  team_authority_key(value.authority_domain, value.team_id),
                  team_ledger_key(value.authority_domain, value.team_id)),
    )


def _result(value):
    return {"team_id": value.team_id, "root_budget_id": value.root_budget_id, "runtime_activated": False}


def _narrow_request(value, step):
    return ControlRequest(
        # Each Team's owner names its own request IDs, so the receipt is scoped by Team (like onboarding).
        request_id=control_key("team-authority-narrow", value.tenant_id, value.team_id, step.request_id),
        request_digest=digest([value.tenant_id, value.team_id, step.model_dump(mode="json")]),
        read_set=(identity_key(value.tenant_id, step.owner_oid),
                  team_authority_key(value.authority_domain, value.team_id)),
    )


def _narrow_result(value):
    return {"team_id": value.team_id, "narrowings": len(value.narrowings),
            **value.limits.model_dump(mode="json")}


def _resolved(defaults, model_cap_microusd, max_agents, connectors):
    """The organization defaults with only kept or lowered explicit values (Q2 A, Q5 B)."""
    if defaults is None:
        raise Problem(503, "TEAM_DEFAULTS_UNCONFIGURED",
                      "the server configuration has no organization Team defaults to start from")
    try:
        requested = TeamLimits(
            model_cap_microusd=defaults.model_cap_microusd if model_cap_microusd is None else model_cap_microusd,
            max_agents=defaults.max_agents if max_agents is None else max_agents,
            connectors=defaults.connectors if connectors is None else tuple(connectors),
        )
    except ValueError:
        raise Problem(422, "TEAM_AUTHORITY_COMMAND", "Team settings are outside their allowed bounds") from None
    organization = TeamLimits(model_cap_microusd=defaults.model_cap_microusd, max_agents=defaults.max_agents,
                              connectors=defaults.connectors)
    if not organization.allows(requested):
        raise Problem(422, "TEAM_SETTINGS_ABOVE_DEFAULT",
                      "a new Team can only keep or lower the organization defaults")
    return requested


def initialize_team(store, registry, principal, request_id, team_id, *, defaults=None, model_cap_microusd=None,
                    max_agents=None, connectors=None, reconcile=False) -> Record:
    """Create one registered owner's Team settings and empty usage ledger, once.

    Omitted values come from the organization defaults (connectors: its approved list); given values may
    only keep or lower them."""
    registry.require_operator(principal, write=not reconcile)
    try:
        _IDENTIFIER.validate_python(request_id, strict=True)
        _IDENTIFIER.validate_python(team_id, strict=True)
    except ValueError:
        raise Problem(422, "TEAM_AUTHORITY_COMMAND", "retain one valid original request ID and Team") from None
    if team_id == registry.legacy_owner.team_id:
        raise Problem(409, "TEAM_AUTHORITY_LEGACY", "the original Team keeps its original policy and ledger")
    tenant, domain = registry.tenant_id, store._scope.authority_domain
    tkey = team_key(tenant, team_id)
    ownership = store.read_snapshot((tkey,))[tkey]
    if ownership is None:
        raise Problem(409, "TEAM_OWNER_REQUIRED", "register the Team owner before initializing the Team")
    owner = _parse(ownership.payload["data"], TeamOwnership)
    limits = _resolved(defaults, model_cap_microusd, max_agents, connectors)
    try:
        value = TeamAuthority(
            tenant_id=tenant, team_id=team_id, owner_oid=owner.owner_oid, authority_domain=domain,
            root_budget_id=team_root_budget_id(tenant, team_id), model_cap_microusd=limits.model_cap_microusd,
            max_agents=limits.max_agents, connectors=limits.connectors, operator_oid=principal.oid,
            request_id=request_id,
        )
    except ValueError:
        raise Problem(422, "TEAM_AUTHORITY_COMMAND", "Team settings are outside their allowed bounds") from None
    request = _request(value)
    if reconcile:
        return store.reconcile(request)
    _, ikey, akey, lkey = request.read_set

    def changes(rows):
        current_owner = _row(rows, tkey, TeamOwnership)
        identity = _row(rows, ikey, IdentityRegistration)
        if (current_owner != owner or identity.kind != "owner" or not identity.active
                or (identity.tenant_id, identity.team_id) != (tenant, team_id)):
            raise Problem(409, "TEAM_OWNER_REQUIRED", "an active registered owner of this Team is required")
        if rows[akey] is not None or rows[lkey] is not None:
            raise Problem(409, "TEAM_AUTHORITY_EXISTS", "a Team is initialized only once")
        return ControlChanges(writes=(_write(akey, value, "create"), _write(lkey, _initial_ledger(value), "create")),
                              result=_result(value))

    return store.transact(request, changes)


def narrow_team(store, registry, principal, request_id, team_id, *, model_cap_microusd=None, max_agents=None,
                connectors=None, reconcile=False) -> Record:
    """The active owner of a T6 Team keeps or lowers its values; raising stays with the operator."""
    from .behavior_auth import BehaviorPrincipal
    if (not isinstance(principal, BehaviorPrincipal) or principal.kind != "human"
            or principal.actor_id is not None or principal.tenant_id != registry.tenant_id):
        raise Problem(403, "TEAM_OWNER_DENIED", "the verified original Team owner is required")
    principal.require(team_id, "read" if reconcile else "approve")
    try:
        step = TeamNarrowing(request_id=request_id, owner_oid=principal.oid, model_cap_microusd=model_cap_microusd,
                             max_agents=max_agents, connectors=None if connectors is None else tuple(connectors))
    except ValueError:
        raise Problem(422, "TEAM_AUTHORITY_COMMAND", "retain one request ID and at least one lowered value") from None
    domain = store._scope.authority_domain
    found = read_team_authority(store, domain, team_id)
    if found is None:
        raise Problem(409, "TEAM_AUTHORITY_MISSING", "only an initialized Team can lower its values")
    request = _narrow_request(found[0], step)
    if reconcile:
        return store.reconcile(request)
    owner = registry.owner(principal.oid)
    if owner is None or not owner.active or owner.kind != "owner" or owner.team_id != team_id:
        raise Problem(403, "TEAM_OWNER_DENIED", "an active registered owner of this Team is required")
    ikey, akey = request.read_set

    def changes(rows):
        identity = _row(rows, ikey, IdentityRegistration)
        if (identity.kind != "owner" or not identity.active
                or (identity.tenant_id, identity.team_id, identity.oid) != (registry.tenant_id, team_id, principal.oid)):
            raise Problem(403, "TEAM_OWNER_DENIED", "an active registered owner of this Team is required")
        current = _row(rows, akey, TeamAuthority)
        if (current.team_id, current.authority_domain, current.owner_oid) != (team_id, domain, principal.oid):
            raise Problem(409, "TEAM_AUTHORITY_INVALID", "stored Team settings differ from their owner")
        if not current.limits.allows(step.applied(current.limits)):
            raise Problem(422, "TEAM_SETTINGS_RAISED", "the Team owner can only keep or lower its values")
        if len(current.narrowings) >= MAX_TEAM_NARROWINGS:
            raise Problem(409, "TEAM_NARROWING_LIMIT", "this Team has no remaining owner steps")
        updated = current.model_copy(update={"narrowings": (*current.narrowings, step)})
        return ControlChanges(writes=(_write(akey, updated, "replace"),), result=_narrow_result(updated))

    return store.transact(request, changes)


def read_team_authority(store, domain, team_id):
    akey, lkey = team_authority_key(domain, team_id), team_ledger_key(domain, team_id)
    rows = store.read_snapshot((akey, lkey))
    if rows[akey] is None:
        return None
    value = _row(rows, akey, TeamAuthority)
    ledger = _row(rows, lkey, TeamUsageLedger)
    if (value.team_id, value.authority_domain) != (team_id, domain) or (
            ledger.tenant_id, ledger.team_id, ledger.authority_domain) != (value.tenant_id, team_id, domain):
        raise Problem(409, "TEAM_AUTHORITY_INVALID", "stored Team rows differ from their keys")
    return value, ledger


def validate_team_authority_history(inventory, config):
    """Recognize only exact Team settings/ledger pairs created by their original receipt."""
    from .behavior_recovery_history import _Inventory

    view = _Inventory(inventory, config)
    domain, tenant = config.table.scope.authority_domain, str(config.identity.tenant_id)
    classified = {}
    for key, row in inventory.rows.items():
        if row.payload["data"].get("kind") != "team_authority":
            continue
        value = view.row(key, TeamAuthority)
        ownership = view.row(team_key(value.tenant_id, value.team_id), TeamOwnership)
        if (key != team_authority_key(domain, value.team_id) or value.authority_domain != domain
                or value.tenant_id != tenant or value.team_id == config.identity.team_id
                or value.operator_oid != str(config.identity.human_oid) or ownership.owner_oid != value.owner_oid):
            raise Problem(409, "TEAM_AUTHORITY_INVALID", "retained Team settings differ from their original scope")
        lkey = team_ledger_key(domain, value.team_id)
        ledger = view.row(lkey, TeamUsageLedger)
        created = value.created()
        view.receipt(_request(created), (key, lkey),
                     exact={key: view.image_hash(key, created), lkey: view.image_hash(lkey, _initial_ledger(value))},
                     result=_result(created))
        for count, step in enumerate(value.narrowings, start=1):
            image = value.through(count)
            view.receipt(_narrow_request(value, step), (key,), exact={key: view.image_hash(key, image)},
                         result=_narrow_result(image))
        if ledger != _initial_ledger(value):
            raise Problem(409, "TEAM_AUTHORITY_INVALID", "a Team usage ledger has no supported update yet")
        classified.update({key: value, lkey: ledger})
    for key, row in inventory.rows.items():
        if row.payload["data"].get("kind") == "team_usage_ledger" and key not in classified:
            raise Problem(409, "TEAM_AUTHORITY_INVALID", "a Team usage ledger has no original Team settings")
    return classified
