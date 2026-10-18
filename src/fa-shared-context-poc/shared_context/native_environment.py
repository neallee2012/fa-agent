"""Agent-wide version following over the original N1 receipts and single Agent item."""

from typing import Annotated, Literal, get_args
from dataclasses import dataclass, field
from contextlib import AsyncExitStack, asynccontextmanager
import asyncio
import logging
from time import monotonic, perf_counter

from pydantic import Field, model_validator

from .behavior_native_turn_wire import NativeEnvironmentView, NativeFilesCheckDetail, NativeStorageErrorCode, material_digest
from .behavior_wire import Identifier, WireModel
from .errors import Problem
from .native_responses_wire import NATIVE_RESPONSES_PROFILE, responses_environment_blocker
from .native_files_wire import NativeFilesProgress

_SCAN_SECONDS = 10
_CHECK_READ_SECONDS = 5
_CHECK_PAGE_KEYS = 100


class _CutScan(WireModel):
    """A ``NativeSessionCut`` scan that goes on across calls (ADR-0009 U5): the Agent ETag it started from (E0, kept
    until the commit or the fence) and its totals so far, for the ``NATIVE_UPGRADE_CUT`` log (gate 7)."""
    etag: Annotated[str, Field(min_length=1, max_length=256)]
    calls: Annotated[int, Field(ge=1)]
    scan_ms: Annotated[int, Field(ge=0)]
    receipts: Annotated[int, Field(ge=0)]


class _Following(WireModel):
    view: NativeEnvironmentView
    phase: Literal["draining", "files", "stopping", "confirming"] = "draining"
    after: Annotated[str, Field(min_length=1, max_length=512)] | None = None
    cursor_profile: Literal["page-last-id-v1"] | None = Field(default=None, exclude_if=lambda value: value is None)
    page_offset: Annotated[int, Field(ge=0, le=16)] = Field(default=0, exclude_if=lambda value: value == 0)
    page_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None = Field(default=None, exclude_if=lambda value: value is None)
    responses: bool | None = Field(default=None, exclude_if=lambda value: value is None)
    idle_only: bool = Field(default=False, exclude_if=lambda value: not value)
    files_required: bool = Field(default=False, exclude_if=lambda value: not value)
    files: NativeFilesProgress | None = Field(default=None, exclude_if=lambda value: value is None)
    retarget_request_id: Identifier | None = Field(default=None, exclude_if=lambda value: value is None)
    waiting_request_id: Identifier | None = Field(default=None, exclude_if=lambda value: value is None)
    # Only while a cut scans with no ``pending``: the source's H0 file stages read this record during a fence.
    cut: _CutScan | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def partial_page(self):
        if (bool(self.page_offset) != (self.page_digest is not None)
                or self.page_offset and (self.cursor_profile is None or self.phase not in {"draining", "confirming"})):
            raise ValueError("partial follow progress must keep its original page while scanning")
        return self

    @model_validator(mode="after")
    def original_files(self):
        if self.files is not None:
            manifest, view = self.files.manifest, self.view
            expected_digest = None if view.files_migrated is False else manifest.sha256
            if (not self.files_required or view.files_manifest_sha256 != expected_digest
                    or (manifest.agent_id, manifest.request_id, manifest.source_session_id, manifest.source_agent_version,
                        manifest.target_session_id, manifest.target_agent_version) != (
                            view.agent_id, view.request_id, view.previous_hosted_session_id, view.previous_agent_version,
                            view.hosted_session_id, view.hosted_agent_version)):
                raise ValueError("file progress must keep its original follow scope and manifest digest")
        elif self.view.files_manifest_sha256 is not None:
            raise ValueError("a file reference requires its original manifest")
        return self


class _EnvironmentBefore(WireModel):
    """``environment-before:<request ID>`` (immutable): the Agent's environment just before that request wrote
    ``pending`` over it, so ``NativeSessionWithdraw`` restores it exactly (ADR-0009 lifecycle "pending 切換用法")."""
    schema_version: Literal["fa-native-environment-before-v1"] = "fa-native-environment-before-v1"
    hosted_session_id: Identifier
    environment: NativeEnvironmentView | None = Field(default=None, exclude_if=lambda value: value is None)


def before_key(request_id):
    return "environment-before:" + request_id


async def save_before(runtime, current, request_id):
    """Right before ``pending`` replaces a ready (or no) environment. H0 reads only ``rid:`` keys here."""
    from .native_turns import TURNS_STORE

    async with runtime._store(TURNS_STORE) as store:
        await runtime._immutable(store, before_key(request_id), _EnvironmentBefore(
            hosted_session_id=current.hosted_session_id, environment=current.environment))


class _Retarget(WireModel):
    original: NativeEnvironmentView
    target: NativeEnvironmentView

    @model_validator(mode="after")
    def preserved_source(self):
        if (self.original.state != "pending" or self.target.state != "pending"
                or self.original.request_id == self.target.request_id
                or self.original.hosted_session_id == self.target.hosted_session_id
                or int(self.original.hosted_agent_version) >= int(self.target.hosted_agent_version)
                or (self.original.agent_id, self.original.previous_hosted_session_id, self.original.previous_agent_version)
                != (self.target.agent_id, self.target.previous_hosted_session_id, self.target.previous_agent_version)):
            raise ValueError("retargeting must preserve the original pending source and select a newer destination")
        return self


def _key(runtime, *, session_id=None):
    return "following:" + material_digest({"sid": runtime.identity.session_id if session_id is None else session_id})


