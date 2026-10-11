"""Complete SDK state outside inline rows; all model and Blob traffic is offline."""
import asyncio
from dataclasses import replace
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from shared_context.behavior_sdk_state import SdkState
from shared_context.behavior_sdk_files import SdkSessionFiles, STATE_PATH
from shared_context.behavior_sdk_checkpoint import (
    SdkCheckpointStore, SaveSdkState, SdkStateUpload, checkpoint_key, save_state, load_state,
)
from shared_context.behavior_assistant_wire import InstructionSubmit, SdkStop
from shared_context.behavior_wire import Command
from shared_context.behavior_operations import _row
from shared_context.errors import Problem
from shared_context.m3_store import InMemoryArtifactStore
from test_behavior_assistant import assistant
from test_instruction_flow import instruction_models, runtime_path
from test_m3_auth import signing_key, jwk_client
from test_work_policy_lifecycle import enabled


def state(sid="sdk-original"):
    return SdkState(sdk_session_id=sid, files={
        "events.jsonl": json.dumps({"type":"session.start","data":{"sessionId":sid}})+"\n",
        "workspace.yaml": "id: "+sid+"\n",
        "checkpoints/index.md": "# Checkpoint History\n",
        "temp/tool-output.txt": "Original tool result",
    }, directories=("checkpoints","temp"))


def binding():
    return SimpleNamespace(tenant_id="11111111-1111-4111-8111-111111111111", team_id="one",
                           agent_oid="agent-one", job_id="job-one")


@pytest.mark.parametrize("mutation",["scope","hash","version","missing"])
def test_recovery_refuses_wrong_scope_hash_version_or_missing_material(mutation):
    b=binding()
    store=InMemoryArtifactStore(b.tenant_id)
    checkpoints=SdkCheckpointStore(store)
    original=state()
    ref=checkpoints.save(original,b,conversation_id="conversation")
    if mutation=="scope":
        ref=ref.model_copy(update={"agent_oid":"other"})
    elif mutation=="hash":
        ref=ref.model_copy(update={"sha256":"f"*64})
    elif mutation=="version":
        ref=ref.model_copy(update={"runtime_version":"newer"})
    else:
        del store._versions[(b.team_id,ref.artifact_id,ref.parts[0].version_id)]
    with pytest.raises((Problem,ValueError)):
        checkpoints.load(ref,b,conversation_id="conversation",sdk_session_id=original.sdk_session_id,job_id=b.job_id)


def test_all_sdk_files_survive_storage_without_inline_limits():
    b=binding(); original=state()
    original.files["temp/tool-output.txt"]="".join(hashlib.sha256(str(i).encode()).hexdigest()+"\n" for i in range(9000))
    checkpoints=SdkCheckpointStore(InMemoryArtifactStore(b.tenant_id))
    ref=checkpoints.save(original,b,conversation_id="conversation")
    assert len(ref.parts)==2 and ref.byte_length>131072
    assert len(ref.model_dump_json().encode())<16000
    assert checkpoints.load(ref,b,conversation_id="conversation",sdk_session_id=original.sdk_session_id,job_id=b.job_id)==original


def test_sdk_files_cannot_escape_or_publish_after_io_error():
    fs=SdkSessionFiles("sdk-original",state())
    with pytest.raises(PermissionError):
        asyncio.run(fs.read_file("/session-state/../another/events.jsonl"))
    with pytest.raises(Problem):
        fs.snapshot()
    clean=SdkSessionFiles("sdk-original",state())
    assert asyncio.run(clean.read_file(STATE_PATH+"/temp/tool-output.txt"))=="Original tool result"
    clean.snapshot()
    with pytest.raises(PermissionError):
        asyncio.run(clean.write_file(STATE_PATH+"/events.jsonl","replacement"))
    with pytest.raises(Problem):
        SdkSessionFiles("other",state())


def assigned(assistant):
    h,control,human,_=enabled(assistant)
    b,_=control.submit(human,InstructionSubmit(request_id="state-checkpoint",content="OFFLINE source task"))
    starts=[]
    h.workers.adapter=replace(h.workers.adapter,start=lambda command:starts.append(command))
    h.workers.start(b.job_id,b.owner)
    assignment,ticket=h.assistant.issue(starts[0])
    principal=h.verifier(h.client.build_request("GET","/",headers=h.headers("agent")))
    h.assistant.acquire_current(b.binding_id,assignment.session_id,ticket,Command(request_id="sdk-start"),principal)
    session=h.assistant.current_session(b.binding_id,assignment.session_id,ticket,principal)
    return h,b,session


