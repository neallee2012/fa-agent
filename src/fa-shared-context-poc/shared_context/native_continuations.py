"""A bounded durable handoff from official recovery; never a scan of historical UNKNOWN turns."""

import logging
import hashlib
import json
from typing import Literal

from .behavior_native_turn_wire import check_continuation_link, continuation_request_id, material_digest
from .behavior_sdk_state import NativeCheckpointRef
from .behavior_wire import Digest, Identifier, WireModel
from .errors import Problem
from .native_continuation_wire import (
    CONTINUATION_STOP_STATUS, MAX_AUTOMATIC_CONTINUATIONS, NativeContinuation, NativeContinuationView,
    NativeContinuationPage, NativeContinuationQuery,
)

log = logging.getLogger(__name__)


class _Intent(WireModel):
    kind: Literal["fa-native-continuation-intent-v1"] = "fa-native-continuation-intent-v1"
    view: NativeContinuationView
    source_submit_digest: Digest
    source_run_token: Identifier
    source_turn: int | None
    checkpoint: NativeCheckpointRef | None


class _Stop(WireModel):
    kind: Literal["fa-native-continuation-stop-v1"] = "fa-native-continuation-stop-v1"
    agent_id: Identifier
    conversation_id: Identifier
    root_request_id: Identifier
    failure_code: Identifier = "NATIVE_TURN_CANCELLED"


class _Handoffs(WireModel):
    kind: Literal["fa-native-continuation-handoffs-v1"] = "fa-native-continuation-handoffs-v1"
    agent_id: Identifier
    request_ids: tuple[Identifier, ...] = ()


async def pending_handoffs(runtime, *, add=None, remove=None):
    from .native_turns import TURNS_STORE, _UNCERTAIN, _decode, _json
    from azure.ai.agentserver.core.storage import FoundryStoragePreconditionError

    key = "continuation-handoffs"
    async with runtime._store(TURNS_STORE) as store:
        for _ in range(3):
            item = await store.get_item(key)
            value = _Handoffs(agent_id=runtime.identity.agent_id) if item is None else _decode(item.value, _Handoffs)
            if value.agent_id != runtime.identity.agent_id or len(set(value.request_ids)) != len(value.request_ids):
                raise Problem(409, "NATIVE_CONTINUATION_CONFLICT", "the pending handoffs belong to another Agent")
            ids = tuple(rid for rid in value.request_ids if rid != remove)
            if add is not None and add not in ids:
                ids = (*ids, add)
            if ids == value.request_ids:
                return value.request_ids
            changed = value.model_copy(update={"request_ids": ids})
            try:
                if item is None:
                    await store.create_item(key, _json(changed))
                else:
                    await store.set_item(key, _json(changed), if_match=item.etag)
                return changed.request_ids
            except _UNCERTAIN as error:
                observed = await store.get_item(key)
                if observed is not None and _decode(observed.value, _Handoffs) == changed:
                    return changed.request_ids
                if not isinstance(error, FoundryStoragePreconditionError):
                    raise Problem(503, "NATIVE_CONTINUATION_UNCONFIRMED", "the original handoff references are unconfirmed") from None
    raise Problem(503, "NATIVE_CONTINUATION_UNCONFIRMED", "the original handoff references remain conflicted")


async def files_ready(runtime):
    current, _ = await runtime._agent_record()
    env = current.environment
    return bool(
        env is not None and env.state == "ready" and getattr(env, "files_manifest_sha256", None) is not None
        and env.agent_id == current.agent_id == runtime.identity.agent_id
        and env.hosted_session_id == current.hosted_session_id == runtime.identity.session_id
        and env.hosted_agent_version == runtime.identity.agent_version)


async def ready_handoffs(runtime):
    """Only called after a ready control POST, including its idempotent success path."""
    if not await files_ready(runtime):
        return
    for rid in await pending_handoffs(runtime):
        try:
            source = await runtime.read(rid)
            view = await read_continuation(runtime, rid)
            if await stop_requested(runtime, source):
                await stop_handoff(runtime, source)
                continue
            if view.state == "blocked":
                await pending_handoffs(runtime, remove=rid)
                continue
            if view.state == "admitted":
                child = await runtime.read(view.request_id)
                if child.status != "accepted":
                    await pending_handoffs(runtime, remove=rid)
                    continue
            await schedule_continuation(runtime, source)
        except Problem as error:
            # Environment readiness and continuation delivery are separate outcomes.
            log.warning("NATIVE_CONTINUATION_HANDOFF_PENDING code=%s", error.body["error"]["code"])