def _retarget_keys(request_id):
    identifier = material_digest({"request_id": request_id})
    return "retarget:" + identifier, "retarget-progress:" + identifier


async def _selected_following_key(runtime, current):
    from .native_turns import TURNS_STORE, _decode

    selected = current.environment
    if selected is not None and selected.state == "pending" and selected.hosted_session_id == runtime.identity.session_id:
        key, progress_key = _retarget_keys(selected.request_id)
        async with runtime._store(TURNS_STORE) as store:
            item = await store.get_item(key)
        if item is not None:
            intent = _decode(item.value, _Retarget)
            observed_fields = {"reason", "source_invocation_refused"}
            if intent.target.model_dump(exclude=observed_fields) != selected.model_dump(exclude=observed_fields):
                raise Problem(409, "NATIVE_ENVIRONMENT_CONFLICT", "the selected retarget differs from its original intent")
            return progress_key
    return _key(runtime)


async def _read(runtime, *, key=None):
    from .native_turns import TURNS_STORE, _decode

    async with runtime._store(TURNS_STORE) as store:
        item = await store.get_item(_key(runtime) if key is None else key)
    return (None, None) if item is None else (_decode(item.value, _Following), item.etag)


async def _original_progress(runtime, view):
    from .native_turns import TURNS_STORE, _decode

    key = _key(runtime, session_id=view.hosted_session_id)
    intent_key, progress_key = _retarget_keys(view.request_id)
    async with runtime._store(TURNS_STORE) as store:
        saved = await store.get_item(intent_key)
    if saved is not None:
        intent = _decode(saved.value, _Retarget)
        if (intent.target.request_id, intent.target.hosted_session_id) != (view.request_id, view.hosted_session_id):
            raise Problem(409, "NATIVE_ENVIRONMENT_CONFLICT", "the retained file operation differs")
        key = progress_key
    record, etag = await _read(runtime, key=key)
    if record is None or any(getattr(record.view, name) != getattr(view, name) for name in (
            "agent_id", "request_id", "previous_hosted_session_id", "previous_agent_version",
            "hosted_session_id", "hosted_agent_version")):
        raise Problem(503, "NATIVE_ENVIRONMENT_UNKNOWN", "the original pending operation could not be confirmed")
    return record, etag, key


async def following_view(runtime, current, *, environment_request_id=None):
    if (current.environment is not None and current.environment.state == "ready"
            and current.environment.hosted_session_id == runtime.identity.session_id):
        return current.environment
    key = await _selected_following_key(runtime, current)
    target = None
    if environment_request_id is not None:
        from .native_turns import TURNS_STORE, _decode
        intent_key, progress_key = _retarget_keys(environment_request_id)
        async with runtime._store(TURNS_STORE) as store:
            saved = await store.get_item(intent_key)
        if saved is not None:
            target = _decode(saved.value, _Retarget).target
            if (target.agent_id, target.hosted_session_id, target.hosted_agent_version, target.request_id) != (
                    runtime.identity.agent_id, runtime.identity.session_id,
                    runtime.identity.agent_version, environment_request_id):
                raise Problem(409, "NATIVE_ENVIRONMENT_CONFLICT", "the original retarget observation has another scope")
            key = progress_key
    record, _ = await _read(runtime, key=key)
    if record is None:
        return None
    if target is not None and any(getattr(record.view, name) != getattr(target, name) for name in (
            "agent_id", "request_id", "previous_hosted_session_id", "previous_agent_version",
            "hosted_session_id", "hosted_agent_version")):
        raise Problem(409, "NATIVE_ENVIRONMENT_CONFLICT", "the retained retarget progress differs from its intent")
    from .native_files_omission import omission_for, omission_view, _recorded_omission, scan_observation
    from .behavior_native_turn_wire import automatic_omission_id
    from .native_turns import _changed
    omitted = await omission_for(runtime, record)
    view = record.view if omitted is None else omission_view(record.view, omitted)
    previous = omitted or await _recorded_omission(runtime, record, match_material=False)
    if previous is not None and previous.original.files != record.files:
        previous = None
    if previous is not None and omitted is None:
        view = _changed(view, files_confirmation_saved=previous.request_id,
                        files_confirmation_reason=previous.missing)
    request_id = previous.request_id if previous is not None else automatic_omission_id(
        view.agent_id, view.previous_hosted_session_id, view.hosted_session_id, view.hosted_agent_version)
    scan = await scan_observation(runtime, view, request_id)
    waiting = None
    if record.waiting_request_id is not None:
        waiting = (await runtime._rid(record.waiting_request_id)).value.receipt.status
    tasks_complete = None
    if scan is not None or record.view.reason in {"task_active", "task_unconfirmed", "accepted_or_started"}:
        tasks_complete = await _tasks_pending(
            runtime, current, view.previous_hosted_session_id, responses=bool(record.responses)) is None
    progress = material_digest({
        "record": record.model_dump(mode="json"), "scan": scan, "waiting_status": waiting,
        "tasks_complete": tasks_complete, "admitting": None if current.admitting is None else current.admitting.model_dump(mode="json")})
    uncertain_files = (record.files is not None and (record.files.sending is not None
                       or record.files.publishing and not record.files.published))
    return _changed(view, progress_token=progress, operation_idle_only=record.idle_only,
                    files_reconcile_required=uncertain_files)


async def current_source_refused(runtime, current):
    view = current.environment
    if view is None or view.state != "pending":
        return False
    original, _, _ = await _original_progress(runtime, view)
    return view.source_invocation_refused or original.view.source_invocation_refused