@pytest.mark.parametrize("failure",["partial-put","commit-unknown"])
def test_partial_or_unknown_checkpoint_never_reuploads_or_publishes_empty(assistant,monkeypatch,failure):
    h,b,session=assigned(assistant)
    previous_binding=SimpleNamespace(tenant_id=b.tenant_id,team_id=b.team_id,agent_oid=b.agent_oid,job_id="prior-job")
    checkpoints=SdkCheckpointStore(h.assistant.artifacts.store)
    previous=state(session.assignment.sdk_session_id)
    prior_ref=checkpoints.save(previous,previous_binding,conversation_id=session.assignment.task.instruction.conversation_id)
    original=state(session.assignment.sdk_session_id)
    original.files["temp/tool-output.txt"]="".join(hashlib.sha256(str(i).encode()).hexdigest()+"\n" for i in range(9000))
    command=SaveSdkState(request_id="sdk-state",state=original,sdk_stop=SdkStop(pid=42,exit_code=0,graceful=True))
    artifacts=h.assistant.artifacts.store
    put=artifacts.put;calls=[]
    def broken_put(*args):
        calls.append(args)
        if failure=="partial-put" and len(calls)==2:
            raise Problem(503,"ARTIFACT_UNKNOWN","OFFLINE response unknown")
        return put(*args)
    monkeypatch.setattr(artifacts,"put",broken_put)
    transact=h.store.transact
    def broken_commit(request,decide,**kwargs):
        from shared_context.behavior_assistant import key
        if failure=="commit-unknown" and request.request_id==key("sdk-state-complete",session.assignment.session_id):
            raise Problem(503,"BEHAVIOR_COMMIT_UNKNOWN","OFFLINE commit response unknown")
        return transact(request,decide,**kwargs)
    monkeypatch.setattr(h.store,"transact",broken_commit)
    with pytest.raises(Problem):save_state(h.assistant,session,command)
    assert len(calls)==2
    pending=_row(h.store.read_snapshot((checkpoint_key(b.job_id),)),checkpoint_key(b.job_id),SdkStateUpload)
    assert pending.reference is None
    before=len(calls)
    with pytest.raises(Problem):save_state(h.assistant,session,command)
    assert len(calls)==before
    assert checkpoints.load(prior_ref,b,conversation_id=session.assignment.task.instruction.conversation_id,
                            sdk_session_id=session.assignment.sdk_session_id,job_id="prior-job")==previous


def test_checkpoint_completion_and_exact_readback_are_bound_to_original_job(assistant):
    h,b,session=assigned(assistant)
    original=state(session.assignment.sdk_session_id)
    command=SaveSdkState(request_id="sdk-state",state=original,sdk_stop=SdkStop(pid=42,exit_code=0,graceful=True))
    ref=save_state(h.assistant,session,command)
    assert load_state(h.assistant,session,ref,job_id=b.job_id)==original
    assert save_state(h.assistant,session,command)==ref
    with pytest.raises(Problem):
        load_state(h.assistant,session,ref.model_copy(update={"conversation_id":"wrong"}),job_id=b.job_id)


def test_legacy_inline_archive_is_unchanged_and_importable_into_sdk_fs(tmp_path):
    from shared_context.behavior_assistant_checkpoint import export,unpack,restore
    from shared_context.behavior_assistant_wire import SdkArchive,ArchiveUnavailable
    original=state()
    directory=tmp_path/"old"/"config"/"session-state"/original.sdk_session_id
    directory.mkdir(parents=True)
    for name in ("events.jsonl","workspace.yaml"):
        (directory/name).write_text(original.files[name],encoding="utf-8")
    inline=export(tmp_path/"old",original.sdk_session_id)
    encoded=inline.model_dump_json()
    assert SdkArchive.model_validate_json(encoded).model_dump_json()==encoded
    restored=SdkState(sdk_session_id=inline.sdk_session_id,files=unpack(inline))
    fs=SdkSessionFiles(inline.sdk_session_id,restored)
    assert fs.restored and fs.files==unpack(inline)
    restore(inline,tmp_path/"legacy-restore")
    assert (tmp_path/"legacy-restore"/"config"/"session-state"/inline.sdk_session_id/"events.jsonl").is_file()
    unavailable=ArchiveUnavailable(error="ASSISTANT_CHECKPOINT_LIMIT")
    assert ArchiveUnavailable.model_validate_json(unavailable.model_dump_json())==unavailable


def test_resume_cannot_start_with_an_empty_same_id_filesystem(tmp_path):
    from shared_context.m2_copilot import run_copilot
    from shared_context.behavior_assistant_boundary import AssistantModelBoundary
    async def reserved(_):raise AssertionError("no model may be requested")
    with pytest.raises(ValueError,match="Resume requires restored"):
        asyncio.run(run_copilot(state_dir=tmp_path/"sdk",working_directory=tmp_path,
            sdk_session_id="sdk-original",resume=True,prompt="Continue",tools=[],
            api_url="https://offline.invalid",token_provider=lambda:"OFFLINE",on_event=lambda _:None,
            runtime_path=tmp_path/"does-not-exist",behavior_boundary=AssistantModelBoundary(reserved,text_reply=True),
            instruction=True,session_files=SdkSessionFiles("sdk-original")))
