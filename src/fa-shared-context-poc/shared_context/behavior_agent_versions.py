"""The endpoint selects the served version; deployment synchronizes the exact Hosted definition.

The API reads the selector and validates its concrete version, identity and compatible code build. Deployment
preserves each Agent's published environment, applies explicit non-secret overrides and publishes only after
the complete immutable definition reads back. Historical active versions are not a routing policy.

A POST whose outcome is unknown is never repeated in the same run; the next run lists first.
"""

from __future__ import annotations

import argparse
import copy
from dataclasses import dataclass
import hashlib
import json
import logging
from pathlib import Path
import re
import sys
import threading
import time

from .behavior_agent_onboarding import _PROBE_ENV, FoundryCallError, _list
from .behavior_code_identity import code_sha256
from .errors import Problem
from .native_responses_wire import ACCEPTED_STATE_PROFILES, NATIVE_V2_PROFILE

log = logging.getLogger(__name__)

METADATA_BUILD, METADATA_SOURCE = "fa_build", "fa_source"
METADATA_DEFINITION = "fa_definition_sha256"
FOLLOWING = "AGENT_VERSION_FOLLOWING"
# Platform answers that say nothing about which version is selected (transport failure, timeout, throttling, 5xx).
_TRANSIENT_STATUSES = frozenset({408, 429})
_SETTLED = frozenset({"active", "failed", "deleting", "deleted"})
SYNC_WAIT_SECONDS = 900
_POLL_SECONDS = 5


class SelectorUnreadable(Problem):
    """503 ``AGENT_VERSION_FOLLOWING`` because the endpoint selector could not be read this time.

    Unlike the other 503 ``AGENT_VERSION_FOLLOWING`` answers (an incompatible or unready selected version), it
    does not say the selected version moved; the body is the same, so every caller handling a Problem is unchanged."""


def _number(item):
    value = item.get("version")
    if isinstance(value, str) and value.isascii() and value.isdigit() and not value.startswith("0") and len(value) <= 16:
        return int(value)
    return None


def _of_build(items, build):
    return [item for item in items if isinstance(item, dict) and _number(item) is not None
            and isinstance(item.get("metadata"), dict) and item["metadata"].get(METADATA_BUILD) == build]


def _same_identity(item, target):
    guid = item.get("agent_guid")
    identity = item.get("instance_identity")
    principal = identity.get("principal_id") if isinstance(identity, dict) else None
    return (isinstance(guid, str) and isinstance(principal, str)
            and guid.lower() == target.agent_id.lower() and principal.lower() == target.agent_oid.lower())


def _active_match(same, target, preferred_version=None):
    """Choose only between already matched, identical definitions; this never chooses an endpoint route."""
    active = [item for item in same if item.get("status") == "active"]
    mine = [item for item in active if _same_identity(item, target)]
    if not mine:
        return None, bool(active)
    preferred = next((item for item in mine if item["version"] == preferred_version), None)
    if preferred is not None:
        return preferred["version"], False
    chosen = min(mine, key=_number)
    if any(item.get("status") not in _SETTLED and _number(item) < _number(chosen) for item in same):
        return None, False
    return chosen["version"], False


def _ready(foundry, name, version):
    """The listed status can turn active minutes before GET does (live, 2026-10-01); only GET decides."""
    return foundry.get_version(name, version).get("status") == "active"


def definition_sha256(definition):
    if not isinstance(definition, dict) or definition.get("kind") != "hosted":
        raise Problem(409, "HOSTED_DEFINITION_INVALID", "one Hosted definition is required")
    value = copy.deepcopy(definition)
    protocols = value.get("protocol_versions")
    try:
        if isinstance(protocols, list):
            value["protocol_versions"] = sorted(protocols, key=lambda item: json.dumps(item, sort_keys=True))
        raw = json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError):
        raise Problem(409, "HOSTED_DEFINITION_INVALID", "the Hosted definition cannot be encoded") from None
    return hashlib.sha256(raw).hexdigest()


def definition_matches(definition, expected_hash):
    """Keep saved fingerprints unchanged; accept only the service's added empty protocol default."""
    if definition_sha256(definition) == expected_hash:
        return True
    if definition.get("container_protocol_versions") == []:
        original = {key: value for key, value in definition.items() if key != "container_protocol_versions"}
        return definition_sha256(original) == expected_hash
    return False