async def acknowledge_ready(runtime, command):
    from .behavior_native_turn_wire import continuation_ready_id
    from .native_continuation_wire import NativeContinuationReadyReceipt

    if not await files_ready(runtime):
        raise Problem(409, "NATIVE_CONTINUATION_WORKSPACE_REQUIRED", "the original workspace is not freshly ready")
    current, _ = await runtime._agent_record()
    env = current.environment
    if ((env.request_id, env.hosted_session_id, env.files_manifest_sha256) != (
            command.environment_request_id, command.hosted_session_id, command.files_manifest_sha256)
            or command.request_id != continuation_ready_id(
                command.environment_request_id, command.hosted_session_id, command.files_manifest_sha256)):
        raise Problem(409, "NATIVE_CONTINUATION_CONFLICT", "the ready control differs from the published file handoff")
    await ready_handoffs(runtime)
    return NativeContinuationReadyReceipt(
        agent_id=runtime.identity.agent_id, hosted_session_id=runtime.identity.session_id,
        request_id=command.request_id, environment_request_id=env.request_id,
        pending_request_ids=await pending_handoffs(runtime))


def _stop(receipt):
    return _Stop(agent_id=receipt.agent_id, conversation_id=receipt.conversation_id,
                 root_request_id=receipt.request_id if receipt.continuation is None else receipt.continuation.root_request_id)


def _stop_key(value):
    return "continuation-stop:" + material_digest({"rid": value.root_request_id})


async def _read_stop(runtime, receipt):
    from .native_turns import TURNS_STORE, _decode

    expected = _stop(receipt)
    async with runtime._store(TURNS_STORE) as store:
        item = await store.get_item(_stop_key(expected))
    if item is None:
        return None
    value = _decode(item.value, _Stop)
    if value.model_dump(exclude={"failure_code"}) != expected.model_dump(exclude={"failure_code"}):
        raise Problem(409, "NATIVE_CONTINUATION_CONFLICT", "the continuation stop belongs to another original root")
    return value


async def stop_requested(runtime, receipt):
    return await _read_stop(runtime, receipt) is not None


async def stop_handoff(runtime, receipt, *, failure_code="NATIVE_TURN_CANCELLED"):
    from .behavior_native_turn_wire import NativeCancel
    from .native_turns import TURNS_STORE

    try:
        await read_intent(runtime, receipt.request_id)
    except Problem as error:
        if error.body["error"]["code"] == "NATIVE_CONTINUATION_MISSING":
            return
        raise
    abandoned = active_child = None
    async with runtime._admission_lock:
        stopped = await _read_stop(runtime, receipt)
        if stopped is None:
            stopped = _stop(receipt).model_copy(update={"failure_code": failure_code})
            async with runtime._store(TURNS_STORE) as store:
                await runtime._immutable(store, _stop_key(stopped), stopped)
        current = receipt
        for _ in range(MAX_AUTOMATIC_CONTINUATIONS):
            view = await _read_continuation(runtime, current.request_id)
            if view.request_id is None:
                return
            if view.state == "blocked":
                abandoned = current
                break
            if view.state != "admitted":
                raise Problem(503, "NATIVE_CONTINUATION_UNCONFIRMED",
                              "the stopped continuation's original admission remains unconfirmed")
            child = await runtime.read(view.request_id)
            if child.status in {"accepted", "started"}:
                active_child = child
                break
            try:
                await read_intent(runtime, child.request_id)
            except Problem as error:
                if error.body["error"]["code"] == "NATIVE_CONTINUATION_MISSING":
                    return
                raise
            current = child
    if active_child is not None:
        if active_child.hosted_session_id == runtime.identity.session_id:
            cancelled = await runtime.cancel(NativeCancel(
                conversation_id=active_child.conversation_id, request_id=active_child.request_id))
            if cancelled.status == "accepted":
                original = await runtime._rid(active_child.request_id)
                if original.value.receipt.status == "accepted":
                    await runtime._notify_terminal(await runtime._unknown(original, stopped.failure_code))
        return
    if abandoned is not None:
        await runtime._notify_terminal(abandoned, continuation_stopped=True)
        await pending_handoffs(runtime, remove=abandoned.request_id)


