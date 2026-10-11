"""One explicit owner decision attached to the original follow; its required-files intent stays intact."""

from typing import Annotated, Literal
import logging

from pydantic import Field, model_validator

from .behavior_native_turn_wire import NativeEnvironmentView, NativeFilesCheckDetail, NativeFilesOmissionReason, NativeSessionRetarget, material_digest, canonical_bytes
from .behavior_wire import Identifier, WireModel
from .errors import Problem
from .native_environment import _Following, _read, _selected_following_key


def _retained_staging(record, missing, decision_basis=None):
    files = record.files
    return files is None or ((missing == "source_changed" or
                             missing == "source_not_migratable" and decision_basis == "system_policy")
                             and files.sending is None
                             and not files.publishing and not files.published)


class _Omission(WireModel):
    request_id: Identifier
    missing: NativeFilesOmissionReason
    original: _Following
    decision_basis: Literal["system_policy"] | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def no_unknown_file_effect(self):
        if not _retained_staging(self.original, self.missing, self.decision_basis) or self.original.view.state != "pending":
            raise ValueError("file omission requires unchanged confirmed staging, not an unknown upload or publication")
        return self


class _ConfirmationIdentity(WireModel):
    request_id: Identifier
    command_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    operation_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


async def _confirmation_identity(runtime, command, record, *, save=False, previous=None):
    from .native_environment import _check_read, _check_store
    from .native_turns import TURNS_STORE, _decode

    operation = {name: getattr(record.view, name) for name in (
        "agent_id", "request_id", "previous_hosted_session_id", "previous_agent_version",
        "hosted_session_id", "hosted_agent_version")}
    operation["idle_only"] = command.follow.idle_only
    identity = _ConfirmationIdentity(
        request_id=command.request_id, operation_sha256=material_digest(operation),
        command_sha256=command.confirmation_digest or material_digest({
            "request_id": command.request_id, "operation": operation}))
    key = "files-confirmation:" + material_digest({"request_id": command.request_id})
    async with _check_store(runtime, TURNS_STORE, "confirmation") as store:
        saved = await _check_read(store.get_item(key), "confirmation")
        if command.decision_basis == "system_policy" and previous is not None:
            if saved is not None:
                original = _decode(saved.value, _ConfirmationIdentity)
                if (original.request_id, original.operation_sha256) != (command.request_id, identity.operation_sha256):
                    raise Problem(409, "NATIVE_FILES_CONFLICT", "the saved decision belongs to another operation")
            return
        if saved is not None and _decode(saved.value, _ConfirmationIdentity) != identity:
            raise Problem(409, "NATIVE_FILES_CONFLICT", "this confirmation ID already names different content")
        if save and saved is None:
            try:
                await runtime._immutable(store, key, identity)
            except Problem as error:
                if error.body["error"]["code"] != "NATIVE_STATE_INVALID":
                    raise
                raise Problem(409, "NATIVE_FILES_CONFLICT", "the confirmation ID was bound to different content") from None


def _key(view):
    return "files-omission:" + material_digest({
        "agent_id": view.agent_id, "request_id": view.request_id,
        "source": view.previous_hosted_session_id, "target": view.hosted_session_id,
        "version": view.hosted_agent_version,
    })


async def _recorded_omission(runtime, record, *, match_material=True):
    from .native_turns import TURNS_STORE, _decode

    async with runtime._store(TURNS_STORE) as store:
        item = await store.get_item(_key(record.view))
    if item is None:
        return None
    value = _decode(item.value, _Omission)
    fields = ("agent_id", "request_id", "previous_hosted_session_id", "previous_agent_version",
              "hosted_session_id", "hosted_agent_version")
    if (match_material and record.files != value.original.files
            or any(getattr(value.original.view, name) != getattr(record.view, name) for name in fields)):
        raise Problem(409, "NATIVE_FILES_CONFLICT", "the original file omission belongs to different follow material")
    return value


async def omission_for(runtime, record):
    if record.view.files_migrated is not False:
        return None
    value = await _recorded_omission(runtime, record)
    if value is None or value.request_id != record.view.files_skip_request_id:
        raise Problem(503, "NATIVE_ENVIRONMENT_UNKNOWN", "the original active file omission decision is missing")
    return value