async def _progress(runtime, record, etag, *, key=None):
    from .native_turns import TURNS_STORE, _UNCERTAIN, _decode, _json

    key = _key(runtime) if key is None else key
    async with runtime._store(TURNS_STORE) as store:
        try:
            if etag is None:
                await store.create_item(key, _json(record))
            else:
                await store.set_item(key, _json(record), if_match=etag)
        except _UNCERTAIN:
            item = await store.get_item(key)
            if item is None or _decode(item.value, _Following) != record:
                raise Problem(503, "NATIVE_ENVIRONMENT_UNKNOWN", "read the original environment operation") from None


@dataclass
class _ScanProgress:
    after: str | None = None
    page_offset: int = 0
    page_digest: str | None = None
    cursor_source: Literal["page_last_id", "none"] | None = None
    responses: bool = False
    request_id_prefix: str | None = None
    conversation_id_prefix: str | None = None
    terminal_unknowns: int = 0
    waiting_request_id: str | None = None
    conversation_heads: dict[str, int] = field(default_factory=dict)
    # For the cut's log (ADR-0009 gate 7): "set" (only the open-RID set) or "full", and the receipts read.
    mode: Literal["set", "full"] | None = None
    receipts: int = 0

    @classmethod
    def from_record(cls, record):
        return cls(after=record.after, page_offset=record.page_offset, page_digest=record.page_digest,
                   cursor_source="none" if record.after is None else "page_last_id")


def _storage_error_details(error):
    from azure.ai.agentserver.core.storage import FoundryStorageError

    if not isinstance(error, FoundryStorageError):
        return {}
    status = error.status_code
    body = error.response_body
    envelope = body.get("error") if isinstance(body, dict) else None
    code = envelope.get("code") if isinstance(envelope, dict) else None
    return {
        **({"backend_status": status} if type(status) is int and 100 <= status <= 599 else {}),
        "backend_code": code if type(code) is str and code in get_args(NativeStorageErrorCode) else "UNCLASSIFIED",
    }


async def _check_read(operation, key_category, *, progress=None, stage="read", bounded=True):
    from .native_turns import _UNCERTAIN

    read_deadline = asyncio.timeout(_CHECK_READ_SECONDS if bounded else None)
    started = perf_counter()
    try:
        async with read_deadline:
            return await operation
    except (Problem, *_UNCERTAIN) as error:
        if isinstance(error, Problem) and error.body["error"]["code"] != "NATIVE_STATE_STORE_UNKNOWN":
            raise
        detail = NativeFilesCheckDetail(
            category="storage_read_failed", stage=stage, key_category=key_category,
            failure_kind="deadline" if read_deadline.expired() else "backend",
            read_elapsed_ms=max(0, round((perf_counter() - started) * 1000)),
            request_id_prefix=None if progress is None else progress.request_id_prefix,
            conversation_id_prefix=None if progress is None else progress.conversation_id_prefix,
            cursor_source=None if progress is None else progress.cursor_source,
            **_storage_error_details(error))
        raise Problem(503, "NATIVE_STATE_STORE_UNKNOWN", "the original file check read could not be confirmed",
                      files_check=detail.model_dump(exclude_none=True)) from None


@asynccontextmanager
async def _check_store(runtime, name, key_category, *, progress=None):
    stage = "open"
    try:
        async with AsyncExitStack() as stack:
            store = await _check_read(
                stack.enter_async_context(runtime._store(name)), key_category, progress=progress, stage="open")
            stage = "read"
            yield store
            stage = "close"
    except Problem as error:
        if error.body["error"]["code"] != "NATIVE_STATE_STORE_UNKNOWN" or "files_check" in error.body["error"]:
            raise
        raise Problem(error.status, error.body["error"]["code"], "the original file check store could not be confirmed",
                      files_check=NativeFilesCheckDetail(
                          category="storage_read_failed", stage=stage, key_category=key_category,
                          request_id_prefix=None if progress is None else progress.request_id_prefix,
                          conversation_id_prefix=None if progress is None else progress.conversation_id_prefix,
                          cursor_source=None if progress is None else progress.cursor_source,
                      ).model_dump(exclude_none=True)) from None


async def _drain_pages(runtime, after, source_sid, task_id, *, responses=False, idle_only=False, require_complete=False,
                       progress=None, single_page=False, restart_changed_page=False, settled=frozenset()):
    progress = (_ScanProgress(after=after, cursor_source="none" if after is None else "page_last_id")
                if progress is None else progress)
    progress.responses = responses
    if not require_complete:
        # The omission check reads every completed receipt's checkpoint by design, so it always scans every key.
        from .native_open_rids import read as read_open
        open_set = await read_open(runtime)
        if open_set is not None and open_set.complete_for == source_sid:
            progress.mode = "set"
            try:
                async with asyncio.timeout(_SCAN_SECONDS):
                    return await _scan_open(runtime, open_set, source_sid, task_id, responses=responses,
                                            idle_only=idle_only, progress=progress, settled=settled)
            except TimeoutError:
                logging.getLogger(__name__).warning("FA_NATIVE_FOLLOW_SCAN_PENDING reason=read_deadline")
                return "scan_pending", None, progress.responses
    progress.mode = "full"
    try:
        # Omission checks stop between complete receipts; every constituent read has its own short deadline.
        deadline = monotonic() + _SCAN_SECONDS if require_complete else None
        async with asyncio.timeout(None if require_complete else _SCAN_SECONDS):
            while True:
                options = {"idle_only": True} if idle_only else {}
                if require_complete:
                    options["require_complete"] = True
                    options["deadline"] = deadline
                if single_page:
                    options["page_limit"] = _CHECK_PAGE_KEYS
                if settled:
                    options["settled"] = settled
                reason, after, responses = await _scan_page(
                    runtime, after, source_sid, task_id, responses=responses, progress=progress, **options)
                if single_page or reason != "scan_pending" or deadline is not None and monotonic() >= deadline:
                    return reason, after, responses
                await asyncio.sleep(0)
    except Problem as error:
        detail = error.body["error"].get("files_check", {})
        if (not restart_changed_page or error.body["error"]["code"] != "NATIVE_ENVIRONMENT_CONFLICT"
                or (detail.get("category"), detail.get("stage"), detail.get("key_category"))
                != ("page_changed", "list", "receipt")):
            raise
        progress.after, progress.page_offset, progress.page_digest = None, 0, None
        progress.cursor_source, progress.waiting_request_id = "none", None
        progress.conversation_heads.clear()
        logging.getLogger(__name__).info("FA_NATIVE_FOLLOW_SCAN_PENDING reason=page_changed cursor_source=none")
        return "scan_pending", None, progress.responses
    except TimeoutError:
        logging.getLogger(__name__).warning("FA_NATIVE_FOLLOW_SCAN_PENDING reason=read_deadline")
        return "scan_pending", progress.after, progress.responses