def _of_definition(items, build, definition_hash):
    matches = []
    for item in _of_build(items, build):
        recorded = item["metadata"].get(METADATA_DEFINITION)
        if recorded == definition_hash:
            matches.append(item)
        elif (isinstance(item.get("definition"), dict)
              and (recorded is None or item["definition"].get("kind") == "hosted"
                   and item["definition"].get("container_protocol_versions") == [])):
            if (definition_matches(item["definition"], definition_hash)
                    and (recorded is None or definition_matches(item["definition"], recorded))):
                matches.append(item)
    return matches


@dataclass(frozen=True)
class _SelectedVersion:
    version: str
    responses_enabled: bool
    model_switch: bool = False

    def require(self, responses):
        if responses and not self.responses_enabled:
            raise Problem(409, "NATIVE_TURNS_DISABLED", "the selected build does not enable Responses turns")
        return self.version


def _responses_enabled(definition):
    if not isinstance(definition, dict) or definition.get("kind") != "hosted":
        return False
    environment = definition.get("environment_variables")
    protocols = definition.get("protocol_versions")
    return (isinstance(environment, dict) and environment.get("FA_NATIVE_TURNS") in ACCEPTED_STATE_PROFILES
            and environment.get("FA_RESPONSES_ENABLED") == "1"
            and not {"FA_N6_RESPONSES_PROBE", "FA_N6_RESPONSES_INTERRUPT_SESSION_ID"} & environment.keys()
            and isinstance(protocols, list) and any(
                isinstance(item, dict) and item.get("protocol") == "responses" and item.get("version") == "2.0.0"
                for item in protocols))


def _declared_profile(item):
    """The native state profile ``item``'s definition declares (``FA_NATIVE_TURNS``), None when it declares none."""
    from .native_deploy_gate import definition_profile
    return definition_profile(item.get("definition"))


def _build(item):
    """The installed code identity (``code_sha256()``) the version was published for (``fa_build``), or None."""
    metadata = item.get("metadata") if isinstance(item, dict) else None
    value = metadata.get(METADATA_BUILD) if isinstance(metadata, dict) else None
    return value if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) else None


def _model_switch(definition):
    """The selected Hosted definition switches models mid-conversation (FA_NATIVE_MODEL_SWITCH=1); display only."""
    from .behavior_native_turn_wire import MODEL_SWITCH_ENV

    environment = definition.get("environment_variables") if isinstance(definition, dict) else None
    return isinstance(environment, dict) and environment.get(MODEL_SWITCH_ENV) == "1"


class HostedVersions:
    """API-side, read only: the concrete version every new Hosted session of an Agent is pinned to."""

    def __init__(self, foundry_for, *, build=None):
        self._foundry_for = foundry_for
        self.build = code_sha256() if build is None else build
        # The last selection read per Agent, so a caller that just checked a version can show its flags without
        # another Foundry read. Never an authority: the Hosted session enforces its own ENV.
        self._observed = {}
        # (endpoint, agent, version) -> the profile its definition declares, and its ``fa_build``; both immutable.
        self._profiles = {}
        self._builds = {}

    def observed(self, target):
        return self._observed.get((target.project_endpoint, target.agent_name))

    def _keep(self, key, observed):
        self._profiles[key], self._builds[key] = _declared_profile(observed), _build(observed)

    def profile(self, target, version):
        """The native state profile ``version``'s definition declares (None: none), for the binding row (ADR-0009
        2d-i). One ``get_version`` per version and process; an unreadable definition raises and is never cached."""
        key = (target.project_endpoint, target.agent_name, version)
        if key not in self._profiles:
            try:
                observed = self._foundry_for(target.project_endpoint).get_version(target.agent_name, version)
            except FoundryCallError:
                raise Problem(503, FOLLOWING, "the version's definition is not readable yet") from None
            if not isinstance(observed, dict) or (observed.get("name"), observed.get("version")) != (
                    target.agent_name, version):
                raise Problem(503, FOLLOWING, "the version's definition is invalid")
            self._keep(key, observed)
        return self._profiles[key]

    def build_of(self, target, version):
        """``version``'s ``fa_build``: the ``code_sha256()`` of the image it runs (sync publishes it from that build).
        Read and kept with ``profile``; None when it is unreadable or names none."""
        try:
            self.profile(target, version)
        except Problem:
            return None
        return self._builds.get((target.project_endpoint, target.agent_name, version))

    def current(self, target, *, require_responses=False):
        """The selected version, when this API can follow to it (ADR-0009 2d-i). A v1 definition must be this API
        build's (``fa_build`` equal): the v1 string never pinned a schema (§3). A v2 definition of any build passes:
        v2 has its snapshot test, and every v2 runtime accepts the frozen v1 wire this API sends until 2d-ii."""
        try:
            foundry = self._foundry_for(target.project_endpoint)
            version = endpoint_version(foundry, target.agent_name)
            observed = foundry.get_version(target.agent_name, version)
        except FoundryCallError as error:
            log.warning("agent version lookup unknown agent=%s status=%s", target.agent_name, error.status)
            if require_responses and error.status in {401, 403}:
                raise Problem(error.status, "NATIVE_TURNS_DISABLED",
                              "the selected Responses build definition cannot be read") from None
            refusal = (SelectorUnreadable if error.status is None or error.status in _TRANSIENT_STATUSES
                       or error.status >= 500 else Problem)
            raise refusal(503, FOLLOWING, "this Agent's Hosted version for the deployed build is not readable yet") from None
        if (not isinstance(observed, dict) or observed.get("name") != target.agent_name or observed.get("version") != version
                or not _same_identity(observed, target)
                or not (_of_build([observed], self.build) or _declared_profile(observed) == NATIVE_V2_PROFILE)
                or observed.get("status") != "active" or not isinstance(observed.get("definition"), dict)
                or observed["definition"].get("kind") != "hosted"):
            log.warning("agent endpoint version is incompatible agent=%s version=%s", target.agent_name, version)
            raise Problem(503, FOLLOWING, "the endpoint's selected version is not ready for this API build")
        selected = _SelectedVersion(version, _responses_enabled(observed.get("definition")),
                                    _model_switch(observed.get("definition")))
        self._observed[(target.project_endpoint, target.agent_name)] = selected
        self._keep((target.project_endpoint, target.agent_name, version), observed)
        return selected.require(require_responses)


