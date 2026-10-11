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


def follow_agent(composition, agent_id, *, wait_seconds=FOLLOW_WAIT_SECONDS, sleep=time.sleep, clock=time.monotonic):
    from .behavior_team_registry import resolve_agent_scope
    from .native_agent_api import NativeAgentRelayGateway

    if composition.runtime.work_policy is None:
        raise Problem(503, "WORK_POLICY_DISABLED", "the installed standing policy is required for server following")
    scope = resolve_agent_scope(composition.runtime.store, composition.config, agent_id)
    try:
        peer, client = NativeAgentRelayGateway(composition, enabled=True)._client(scope, prepared=False)
    except Problem as error:
        if (error.body["error"]["code"] == "AGENT_SCOPE_MISSING"
                and any(target.agent_id == agent_id for target in composition.config.targets[1:])):
            return {"agent_id": agent_id, "state": "skipped", "reason": "AGENT_SCOPE_MISSING"}
        raise
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
                NativeTurnGateway._signal_ready_handoffs(sessions.current(client), view)
            except Problem as error:
                log.warning("NATIVE_CONTINUATION_HANDOFF_PENDING code=%s", error.body["error"]["code"])
        try:
            publisher = realtime_publisher(composition)
        except Exception:
            publisher = None
            log.warning("FA_REALTIME_PUBLISH_FAILED type=agent.environment reason=configuration")
        if publisher is not None:
            publisher.notify(scope.team_id, "agent.environment", agent_id=agent_id, version=view.hosted_agent_version)
        return _result(view, "ok", clock() - started)

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


def _result(view, state, elapsed):
    return {"agent_id": view.agent_id, "version": view.hosted_agent_version, "state": state,
            "environment": view.model_dump(mode="json"), "elapsed_ms": round(elapsed * 1000)}


def follow_all(composition, *, agent_id=None, published=None, wait_seconds=FOLLOW_WAIT_SECONDS,
               sleep=time.sleep, clock=time.monotonic):
    from .behavior_agent_versions import _agents

    _, served = _agents(composition)
    identifiers = list(dict.fromkeys(target.agent_id for target, _ in served
                                    if (agent_id is None or target.agent_id == agent_id)
                                    and (published is None or target.agent_id in published)))
    if not identifiers:
        return [{"agent_id": agent_id, "state": "failed", "reason": "AGENT_NOT_FOUND"}]

    def following(identifier):
        try:
            result = follow_agent(composition, identifier, wait_seconds=wait_seconds, sleep=sleep, clock=clock)
        except Problem as error:
            result = {"agent_id": identifier, "state": "failed", "reason": error.body["error"]["code"]}
        (log.info if result["state"] in {"ok", "skipped"} else log.warning)(
            "NATIVE_SERVER_FOLLOW agent_id=%s state=%s reason=%s",
            identifier, result["state"], result.get("reason", result.get("environment", {}).get("reason")))
        return result

    # Each Agent keeps its own N1 admission/CAS; a waiting Agent must not block another Agent's rollout.
    with ThreadPoolExecutor(max_workers=len(identifiers), thread_name_prefix="fa-environment") as pool:
        return list(pool.map(following, identifiers))
