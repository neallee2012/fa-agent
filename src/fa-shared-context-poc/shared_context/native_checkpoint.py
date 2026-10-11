"""ADR-0007 N3: SDK checkpoints in the Agent's own Foundry State store, plus a verified ``$HOME`` hot copy. Hosted only.

The State store is the authority (Neal 2026-10-01 16:00); the hot copy only saves reads. Both are verified by hash
against the ``NativeCheckpointRef`` that N1 writes into ``turn:<n>``; nothing is trusted because it merely exists.

- One checkpoint is ``ceil(bytes / 512 KiB)`` items ``ckpt:<sha256>:<i>`` in ``fa/v1/conv/<CID>``, written with
  ``create_item`` ("fail on duplicate keys"). A 409 is accepted only when the stored segment is byte-identical: keys
  are content-addressed, so it is the same checkpoint after a lost ACK or a retried save, never an overwrite.
- ``save_checkpoint`` returns the reference only after every segment was read back and verified. An unknown store
  outcome raises 503 ``SDK_STATE_UPLOAD_UNKNOWN``; the caller must not treat it as saved or switch to another path.
- ``verify_checkpoint`` always reads every segment from the store, never the hot copy: N1 uses it before it marks a
  re-entered turn completed. ``load_checkpoint`` may use a hot copy whose length and hash match the reference.
- Native manual compaction retains the original RID/CID source; legacy job-bound archives stay separate.
- Stores are created with ``item_ttl_seconds=-1`` and re-read on open: the default 30-day TTL is fixed at creation
  and ``get_or_create`` ignores the argument for an existing store, so a store that would expire is refused.

Nothing here imports the SDK at module load; ``store`` is a ``FoundryStateStore`` (async ``create_item``/``get_item``).
"""

from __future__ import annotations

import base64
import asyncio
import binascii
import hashlib
import logging
import os
from pathlib import Path

from .behavior_sdk_state import (
    DIAG_CODECS, NATIVE_CODECS, NATIVE_SEGMENT_BYTES, NativeCheckpointRef, NativeCheckpointScope,
    AUTO_PROFILE, NativeCheckpointSource, NativeCheckpointTurn, NativeCompactionExecution, SdkState,
)
from .errors import Problem

SEGMENT_KIND = "fa-native-checkpoint-segment-v1"


def conversation_store_name(conversation_id: str) -> str:
    return "fa/v1/conv/" + conversation_id


def segment_key(sha256: str, index: int) -> str:
    return f"ckpt:{sha256}:{index}"


def _storage():
    from azure.ai.agentserver.core import storage
    return storage


def _unconfirmed():
    # The pinned SDK re-raises transport, lost-response and token failures unwrapped (storage/_client.py).
    from azure.core.exceptions import AzureError
    return (_storage().FoundryStorageError, AzureError, OSError)


async def open_durable_store(name, *, create, credential=None, endpoint=None):
    """An open store whose items never expire. ``create=False`` only opens an existing store (404 when missing)."""
    storage = _storage()
    store = None
    try:
        if create:
            store = await storage.FoundryStateStore.get_or_create(name, credential, endpoint, item_ttl_seconds=-1)
        else:
            store = storage.FoundryStateStore(name, credential, endpoint)
        ttl = (await store.get()).item_ttl_seconds
    except storage.FoundryStorageNotFoundError:
        await _close(store)
        raise Problem(404, "NATIVE_STATE_STORE_MISSING", "the State store does not exist; nothing was created") from None
    except _unconfirmed():
        await _close(store)
        raise Problem(503, "NATIVE_STATE_STORE_UNKNOWN", "the State store could not be confirmed") from None
    except asyncio.CancelledError:
        await _close(store)
        raise
    if ttl != -1:
        await _close(store)
        raise Problem(409, "NATIVE_STATE_STORE_TTL", "the State store would expire its records; use a new store name")
    return store


async def _close(store):
    if store is not None:
        await store.aclose()


def _chunks(raw: bytes):
    return [raw[offset:offset + NATIVE_SEGMENT_BYTES] for offset in range(0, len(raw), NATIVE_SEGMENT_BYTES)]


def _segment(ref: NativeCheckpointRef, index: int, chunk: bytes):
    return {"kind": SEGMENT_KIND, "sha256": ref.sha256, "index": index, "count": ref.segments,
            "segment_sha256": hashlib.sha256(chunk).hexdigest(), "data": base64.b64encode(chunk).decode("ascii")}


def _require_scope(scope: NativeCheckpointScope, ref: NativeCheckpointRef):
    if NativeCheckpointScope.model_validate_json(scope.model_dump_json()) != ref.scope:
        raise Problem(403, "ASSISTANT_CHECKPOINT_SCOPE", "native checkpoint reference belongs to another scope")