def endpoint_version(foundry, name):
    """Resolve the platform selector; @latest uses AgentDetails.versions.latest, not local version ordering."""
    agent = foundry.get_agent(name)
    if not isinstance(agent, dict):
        raise Problem(503, FOLLOWING, "the endpoint metadata is invalid")
    endpoint = agent.get("agent_endpoint")
    selector = endpoint.get("version_selector") if isinstance(endpoint, dict) else None
    rules = selector.get("version_selection_rules") if isinstance(selector, dict) else None
    if (agent.get("name") != name or agent.get("state") != "enabled"
            or not isinstance(rules, list) or len(rules) != 1 or not isinstance(rules[0], dict)
            or rules[0].get("type") != "FixedRatio" or type(rules[0].get("traffic_percentage")) is not int
            or rules[0]["traffic_percentage"] != 100):
        raise Problem(503, FOLLOWING, "one enabled endpoint with a single served version is required")
    version = rules[0].get("agent_version")
    if version == "@latest":
        versions = agent.get("versions")
        latest = versions.get("latest") if isinstance(versions, dict) else None
        version = latest.get("version") if isinstance(latest, dict) else None
    if _number({"version": version}) is None:
        raise Problem(503, FOLLOWING, "the endpoint's concrete served version could not be read")
    return version


def current_hosted_version(runtime, target, *, require_responses=False):
    """The resolver of the owning composition; offline harnesses without one keep the configured version.

    ``target`` is None only for a non-native harness, which never pins a Hosted version."""
    resolver = _resolver(runtime)
    if target is None or resolver is None:
        if require_responses:
            raise Problem(503, "NATIVE_TURNS_DISABLED", "the selected Responses build definition is unavailable")
        if target is None:
            return None
        return target.agent_version
    return resolver.current(target, require_responses=True) if require_responses else resolver.current(target)


def hosted_model_switch(runtime, target):
    """Whether the version ``current_hosted_version`` last read for ``target`` switches models; False when unread."""
    resolver = _resolver(runtime)
    selected = None if resolver is None or target is None else resolver.observed(target)
    return selected is not None and selected.model_switch


def _resolver(runtime):
    composition = getattr(runtime, "agent_message_composition", None)
    return getattr(composition, "hosted_versions", None)


def hosted_build(runtime, target):
    """The code identity of the image ``target``'s version runs (its ``fa_build``); None when unknown."""
    build_of = getattr(_resolver(runtime), "build_of", None)
    if build_of is None or target is None or target.agent_version is None:
        return None
    return build_of(target, target.agent_version)


