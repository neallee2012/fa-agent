"""ADR-0009 §7a abandon-source switch: a separate path, not exemptions threaded through ``follow_session``.

The API decided from platform metadata and its binding row that the Agent's current session can not be reached
(incompatible, retired, or a drifted idle SID with only its own unfinished receipts left). This destination session
then commits: it never invokes, reads from or stops the source, and nothing running or queued on the source (``admitting``,
the chain task, steering inputs) is resumed, copied or waited for by the switch. Right after the commit it adopts what
the source accepted and never started (ADR-0009 M9, ``native_left_receipts.adopt_left``: same RID and request, run here
once); everything else the source left keeps its bytes until the stall rule marks it, and the API answers it from its
previous-SID list. No files move. Conversations continue from the shared history and checkpoints, which every session
of the Agent reads from the same ``fa/v1/turns`` and conversation stores.
"""

import asyncio
import logging
from time import monotonic

from .behavior_native_turn_wire import NativeEnvironmentView, NativeUnfinishedSessions, material_digest
from .errors import Problem

log = logging.getLogger(__name__)
_OBSERVE_SECONDS = 5
# ADR-0009 §7a (f): the one-time backfill scan stops here and reports itself incomplete (recorded as unverified).
_UNFINISHED_SECONDS = 3
_UNFINISHED_SESSIONS = 64


async def unfinished_sessions(runtime):
    """Read-only: SIDs other than the current one that still name an unfinished (accepted/started) receipt."""
    from .native_turns import TURNS_STORE

    current, _ = await runtime._agent_record()
    found, complete, after, deadline = [], True, None, monotonic() + _UNFINISHED_SECONDS
    async with runtime._store(TURNS_STORE) as store:
        while complete:
            page = await store.list_keys(limit=100, after=after)
            for key in page.keys:
                if not key.key.startswith("rid:"):
                    continue
                if monotonic() > deadline:
                    complete = False
                    break
                receipt = runtime._check_rid(await store.get_item(key.key), key.key[4:]).value.receipt
                sid = receipt.hosted_session_id
                if receipt.status in {"accepted", "started"} and sid != current.hosted_session_id and sid not in found:
                    if len(found) == _UNFINISHED_SESSIONS:
                        complete = False
                        break
                    found.append(sid)
            if not page.has_more:
                break
            if not page.last_id or page.last_id == after:
                complete = False
                break
            after = page.last_id
    return NativeUnfinishedSessions(agent_id=runtime.identity.agent_id, hosted_session_id=current.hosted_session_id,
                                    session_ids=tuple(found), complete=complete)


def _source_version(current):
    environment = current.environment
    if environment is not None:
        return environment.previous_agent_version if environment.state == "pending" else environment.hosted_agent_version
    if current.preparation is None:
        raise Problem(409, "NATIVE_ENVIRONMENT_UNAVAILABLE", "the Agent has no original prepared environment")
    return current.preparation.hosted_agent_version


async def _log_left_work(runtime, current):
    """Content-free audit of what the abandoned source still held (ADR-0009 §7a (b)(c)); never blocks the switch."""
    task, pending, active, status, lease = current.task_id, None, None, None, None
    observer = runtime.task_observer
    if observer is not None and hasattr(observer, "original"):
        try:
            async with asyncio.timeout(_OBSERVE_SECONDS):
                original = await observer.original(task_id=task)
            if isinstance(original, dict):
                status, lease = original.get("status"), original.get("lease") is not None
                payload = original.get("payload")
                steering = payload.get("steering") if isinstance(payload, dict) else None
                if isinstance(steering, dict):
                    inputs = steering.get("pending_inputs", [])
                    pending = len(inputs) if isinstance(inputs, list) else None
                    active = steering.get("active_input") is not None
        except Exception as error:  # noqa: BLE001 - an audit read never blocks the switch (CancelledError passes)
            log.warning("NATIVE_ENVIRONMENT_SOURCE_TASK_UNREAD type=%s", type(error).__name__)
    log.warning("NATIVE_ENVIRONMENT_SOURCE_WORK_LEFT task_id=%s task_status=%s lease=%s pending_inputs=%s "
                "active_input=%s admitting=%s", task, status, lease, pending, active,
                None if current.admitting is None else current.admitting.request_id[:8])


async def abandon_session(runtime, command):
    # ADR-0009 M9: the commit and the adoption of what the source never started run under the admission lock, so every
    # adopted message is queued before this session admits a new one.
    async with runtime._admission_lock:
        return await _abandon_session(runtime, command)


async def _abandon_session(runtime, command):
    from .native_environment import _save_agent
    from .native_left_receipts import adopt_left, record_retired
    from .native_turns import TURNS_STORE, _changed, binding_task_id

    me = runtime.identity.session_id
    current, etag = await runtime._agent_record()
    existing = current.environment
    if current.hosted_session_id == me:
        # A rerun after the commit (ADR-0009 §7a (j)) reads the same ready result; nothing is created twice. Its
        # adoption read finds only what an interrupted one left.
        if (existing is not None and existing.state == "ready" and existing.hosted_session_id == me
                and existing.previous_hosted_session_id == command.expected_session_id):
            await adopt_left(runtime, command.expected_session_id)
            return existing
        raise Problem(409, "NATIVE_ENVIRONMENT_CONFLICT", "this session already holds the Agent from another source")
    if (current.hosted_session_id, etag) != (command.expected_session_id, command.expected_etag):
        raise Problem(409, "NATIVE_ENVIRONMENT_CONFLICT", "the original Agent environment comparison changed")
    ready = NativeEnvironmentView(
        agent_id=runtime.identity.agent_id, request_id=command.request_id,
        previous_hosted_session_id=current.hosted_session_id, previous_agent_version=_source_version(current),
        previous_files_profile=None, hosted_session_id=me, hosted_agent_version=runtime.identity.agent_version,
        state="ready", files_migrated=False, files_skip_request_id=command.request_id,
        files_omission_basis="system_policy", files_omission_reason="source_not_migratable",
        source_invocation_refused=True,
        files_profile="fa-workspace-files-v1" if runtime.workspace_files is not None else None)
    if runtime.workspace_files is not None:
        runtime.workspace_files.initialize()
    async with runtime._store(TURNS_STORE) as store:
        await runtime._immutable(store, "environment:" + material_digest(ready), ready)
        await record_retired(runtime, store, command.expected_session_id, command.source_status)
    # No scan backs this commit, and it never touches the open-RID set: a marker this SID wrote when it held the Agent
    # before names that hold's follow, not this abandon, so its next follow clears it (``check_hold``) and scans.
    # A new chain task and an empty ``admitting``: nothing the source left can hold this session's admission.
    await _save_agent(runtime, current, etag, _changed(
        current, hosted_session_id=me, environment=ready, admitting=None, following_supported=True,
        chain_task_id=binding_task_id(runtime.identity.agent_id, me)))
    # After the commit, so the audit read never delays or blocks it.
    await _log_left_work(runtime, current)
    await adopt_left(runtime, command.expected_session_id)
    log.warning("NATIVE_ENVIRONMENT_SOURCE_ABANDONED source_session_id=%s source_version=%s session_id=%s version=%s",
                ready.previous_hosted_session_id, ready.previous_agent_version, me, ready.hosted_agent_version)
    return ready
