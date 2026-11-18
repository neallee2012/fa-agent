"""`files_case`: one compacted SDK session's files built directly, without a submitted or claimed turn.

The legacy `checkpoint_case` reached the same files through `/work-policy/v1/submit`, a claimed Hosted session and
an uploaded first checkpoint. Here the first (ordinary) checkpoint is written straight into an in-memory Blob store
and Behavior table with the source's own save, model and key functions, so `load_state` reads it back unchanged.
"""
import hashlib
from types import SimpleNamespace

import pytest

from shared_context.behavior_operations import _write
from shared_context.behavior_sdk_checkpoint import SdkCheckpointStore, SdkStateUpload, checkpoint_key, load_state
from shared_context.behavior_sdk_files import SdkSessionFiles
from shared_context.behavior_store import BehaviorStore
from shared_context.behavior_sdk_state import (
    CompactionExecution, NativeCheckpointLineage, NativeCheckpointRef, NativeCheckpointScope, NativeCheckpointTurn,
    NATIVE_SEGMENT_BYTES,
)
from shared_context.behavior_wire import AssistantSdkStop
from shared_context.m3_store import InMemoryArtifactStore
from shared_context.source_materials import digest
from sdk_compaction_product_helpers import SUMMARY, checkpoint_profile, ordinary, raw_files
from test_behavior_store import SCOPE, FakeTable, changes, request

TENANT = "00000000-0000-4000-8000-000000000001"
SDK_SESSION = "sdk-files-case"
CONVERSATION = "files-case-conversation"


@pytest.fixture
def files_case():
    artifacts = InMemoryArtifactStore(TENANT)
    rows = BehaviorStore(FakeTable(), SCOPE)
    rows.initialize()
    binding = SimpleNamespace(tenant_id=TENANT, team_id="team-one", agent_oid="agent-one", job_id="files-case-first")
    profile = checkpoint_profile()
    session = SimpleNamespace(assignment=SimpleNamespace(sdk_session_id=SDK_SESSION, task=SimpleNamespace(
        instruction=SimpleNamespace(conversation_id=CONVERSATION, checkpoint_profile=profile, model_provider=None))))
    stop = AssistantSdkStop(pid=99, exit_code=0)
    previous = ordinary(SDK_SESSION)
    previous_ref = SdkCheckpointStore(artifacts).save(previous, binding, conversation_id=CONVERSATION,
                                                      checkpoint_profile=profile)
    upload = SdkStateUpload(job_id=binding.job_id, sdk_session_id=SDK_SESSION, conversation_id=CONVERSATION,
        sha256=previous_ref.sha256, byte_length=previous_ref.byte_length, sdk_stop=stop,
        prepare=request("files-case-first-upload"), reference=previous_ref)
    rows.transact(request("files-case-first-publish"),
                  lambda _: changes(_write(checkpoint_key(binding.job_id), upload, "create")))
    # Only what `load_state` asks of the authority: its two stores and the session's binding.
    authority = SimpleNamespace(store=rows, artifacts=SimpleNamespace(store=artifacts), _binding=lambda _: binding)
    assert load_state(authority, session, previous_ref, job_id=binding.job_id) == previous
    scope = NativeCheckpointScope(agent_id=binding.agent_oid, conversation_id=CONVERSATION, sdk_session_id=SDK_SESSION)
    previous_native = NativeCheckpointRef(codec="native-session-fs-v1", agent_id=scope.agent_id,
        conversation_id=CONVERSATION, sdk_session_id=SDK_SESSION, sha256=previous.sha256,
        byte_length=len(previous.encoded()), segments=-(-len(previous.encoded()) // NATIVE_SEGMENT_BYTES),
        lineage=NativeCheckpointLineage(request_id="files-case-first", turn_number=1, capture_digest="a" * 64))
    task = NativeCheckpointTurn(scope=scope, request_id="files-case-second", turn_number=2,
                                capture_digest="b" * 64, profile=profile, previous=previous_native)
    body = {"model": "gpt-4.1-mini", "messages": [{"role": "user", "content": "OFFLINE synthetic compact request"}],
            "stream": True}
    fs = SdkSessionFiles(SDK_SESSION, previous, checkpoint_profile=profile)
    fs.files = raw_files(previous)
    fs.compaction = CompactionExecution(job_id="files-case-second", conversation_id=CONVERSATION,
        sdk_session_id=SDK_SESSION, profile_digest="c" * 64, base_state_sha256=previous.sha256,
        runtime_pid=stop.pid, rpc_success=True, trigger="manual", model_request_sha256=digest(body),
        start_event_id="compact-start", complete_event_id="compact-complete", checkpoint_number=1,
        summary_sha256=hashlib.sha256(SUMMARY.encode()).hexdigest(), messages_removed=2, tokens_removed=20)
    return SimpleNamespace(
        h=SimpleNamespace(artifacts=SimpleNamespace(store=artifacts), store=rows, assistant=authority),
        binding=SimpleNamespace(job_id="files-case-second"), session=session, stop=stop,
        assignment=SimpleNamespace(task=task), compaction_request_sha256=digest(body),
        previous=previous, previous_ref=previous_ref, fs=fs)