def deployed_build(runtime):
    resolver = _resolver(runtime)
    return code_sha256() if resolver is None else resolver.build


# ----- sync (deployment) ------------------------------------------------------------------------------------------

_SYNC_LOCKS = {}
_SYNC_GUARD = threading.Lock()


def _sync_lock(target):
    with _SYNC_GUARD:
        return _SYNC_LOCKS.setdefault((target.project_endpoint, target.agent_name), threading.Lock())


def sync_agent_version(foundry, target, *, build, source, preferred_version=None, sleep=time.sleep, clock=time.monotonic,
                       wait_seconds=SYNC_WAIT_SECONDS):
    """Ensure the requested immutable definition, not merely any version of the same build."""
    result = {"agent_id": target.agent_id, "agent_name": target.agent_name, "build": build}
    with _sync_lock(target):
        try:
            items = _list(foundry, target.agent_name)
            definition, label = source()
            definition = copy.deepcopy(definition)
            expected_hash = definition_sha256(definition)
        except FoundryCallError:
            return {**result, "state": "pending", "reason": "SOURCE_READ_UNKNOWN"}
        except Problem as error:
            return {**result, "state": "failed", "reason": error.body["error"]["code"]}
        result["definition_sha256"] = expected_hash
        deadline, posted, before = clock() + wait_seconds, False, set()
        first_read = True
        while True:
            try:
                if not first_read:
                    items = _list(foundry, target.agent_name)
                first_read = False
                same = _of_definition(items, build, expected_hash)
                version, mismatch = _active_match(same, target, preferred_version)
                observed = None if version is None else foundry.get_version(target.agent_name, version)
                ready = observed is not None and observed.get("status") == "active"
            except FoundryCallError:
                return {**result, "state": "pending", "reason": "FOUNDRY_READ_UNKNOWN"}
            if ready:
                if (observed.get("name") != target.agent_name or observed.get("version") != version
                        or not _same_identity(observed, target)
                        or not _of_build([observed], build)
                        or not definition_matches(observed.get("definition"), expected_hash)):
                    return {**result, "state": "failed", "reason": "HOSTED_DEFINITION_READBACK_CONFLICT"}
                return {**result, "state": "ok", "version": version}
            if version is not None:
                if clock() >= deadline:
                    return {**result, "state": "pending", "reason": "VERSION_NOT_ACTIVE_YET"}
                sleep(_POLL_SECONDS)
                continue
            if mismatch:
                return {**result, "state": "failed", "reason": "AGENT_VERSION_IDENTITY_MISMATCH"}
            # Only versions that were not listed before our POST can be its result.
            new = [item for item in same if _number(item) not in before]
            if any(item.get("status") not in _SETTLED for item in same) or (posted and not new):
                if clock() >= deadline:
                    return {**result, "state": "pending", "reason": "VERSION_NOT_ACTIVE_YET"}
                sleep(_POLL_SECONDS)
                continue
            if posted:
                return {**result, "state": "failed", "reason": "FOUNDRY_VERSION_FAILED"}
            before = {_number(item) for item in same}
            try:
                foundry.create_version(target.agent_name, definition=definition,
                                       metadata={METADATA_BUILD: build, METADATA_SOURCE: label,
                                                 METADATA_DEFINITION: expected_hash})
            except FoundryCallError as error:
                if error.status in {400, 422}:
                    return {**result, "state": "failed", "reason": "FOUNDRY_CREATE_REJECTED"}
                # Unknown outcome: never POST again in this run; read back until it shows up or the wait ends.
                log.warning("agent version create unknown agent=%s status=%s", target.agent_name, error.status)
            posted = True


def _definition(foundry, name, version, code="SOURCE_VERSION_MISSING"):
    details = foundry.get_version(name, version)
    if details.get("status") not in {None, "active"} or not isinstance(details.get("definition"), dict):
        raise Problem(409, code, "the source version is not active")
    return copy.deepcopy(details["definition"])


def image_source(foundry, target, image):
    """The Agent's published version, never another Agent's flags or an unpublished candidate."""
    def source():
        if not image:
            raise Problem(400, "IMAGE_REQUIRED", "creating a configured Agent's version needs --image")
        version = endpoint_version(foundry, target.agent_name)
        definition = _definition(foundry, target.agent_name, version)
        container = definition.get("container_configuration")
        if isinstance(container, dict) and "image" in container:
            container["image"] = image
        elif "image" in definition:
            definition["image"] = image
        else:
            raise Problem(409, "SOURCE_IMAGE_FIELD_MISSING", "the source definition has no image field")
        _declare_host_protocols(definition)
        return definition, f"{target.agent_name}:{version}"
    return source