def omission_view(view, omission):
    from .native_turns import _changed
    return _changed(view, files_migrated=False, files_skip_request_id=omission.request_id, files_manifest_sha256=None, **(
        {"files_omission_basis": omission.decision_basis, "files_omission_reason": omission.missing}
        if omission.decision_basis is not None else {}))


async def check_latest_checkpoint(runtime, receipt, *, progress=None):
    from .native_checkpoint import conversation_store_name, verify_checkpoint
    from .native_environment import _check_read, _check_store, _CHECK_READ_SECONDS

    heads = None if progress is None else progress.conversation_heads
    # A higher original turn proves only that older turns are superseded, never that a newer checkpoint is valid.
    if heads is not None and receipt.turn_number is not None and receipt.turn_number < heads.get(receipt.conversation_id, 0):
        return
    async with _check_store(runtime, conversation_store_name(receipt.conversation_id), "conversation", progress=progress) as store:
        meta, _ = await _check_read(runtime._meta(receipt.conversation_id, store=store), "conversation", progress=progress)
        if heads is not None:
            heads[receipt.conversation_id] = max(heads.get(receipt.conversation_id, 0), meta.n)
        if meta.rid != receipt.request_id:
            return
        turn = await _check_read(runtime._turn(store, receipt), "turn", progress=progress)
        for checkpoint in turn.checkpoints:
            await _check_read(verify_checkpoint(store, runtime._scope(meta), checkpoint,
                             checkpoint_profile=receipt.capture.template.settings.checkpoint_profile,
                             read_timeout=_CHECK_READ_SECONDS),
                             "checkpoint", progress=progress, bounded=False)