def _state_from(raw: bytes, ref: NativeCheckpointRef) -> SdkState:
    try:
        state = SdkState.model_validate_json(raw)
        if (state.encoded() != raw or state.sdk_session_id != ref.sdk_session_id
                or state.model_provider != ref.model_provider or NATIVE_CODECS.get(state.codec) != ref.codec):
            raise ValueError()
    except ValueError:
        raise Problem(409, "SDK_STATE_INCOMPLETE", "native SDK state version, identity or content differs") from None
    if ref.source is not None:
        from .behavior_sdk_compaction import events_from, validate_execution
        validate_execution(events_from(state.files), ref.source.execution)
    return state


async def _read_segments(store, ref: NativeCheckpointRef, *, unknown="NATIVE_STATE_STORE_UNKNOWN", read_timeout=None) -> bytes:
    chunks = []
    for index in range(ref.segments):
        try:
            async with asyncio.timeout(read_timeout):
                item = await store.get_item(segment_key(ref.sha256, index))
        except _unconfirmed():
            raise Problem(503, unknown, "a native checkpoint segment could not be read") from None
        try:
            value = item.value
            expected = min(NATIVE_SEGMENT_BYTES, ref.byte_length - index * NATIVE_SEGMENT_BYTES)
            chunk = base64.b64decode(value["data"], validate=True)
            if ((value["kind"], value["sha256"], value["index"], value["count"]) != (
                    SEGMENT_KIND, ref.sha256, index, ref.segments) or len(chunk) != expected
                    or hashlib.sha256(chunk).hexdigest() != value["segment_sha256"]):
                raise ValueError()
        except (AttributeError, KeyError, TypeError, ValueError, binascii.Error):
            raise Problem(409, "SDK_STATE_INCOMPLETE", "a native checkpoint segment is missing or differs") from None
        chunks.append(chunk)
    raw = b"".join(chunks)
    if len(raw) != ref.byte_length or hashlib.sha256(raw).hexdigest() != ref.sha256:
        raise Problem(409, "SDK_STATE_INCOMPLETE", "the complete native checkpoint hash differs")
    return raw


def _cache_path(cache_root, ref: NativeCheckpointRef):
    if cache_root is None:
        return None
    conversation = hashlib.sha256(
        f"fa-native-checkpoint-cache-v1\0{ref.agent_id}\0{ref.conversation_id}".encode("utf-8")).hexdigest()[:32]
    return Path(cache_root) / conversation / (ref.sha256 + ".json")


def _read_cache(cache_root, ref):
    path = _cache_path(cache_root, ref)
    try:
        if path is None or path.parent.is_symlink() or path.is_symlink() or not path.is_file():
            return None
        if path.stat().st_size != ref.byte_length:
            return None
        raw = path.read_bytes()
    except OSError:
        return None
    return raw if hashlib.sha256(raw).hexdigest() == ref.sha256 else None


def _write_cache(cache_root, ref, raw):
    """Best effort: keep only this conversation's latest verified checkpoint; a failure only costs a store read."""
    path = _cache_path(cache_root, ref)
    if path is None:
        return
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.parent.is_symlink():
            return
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_bytes(raw)
        os.replace(temporary, path)
        for other in path.parent.glob("*.json"):
            if other != path and not other.is_symlink():
                other.unlink()
    except OSError:
        logging.getLogger(__name__).warning("FA_NATIVE_CHECKPOINT_CACHE status=write_failed")


def _require_profile(ref, profile):
    if ref.source is not None and (profile is None or profile != ref.source.profile):
        raise Problem(409, "SDK_STATE_UNSUPPORTED_FORMAT", "the native checkpoint consumer profile differs or is disabled")


def _reference(ref):
    if (ref.codec not in NATIVE_CODECS.values()
            or ref.sdk_version != "1.0.13" or ref.runtime_version != "1.0.83"):
        raise Problem(409, "SDK_STATE_UNSUPPORTED_FORMAT", "the native checkpoint format or version is unsupported")
    try:
        return NativeCheckpointRef.model_validate_json(ref.model_dump_json())
    except ValueError:
        raise Problem(409, "SDK_STATE_INCOMPLETE", "the native checkpoint reference is incomplete") from None