def _key(rid):
    return "continuation:" + material_digest({"rid": rid})


async def _save_intent(runtime, value):
    from .native_turns import TURNS_STORE, _UNCERTAIN, _decode, _json

    key = _key(value.view.original_request_id)
    try:
        async with runtime._store(TURNS_STORE) as store:
            try:
                await store.create_item(key, _json(value))
            except _UNCERTAIN:
                pass
            original = await store.get_item(key)
            if original is None:
                raise Problem(503, "NATIVE_CONTINUATION_UNCONFIRMED",
                              "the original handoff write is unconfirmed; no new input was dispatched")
            if _decode(original.value, _Intent) != value:
                raise Problem(409, "NATIVE_CONTINUATION_CONFLICT", "the original handoff contains different material")
    except Problem as error:
        if error.body["error"]["code"] == "NATIVE_STATE_STORE_UNKNOWN":
            raise Problem(503, "NATIVE_CONTINUATION_UNCONFIRMED", "the original handoff could not be read back") from None
        raise


async def read_intent(runtime, rid):
    from .native_turns import TURNS_STORE, _decode

    async with runtime._store(TURNS_STORE) as store:
        item = await store.get_item(_key(rid))
    if item is None:
        raise Problem(404, "NATIVE_CONTINUATION_MISSING", "no official recovery intent exists for this original RID")
    value = _decode(item.value, _Intent)
    view = value.view
    if (view.agent_id, view.original_request_id) != (runtime.identity.agent_id, rid):
        raise Problem(409, "NATIVE_CONTINUATION_CONFLICT", "the original continuation scope differs")
    if view.continuation is not None:
        ref = view.continuation
        if (ref.continuation_of != rid or ref.root_request_id != view.root_request_id
                or ref.source_submit_digest != value.source_submit_digest
                or view.request_id != continuation_request_id(
                    view.agent_id, view.conversation_id, ref.root_request_id, ref.attempt)
                or value.checkpoint is None or ref.checkpoint_sha256 != value.checkpoint.sha256):
            raise Problem(409, "NATIVE_CONTINUATION_CONFLICT", "the derived request differs from its original intent")
    return value


async def read_continuation(runtime, rid):
    async with runtime._admission_lock:
        return await _read_continuation(runtime, rid)


async def _read_continuation(runtime, rid):
    from .native_turns import _changed

    intent = await read_intent(runtime, rid)
    view = intent.view
    original = await runtime.read(view.original_request_id)
    stopped = await _read_stop(runtime, original)
    if stopped is not None:
        view = _changed(view, cancel_requested=stopped.failure_code == "NATIVE_TURN_CANCELLED",
                        failure_code=stopped.failure_code)
    if view.request_id is None:
        return view
    before = await runtime._agent_record() if stopped is not None else None
    try:
        child = await runtime.read(view.request_id)
    except Problem as error:
        if error.body["error"]["code"] != "NATIVE_RID_MISSING":
            raise
        if before is not None:
            after = await runtime._agent_record()
            if before == after and before[0].admitting is None:
                return _changed(view, state="blocked")
            raise Problem(503, "NATIVE_CONTINUATION_UNCONFIRMED",
                          "the stopped continuation's original admission remains unconfirmed")
        if original.hosted_session_id != runtime.identity.session_id and not await files_ready(runtime):
            return _changed(view, failure_code="NATIVE_CONTINUATION_WORKSPACE_REQUIRED")
        return view
    if (child.agent_id, child.conversation_id, child.continuation) != (
            view.agent_id, view.conversation_id, view.continuation):
        raise Problem(409, "NATIVE_CONTINUATION_CONFLICT", "the admitted continuation differs from its original intent")
    return _changed(view, state="admitted", target_hosted_session_id=child.hosted_session_id)