async def _scan_page(runtime, after, source_sid, task_id, *, responses=False, idle_only=False, require_complete=False,
                     progress=None, deadline=None, page_limit=16, settled=frozenset()):
    """``settled``: SIDs the API lists as previous (ADR-0009 §7a (i)); their unfinished receipts are not waited for
    and stay unchanged."""
    from .native_turns import TURNS_STORE

    manager = (_check_store(runtime, TURNS_STORE, "receipt", progress=progress)
               if require_complete else runtime._store(TURNS_STORE))
    async with manager as store:
        page_read = store.list_keys(limit=page_limit, after=after)
        page = (await _check_read(page_read, "receipt", progress=progress, stage="list")
                if require_complete else await page_read)
        page_digest = material_digest({
            "entries": [(key.id, key.key) for key in page.keys],
            "last_id": page.last_id, "has_more": page.has_more, "limit": page_limit})
        offset = 0 if progress is None else progress.page_offset
        if (offset > len(page.keys) or progress is not None and progress.page_digest is not None
                and progress.page_digest != page_digest):
            raise Problem(409, "NATIVE_ENVIRONMENT_CONFLICT", "the original receipt page changed before it was fully checked",
                          files_check=NativeFilesCheckDetail(
                              category="page_changed", stage="list", key_category="receipt",
                              cursor_source=None if progress is None else progress.cursor_source).model_dump(exclude_none=True))
        for index, key in enumerate(page.keys):
            if index < offset:
                continue
            if key.key.startswith("rid:"):
                if progress is not None:
                    progress.request_id_prefix = key.key[4:12]
                    progress.conversation_id_prefix = None
                receipt_read = store.get_item(key.key)
                item = (await _check_read(receipt_read, "receipt", progress=progress)
                        if require_complete else await receipt_read)
                if progress is not None:
                    progress.receipts += 1
                original = runtime._check_rid(item, key.key[4:])
                if _settled_open(original, settled):
                    # It stays open: keep it in the open-RID set, so a later follow reading only the set still sees it.
                    from .native_open_rids import add as add_open
                    await add_open(runtime, (original.value.receipt.request_id,))
                    if progress is not None:
                        if not key.id:
                            raise Problem(503, "NATIVE_ENVIRONMENT_UNKNOWN", "the original receipt cursor is missing")
                        progress.page_offset = index + 1
                        progress.page_digest = page_digest
                    continue
                if original.value.receipt.state_profile == NATIVE_RESPONSES_PROFILE:
                    responses = _responses_seen(progress)
                receipt = (original.value.receipt if idle_only else
                           await runtime.close_suspended_start(original, source_sid, task_id))
                if progress is not None:
                    progress.request_id_prefix = receipt.request_id[:8]
                    progress.conversation_id_prefix = receipt.conversation_id[:8]
                if receipt.status in {"accepted", "started"}:
                    if progress is not None:
                        progress.waiting_request_id = receipt.request_id
                    if require_complete:
                        raise Problem(409, "NATIVE_FILES_CONFIRMATION_REQUIRED",
                                      "the original request is executing or waiting for input",
                                      files_check=NativeFilesCheckDetail(
                                          category="active_waiting", request_id_prefix=receipt.request_id[:8],
                                          conversation_id_prefix=receipt.conversation_id[:8]).model_dump(exclude_none=True))
                    return "accepted_or_started", after, responses
                if require_complete:
                    if receipt.status == "unknown":
                        if progress is not None:
                            progress.terminal_unknowns += 1
                    if receipt.status == "completed":
                        from .native_files_omission import check_latest_checkpoint
                        try:
                            await check_latest_checkpoint(runtime, receipt, progress=progress)
                        except Problem as error:
                            if "files_check" in error.body["error"]:
                                raise
                            raise Problem(error.status, error.body["error"]["code"],
                                          "the original conversation checkpoint could not be verified",
                                          files_check=NativeFilesCheckDetail(
                                              category="checkpoint_unconfirmed", request_id_prefix=receipt.request_id[:8],
                                              conversation_id_prefix=receipt.conversation_id[:8]).model_dump(exclude_none=True)) from None
            if progress is not None:
                if not key.id:
                    raise Problem(503, "NATIVE_ENVIRONMENT_UNKNOWN", "the original receipt cursor is missing")
                progress.page_offset = index + 1
                progress.page_digest = page_digest
            if deadline is not None and monotonic() >= deadline and index + 1 < len(page.keys):
                return "scan_pending", after, responses
        if progress is not None:
            progress.page_offset, progress.page_digest = 0, None
        if not page.has_more:
            if progress is not None:
                progress.after = None
            return None, None, responses
        if not page.last_id or page.last_id == after:
            raise Problem(503, "NATIVE_ENVIRONMENT_UNKNOWN", "the original receipt page did not advance")
        if progress is not None:
            progress.after = page.last_id
            progress.cursor_source = "page_last_id"
        return "scan_pending", page.last_id, responses