_ENVIRONMENT_FLAGS = frozenset({
    "FA_NATIVE_TURNS", "FA_RESPONSES_ENABLED", "FA_NATIVE_HITL_ENABLED", "FA_NATIVE_HITL_TTL_SECONDS",
    "FA_NATIVE_GROUPS_ENABLED", "FA_TEAM_MEMORY_ENABLED", "FA_PRIVATE_MEMORY_STORE", "TZ",
    # Tracing is on when unset; an explicit "0" is the deployment-only way to turn it off.
    "FA_SDK_TELEMETRY",
    # Native routines (README#native-routines-stage): scheduled turns and propose_routine; "0" turns them off.
    "FA_NATIVE_ROUTINES_ENABLED", "FA_NATIVE_ROUTINE_PROPOSALS",
    # Mid-conversation model switch (README#native-model-switch); unset or "0" keeps a conversation on its model.
    "FA_NATIVE_MODEL_SWITCH",
})


def _check_environment(environment, idle_timeout_seconds):
    environment = {} if environment is None else environment
    try:
        invalid = (not isinstance(environment, dict) or set(environment) - _ENVIRONMENT_FLAGS
                   or any(not isinstance(value, str) or not value or len(value.encode("utf-8")) > 2048
                          for value in environment.values()))
    except UnicodeError:
        invalid = True
    if invalid or type(idle_timeout_seconds) is not int or not 120 <= idle_timeout_seconds <= 3600:
        raise Problem(409, "HOSTED_ENVIRONMENT_INVALID", "only bounded non-secret deployment settings are accepted")
    switches = _ENVIRONMENT_FLAGS - {"FA_NATIVE_TURNS", "FA_NATIVE_HITL_TTL_SECONDS", "FA_PRIVATE_MEMORY_STORE", "TZ"}
    if (any(key in environment and environment[key] not in {"0", "1"} for key in switches)
            or "FA_NATIVE_TURNS" in environment and environment["FA_NATIVE_TURNS"] not in ACCEPTED_STATE_PROFILES
            or "FA_NATIVE_HITL_TTL_SECONDS" in environment
            and re.fullmatch(r"[1-9][0-9]{0,5}", environment["FA_NATIVE_HITL_TTL_SECONDS"]) is None
            or any("\x00" in value or "\r" in value or "\n" in value for value in environment.values())):
        raise Problem(409, "HOSTED_ENVIRONMENT_INVALID", "a deployment flag value is invalid")
    return dict(environment)


def _carried_environment(environment):
    """The ENV a new definition keeps: probe keys never carry over, and a native Agent with text groups also gets
    the Responses lane every group turn runs on (only when unset; an explicit 0 stays 0)."""
    carried = {key: value for key, value in environment.items() if key not in _PROBE_ENV}
    if (carried.get("FA_NATIVE_GROUPS_ENABLED") == "1" and carried.get("FA_NATIVE_TURNS") in ACCEPTED_STATE_PROFILES
            and "FA_RESPONSES_ENABLED" not in carried):
        carried["FA_RESPONSES_ENABLED"] = "1"
    return carried


def desired_source(source, environment=None, *, idle_timeout_seconds=900):
    """Deployment-only overlay; registered identities and their original configuration digests do not change."""
    environment = _check_environment(environment, idle_timeout_seconds)
    def desired():
        definition, label = source()
        definition = copy.deepcopy(definition)
        current = definition.get("environment_variables")
        if current is not None and not isinstance(current, dict):
            raise Problem(409, "HOSTED_DEFINITION_INVALID", "the original Hosted environment is invalid")
        definition["environment_variables"] = _carried_environment({**(current or {}), **environment})
        session = definition.get("session_configuration")
        if session is not None and not isinstance(session, dict):
            raise Problem(409, "HOSTED_DEFINITION_INVALID", "the original session configuration is invalid")
        session = dict(session or {})
        previous = session.get("idle_timeout_seconds", idle_timeout_seconds)
        if type(previous) is not int or not 120 <= previous <= 3600:
            raise Problem(409, "HOSTED_DEFINITION_INVALID", "the original idle timeout is invalid")
        session["idle_timeout_seconds"] = max(previous, idle_timeout_seconds)
        definition["session_configuration"] = session
        return definition, label
    return desired