async def prepare_continuation(runtime, original, *, failure_code):
    from .native_checkpoint import conversation_store_name, verify_checkpoint
    from .native_effects import NativeEffects
    from .native_turns import TURNS_STORE

    receipt = original.value.receipt
    if receipt.status != "started":
        return receipt
    try:
        intent = await read_intent(runtime, receipt.request_id)
    except Problem as error:
        if error.body["error"]["code"] != "NATIVE_CONTINUATION_MISSING":
            raise
        intent = None
    if intent is None:
        ref = receipt.continuation
        root = receipt.request_id if ref is None else ref.root_request_id
        attempt = 1 if ref is None else ref.attempt + 1
        checkpoint = None
        reason = None
        try:
            if runtime.on_continuation is None:
                raise Problem(409, "NATIVE_CONTINUATION_UNAVAILABLE", "the continuation dispatcher is not configured")
            if receipt.cancel_requested or await stop_requested(runtime, receipt):
                raise Problem(409, "NATIVE_TURN_CANCELLED", "the original request was cancelled")
            if attempt > MAX_AUTOMATIC_CONTINUATIONS:
                raise Problem(409, "NATIVE_CONTINUATION_LIMIT", "the original root exhausted its two continuations")
            if receipt.delegation is not None or receipt.queued_relay is not None:
                raise Problem(409, "NATIVE_CONTINUATION_PEER_PENDING", "the original peer result owns continuation")
            meta, _ = await runtime._meta(receipt.conversation_id)
            if meta.rid != receipt.request_id or meta.n != receipt.turn_number:
                raise Problem(409, "NATIVE_CONTINUATION_ADVANCED", "another turn has advanced this conversation")
            if meta.base_ckpt is None:
                raise Problem(409, "NATIVE_CONTINUATION_CHECKPOINT_REQUIRED", "no previous lawful checkpoint is available")
            checkpoint = meta.base_ckpt
            async with runtime._store(conversation_store_name(receipt.conversation_id)) as store:
                await verify_checkpoint(store, runtime._scope(meta), checkpoint,
                                        checkpoint_profile=receipt.capture.template.settings.checkpoint_profile)
            await NativeEffects(runtime, receipt).operation_records()
        except Problem as error:
            reason = error.body["error"]["code"]
            log.warning("NATIVE_CONTINUATION_BLOCKED code=%s", reason)
        continuation = (None if reason is not None else NativeContinuation(
            continuation_of=receipt.request_id, root_request_id=root, attempt=attempt,
            source_submit_digest=receipt.submit_digest, checkpoint_sha256=checkpoint.sha256))
        view = NativeContinuationView(
            agent_id=receipt.agent_id, conversation_id=receipt.conversation_id,
            original_request_id=receipt.request_id, root_request_id=root,
            request_id=None if continuation is None else continuation_request_id(
                receipt.agent_id, receipt.conversation_id, root, attempt),
            continuation=continuation, state="blocked" if reason is not None else "prepared",
            failure_code=reason, source_hosted_session_id=receipt.hosted_session_id)
        intent = _Intent(view=view, source_submit_digest=receipt.submit_digest,
                         source_run_token=original.value.run_token, source_turn=receipt.turn_number,
                         checkpoint=checkpoint)
        try:
            await _save_intent(runtime, intent)
        except Problem as error:
            if error.body["error"]["code"] == "NATIVE_CONTINUATION_UNCONFIRMED":
                return await runtime._unknown(original, "NATIVE_CONTINUATION_UNCONFIRMED")
            raise
    if (intent.source_submit_digest, intent.source_run_token) != (receipt.submit_digest, original.value.run_token):
        raise Problem(409, "NATIVE_CONTINUATION_CONFLICT", "recovery cannot replace its original started execution")
    return await runtime._unknown(original, failure_code)


