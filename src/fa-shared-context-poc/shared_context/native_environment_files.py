"""File steps share the original follow item, its CAS and the original Agent binding."""

from .errors import Problem
from .native_files_wire import NativeFilesProgress, NativeFilesScope, NativeFilesView, advance_listing


async def _operation(runtime, command):
    from .native_environment import _key, _read, _retarget_keys, _Retarget
    from .native_turns import TURNS_STORE, _decode

    current, _ = await runtime._agent_record()
    view = current.environment
    if (runtime.identity.agent_id != command.agent_id
            or view is None or (view.request_id, view.previous_hosted_session_id, view.previous_agent_version,
                                view.hosted_session_id, view.hosted_agent_version) != (
                command.request_id, command.source_session_id, command.source_agent_version,
                command.target_session_id, command.target_agent_version)):
        raise Problem(409, "NATIVE_ENVIRONMENT_CONFLICT", "the original file operation or reader changed")
    source = command.stage in {"inspect", "verify_source"}
    if runtime.identity.session_id != (command.source_session_id if source else command.target_session_id):
        raise Problem(409, "NATIVE_FILES_CONFLICT", "the file step belongs to another original session")
    if (not source and runtime.identity.agent_version != command.target_agent_version
            and command.stage not in {"read", "observe", "confirm_readback"}):
        raise Problem(409, "NATIVE_FILES_CONFLICT", "the destination file reader has another version")
    if view.state != "pending" and command.stage not in {"read", "observe"}:
        raise Problem(409, "NATIVE_ENVIRONMENT_CONFLICT", "a published operation cannot send or copy more files")
    key = _key(runtime, session_id=command.target_session_id)
    intent_key, progress_key = _retarget_keys(command.request_id)
    async with runtime._store(TURNS_STORE) as store:
        item = await store.get_item(intent_key)
    if item is not None:
        intent = _decode(item.value, _Retarget)
        if (intent.target.request_id, intent.target.hosted_session_id) != (command.request_id, command.target_session_id):
            raise Problem(409, "NATIVE_ENVIRONMENT_CONFLICT", "the original retarget differs")
        key = progress_key
    record, etag = await _read(runtime, key=key)
    if (record is None or record.view.request_id != command.request_id or not record.files_required
            or runtime.workspace_files is None):
        raise Problem(409, "NATIVE_FILES_UNSUPPORTED", "this source or destination does not support safe workspace migration")
    if record.retarget_request_id is not None and command.stage not in {"read", "observe"}:
        raise Problem(409, "NATIVE_ENVIRONMENT_CONFLICT", "the original file operation has an explicit retarget; no further writes")
    if record.view.files_migrated is False:
        raise Problem(409, "NATIVE_FILES_CONFLICT", "the owner confirmed no file transfer for this original operation")
    if current.admitting is not None or runtime.execution_lock.locked():
        raise Problem(409, "NATIVE_FILES_UNAVAILABLE", "original admission or workspace execution is not idle")
    return current, record, etag, key