def _settled_open(original, settled):
    """An unfinished receipt on a SID the API lists as previous (ADR-0009 §7a (i)): not waited for, left unchanged."""
    return original.value.receipt.hosted_session_id in settled and original.value.receipt.status in {"accepted", "started"}


def _responses_seen(progress):
    blocker = responses_environment_blocker()
    if blocker is not None:
        raise Problem(503, blocker, "Responses environment continuation has not been selected")
    if progress is not None:
        progress.responses = True
    return True


async def _scan_open(runtime, open_set, source_sid, task_id, *, responses, idle_only, progress, settled):
    """Only the receipts the open-RID set lists, when it is complete for the source (``native_open_rids``): the same
    decisions as ``_scan_page``, at a cost that follows the open work instead of every receipt ever written."""
    from .native_open_rids import remove as remove_open
    from .native_turns import TURNS_STORE

    progress.after, progress.page_offset, progress.page_digest, progress.cursor_source = None, 0, None, "none"
    if open_set.responses:
        responses = _responses_seen(progress)
    async with runtime._store(TURNS_STORE) as store:
        for rid in sorted(open_set.rids):
            progress.request_id_prefix, progress.conversation_id_prefix = rid[:8], None
            item = await store.get_item("rid:" + rid)
            progress.receipts += 1
            if item is None:
                # Added while its receipt is being created: that admission still holds ``admitting``, which the caller
                # checked before this scan, and a later mark changes the ETag the caller's next Agent CAS compares.
                logging.getLogger(__name__).info("FA_NATIVE_OPEN_RID_UNWRITTEN request_id_prefix=%s", rid[:8])
                continue
            original = runtime._check_rid(item, rid)
            if _settled_open(original, settled):
                continue
            if original.value.receipt.state_profile == NATIVE_RESPONSES_PROFILE:
                responses = _responses_seen(progress)
            receipt = (original.value.receipt if idle_only else
                       await runtime.close_suspended_start(original, source_sid, task_id))
            progress.request_id_prefix = receipt.request_id[:8]
            progress.conversation_id_prefix = receipt.conversation_id[:8]
            if receipt.status in {"accepted", "started"}:
                progress.waiting_request_id = receipt.request_id
                return "accepted_or_started", None, responses
            # Terminal, still listed: its own removal failed or never ran (a crash in between, an older writer).
            await remove_open(runtime, rid)
    return None, None, responses


async def _save_agent(runtime, original, etag, replacement):
    """CAS ``replacement`` over ``original``; returns its new ETag when the store reported one."""
    from .native_turns import TURNS_STORE, _UNCERTAIN, _decode, _json

    saved = None
    async with runtime._store(TURNS_STORE) as store:
        try:
            ref = await store.set_item("agent", _json(replacement), if_match=etag)
            saved = getattr(ref, "etag", None) or None
        except _UNCERTAIN:
            item = await store.get_item("agent")
            found = None if item is None else _decode(item.value, type(original))
            if found != replacement:
                raise Problem(503, "NATIVE_ENVIRONMENT_UNKNOWN",
                              "the original environment update is unconfirmed; read it without replacing its ID") from None
            saved = item.etag or None
    runtime._agent_session = replacement
    return saved


async def _tasks_pending(runtime, current, source_sid, *, responses=False):
    if not current.admission_started:
        return None
    if runtime.task_observer is None:
        return "task_unconfirmed"
    try:
        async with asyncio.timeout(10):
            options = {"include_responses": True} if responses else {}
            quiet = await runtime.task_observer.quiet(source_sid, task_id=current.task_id, **options)
    except TimeoutError:
        logging.getLogger(__name__).warning("FA_NATIVE_FOLLOW_TASK_READ reason=read_deadline")
        return "task_unconfirmed"
    except Problem as error:
        logging.getLogger(__name__).warning("FA_NATIVE_FOLLOW_TASK_READ status=%s code=%s",
                                           error.status, error.body["error"]["code"])
        return "task_access_denied" if error.status in {401, 403} else "task_unconfirmed"
    if type(quiet) is not bool:
        raise Problem(503, "NATIVE_ENVIRONMENT_UNKNOWN", "the original task observation is invalid")
    return None if quiet else "task_active"


def _source_matches(current, view):
    if current.hosted_session_id != view.previous_hosted_session_id or (
            current.environment is not None and current.environment.state == "pending"
            and (current.environment.hosted_session_id, current.environment.request_id)
            != (view.hosted_session_id, view.request_id)):
        raise Problem(409, "NATIVE_ENVIRONMENT_CONFLICT", "another original environment transition won the comparison")


async def _compatible_retarget_following(runtime, target):
    record, _ = await _read(runtime)
    if record is not None and record.view.model_dump(exclude={"reason"}) != target.model_dump(exclude={"reason"}):
        raise Problem(409, "NATIVE_ENVIRONMENT_CONFLICT", "the destination has another original operation")
    return record


