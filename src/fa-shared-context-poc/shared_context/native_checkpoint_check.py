"""ADR-0009 phase 4 U3 / U9 on the destination session, before it is the Agent's current SID.

``session_checkpoint_check`` reads back from the store the latest checkpoint of each named conversation;
``session_prerestore`` restores it into this session's local hot copy, so the conversation's next turn here skips the
store read (``load_checkpoint`` takes the hot copy). Both pick the checkpoint the next turn would (``_allocate``),
read receipts without claiming them, never write a receipt or the Agent record, never start the SDK or a model (a
prerestore leaves a conversation whose turn is queued or running alone and answers ``ok``), and
answer each conversation ``ok`` (also when no checkpoint is owed, ADR-0009 §7a (r)), ``unreadable`` or ``unsupported``
(rule 8: an unknown codec, version or shape is refused explicitly). Results are per conversation: a missing or
different checkpoint is that conversation's ``unreadable``, not the whole command's failure. Two cases do fail the
whole command: an unconfirmed read (5xx) and ``NATIVE_STATE_SCOPE`` (the API named another Agent's conversation) are
raised, not counted.
"""

from .errors import Problem

# CTX-03 refusals that mean this image does not know the format, as opposed to a checkpoint that is missing or differs.
_UNSUPPORTED = frozenset({"SDK_STATE_UNSUPPORTED_FORMAT", "NATIVE_STATE_INVALID"})
# No State store or meta yet (a receipt accepted, no turn numbered): no turn ever completed, so none is owed (§7a (r)).
_NONE_OWED = frozenset({"NATIVE_STATE_STORE_MISSING", "NATIVE_CONVERSATION_MISSING"})


async def _latest(runtime, cid, *, warm):
    from .native_checkpoint import conversation_store_name, load_checkpoint, verify_checkpoint
    from .native_environment import _CHECK_READ_SECONDS, _check_read, _check_store
    from .native_files_omission import check_latest_checkpoint

    async with _check_store(runtime, conversation_store_name(cid), "conversation") as store:
        meta, _ = await _check_read(runtime._meta(cid, store=store), "conversation")
        last = None if meta.rid is None else (await _check_read(runtime._rid(meta.rid), "receipt")).value.receipt
        checkpoint, profile = meta.base_ckpt, meta.capture.template.settings.checkpoint_profile
        if warm and last is not None and last.status in {"accepted", "started"}:
            # a turn is queued or running: its base_ckpt is about to be superseded, and warming it could replace the
            # newer hot copy that turn saves; its next turn restores as usual
            return
        if last is not None and last.status == "completed":
            if not warm:
                return await check_latest_checkpoint(runtime, last)
            checkpoint = (await _check_read(runtime._turn(store, last), "turn")).checkpoints[0]
            profile = last.capture.template.settings.checkpoint_profile
        if checkpoint is None:
            return
        if warm:  # each segment read bounded like the check's
            await _check_read(load_checkpoint(store, runtime._scope(meta), checkpoint, cache_root=runtime.cache_root,
                                              checkpoint_profile=profile, read_timeout=_CHECK_READ_SECONDS),
                              "checkpoint", bounded=False)
        else:  # the store itself: a hot copy never stands in for a missing segment
            await _check_read(verify_checkpoint(store, runtime._scope(meta), checkpoint, checkpoint_profile=profile,
                                                read_timeout=_CHECK_READ_SECONDS), "checkpoint", bounded=False)


async def read_back(runtime, command):
    from .behavior_native_turn_wire import NativePrewarmView, NativeSessionPrerestore

    results = {}
    for cid in command.conversation_ids:
        try:
            await _latest(runtime, cid, warm=isinstance(command, NativeSessionPrerestore))
            results[cid] = "ok"
        except Problem as error:
            code = error.body["error"]["code"]
            # 5xx is unconfirmed; NATIVE_STATE_SCOPE: the API named another Agent's conversation. Neither is an answer
            # about this image, so neither is counted as one.
            if error.status >= 500 or code == "NATIVE_STATE_SCOPE":
                raise
            results[cid] = ("ok" if code in _NONE_OWED else "unsupported" if code in _UNSUPPORTED else "unreadable")
    return NativePrewarmView(agent_id=runtime.identity.agent_id, hosted_session_id=runtime.identity.session_id,
                             toolboxes=0, checkpoints=results)