def _declare_host_protocols(definition):
    """Declare supported transports without enabling a Responses handler or replacing existing protocols."""
    protocols = definition.get("protocol_versions")
    if isinstance(protocols, list):
        for protocol in ("invocations_ws", "responses"):
            if not any(isinstance(item, dict) and item.get("protocol") == protocol for item in protocols):
                protocols.append({"protocol": protocol, "version": "2.0.0"})


def copy_source(foundry, original, build):
    """Copy an explicitly published, compatible source; normal updates use the recipient's own image_source."""
    def source():
        version = endpoint_version(foundry, original.agent_name)
        details = foundry.get_version(original.agent_name, version)
        if not _of_build([details], build) or not _same_identity(details, original):
            raise Problem(409, "SOURCE_NOT_SYNCED", "the published source is not compatible with this build")
        definition = _definition(foundry, original.agent_name, version, "SOURCE_NOT_SYNCED")
        env = definition.get("environment_variables")
        if isinstance(env, dict):
            definition["environment_variables"] = _carried_environment(env)
        return definition, f"{original.agent_name}:{version}"
    return source


def original_source(composition, foundry):
    """onboarding: A's version of the deployed build (definition, label); 503 FOLLOWING until it is synced."""
    from .behavior_work_target import selected_targets
    original = selected_targets(composition.runtime.store, composition.config)[0]
    version = current_hosted_version(composition.runtime, original)
    definition = _definition(foundry, original.agent_name, version, "AGENT_ONBOARDING_SOURCE_UNAVAILABLE")
    env = definition.get("environment_variables")
    if isinstance(env, dict):
        definition["environment_variables"] = _carried_environment(env)
    return definition, f"{original.agent_name}:{version}"


def route(foundry, name, version):
    """Publish once and verify the exact selector; a lost acknowledgment is resolved only by readback."""
    if _number({"version": version}) is None:
        raise Problem(409, "ROUTE_VERSION_INVALID", "a concrete version is required for publication")
    current = foundry.get_agent(name).get("agent_endpoint")
    if not isinstance(current, dict) or not isinstance(current.get("protocol_configuration"), dict):
        raise Problem(409, "ROUTE_PROTOCOL_UNKNOWN", "the agent endpoint's protocol configuration is not readable")
    selector = {"version_selection_rules": [
        {"type": "FixedRatio", "agent_version": version, "traffic_percentage": 100}]}
    if current.get("version_selector") == selector:
        return
    try:
        foundry.update_route(name, {**current, "version_selector": selector})
    except FoundryCallError as error:
        if error.status in {400, 401, 403, 422}:
            raise
        log.warning("agent route write unconfirmed agent=%s status=%s", name, error.status)
    observed = foundry.get_agent(name).get("agent_endpoint")
    if (not isinstance(observed, dict) or observed.get("version_selector") != selector
            or observed.get("protocol_configuration") != current["protocol_configuration"]):
        raise Problem(503, "ROUTE_UNKNOWN", "the published selector did not read back; do not blindly repeat its write")


def _agents(composition):
    """(target, kind) for A, the other configured Agents, then the Team's registered Agents."""
    from .behavior_team_registry import registered_agent_ids, resolve_agent_scope
    from .behavior_work_target import selected_targets
    config, store = composition.config, composition.runtime.store
    original = selected_targets(store, config)[0]
    seen = {(original.project_endpoint, original.agent_name)}
    values = [(original, "configured")]
    for target in config.targets[1:]:
        if (target.project_endpoint, target.agent_name) not in seen:
            seen.add((target.project_endpoint, target.agent_name))
            values.append((target, "configured"))
    for agent_id in registered_agent_ids(store, config):
        values.append((resolve_agent_scope(store, config, agent_id).target, "registered"))
    return original, values