async def _freeze_files_for_retarget(runtime, original, request_id):
    from .native_turns import _changed

    record, etag, key = await _original_progress(runtime, original)
    if record.files_required:
        if record.retarget_request_id is not None:
            if record.retarget_request_id != request_id:
                raise Problem(409, "NATIVE_ENVIRONMENT_CONFLICT", "another explicit retarget owns the original file handoff")
            return record.view.source_invocation_refused
        files = record.files
        if files is not None and (files.sending is not None or files.publishing and not files.published):
            raise Problem(409, "NATIVE_FILES_UNKNOWN", "confirm the original file operation before selecting another target")
        await _progress(runtime, _changed(record, retarget_request_id=request_id), etag, key=key)
    return record.view.source_invocation_refused


async def retarget_session(runtime, command):
    from .native_turns import TURNS_STORE, _changed, _decode

    if runtime.identity.agent_version != command.expected_agent_version:
        raise Problem(409, "NATIVE_ENVIRONMENT_CONFLICT", "the destination is not the explicitly selected version")
    current, etag = await runtime._agent_record()
    key, progress_key = _retarget_keys(command.request_id)
    async with runtime._store(TURNS_STORE) as store:
        saved = await store.get_item(key)
    if saved is None:
        original = current.environment
        if (original is None or original.state != "pending"
                or original.request_id != command.pending_request_id
                or original.hosted_session_id != command.pending_session_id
                or original.previous_hosted_session_id != command.expected_session_id
                or current.hosted_session_id != command.expected_session_id
                or etag != command.expected_etag or command.request_id == original.request_id
                or runtime.identity.session_id in {original.previous_hosted_session_id, original.hosted_session_id}
                or int(runtime.identity.agent_version) <= int(original.hosted_agent_version)):
            raise Problem(409, "NATIVE_ENVIRONMENT_CONFLICT", "the original pending transition or new version changed")
        original_progress, _, _ = await _original_progress(runtime, original)
        target = NativeEnvironmentView(
            agent_id=runtime.identity.agent_id, request_id=command.request_id,
            previous_hosted_session_id=original.previous_hosted_session_id,
            previous_agent_version=original.previous_agent_version,
            previous_files_profile=original.previous_files_profile,
            source_invocation_refused=original.source_invocation_refused or original_progress.view.source_invocation_refused,
            hosted_session_id=runtime.identity.session_id, hosted_agent_version=runtime.identity.agent_version,
            state="pending", reason="source_stop_required",
        )
        intent = _Retarget(original=original, target=target)
        async with runtime._store(TURNS_STORE) as store:
            await runtime._immutable(store, key, intent)
    else:
        intent = _decode(saved.value, _Retarget)
    target, original = intent.target, intent.original
    if ((target.agent_id, target.hosted_session_id, target.hosted_agent_version, target.request_id)
            != (runtime.identity.agent_id, runtime.identity.session_id, runtime.identity.agent_version, command.request_id)
            or original.request_id != command.pending_request_id
            or original.hosted_session_id != command.pending_session_id
            or target.previous_hosted_session_id != command.expected_session_id):
        raise Problem(409, "NATIVE_ENVIRONMENT_CONFLICT", "this retarget ID already names another original operation")
    selected = current.environment
    if selected is not None and (
            selected.request_id, selected.hosted_session_id, selected.hosted_agent_version,
            selected.previous_hosted_session_id,
    ) == (target.request_id, target.hosted_session_id, target.hosted_agent_version, target.previous_hosted_session_id):
        # Re-enter follow to reconcile its phase instead of treating an older scan reason as a new block.
        return selected if selected.state == "ready" else _changed(
            target, source_invocation_refused=target.source_invocation_refused or selected.source_invocation_refused)
    if (selected is None or selected.model_dump(exclude={"source_invocation_refused"})
            != original.model_dump(exclude={"source_invocation_refused"})
            or current.hosted_session_id != original.previous_hosted_session_id):
        raise Problem(409, "NATIVE_ENVIRONMENT_CONFLICT", "the original pending operation is no longer current")
    record, record_etag = await _read(runtime, key=progress_key)
    if record is None:
        record = _Following(view=target, responses=False, files_required=runtime.workspace_files is not None,
                            idle_only=command.idle_only)
        await _progress(runtime, record, None, key=progress_key)
        record, record_etag = await _read(runtime, key=progress_key)
    if (record is None or record.view.request_id != target.request_id
            or record.view.hosted_session_id != target.hosted_session_id):
        raise Problem(409, "NATIVE_ENVIRONMENT_CONFLICT", "the new environment is reserved by another operation")
    if record.responses is None:
        record = _changed(record, phase="draining", after=None, responses=False, page_offset=0, page_digest=None)
    if record.cursor_profile is None:
        record = _changed(record, after=None, cursor_profile="page-last-id-v1", page_offset=0, page_digest=None)
    if current.admitting is not None:
        reason, after, responses = "admission_pending", record.after, record.responses
    else:
        progress = _ScanProgress.from_record(record)
        reason, after, responses = await _drain_pages(
            runtime, record.after, original.previous_hosted_session_id, current.task_id,
            responses=record.responses, idle_only=record.idle_only or record.files_required, progress=progress,
            restart_changed_page=record.idle_only)
        record = _changed(record, page_offset=progress.page_offset, page_digest=progress.page_digest,
                          waiting_request_id=progress.waiting_request_id)
        if reason is None:
            reason = await _tasks_pending(runtime, current, original.previous_hosted_session_id, responses=responses)
    record = _changed(record, responses=responses)
    if reason is not None:
        pending = _changed(record, view=_changed(target, reason=reason), after=after)
        await _progress(runtime, pending, record_etag, key=progress_key)
        return pending.view
    await _compatible_retarget_following(runtime, target)
    refused = await _freeze_files_for_retarget(runtime, original, command.request_id)
    if refused or selected.source_invocation_refused or record.view.source_invocation_refused:
        target = _changed(target, source_invocation_refused=True)
    await _save_agent(runtime, current, etag, _changed(current, environment=target))
    target = _changed(target, reason="files_pending" if record.files_required else "source_stop_required")
    await _progress(runtime, _changed(record, phase="files" if record.files_required else "stopping",
                                      after=None, view=target), record_etag, key=progress_key)
    return target