class _OmissionScan(WireModel):
    scope_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    agent_etag: Annotated[str, Field(min_length=1, max_length=256)]
    after: Annotated[str, Field(min_length=1, max_length=512)] | None = None
    responses: bool = False
    complete: bool = False
    waiting_request_id: Identifier | None = Field(default=None, exclude_if=lambda value: value is None)
    cursor_profile: Literal["page-last-id-v1"] | None = Field(default=None, exclude_if=lambda value: value is None)
    page_offset: Annotated[int, Field(ge=0, le=100)] = Field(default=0, exclude_if=lambda value: value == 0)
    page_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None = Field(default=None, exclude_if=lambda value: value is None)
    list_timeout_cursor: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None = Field(
        default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def partial_page(self):
        if (bool(self.page_offset) != (self.page_digest is not None)
                or self.page_offset and (self.complete or self.cursor_profile is None)):
            raise ValueError("partial page progress must keep its original page and verified cursor profile")
        return self


async def _save_scan(runtime, key, scan, item):
    from .native_turns import TURNS_STORE, _UNCERTAIN, _decode, _json
    from .native_environment import _check_store, _check_read

    async with _check_store(runtime, TURNS_STORE, "check_progress") as store:
        try:
            if item is None:
                await store.create_item(key, _json(scan))
            else:
                await store.set_item(key, _json(scan), if_match=item.etag)
        except _UNCERTAIN:
            observed = await _check_read(store.get_item(key), "check_progress")
            if observed is None or _decode(observed.value, _OmissionScan) != scan:
                raise Problem(503, "NATIVE_ENVIRONMENT_UNKNOWN", "read the original file check progress") from None


def _blocked(category, *, rid=None, cid=None):
    detail = NativeFilesCheckDetail(category=category, request_id_prefix=None if rid is None else rid[:8],
                                   conversation_id_prefix=None if cid is None else cid[:8])
    return Problem(409, "NATIVE_FILES_CONFIRMATION_REQUIRED", "resolve the original work before confirming no files",
                   files_check=detail.model_dump(exclude_none=True))


def _policy_wait(command, category, *, rid=None, cid=None):
    if command.decision_basis != "system_policy" or category == "task_access_denied":
        raise _blocked(category, rid=rid, cid=cid)
    return NativeFilesCheckDetail(category=category, request_id_prefix=None if rid is None else rid[:8],
                                  conversation_id_prefix=None if cid is None else cid[:8])


def _check_view(view, detail, **fields):
    from .native_turns import _changed

    if detail is None:
        return _changed(view, reason=None, files_check=None, **fields)
    if detail.category == "scan_pending":
        return _changed(view, reason="scan_pending", files_check=detail, **fields)
    reasons = {"active_waiting": "accepted_or_started", "execution_active": "accepted_or_started",
               "admission_pending": "admission_pending", "task_active": "task_active",
               "task_unconfirmed": "task_unconfirmed", "task_access_denied": "task_access_denied"}
    return _changed(view, reason=reasons[detail.category], files_check=None, **fields)


def _scan_scope(view, request_id):
    return material_digest({
        "agent": view.agent_id, "confirmation": request_id, "follow": view.request_id,
        "source": view.previous_hosted_session_id, "source_version": view.previous_agent_version,
        "target": view.hosted_session_id, "target_version": view.hosted_agent_version,
    })


async def scan_observation(runtime, view, request_id):
    from .native_turns import TURNS_STORE, _decode

    scope = _scan_scope(view, request_id)
    async with runtime._store(TURNS_STORE) as store:
        item = await store.get_item("files-check:" + scope)
    if item is None:
        return None
    value = _decode(item.value, _OmissionScan)
    if value.scope_digest != scope:
        raise Problem(409, "NATIVE_ENVIRONMENT_CONFLICT", "the original check progress differs")
    data = value.model_dump(mode="json")
    if value.waiting_request_id is not None:
        data["waiting_status"] = (await runtime._rid(value.waiting_request_id)).value.receipt.status
    return data


async def _idle(runtime, current, record, command, agent_etag):
    from .native_environment import _drain_pages, _tasks_pending, _ScanProgress, _check_read, _check_store
    from .native_turns import TURNS_STORE, _decode, _changed

    if current.admitting is not None:
        return _policy_wait(command, "admission_pending", rid=current.admitting.request_id)
    if runtime.execution_lock.locked():
        return _policy_wait(command, "execution_active", rid=runtime.execution_owner)
    view = record.view
    scope = _scan_scope(view, command.request_id)
    key = "files-check:" + scope
    async with _check_store(runtime, TURNS_STORE, "check_progress") as store:
        item = await _check_read(store.get_item(key), "check_progress")
    scan = None if item is None else _decode(item.value, _OmissionScan)
    if scan is not None and scan.scope_digest != scope:
        raise Problem(409, "NATIVE_ENVIRONMENT_CONFLICT", "the original file check scope differs")
    if scan is None or scan.agent_etag != agent_etag or scan.cursor_profile is None:
        scan = _OmissionScan(scope_digest=scope, agent_etag=agent_etag, responses=bool(record.responses),
                             cursor_profile="page-last-id-v1")
        # Reserve the progress key before listing so our own new key cannot shift the first page.
        await _save_scan(runtime, key, scan, item)
        async with _check_store(runtime, TURNS_STORE, "check_progress") as store:
            item = await _check_read(store.get_item(key), "check_progress")
        if item is None or _decode(item.value, _OmissionScan) != scan:
            raise Problem(503, "NATIVE_ENVIRONMENT_UNKNOWN", "the original page-boundary scan did not read back")
    progress = _ScanProgress.from_record(scan)
    if not scan.complete:
        try:
            reason, after, responses = await _drain_pages(
                runtime, scan.after, view.previous_hosted_session_id, current.task_id, responses=scan.responses,
                idle_only=True, require_complete=True, progress=progress, single_page=True,
                restart_changed_page=command.decision_basis == "system_policy")
        except Problem as error:
            detail = error.body["error"].get("files_check", {})
            if command.decision_basis == "system_policy" and detail.get("category") == "active_waiting":
                await _save_scan(runtime, key, _changed(
                    scan, after=progress.after, page_offset=progress.page_offset, page_digest=progress.page_digest,
                    responses=progress.responses, waiting_request_id=progress.waiting_request_id), item)
                return NativeFilesCheckDetail.model_validate(detail)
            if (error.body["error"]["code"] == "NATIVE_STATE_STORE_UNKNOWN"
                    and detail.get("category") == "storage_read_failed" and detail.get("stage") == "list"
                    and detail.get("failure_kind") == "deadline"):
                cursor = material_digest({"after": progress.after})
                if scan.list_timeout_cursor == cursor:
                    raise Problem(error.status, error.body["error"]["code"],
                                  "the same original list page timed out twice; no page was skipped",
                                  files_check={**detail, "read_attempt": 2}) from None
                _, latest_etag = await _check_read(runtime._agent_record(), "agent")
                if latest_etag != agent_etag:
                    raise Problem(409, "NATIVE_ENVIRONMENT_CONFLICT", "the Agent changed during the original list timeout") from None
                await _save_scan(runtime, key, _changed(
                    scan, after=progress.after, responses=progress.responses,
                    page_offset=progress.page_offset, page_digest=progress.page_digest,
                    list_timeout_cursor=cursor), item)
                logging.getLogger(__name__).warning(
                    "FA_NATIVE_FILES_CHECK category=scan_pending stage=list failure_kind=deadline read_attempt=1")
                return NativeFilesCheckDetail(category="scan_pending", stage="list", key_category="receipt",
                                              failure_kind="deadline", read_attempt=1,
                                              read_elapsed_ms=detail.get("read_elapsed_ms"),
                                              cursor_source=progress.cursor_source)
            if detail.get("category") == "storage_read_failed" and (
                    progress.after != scan.after or progress.page_offset != scan.page_offset):
                _, latest_etag = await _check_read(runtime._agent_record(), "agent")
                if latest_etag != agent_etag:
                    raise Problem(409, "NATIVE_ENVIRONMENT_CONFLICT", "the Agent changed while the original read was incomplete") from None
                await _save_scan(runtime, key, _changed(
                    scan, after=progress.after, responses=progress.responses,
                    page_offset=progress.page_offset, page_digest=progress.page_digest,
                    list_timeout_cursor=None), item)
            raise
        _, latest_etag = await _check_read(runtime._agent_record(), "agent")
        if latest_etag != agent_etag:
            raise Problem(409, "NATIVE_ENVIRONMENT_CONFLICT", "the Agent binding changed during the original file check")
        if reason not in {None, "scan_pending"}:
            raise _blocked("active_waiting", rid=progress.request_id_prefix, cid=progress.conversation_id_prefix)
        scan = _changed(scan, after=after, responses=responses, complete=reason is None,
                        page_offset=progress.page_offset, page_digest=progress.page_digest,
                        list_timeout_cursor=None, waiting_request_id=None)
        await _save_scan(runtime, key, scan, item)
        logging.getLogger(__name__).info(
            "FA_NATIVE_FILES_CHECK category=%s request_id_prefix=%s terminal_unknowns_preserved=%s",
            "complete" if scan.complete else "scan_pending", progress.request_id_prefix, progress.terminal_unknowns)
    if not scan.complete:
        return NativeFilesCheckDetail(category="scan_pending", request_id_prefix=progress.request_id_prefix,
                                      conversation_id_prefix=progress.conversation_id_prefix,
                                      cursor_source="none" if scan.after is None else "page_last_id")
    reason = await _tasks_pending(runtime, current, view.previous_hosted_session_id, responses=scan.responses)
    if reason is not None:
        return _policy_wait(command, reason)
    return None


def _initial_record(runtime, current, command):
    original = current.environment or current.preparation
    if (original is None or current.hosted_session_id != command.follow.expected_session_id
            or current.environment is not None and current.environment.state != "ready" and (
                current.environment.request_id != command.pending_request_id
                or current.environment.hosted_session_id != command.pending_session_id)
            or int(runtime.identity.agent_version) <= int(original.hosted_agent_version)):
        raise Problem(409, "NATIVE_ENVIRONMENT_CONFLICT", "the original source or target changed before confirmation")
    view = NativeEnvironmentView(
        agent_id=runtime.identity.agent_id, request_id=command.follow.request_id,
        previous_hosted_session_id=current.hosted_session_id,
        previous_agent_version=(original.previous_agent_version if current.environment is not None
                                and current.environment.state == "pending" else original.hosted_agent_version),
        previous_files_profile=(original.previous_files_profile if current.environment is not None
                                and current.environment.state == "pending" else original.files_profile),
        source_invocation_refused=bool(current.environment is not None and current.environment.state == "pending"
                                       and original.source_invocation_refused),
        hosted_session_id=runtime.identity.session_id, hosted_agent_version=runtime.identity.agent_version,
        state="pending", reason="source_stop_required")
    return _Following(view=view, idle_only=command.follow.idle_only, files_required=True, responses=False)


async def confirm_omission(runtime, command):
    from .native_environment import follow_session, retarget_session, _source_matches, _key as following_key, _retarget_keys, _progress, _check_read
    from .native_turns import TURNS_STORE, _changed

    current, agent_etag = await _check_read(runtime._agent_record(), "agent")
    key = await _check_read(_selected_following_key(runtime, current), "following")
    record, record_etag = await _check_read(_read(runtime, key=key), "following")
    if record is None or record.view.request_id != command.follow.request_id:
        _, progress_key = _retarget_keys(command.follow.request_id)
        alternate, alternate_etag = await _check_read(_read(runtime, key=progress_key), "following")
        if alternate is not None:
            record, record_etag, key = alternate, alternate_etag, progress_key
    initial = record is None
    if initial and command.stage in {"check", "confirm", "read"}:
        record = _initial_record(runtime, current, command)
    if (record is None or record.view.request_id != command.follow.request_id
            or record.view.previous_hosted_session_id != command.follow.expected_session_id
            or record.view.hosted_session_id != runtime.identity.session_id
            or record.view.hosted_agent_version != runtime.identity.agent_version
            or record.idle_only != command.follow.idle_only):
        raise Problem(409, "NATIVE_FILES_CONFIRMATION_REQUIRED", "confirm the exact original source and destination")
    pending_retarget = (command.pending_request_id is not None and current.environment is not None
                        and current.environment.request_id != command.follow.request_id)
    if pending_retarget:
        expected = _initial_record(runtime, current, command).view
        fields = ("agent_id", "request_id", "previous_hosted_session_id", "previous_agent_version",
                  "hosted_session_id", "hosted_agent_version")
        if (record.files is not None or record.retarget_request_id is not None
                or any(getattr(record.view, name) != getattr(expected, name) for name in fields)):
            raise Problem(409, "NATIVE_FILES_CONFLICT", "the unselected retarget has different file material or scope")
    previous = await _check_read(_recorded_omission(runtime, record), "confirmation")
    await _confirmation_identity(runtime, command, record, previous=previous)
    if previous is not None:
        if (previous.request_id != command.request_id
                or command.stage in {"confirm", "activate"} and previous.missing != command.missing):
            raise Problem(409, "NATIVE_FILES_CONFLICT", "the original file omission has another confirmation request")
        if record.view.files_migrated is not False:
            if command.stage == "read":
                return _changed(record.view, files_confirmation_saved=previous.request_id,
                                files_confirmation_reason=previous.missing)
            if command.stage == "check":
                pending = await _idle(runtime, current, record, command, agent_etag)
                return _check_view(record.view, pending, files_confirmation_saved=previous.request_id,
                                   files_confirmation_reason=previous.missing)
            if command.stage not in {"activate", "confirm"}:
                raise Problem(409, "NATIVE_FILES_CONFIRMATION_REQUIRED", "continue the saved confirmation; do not submit it again")
            if canonical_bytes(record) != canonical_bytes(previous.original):
                raise Problem(409, "NATIVE_FILES_CONFLICT", "the original following changed after its saved confirmation")
            _source_matches(current, record.view)
            pending = await _idle(runtime, current, record, command, agent_etag)
            if pending is not None:
                return _check_view(record.view, pending)
            await _confirmation_identity(runtime, command, record, save=True, previous=previous)
            marked = _changed(record, view=omission_view(record.view, previous),
                              after=None, page_offset=0, page_digest=None)
            await _progress(runtime, marked, record_etag, key=key)
            record = marked
        elif record.view.files_skip_request_id != previous.request_id:
            raise Problem(409, "NATIVE_FILES_CONFLICT", "the active file omission names another confirmation")
    if previous is None:
        if command.stage == "activate":
            raise Problem(503, "NATIVE_ENVIRONMENT_UNKNOWN", "the original file omission has not been confirmed")
        if not pending_retarget:
            _source_matches(current, record.view)
        if command.stage == "read":
            from .native_environment import _tasks_pending
            scan = await scan_observation(runtime, record.view, command.request_id)
            return _changed(record.view, files_confirmation_absent=command.request_id,
                            progress_token=material_digest({
                                "record": record.model_dump(mode="json"), "scan": scan,
                                "admitting": None if current.admitting is None else current.admitting.model_dump(mode="json"),
                                "executing": runtime.execution_lock.locked(),
                                "tasks_complete": await _tasks_pending(
                                    runtime, current, record.view.previous_hosted_session_id, responses=bool(record.responses)) is None}),
                            operation_idle_only=record.idle_only)
        if not _retained_staging(record, command.missing, command.decision_basis) or record.retarget_request_id is not None:
            raise Problem(409, "NATIVE_FILES_CONFLICT", "file material or another transition cannot be discarded")
        if pending_retarget:
            original_key = following_key(runtime, session_id=command.pending_session_id)
            original, _ = await _check_read(_read(runtime, key=original_key), "following")
            if original is None or original.view.request_id != command.pending_request_id:
                _, original_key = _retarget_keys(command.pending_request_id)
                original, _ = await _check_read(_read(runtime, key=original_key), "following")
            if (original is None or original.view.request_id != command.pending_request_id
                    or not _retained_staging(original, command.missing, command.decision_basis)
                    or original.retarget_request_id not in {None, command.follow.request_id}):
                raise Problem(409, "NATIVE_FILES_CONFLICT", "the blocked original file intent cannot be discarded")
        if command.missing == "destination" and runtime.workspace_files is not None:
            raise Problem(409, "NATIVE_FILES_CONFLICT", "the destination helper is available; do not skip its files")
        pending = await _idle(runtime, current, record, command, agent_etag)
        if pending is not None:
            return _check_view(record.view, pending)
        if command.stage == "check":
            return _changed(record.view, reason=None, files_check=None)
        await _confirmation_identity(runtime, command, record, save=True)
        if initial or pending_retarget:
            if command.pending_request_id is None:
                await follow_session(runtime, command.follow)
            else:
                transition = await retarget_session(runtime, NativeSessionRetarget(
                    request_id=command.follow.request_id, expected_session_id=command.follow.expected_session_id,
                    expected_etag=agent_etag, expected_agent_version=runtime.identity.agent_version,
                    pending_request_id=command.pending_request_id, pending_session_id=command.pending_session_id,
                    idle_only=True))
            current, _ = await runtime._agent_record()
            if pending_retarget:
                if current.environment is None:
                    raise Problem(409, "NATIVE_ENVIRONMENT_CONFLICT", "the original pending retarget is no longer selected")
                if current.environment.request_id != command.follow.request_id:
                    _initial_record(runtime, current, command)
                    if transition.reason == "task_access_denied":
                        raise _blocked("task_access_denied")
                    detail = (NativeFilesCheckDetail(category="scan_pending")
                              if transition.reason == "scan_pending" else None)
                    return _changed(transition, files_confirmation_pending=command.request_id, files_check=detail)
            key = await _selected_following_key(runtime, current)
            record, record_etag = await _read(runtime, key=key)
            if record is None or record.files is not None:
                raise Problem(409, "NATIVE_FILES_CONFLICT", "the original file operation changed during confirmation")
        previous = _Omission(request_id=command.request_id, missing=command.missing, original=record,
                             decision_basis=command.decision_basis)
        async with runtime._store(TURNS_STORE) as store:
            await runtime._immutable(store, _key(record.view), previous)
        # The decision adds keys to this store; only the mutable follow scan starts over.
        marked = _changed(record, view=omission_view(record.view, previous),
                          after=None, page_offset=0, page_digest=None)
        await _progress(runtime, marked, record_etag, key=key)
        record = marked
    if command.stage == "check":
        return omission_view(record.view, previous)
    if previous.request_id != command.request_id or command.stage in {"confirm", "activate"} and previous.missing != command.missing:
        raise Problem(409, "NATIVE_FILES_CONFLICT", "the original file omission has another confirmation request")
    if (current.environment is not None and current.environment.state == "ready"
            and current.environment.request_id == record.view.request_id):
        return current.environment
    return omission_view(record.view, previous)