def sync_all(composition, foundry_for, *, image=None, agent_id=None, set_route=False, build=None,
             hosted_environment=None, idle_timeout_seconds=900, refuse_native_v2=False,
             sleep=time.sleep, clock=time.monotonic, wait_seconds=SYNC_WAIT_SECONDS):
    """One result per Agent; each is independent and a failure only logs. ``refuse_native_v2`` (the ADR-0009 2d-i
    switch is off): an Agent whose served definition declares v2 fails before any write instead of carrying v2 on."""
    build = code_sha256() if build is None else build
    original, agents = _agents(composition)
    hosted_environment = {} if hosted_environment is None else hosted_environment
    if not isinstance(hosted_environment, dict) or set(hosted_environment) - {target.agent_id for target, _ in agents}:
        raise Problem(409, "HOSTED_ENVIRONMENT_INVALID", "deployment settings must name a served Agent")
    for environment in hosted_environment.values():
        if not isinstance(environment, dict):
            raise Problem(409, "HOSTED_ENVIRONMENT_INVALID", "each Agent override must be an object")
        _check_environment(environment, idle_timeout_seconds)
    _check_environment({}, idle_timeout_seconds)
    results = []
    for target, kind in agents:
        if agent_id is not None and target.agent_id != agent_id:
            continue
        foundry = foundry_for(target.project_endpoint)
        try:
            published = endpoint_version(foundry, target.agent_name)
            details = foundry.get_version(target.agent_name, published) if set_route or refuse_native_v2 else None
            if refuse_native_v2 and _declared_profile(details) == NATIVE_V2_PROFILE:
                raise Problem(409, "NATIVE_V2_SERVED_SWITCH_OFF",
                              "the served definition declares foundry-native-v2 and the 2d-i switch is off")
            if set_route:
                if details.get("status") != "active" or not _same_identity(details, target):
                    raise Problem(409, "SOURCE_VERSION_MISSING", "the served source version is not confirmed")
                route(foundry, target.agent_name, published)
            source = desired_source(image_source(foundry, target, image), hosted_environment.get(target.agent_id),
                                    idle_timeout_seconds=idle_timeout_seconds)
            result = sync_agent_version(foundry, target, build=build, source=source, sleep=sleep, clock=clock,
                                        wait_seconds=wait_seconds, preferred_version=published)
            if set_route and result["state"] == "ok":
                try:
                    route(foundry, target.agent_name, result["version"])
                    result["route"] = "ok"
                except (Problem, FoundryCallError) as error:
                    result["route"] = getattr(error, "body", {}).get("error", {}).get("code", "ROUTE_UNKNOWN")
        except Exception as error:  # one Agent never stops the others
            code = error.body["error"]["code"] if isinstance(error, Problem) else type(error).__name__
            result = {"agent_id": target.agent_id, "agent_name": target.agent_name, "build": build,
                      "state": "failed", "reason": code}
        (log.info if result["state"] == "ok" else log.warning)("agent version sync %s", json.dumps(result, sort_keys=True))
        results.append(result)
    if agent_id is not None and not results:
        results.append({"agent_id": agent_id, "build": build, "state": "failed", "reason": "AGENT_NOT_FOUND"})
    return results