async def session_files(runtime, command):
    from .native_environment import _progress, _read
    from .native_turns import _changed

    current, record, etag, key = await _operation(runtime, command)
    scope = {name: getattr(command, name) for name in NativeFilesScope.model_fields}
    files = record.files
    if command.stage == "source_unavailable":
        from .native_environment import _save_agent, _source_matches
        if files is not None:
            raise Problem(409, "NATIVE_FILES_CONFLICT", "existing file material must be reconciled, not discarded")
        await _progress(runtime, _changed(record, view=_changed(record.view, source_invocation_refused=True)),
                        etag, key=key)
        current, agent_etag = await runtime._agent_record()
        _source_matches(current, record.view)
        if current.environment is None:
            raise Problem(409, "NATIVE_ENVIRONMENT_CONFLICT", "the original pending binding is missing")
        await _save_agent(runtime, current, agent_etag, _changed(
            current, environment=_changed(current.environment, source_invocation_refused=True)))
        return NativeFilesView(**scope)
    if command.stage == "inspect":
        if record.phase != "files":
            raise Problem(409, "NATIVE_FILES_UNAVAILABLE", "original writers must drain before the source is fixed")
        if not current.following_supported:
            raise Problem(409, "NATIVE_FILES_UNSUPPORTED", "the original source cannot fence new workspace writers")
        if files is None:
            manifest = runtime.workspace_files.snapshot(**scope)
            files = NativeFilesProgress(manifest=manifest)
            record = _changed(record, files=files,
                              view=_changed(record.view, files_manifest_sha256=manifest.sha256))
            await _progress(runtime, record, etag, key=key)
        return NativeFilesView(**scope, progress=files)
    if files is None:
        if command.stage in {"read", "observe"}:
            return NativeFilesView(**scope)
        raise Problem(409, "NATIVE_FILES_UNAVAILABLE", "the original source manifest is not available")
    if any(getattr(files.manifest, name) != value for name, value in scope.items()):
        raise Problem(409, "NATIVE_FILES_CONFLICT", "the retained manifest names another operation")
    send_allowed = False
    if command.stage in {"list_source", "list_stopped"}:
        stopped = command.stage == "list_stopped"
        if (files.next_index != len(files.manifest.entries)
                or stopped and (not files.stop_started or files.stopped_index != len(files.manifest.entries))
                or not stopped and files.stop_started):
            raise Problem(409, "NATIVE_FILES_CONFLICT", "this listing is outside its original file phase")
        if stopped and (runtime.bootstrap_source_inactive is None
                        or not await runtime.bootstrap_source_inactive(command.source_session_id)):
            raise Problem(409, "NATIVE_FILES_UNAVAILABLE", "the original source is not inactive")
        field = "stopped_listing" if stopped else "source_listing"
        try:
            listing = advance_listing(files.manifest, getattr(files, field), command.page)
        except ValueError:
            raise Problem(409, "NATIVE_FILES_SOURCE_CHANGED", "the original directory page is incomplete or changed") from None
        files = _changed(files, **{field: listing})
        await _progress(runtime, _changed(record, files=files), etag, key=key)
    elif command.stage == "begin":
        if command.index != files.next_index or files.source_verified:
            raise Problem(409, "NATIVE_FILES_CONFLICT", "only the next unsent original entry may start")
        if files.sending is None:
            runtime.workspace_files.prepare(files.manifest)
            files = _changed(files, sending=command.send_token)
            await _progress(runtime, _changed(record, files=files), etag, key=key)
            send_allowed = True
    elif command.stage in {"confirm", "confirm_readback"}:
        if command.index < files.next_index:
            return NativeFilesView(**scope, progress=files)
        if command.index != files.next_index or files.sending is None:
            raise Problem(409, "NATIVE_FILES_CONFLICT", "only an original in-flight file may be confirmed")
        runtime.workspace_files.confirm(files.manifest, command.index)
        files = _changed(files, next_index=files.next_index + 1, sending=None)
        await _progress(runtime, _changed(record, files=files), etag, key=key)
    elif command.stage == "verify_source":
        if (files.next_index != len(files.manifest.entries) or files.sending is not None or files.stop_started
                or not files.source_listing.complete):
            raise Problem(409, "NATIVE_FILES_UNAVAILABLE", "all original destination entries must first be confirmed")
        runtime.workspace_files.verify(files.manifest)
        files = _changed(files, source_verified=True)
        await _progress(runtime, _changed(record, files=files), etag, key=key)
    elif command.stage == "stop":
        if not files.source_verified:
            raise Problem(409, "NATIVE_FILES_UNAVAILABLE", "source stop requires the original complete verification")
        files = _changed(files, stop_started=True)
        await _progress(runtime, _changed(record, files=files), etag, key=key)
    elif command.stage == "source_stopped":
        if not files.stop_started or runtime.bootstrap_source_inactive is None:
            raise Problem(409, "NATIVE_FILES_UNAVAILABLE", "the original source verification is missing")
        if not await runtime.bootstrap_source_inactive(command.source_session_id):
            raise Problem(409, "NATIVE_FILES_UNAVAILABLE", "the original source is not inactive")
        if command.index is None:
            if files.stopped_index != len(files.manifest.entries) or not files.stopped_listing.complete:
                raise Problem(409, "NATIVE_FILES_UNAVAILABLE", "the stopped source has not been completely verified")
            runtime.workspace_files.prepare(files.manifest)
            files = _changed(files, source_stopped_verified=True)
        elif command.index == files.stopped_index:
            files = _changed(files, stopped_index=files.stopped_index + 1)
        elif command.index > files.stopped_index:
            raise Problem(409, "NATIVE_FILES_CONFLICT", "stopped source verification cannot skip an original entry")
        await _progress(runtime, _changed(record, files=files), etag, key=key)
    elif command.stage == "observe":
        if files.publishing and not files.published and runtime.workspace_files.published(files.manifest):
            files = _changed(files, published=True)
            await _progress(runtime, _changed(record, files=files), etag, key=key)
    record, _ = await _read(runtime, key=key)
    if record is None:
        raise Problem(503, "NATIVE_FILES_UNKNOWN", "the original file progress did not read back")
    return NativeFilesView(**scope, progress=record.files, send_allowed=send_allowed)


async def publish_files(runtime, record, etag, key):
    from .native_environment import _progress, _read
    from .native_turns import _changed

    files = record.files
    if files.published:
        return record
    if files.publishing:
        if not runtime.workspace_files.published(files.manifest):
            raise Problem(503, "NATIVE_FILES_UNKNOWN", "the original publish has no confirmed result; do not copy again")
    else:
        record = _changed(record, files=_changed(files, publishing=True))
        await _progress(runtime, record, etag, key=key)
        record, etag = await _read(runtime, key=key)
        runtime.workspace_files.publish(files.manifest)
    record = _changed(record, files=_changed(record.files, published=True))
    await _progress(runtime, record, etag, key=key)
    return record