async def _source(store, scope, state, turn, compaction, sdk_stop):
    from .behavior_sdk_compaction import events_from, validate_execution, validate_inherited, unverified

    if turn is None:
        if state.codec in DIAG_CODECS or compaction is not None:
            raise Problem(409, "SDK_STATE_UNSUPPORTED_FORMAT", "native diagnostic state requires its original RID source")
        return None
    if turn.scope != scope:
        raise Problem(403, "ASSISTANT_CHECKPOINT_SCOPE", "the native checkpoint turn belongs to another scope")
    previous = turn.previous
    if state.codec not in DIAG_CODECS:
        # manual compacts on its first resume; auto stays ordinary state, including the runtime's own compactions.
        automatic = turn.profile is not None and turn.profile.name == AUTO_PROFILE
        if (compaction is not None or previous is not None and (
                previous.source is not None or turn.profile is not None and not automatic)):
            unverified()
        return None
    if turn.profile is None or previous is None or previous.lineage is None:
        unverified()
    prior = await verify_checkpoint(store, scope, previous, checkpoint_profile=turn.profile)
    events = events_from(state.files)
    validate_inherited(events, prior)
    if prior.codec in DIAG_CODECS:
        if compaction is not None or state.projected_event_ids != prior.projected_event_ids:
            unverified()
        source = previous.source
    else:
        if (not isinstance(compaction, NativeCompactionExecution) or sdk_stop is None
                or not sdk_stop.graceful or sdk_stop.release is not None
                or (compaction.agent_id, compaction.request_id, compaction.conversation_id,
                    compaction.sdk_session_id, compaction.capture_digest, compaction.base_state_sha256,
                    compaction.runtime_pid)
                != (scope.agent_id, turn.request_id, scope.conversation_id, scope.sdk_session_id,
                    turn.capture_digest, previous.sha256, sdk_stop.pid)):
            unverified()
        source = NativeCheckpointSource(profile=turn.profile, execution=compaction)
    validate_execution(events, source.execution)
    return source


async def save_checkpoint(store, scope: NativeCheckpointScope, state: SdkState, *, cache_root=None,
                          turn: NativeCheckpointTurn | None = None,
                          compaction: NativeCompactionExecution | None = None, sdk_stop=None) -> NativeCheckpointRef:
    if state.codec not in NATIVE_CODECS:
        raise Problem(409, "SDK_STATE_UNSUPPORTED_FORMAT", "the native checkpoint codec is unsupported")
    if state.codec in DIAG_CODECS and (turn is None or turn.profile is None):
        raise Problem(409, "SDK_STATE_UNSUPPORTED_FORMAT", "native diagnostic state requires its captured manual profile")
    state = SdkState.model_validate_json(state.model_dump_json())
    scope = NativeCheckpointScope.model_validate_json(scope.model_dump_json())
    if (state.sdk_session_id, state.model_provider) != (scope.sdk_session_id, scope.model_provider):
        raise Problem(403, "ASSISTANT_CHECKPOINT_SCOPE", "SDK state belongs to another session or provider")
    source = await _source(store, scope, state, turn, compaction, sdk_stop)
    raw = state.encoded()
    chunks = _chunks(raw)
    ref = NativeCheckpointRef(
        codec=NATIVE_CODECS[state.codec], model_provider=state.model_provider, agent_id=scope.agent_id,
        conversation_id=scope.conversation_id, sdk_session_id=scope.sdk_session_id,
        sha256=hashlib.sha256(raw).hexdigest(), byte_length=len(raw), segments=len(chunks),
        lineage=None if turn is None else turn.lineage, source=source)
    storage = _storage()
    for index, chunk in enumerate(chunks):
        try:
            await store.create_item(segment_key(ref.sha256, index), _segment(ref, index, chunk))
        except storage.FoundryStorageConflictError:
            pass  # content-addressed: the read-back below accepts only a byte-identical segment
        except _unconfirmed():
            raise Problem(503, "SDK_STATE_UPLOAD_UNKNOWN", "a native checkpoint segment write is unconfirmed") from None
    # A write acknowledgment alone is not a complete, restorable checkpoint.
    await _read_segments(store, ref, unknown="SDK_STATE_UPLOAD_UNKNOWN")
    _write_cache(cache_root, ref, raw)
    return ref


async def verify_checkpoint(store, scope: NativeCheckpointScope, ref: NativeCheckpointRef, *,
                            checkpoint_profile=None, read_timeout=None) -> SdkState:
    """Every segment from the store itself; a hot copy never stands in for a missing remote segment."""
    ref = _reference(ref)
    _require_scope(scope, ref)
    _require_profile(ref, checkpoint_profile)
    return _state_from(await _read_segments(store, ref, read_timeout=read_timeout), ref)


async def load_checkpoint(store, scope: NativeCheckpointScope, ref: NativeCheckpointRef, *, cache_root=None,
                          checkpoint_profile=None) -> SdkState:
    ref = _reference(ref)
    _require_scope(scope, ref)
    _require_profile(ref, checkpoint_profile)
    raw = _read_cache(cache_root, ref)
    if raw is not None:
        try:
            return _state_from(raw, ref)
        except Problem:
            pass  # a hot copy is never an authority; read the store instead
    raw = await _read_segments(store, ref)
    state = _state_from(raw, ref)
    _write_cache(cache_root, ref, raw)
    return state