async def schedule_continuation(runtime, receipt):
    from .native_turns import NativePostTurnPending, NativeTaskInput, _TASK_TRANSPORT_ERRORS
    from azure.ai.agentserver.core.tasks import TaskConflictError

    if receipt.status != "unknown":
        return
    try:
        view = (await read_intent(runtime, receipt.request_id)).view
        if view.state == "blocked" or view.request_id is None:
            return
        if await stop_requested(runtime, receipt):
            await stop_handoff(runtime, receipt)
            return
        await pending_handoffs(runtime, add=receipt.request_id)
        await runtime.enqueue.start(
            task_id="continuation-" + material_digest({
                "rid": view.request_id, "session_id": runtime.identity.session_id})[:48],
            input=NativeTaskInput(request_id=view.request_id, accepted_etag="continuation-intent",
                                  continuation_of=receipt.request_id))
    except TaskConflictError:
        log.info("NATIVE_CONTINUATION_RETAINED")
    except Problem as error:
        if error.body["error"]["code"] == "NATIVE_CONTINUATION_MISSING":
            return
        if error.status >= 500:
            raise NativePostTurnPending() from None
        raise
    except _TASK_TRANSPORT_ERRORS:
        raise NativePostTurnPending() from None


async def dispatch_continuation(runtime, supplied):
    from .native_turns import NativePostTurnPending

    try:
        view = await read_continuation(runtime, supplied.continuation_of)
    except Problem as error:
        if error.body["error"]["code"] == "NATIVE_CONTINUATION_MISSING":
            return supplied.request_id
        raise
    original = await runtime.read(view.original_request_id)
    if await stop_requested(runtime, original):
        await stop_handoff(runtime, original)
        raise Problem(409, view.failure_code or "NATIVE_TURN_CANCELLED", "the original continuation was stopped")
    if view.request_id is None or view.continuation is None:
        return supplied.request_id
    if view.request_id != supplied.request_id or view.state == "blocked":
        raise Problem(409, "NATIVE_CONTINUATION_CONFLICT", "the queued continuation differs from its original intent")
    if view.state == "admitted":
        child = await runtime.read(view.request_id)
        if child.status != "accepted":
            await pending_handoffs(runtime, remove=original.request_id)
            return child.request_id
    await pending_handoffs(runtime, add=original.request_id)
    if runtime.on_continuation is None:
        raise Problem(503, "NATIVE_CONTINUATION_UNAVAILABLE", "the original continuation dispatcher is unavailable")
    try:
        value = await runtime.on_continuation(view)
    except Problem as error:
        if (await stop_requested(runtime, original)
                or error.status == CONTINUATION_STOP_STATUS.get(error.body["error"]["code"])):
            await stop_handoff(runtime, original, failure_code=error.body["error"]["code"])
        if error.status >= 500:
            raise NativePostTurnPending() from None
        raise
    if (value.request_id, value.conversation_id, value.continuation) != (
            view.request_id, view.conversation_id, view.continuation):
        raise Problem(409, "NATIVE_CONTINUATION_CONFLICT", "the dispatcher returned another input")
    if value.status == "accepted" and value.dispatch is not None and value.dispatch.state != "already_queued":
        raise NativePostTurnPending()
    return value.request_id


async def check_submission(runtime, request):
    intent = await read_intent(runtime, request.continuation.continuation_of)
    view = intent.view
    if (view.state == "blocked" or request.continuation != view.continuation
            or request.command.request_id != view.request_id
            or request.command.conversation_id != view.conversation_id):
        raise Problem(409, "NATIVE_CONTINUATION_CONFLICT", "submission must retain its official recovery intent")
    source = await runtime._rid(view.original_request_id)
    original = source.value.receipt
    if await stop_requested(runtime, original):
        raise Problem(409, "NATIVE_TURN_CANCELLED", "the original root was stopped before continuation admission")
    expected_command = original.command.model_copy(update={
        "request_id": view.request_id, "conversation_id": view.conversation_id})
    if (original.status != "unknown" or original.cancel_requested
            or (original.submit_digest, source.value.run_token) != (
                intent.source_submit_digest, intent.source_run_token)
            or request.command != expected_command or request.source != original.source
            or request.state_profile != original.state_profile):
        raise Problem(409, "NATIVE_CONTINUATION_CONFLICT", "the continuation cannot rewrite or replace original material")
    try:
        existing = await runtime.read(view.request_id)
    except Problem as error:
        if error.body["error"]["code"] != "NATIVE_RID_MISSING":
            raise
        existing = None
    if existing is None:
        meta, _ = await runtime._meta(view.conversation_id)
        if meta.rid != original.request_id or meta.n != intent.source_turn or meta.base_ckpt != intent.checkpoint:
            raise Problem(409, "NATIVE_CONTINUATION_ADVANCED", "the original conversation has advanced")
        if original.hosted_session_id != runtime.identity.session_id and not await files_ready(runtime):
            raise Problem(409, "NATIVE_CONTINUATION_WORKSPACE_REQUIRED",
                          "a different session requires the original verified workspace handoff")


