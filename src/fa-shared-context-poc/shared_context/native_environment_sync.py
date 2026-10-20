"""Server drivers for the existing N1 follow; no separate operation or session authority."""

from concurrent.futures import ThreadPoolExecutor
import logging
import time

from .behavior_native_sessions import native_sessions
from .behavior_native_turn_wire import NativeEnvironmentCommand, material_digest
from .errors import Problem

FOLLOW_BATCH = 10
FOLLOW_DELAY_SECONDS = 2
FOLLOW_WAIT_SECONDS = 1200
log = logging.getLogger(__name__)


def automatic_environment_command(target, version):
    return NativeEnvironmentCommand(
        request_id=material_digest({"kind": "fa-server-environment-v1", "agent": target.agent_id, "version": version}),
        expected_agent_version=version, idle_only=True)


def _scoped_client(composition, scope, agent_id):
    """``((peer, client), None)``, or ``(None, skipped)`` for a configured executor Agent (``targets[1:]``) that has no
    scope: follow and verify share this one rule."""
    from .native_agent_api import NativeAgentRelayGateway

    try:
        return NativeAgentRelayGateway(composition, enabled=True)._client(scope, prepared=False), None
    except Problem as error:
        if (error.body["error"]["code"] == "AGENT_SCOPE_MISSING"
                and any(target.agent_id == agent_id for target in composition.config.targets[1:])):
            return None, {"agent_id": agent_id, "state": "skipped", "reason": "AGENT_SCOPE_MISSING"}
        raise


def follow_agent(composition, agent_id, *, wait_seconds=FOLLOW_WAIT_SECONDS, sleep=time.sleep, clock=time.monotonic):
    from .behavior_team_registry import resolve_agent_scope

    if composition.runtime.work_policy is None:
        raise Problem(503, "WORK_POLICY_DISABLED", "the installed standing policy is required for server following")
    scope = resolve_agent_scope(composition.runtime.store, composition.config, agent_id)
    pair, skipped = _scoped_client(composition, scope, agent_id)
    if skipped is not None:
        return skipped
    peer, client = pair
    control, sessions = peer.runtime.work_policy, native_sessions(peer)
    version = sessions.selected_version(client)
    command = automatic_environment_command(client.target, version)

    def current():
        live = resolve_agent_scope(composition.runtime.store, composition.config, agent_id)
        if live != scope or control.installed() != control.policy or not control.policy.active or not control.policy.read_enabled:
            raise Problem(403, "WORK_POLICY_REVOKED", "the original Agent authority changed during version following")

    def complete(view):
        from .behavior_native_turn_api import NativeTurnGateway
        from .realtime_push import realtime_publisher

        if view.files_manifest_sha256 is not None:
            current()
            try:
                # A ready handoff delivers work: the selector is read now, not taken from the reader window.
                NativeTurnGateway._signal_ready_handoffs(
                    sessions.current(client, checked_since=time.monotonic()), view)
            except Problem as error:
                log.warning("NATIVE_CONTINUATION_HANDOFF_PENDING code=%s", error.body["error"]["code"])
        try:
            publisher = realtime_publisher(composition)
        except Exception:
            publisher = None
            log.warning("FA_REALTIME_PUBLISH_FAILED type=agent.environment reason=configuration")
        if publisher is not None:
            publisher.notify(scope.team_id, "agent.environment", agent_id=agent_id, version=view.hosted_agent_version)
        return _result(view, "ok", clock() - started, _observed(sessions, client, view.hosted_session_id))

    deadline, batch, baseline, waiting = clock() + wait_seconds, 0, None, False
    started = clock()
    while True:
        current()
        if sessions.selected_version(client) != version:
            raise Problem(409, "NATIVE_ENVIRONMENT_CONFLICT", "the published version changed during this follow")
        if waiting:
            view = sessions.observe_automatic(client, command, before_send=current)
            if view.state not in {"ready", "current"} and view.progress_token == baseline:
                if clock() >= deadline:
                    return _result(view, "pending", clock() - started)
                sleep(FOLLOW_DELAY_SECONDS)
                continue
            baseline, batch, waiting = view.progress_token, 0, False
        else:
            view = sessions.advance(client, command, before_send=current)
            batch += 1
        if view.files_reconcile_required:
            raise Problem(503, "NATIVE_FILES_UNKNOWN", "the original file effect requires reconciliation")
        if view.reason == "task_access_denied":
            raise Problem(403, "NATIVE_ENVIRONMENT_UNAVAILABLE", "the original task observation was denied")
        if view.state in {"ready", "current"}:
            return complete(view)
        if batch == 1 and baseline is None:
            observed = sessions.observe_automatic(client, command, before_send=current)
            if observed.state in {"ready", "current"}:
                return complete(observed)
            baseline = observed.progress_token
        if clock() >= deadline:
            return _result(view, "pending", clock() - started)
        if batch >= FOLLOW_BATCH:
            observed = sessions.observe_automatic(client, command, before_send=current)
            if observed.state in {"ready", "current"}:
                return complete(observed)
            waiting = observed.progress_token == baseline
            baseline, batch = observed.progress_token, 0
        sleep(FOLLOW_DELAY_SECONDS)