async def follow_session(runtime, command):
    from .native_turns import TURNS_STORE, _changed, binding_task_id

    current, etag = await runtime._agent_record()
    existing = current.environment
    same = existing is not None and (
        existing.hosted_session_id, existing.hosted_agent_version,
        existing.previous_hosted_session_id,
    ) == (runtime.identity.session_id, runtime.identity.agent_version, command.expected_session_id)
    if same and existing.state == "ready":
        return existing
    if existing is None or existing.state == "ready":
        # The last point where the record names how the source became the Agent's: a pending view replaces it below.
        from .native_open_rids import check_hold
        await check_hold(runtime, current.hosted_session_id, None if existing is None else existing.request_id)
    following_key = await _selected_following_key(runtime, current)
    record, record_etag = await _read(runtime, key=following_key)
    if record is not None and (
            record.view.previous_hosted_session_id != command.expected_session_id
            or record.view.hosted_agent_version != runtime.identity.agent_version
            or record.idle_only != command.idle_only):
        raise Problem(409, "NATIVE_ENVIRONMENT_CONFLICT", "the named environment has another original operation")
    if not same and record is None:
        if (existing is not None and existing.state == "pending"
                or (current.hosted_session_id, etag) != (command.expected_session_id, command.expected_etag)
                or current.hosted_session_id == runtime.identity.session_id or command.stage != "prepare"):
            raise Problem(409, "NATIVE_ENVIRONMENT_CONFLICT", "the original Agent environment comparison changed")
        source = existing if existing is not None else current.preparation
        if source is None:
            raise Problem(409, "NATIVE_ENVIRONMENT_UNAVAILABLE", "the Agent has no original prepared environment")
        # ADR-0009 4a/M5: the destination SID differs from the current one (checked above); its version is the one the
        # API named (the expected-version header), so a fresh session of the same version may follow too.
        existing = NativeEnvironmentView(
            agent_id=runtime.identity.agent_id, request_id=command.request_id,
            previous_hosted_session_id=current.hosted_session_id,
            previous_agent_version=source.hosted_agent_version,
            previous_files_profile=source.files_profile,
            hosted_session_id=runtime.identity.session_id, hosted_agent_version=runtime.identity.agent_version,
            state="pending", reason="source_stop_required",
        )
        record = _Following(view=existing, responses=False, idle_only=command.idle_only,
                            files_required=command.files_required or runtime.workspace_files is not None)
        await _progress(runtime, record, None, key=following_key)
        record, record_etag = await _read(runtime, key=following_key)
    if record is None:
        raise Problem(503, "NATIVE_ENVIRONMENT_UNKNOWN", "the original environment operation is missing")
    if record.retarget_request_id is not None:
        raise Problem(409, "NATIVE_ENVIRONMENT_CONFLICT", "the original file operation is handing off to an explicit retarget")
    from .native_files_omission import omission_for, omission_view
    omitted = await omission_for(runtime, record)
    if omitted is not None:
        record = _changed(record, view=omission_view(record.view, omitted))
    if omitted is None and ((command.files_required or runtime.workspace_files is not None) and not record.files_required
                            or record.files_required and runtime.workspace_files is None):
        return _changed(record.view, reason="files_unsupported")
    if record.responses is None:
        record = _changed(record, phase="draining", after=None, responses=False, page_offset=0, page_digest=None)
    if record.cursor_profile is None:
        record = _changed(record, after=None, cursor_profile="page-last-id-v1", page_offset=0, page_digest=None)
    existing = record.view
    if (current.environment is not None and current.environment.request_id == existing.request_id
            and current.environment.source_invocation_refused):
        existing = _changed(existing, source_invocation_refused=True)
        record = _changed(record, view=existing)
    if current.hosted_session_id != command.expected_session_id:
        raise Problem(409, "NATIVE_ENVIRONMENT_CONFLICT", "the original execution binding changed")
    if current.environment is not None and current.environment.state == "pending" and (
            current.environment.hosted_session_id != existing.hosted_session_id
            or current.environment.request_id != existing.request_id):
        raise Problem(409, "NATIVE_ENVIRONMENT_CONFLICT", "another original version transition is pending")
    if record.phase == "draining":
        pending_binding = (_changed(existing, files_migrated=None, files_skip_request_id=None,
                                    files_omission_basis=None, files_omission_reason=None)
                           if omitted is not None else existing)
        if current.following_supported and (current.environment is None or current.environment.state == "ready"):
            await save_before(runtime, current, existing.request_id)
            await _save_agent(runtime, current, etag, _changed(current, environment=pending_binding))
        if not record.idle_only and not record.files_required and omitted is None:
            await runtime._settle_admission()
        current, etag = await runtime._agent_record()
        _source_matches(current, existing)
        if current.admitting is not None:
            pending = _changed(record, view=_changed(existing, reason="admission_pending"))
            await _progress(runtime, pending, record_etag, key=following_key)
            return pending.view
        progress = _ScanProgress.from_record(record)
        reason, after, responses = await _drain_pages(
            runtime, record.after, existing.previous_hosted_session_id, current.task_id,
            responses=record.responses, idle_only=record.idle_only or record.files_required or omitted is not None,
            require_complete=omitted is not None, progress=progress, restart_changed_page=record.idle_only,
            settled=frozenset(command.settled_session_ids))
        record = _changed(record, responses=responses, page_offset=progress.page_offset, page_digest=progress.page_digest,
                          waiting_request_id=progress.waiting_request_id)
        if reason is not None:
            pending = _changed(record, view=_changed(existing, reason=reason), after=after)
            await _progress(runtime, pending, record_etag, key=following_key)
            return pending.view
        reason = await _tasks_pending(runtime, current, existing.previous_hosted_session_id, responses=record.responses)
        if reason is not None:
            pending = _changed(record, view=_changed(existing, reason=reason))
            await _progress(runtime, pending, record_etag, key=following_key)
            return pending.view
        # Preserve the original comparison through the final scan, including in-flight admission.
        if current.environment is None or current.environment.state == "ready":
            await save_before(runtime, current, existing.request_id)
        await _save_agent(runtime, current, etag, _changed(current, environment=pending_binding))
        need_files = record.files_required and omitted is None
        reason = "files_pending" if need_files else "source_stop_required"
        record = _changed(record, phase="files" if need_files else "stopping",
                          after=None, view=_changed(existing, reason=reason))
        await _progress(runtime, record, record_etag, key=following_key)
        record, record_etag = await _read(runtime, key=following_key)
        existing = record.view
        current, etag = await runtime._agent_record()
        _source_matches(current, existing)
    if record.phase == "files":
        if omitted is None and (record.files is None or not record.files.source_verified):
            return _changed(existing, reason="files_pending")
        record = _changed(record, phase="stopping", view=_changed(existing, reason="source_stop_required"))
        await _progress(runtime, record, record_etag, key=following_key)
        record, record_etag = await _read(runtime, key=following_key)
        existing = record.view
    if command.stage == "prepare":
        return _changed(existing, reason="source_stop_required")
    if runtime.bootstrap_source_inactive is None:
        return _changed(existing, reason="source_unavailable")
    try:
        inactive = await runtime.bootstrap_source_inactive(existing.previous_hosted_session_id)
    except Problem:
        return _changed(existing, reason="source_unavailable")
    if not inactive:
        return _changed(existing, reason="source_active")
    current, etag = await runtime._agent_record()
    _source_matches(current, existing)
    if (current.environment is None or current.environment.state != "pending"
            or current.environment.request_id != existing.request_id):
        # Withdrawn (``NativeSessionWithdraw``): the source took new work again; never commit over it. The commit's
        # CAS below covers a withdraw after this read.
        raise Problem(409, "NATIVE_ENVIRONMENT_CONFLICT", "the original pending transition was withdrawn")
    if current.admitting is not None:
        return _changed(existing, reason="admission_pending")
    progress = (_ScanProgress.from_record(record) if record.phase == "confirming"
                else _ScanProgress(cursor_source="none"))
    reason, after, responses = await _drain_pages(
        runtime, progress.after,
        existing.previous_hosted_session_id, current.task_id, responses=record.responses,
        idle_only=record.idle_only or record.files_required or omitted is not None,
        require_complete=omitted is not None, progress=progress, restart_changed_page=record.idle_only,
        settled=frozenset(command.settled_session_ids))
    if reason is not None:
        pending = _changed(record, phase="confirming", after=after, view=_changed(existing, reason=reason),
                           responses=responses, page_offset=progress.page_offset, page_digest=progress.page_digest,
                           waiting_request_id=progress.waiting_request_id)
        await _progress(runtime, pending, record_etag, key=following_key)
        return pending.view
    record = _changed(record, responses=responses, page_offset=0, page_digest=None, waiting_request_id=None)
    reason = await _tasks_pending(runtime, current, existing.previous_hosted_session_id, responses=record.responses)
    if reason is not None:
        pending = _changed(record, phase="confirming", after=None, view=_changed(existing, reason=reason))
        await _progress(runtime, pending, record_etag, key=following_key)
        return pending.view
    if record.files_required and omitted is None:
        from .native_environment_files import publish_files
        if record.files is None or not record.files.source_stopped_verified:
            return _changed(existing, reason="files_pending")
        record = await publish_files(runtime, record, record_etag, following_key)
        existing = record.view
    if omitted is not None and runtime.workspace_files is not None:
        runtime.workspace_files.initialize()
    ready = _changed(existing, state="ready", reason=None,
                     files_profile="fa-workspace-files-v1" if runtime.workspace_files is not None else None, **(
        {"files_migrated": True} if record.files_required and omitted is None else {}))
    identifier = "environment:" + material_digest(ready)
    async with runtime._store(TURNS_STORE) as store:
        await runtime._immutable(store, identifier, ready)
    await _save_agent(runtime, current, etag, _changed(
        current, hosted_session_id=runtime.identity.session_id, environment=ready, following_supported=True,
        chain_task_id=binding_task_id(runtime.identity.agent_id, runtime.identity.session_id)))
    if current.admission_started:
        # The final scan above saw every open receipt; from this commit only this session admits, and adds each RID.
        from .native_open_rids import mark_complete
        await mark_complete(runtime, runtime.identity.session_id, ready.request_id, responses=bool(record.responses))
    return ready