async def original_chain(runtime, receipt):
    current = receipt
    chain = []
    for _ in range(MAX_AUTOMATIC_CONTINUATIONS):
        ref = current.continuation
        if ref is None:
            break
        intent = await read_intent(runtime, ref.continuation_of)
        source = await runtime.read(ref.continuation_of)
        check_continuation_link(current, source)
        previous = source.continuation
        if (intent.view.request_id != current.request_id or intent.view.continuation != ref
                or source.status != "unknown" or source.submit_digest != ref.source_submit_digest
                or (source.agent_id, source.conversation_id) != (receipt.agent_id, receipt.conversation_id)
                or ref.attempt != (1 if previous is None else previous.attempt + 1)
                or ref.root_request_id != (source.request_id if previous is None else previous.root_request_id)):
            raise Problem(409, "NATIVE_CONTINUATION_CONFLICT", "the original continuation chain differs")
        chain.append(source)
        current = source
    if current.continuation is not None:
        raise Problem(409, "NATIVE_CONTINUATION_LIMIT", "the saved continuation chain exceeds its root bound")
    return tuple(reversed(chain))


async def has_handoff(runtime, receipt):
    if not receipt.continuation_available:
        return False
    try:
        return (await read_continuation(runtime, receipt.request_id)).owns_outcome
    except Problem as error:
        if error.body["error"]["code"] == "NATIVE_CONTINUATION_MISSING":
            return False
        raise


async def prior_operations(runtime, receipt):
    from .native_effects import NativeEffects

    records = []
    for original in await original_chain(runtime, receipt):
        records.extend(await NativeEffects(runtime, original).operation_records())
    return tuple(records)


async def read_human_context(client, originals):
    from .native_hitl_wire import NativeHitlPendingQuery, NativeHitlTurn

    items = []
    for original in originals:
        turn = NativeHitlTurn(agent_id=original.agent_id, conversation_id=original.conversation_id,
                             request_id=original.request_id)
        for kind, reader in (("question", client.questions), ("approval", client.pending)):
            after = 0
            while True:
                page = await reader(NativeHitlPendingQuery(turn=turn, after=after))
                if page.turn != turn:
                    raise Problem(403, "NATIVE_CONTINUATION_CONFLICT", "the human context belongs to another turn")
                for reference in page.references:
                    if kind == "question":
                        view = await client.read_question(reference.question_id)
                        if view.reference != reference:
                            raise Problem(409, "NATIVE_CONTINUATION_CONFLICT", "the original question reference changed")
                        call = view.reference.call
                        value = {
                            "type": kind, "reference": view.reference.model_dump(mode="json"),
                            "question": view.question.command.question.model_dump(mode="json"),
                            "answer": None if view.answer is None else view.answer.model_dump(mode="json"),
                            "status": view.status, "expires_at": view.question.expires_at.isoformat(),
                            "expired": view.expired, "turn_closed": view.turn_closed, "authorization": False,
                        }
                    else:
                        view = await client.read(reference.pending_id)
                        if (view.approval.pending_id, view.approval.pending_digest) != (
                                reference.pending_id, reference.pending_digest):
                            raise Problem(409, "NATIVE_CONTINUATION_CONFLICT", "the original approval reference changed")
                        call = view.pending.command.action.call
                        value = {
                            "type": kind, "pending_id": reference.pending_id, "pending_digest": reference.pending_digest,
                            "call": call.model_dump(mode="json"), "status": view.status,
                            "expires_at": view.pending.expires_at.isoformat(),
                            "expired": view.expired, "authorization": False,
                            "purpose": "original-decision-not-a-new-call-grant",
                        }
                    if (call.agent_id, call.conversation_id, call.request_id) != (
                            turn.agent_id, turn.conversation_id, turn.request_id):
                        raise Problem(403, "NATIVE_CONTINUATION_CONFLICT", "the original human reference differs")
                    items.append(value)
                if page.next_after is None:
                    break
                if page.next_after <= after:
                    raise Problem(503, "NATIVE_CONTINUATION_UNAVAILABLE", "the original human context page did not advance")
                after = page.next_after
    return items