def _observed(sessions, client, session_id):
    """The platform's version for ``session_id`` (one metadata read), or None when it cannot be read: a failed read
    leaves the result as it was. ADR-0009 "平台實際行為": an idle-resumed SID can run another version than it was
    created on."""
    try:
        view = sessions.environment_view(client)
    except Problem as error:
        log.warning("NATIVE_OBSERVED_VERSION_UNKNOWN code=%s", error.body["error"]["code"])
        return None
    return next((item.hosted_agent_version for item in view.observed_sessions
                 if item.hosted_session_id == session_id), None)


def _result(view, state, elapsed, observed=None):
    """Report only: a SID the platform runs on another version than expected is ``drifted``, never ``ok``."""
    if state == "ok" and observed not in {None, view.hosted_agent_version}:
        state = "drifted"
    return {"agent_id": view.agent_id, "version": view.hosted_agent_version, "state": state,
            "observed_version": observed, "environment": view.model_dump(mode="json"),
            "elapsed_ms": round(elapsed * 1000)}


def verify_agent(composition, agent_id):
    """ADR-0009 2d-i P9 hook (deploy ``follow_all`` becomes verify-only once the background cut moves each Agent):
    platform metadata only (``environment_view``: the session listing and the selected version's ``get_session``), no
    session call, no follow. ``ok`` when the selected version's session (or the binding row's current SID) is
    usable, else ``pending`` (the Agent moves after its next turn end); ``drifted`` when that SID's observed version
    is not the selected one."""
    from .behavior_team_registry import resolve_agent_scope

    scope = resolve_agent_scope(composition.runtime.store, composition.config, agent_id)
    pair, skipped = _scoped_client(composition, scope, agent_id)
    if skipped is not None:
        return skipped
    peer, client = pair
    view = native_sessions(peer).environment_view(client)
    observed = next((item.hosted_agent_version for item in view.observed_sessions
                     if item.hosted_session_id == view.hosted_session_id), None)
    state = "ok" if view.state == "unverified" else "pending"
    if observed not in {None, view.hosted_agent_version}:
        state = "drifted"  # report only: the platform runs this SID on another version than the selected one
    return {"agent_id": agent_id, "version": view.hosted_agent_version, "verify_only": True, "state": state,
            "observed_version": observed, "environment": view.model_dump(mode="json")}


def follow_all(composition, *, agent_id=None, published=None, wait_seconds=FOLLOW_WAIT_SECONDS,
               sleep=time.sleep, clock=time.monotonic, verify_only=False):
    """``verify_only``: the P9 hook; each Agent is only checked (``verify_agent``), never followed."""
    from .behavior_agent_versions import _agents

    _, served = _agents(composition)
    identifiers = list(dict.fromkeys(target.agent_id for target, _ in served
                                    if (agent_id is None or target.agent_id == agent_id)
                                    and (published is None or target.agent_id in published)))
    if not identifiers:
        from .behavior_team_registry import retired_agent_ids
        retired = agent_id is not None and agent_id in retired_agent_ids(composition.runtime.store, composition.config)
        return [{"agent_id": agent_id, "state": "failed", "reason": "AGENT_RETIRED" if retired else "AGENT_NOT_FOUND"}]

    def following(identifier):
        try:
            result = (verify_agent(composition, identifier) if verify_only else
                      follow_agent(composition, identifier, wait_seconds=wait_seconds, sleep=sleep, clock=clock))
        except Problem as error:
            result = {"agent_id": identifier, "state": "failed", "reason": error.body["error"]["code"]}
        (log.info if result["state"] in {"ok", "skipped"} else log.warning)(
            "NATIVE_SERVER_FOLLOW agent_id=%s state=%s reason=%s",
            identifier, result["state"], result.get("reason", result.get("environment", {}).get("reason")))
        return result

    # Each Agent keeps its own N1 admission/CAS; a waiting Agent must not block another Agent's rollout.
    with ThreadPoolExecutor(max_workers=len(identifiers), thread_name_prefix="fa-environment") as pool:
        return list(pool.map(following, identifiers))