def main(argv=None):
    from .behavior_server import build_server
    from .behavior_server_config import load_configuration
    parser = argparse.ArgumentParser(prog="agent-versions",
        description="Create each Agent's Hosted version for this image's build (run from the new image).")
    commands = parser.add_subparsers(dest="command", required=True)
    sync = commands.add_parser("sync")
    sync.add_argument("--config")
    sync.add_argument("--agent-id")
    sync.add_argument("--image", help="container image for configured Agents' new versions")
    sync.add_argument("--core", help="expected 64-hex code identity printed by this image's code_sha256(), not its digest")
    sync.add_argument("--route", action="store_true", help="also pin each endpoint's default route to the version")
    sync.add_argument("--defer-follow", action="store_true",
                      help="publish only; use solely before the matching API image is ready")
    sync.add_argument("--verify-follow", action="store_true",
                      help="ADR-0009 2d-i: report each Agent's environment (metadata only), never follow")
    sync.add_argument("--refuse-native-v2", action="store_true",
                      help="ADR-0009 2d-i switch off: an Agent serving foundry-native-v2 fails, nothing written")
    sync.add_argument("--follow-wait-seconds", type=int, default=1200,
                      help="deployment wait for original environment readiness; a timeout never cancels the follow")
    sync.add_argument("--hosted-environment", help="deployment-only JSON: Agent ID to explicit non-secret ENV overrides")
    sync.add_argument("--idle-timeout-seconds", type=int, default=900)
    fence = commands.add_parser("fence", help="inspect, or with --clear settle, one Agent's stuck 2d-i files fence")
    fence.add_argument("--config")
    fence.add_argument("--agent-id", required=True)
    fence.add_argument("--clear", action="store_true",
                       help="withdraw an unmarked fence, else drive it and only then commit it without files")
    fence.add_argument("--confirm", metavar="AGENT_ID", help="required with --clear: the same Agent ID again")
    patrol = commands.add_parser("patrol", help="ADR-0009 2d-i P8: run one metadata-only session patrol pass now")
    patrol.add_argument("--config")
    patrol.add_argument("--once", action="store_true", required=True, help="the only mode: one pass, print its summary")
    args = parser.parse_args(argv)
    if args.command == "patrol":
        from .native_session_patrol import NativeSessionPatrol
        try:
            with build_server(load_configuration(args.config)) as composition:
                composition.installation()
                if composition.runtime.work_policy is None:
                    raise Problem(503, "WORK_POLICY_DISABLED", "the installed standing policy is required")
                counts = NativeSessionPatrol(composition, switch=lambda _agent_id: False).run_once()
        except Problem as error:
            print(json.dumps({"state": "failed", "reason": error.body["error"]["code"]}), file=sys.stderr)
            return 1
        print(json.dumps({"state": "ok", "patrol": counts}, sort_keys=True))
        return 0
    if args.command == "fence":
        if args.clear and args.confirm != args.agent_id:
            parser.error("--clear needs --confirm with the same Agent ID")
        from .native_cut import fence_command
        try:
            with build_server(load_configuration(args.config)) as composition:
                composition.installation()
                print(json.dumps(fence_command(composition, args.agent_id, clear=args.clear), sort_keys=True))
        except Problem as error:
            print(json.dumps({"state": "failed", "reason": error.body["error"]["code"]}), file=sys.stderr)
            return 1
        return 0
    if (args.follow_wait_seconds < 1 or args.defer_follow and not args.route
            or args.verify_follow and (args.defer_follow or not args.route)):
        parser.error("--defer-follow and --verify-follow require --route and exclude each other; "
                     "--follow-wait-seconds must be positive")
    # verify-only: an Agent still on its earlier session is "pending" (it moves after its next turn end), not a failure.
    settled = {"ok", "skipped", "pending"} if args.verify_follow else {"ok", "skipped"}
    core = code_sha256()
    if args.core is not None and (re.fullmatch(r"[0-9a-f]{64}", args.core) is None or args.core != core):
        parser.error("--core must be the 64-hex code identity from this image's code_sha256(), not its image digest")
    try:
        from .behavior_api import _nonfinite, _unique
        environment = None
        if args.hosted_environment is not None:
            with Path(args.hosted_environment).open("rb") as stream:
                raw = stream.read(65537)
            if len(raw) > 65536:
                raise Problem(413, "HOSTED_ENVIRONMENT_INVALID", "the deployment overlay exceeds 64 KiB")
            environment = json.loads(raw, object_pairs_hook=_unique, parse_constant=_nonfinite)
        with build_server(load_configuration(args.config)) as composition:
            results = sync_all(composition, composition.hosted_versions._foundry_for,
                               image=args.image, agent_id=args.agent_id, set_route=args.route, build=core,
                               hosted_environment=environment, idle_timeout_seconds=args.idle_timeout_seconds,
                               refuse_native_v2=args.refuse_native_v2)
            if args.route and not args.defer_follow:
                from .native_environment_sync import follow_all
                published = {item["agent_id"] for item in results
                             if item["state"] == "ok" and item.get("route") == "ok"}
                if published:
                    composition.installation()
                    verify = {"verify_only": True} if args.verify_follow else {}  # P6's deploy hook
                    followed = {item["agent_id"]: item for item in follow_all(
                        composition, agent_id=args.agent_id, published=published, wait_seconds=args.follow_wait_seconds,
                        **verify)}
                    for item in results:
                        if item["agent_id"] in followed:
                            observation = followed[item["agent_id"]]
                            item["environment_follow"] = observation
                            if observation["state"] not in settled:
                                item.update(version_state=item["state"], state="failed",
                                            reason=observation.get("reason", "NATIVE_ENVIRONMENT_PENDING"))
    except Problem as error:
        print(json.dumps({"state": "failed", "reason": error.body["error"]["code"]}), file=sys.stderr)
        return 1
    except (ValueError, OSError):
        print(json.dumps({"state": "failed", "reason": "AGENT_VERSIONS_CONFIGURATION_INVALID"}), file=sys.stderr)
        return 1
    for result in results:
        print(json.dumps(result, sort_keys=True))
    return 0 if all(result["state"] == "ok" and result.get("route", "ok") == "ok"
                    and result.get("environment_follow", {}).get("state", "ok") in settled
                    for result in results) else 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(main())