async def read_continuation_context(runtime, receipt, query: NativeContinuationQuery, *, hitl_client=None):
    from .native_append_wire import NativeAppendRead
    from .native_appends import read_append

    if receipt.continuation is None or receipt.agent_id != runtime.identity.agent_id:
        raise Problem(403, "NATIVE_CONTINUATION_CONFLICT", "only the original continuing turn may read its context")
    query = NativeContinuationQuery.model_validate_json(query.model_dump_json())
    chain = await original_chain(runtime, receipt)
    appends = []
    for original in chain:
        page = await read_append(runtime, NativeAppendRead(
            request_id=original.request_id, conversation_id=original.conversation_id))
        appends.extend(page.appends)
    records = await prior_operations(runtime, receipt)
    if query.section == "overview":
        if query.index != 0:
            raise Problem(422, "NATIVE_REQUEST_INVALID", "the recovery overview has one item")
        content = {
            "continuation": receipt.continuation.model_dump(mode="json"),
            "requests": [{"index": i, "request_id": item.request_id, "status": item.status}
                         for i, item in enumerate(chain)],
            "appends": [{"index": i, "request_id": item.request_id, "append_id": item.append_id,
                         "sequence": item.sequence, "status": item.status} for i, item in enumerate(appends)],
            "effects": [{"index": i, "request_id": item.request_id, "operation": item.operation.model_dump(mode="json"),
                         "status": item.status, "dispatch_started": item.dispatch_started,
                         "refusal_code": item.refusal_code} for i, item in enumerate(records)],
            "authorization": False,
        }
    elif query.section == "human":
        if hitl_client is None:
            raise Problem(503, "NATIVE_CONTINUATION_UNAVAILABLE", "the original human context reader is unavailable")
        items = await read_human_context(hitl_client, chain)
        if not items and query.index == 0:
            content = {"items": [], "authorization": False}
        elif query.index >= len(items):
            raise Problem(422, "NATIVE_REQUEST_INVALID", "the human reference is outside its original list")
        else:
            content = {"index": query.index, "total": len(items), "item": items[query.index], "authorization": False}
    else:
        items = {"request": chain, "append": appends, "effect": records}[query.section]
        if query.index >= len(items):
            raise Problem(422, "NATIVE_REQUEST_INVALID", "the recovery item is outside its original list")
        selected = items[query.index]
        if query.section == "request":
            content = {
                "request_id": selected.request_id, "command": selected.command.model_dump(mode="json"),
                "source": None if selected.source is None else selected.source.model_dump(mode="json"),
                **({"routine": selected.routine.model_dump(mode="json")} if selected.routine is not None else {}),
                "attached_text": (None if selected.capture.attached_text is None else
                                  selected.capture.attached_text.model_dump(mode="json")),
            }
        elif query.section == "append":
            content = selected.model_dump(mode="json")
        else:
            content = selected.model_dump(mode="json", exclude={"token", "hitl"})
    text = json.dumps(content, ensure_ascii=False, sort_keys=True, allow_nan=False)
    start = query.part * 4096
    if start >= len(text):
        raise Problem(422, "NATIVE_REQUEST_INVALID", "the recovery part is outside its original item")
    return NativeContinuationPage(
        root_request_id=receipt.continuation.root_request_id, continuation_of=receipt.continuation.continuation_of,
        attempt=receipt.continuation.attempt, section=query.section, index=query.index, part=query.part,
        next_part=query.part + 1 if start + 4096 < len(text) else None,
        text=text[start:start + 4096], sha256=hashlib.sha256(text.encode("utf-8")).hexdigest())
