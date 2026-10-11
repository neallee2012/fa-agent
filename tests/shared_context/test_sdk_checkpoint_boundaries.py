"""API dependency isolation and the pre-existing public-only checkpoint contract."""
import asyncio
import json
from pathlib import Path
import subprocess
import sys

import pytest
import httpx

from shared_context.behavior_sdk_files import SdkSessionFiles
from shared_context.behavior_sdk_state import CheckpointProfile, SdkState
from shared_context.behavior_checkpoint_privacy import jsonl_lines
from shared_context.errors import Problem


def test_api_entrypoint_imports_without_the_hosted_copilot_dependency():
    source=Path(__file__).resolve().parents[2]/"src"/"fa-shared-context-poc"
    program=r"""
import importlib.abc,sys
class NoHostedSdk(importlib.abc.MetaPathFinder):
    def find_spec(self,fullname,path=None,target=None):
        if fullname=="copilot" or fullname.startswith("copilot."):
            raise ModuleNotFoundError("Hosted copilot dependency forbidden in API",name=fullname)
sys.meta_path.insert(0,NoHostedSdk())
sys.path.insert(0,sys.argv[1])
import shared_context.behavior_assistant_wire
import shared_context.behavior_assistant_api
import shared_context.behavior_server
import shared_context.behavior_sdk_compaction
from shared_context.behavior_sdk_state import CheckpointProfile, SaveSdkState
assert CheckpointProfile().name == "manual-no-custom-v1"
assert "state" in SaveSdkState.model_json_schema()["properties"]
assert not any(name=="copilot" or name.startswith("copilot.") for name in sys.modules)
print("API imports without Hosted SDK")
"""
    result=subprocess.run([sys.executable,"-I","-c",program,str(source)],capture_output=True,
                          text=True,encoding="utf-8",timeout=30)
    assert result.returncode==0,result.stdout+result.stderr


@pytest.mark.parametrize("separator", [" ", "\u0085", "\u2028", "\u2029"],
                         ids=["space", "next-line", "line-separator", "paragraph-separator"])
def test_new_filesystem_snapshot_preserves_existing_private_event_filter(separator):
    markers=("CTX03_PRIVATE_SYSTEM","CTX03_PRIVATE_REASONING","CTX03_PRIVATE_BLOCK")
    answer="Allowed"+separator+"final answer"
    events=[
        {"id":"start","type":"session.start","data":{"sessionId":"private-test"}},
        {"id":"sys","parentId":"start","type":"system.message","data":{"content":markers[0]}},
        {"id":"thought","parentId":"sys","type":"assistant.reasoning","data":{
            "content":markers[1],"reasoning_blocks":[{"text":markers[2]}]}},
        {"id":"answer","parentId":"thought","type":"assistant.message","data":{"content":answer}},
    ]
    fs=SdkSessionFiles("private-test")
    async def prepare():
        await fs.write_file("/session-state/events.jsonl","".join(json.dumps(e,ensure_ascii=False)+"\n" for e in events))
        await fs.write_file("/session-state/workspace.yaml","id: private-test\n")
    asyncio.run(prepare())
    snapshot=fs.snapshot()
    raw=snapshot.encoded().decode()
    assert all(marker not in raw for marker in markers)
    retained=[json.loads(line) for line in jsonl_lines(snapshot.files["events.jsonl"])]
    assert [(e["id"],e.get("parentId")) for e in retained]==[(e["id"],e.get("parentId")) for e in events]
    assert retained[-1]["data"]["content"]==answer


def files_with_private_marker():
    return {"workspace.yaml":"id: private-test\n","events.jsonl":json.dumps(
        {"id":"start","type":"session.start","data":{"sessionId":"private-test"}})+"\n"+json.dumps(
        {"id":"system","parentId":"start","type":"system.message","data":{"content":"CTX03_PRIVATE_SYSTEM"}})+"\n"}


@pytest.mark.parametrize("extra",["compaction-snapshot","opaque-sidecar","reasoning-copy","credential"])
def test_unknown_private_copies_are_blocked_without_editing_sdk_files(extra):
    fs=SdkSessionFiles("private-test")
    fs.files=files_with_private_marker()
    if extra=="compaction-snapshot":
        fs.files["events.jsonl"]+=json.dumps({"type":"model.messages_snapshot","data":{
            "messages":[{"role":"system","content":"CTX03_PRIVATE_SYSTEM"}]}})+"\n"
    elif extra=="opaque-sidecar":
        fs.files["checkpoints/001-copy.md"]="Native file containing CTX03_PRIVATE_SYSTEM"
    elif extra=="reasoning-copy":
        fs.files["events.jsonl"]+=json.dumps({"type":"assistant.message","data":{
            "content":"Allowed","reasoning_blocks":[{"type":"text","text":"CTX03_BLOCK_PRIVATE_COPY"}]}})+"\n"
        fs.files["temp/private-copy.txt"]="CTX03_BLOCK_PRIVATE_COPY"
    else:
        fs.forbid_value("CTX03_TRANSPORT_CREDENTIAL_CANARY")
        fs.files["temp/copied.txt"]="CTX03_TRANSPORT_CREDENTIAL_CANARY"
    before=dict(fs.files)
    with pytest.raises(Problem) as blocked:
        fs.snapshot()
    assert blocked.value.body["error"]["code"]=="SDK_STATE_PRIVATE_MATERIAL"
    assert fs.files==before


def test_api_dto_does_not_accept_raw_private_state_from_a_client():
    with pytest.raises(Problem) as blocked:
        SdkState(sdk_session_id="private-test",files=files_with_private_marker())
    assert blocked.value.body["error"]["code"]=="SDK_STATE_PRIVATE_MATERIAL"


def test_legacy_event_transform_bytes_are_preserved(tmp_path):
    from shared_context.behavior_assistant_checkpoint import export,unpack
    directory=tmp_path/"config"/"session-state"/"private-test"
    directory.mkdir(parents=True)
    originals=files_with_private_marker()
    for name,text in originals.items():
        (directory/name).write_text(text,encoding="utf-8")
    legacy=unpack(export(tmp_path,"private-test",public_only=True))
    fs=SdkSessionFiles("private-test");fs.files=originals
    assert fs.snapshot().files["events.jsonl"]==legacy["events.jsonl"]


def test_hosted_checkpoint_rejects_known_transport_credentials_before_upload(tmp_path):
    from types import SimpleNamespace
    from pydantic import SecretStr
    from shared_context.behavior_assistant_hosted import AssistantClient,AssistantSession
    from shared_context.behavior_assistant_wire import SdkStop
    token="CTX03_REAL_CLIENT_TRANSPORT_CANARY"
    def no_upload(_):
        pytest.fail("private material must be rejected before any HTTP upload")
    client=AssistantClient("https://offline.invalid",SimpleNamespace(task=SimpleNamespace(instruction=None)),SecretStr("a"*64),
                           token_provider=lambda:token,transport=httpx.MockTransport(no_upload))
    try:
        assert client._token()==token
        fs=SdkSessionFiles("private-test");fs.files=files_with_private_marker()
        fs.files["temp/copied.txt"]=token
        session=AssistantSession(client,root=tmp_path,runtime_path=tmp_path/"not-started")
        session._session_files=fs
        session._sdk_stop=SdkStop(pid=42,exit_code=0,graceful=True)
        result=session._archive()
        assert result.error=="SDK_STATE_PRIVATE_MATERIAL"
    finally:
        client.close()


@pytest.mark.parametrize("separator", ["\u2028", "\u2029", "\u0085"], ids=["u2028", "u2029", "u0085"])
@pytest.mark.parametrize("line_ending", ["\n", "\r\n"], ids=["lf", "crlf"])
def test_unicode_content_round_trips_ordinary_and_inline_jsonl(tmp_path, separator, line_ending):
    from shared_context.behavior_assistant_checkpoint import export, restore, unpack
    from shared_context.behavior_checkpoint_privacy import checkpoint_events
    content = "Public before" + separator + "public after"
    events = [{"type": "session.start", "data": {"sessionId": "unicode"}},
              {"type": "user.message", "data": {"content": content}}]
    files = {"events.jsonl": line_ending.join(json.dumps(e, ensure_ascii=False) for e in events) + line_ending,
             "workspace.yaml": "id: unicode\n"}
    original = SdkState(sdk_session_id="unicode", files=files)
    copied = SdkSessionFiles("unicode", original).snapshot()
    assert checkpoint_events(copied.files["events.jsonl"]) == events
    directory = tmp_path / "source" / "config" / "session-state" / "unicode"
    directory.mkdir(parents=True)
    for name, text in files.items():
        (directory / name).write_text(text, encoding="utf-8", newline="")
    legacy = export(tmp_path / "source", "unicode", public_only=True)
    assert checkpoint_events(unpack(legacy)["events.jsonl"]) == events
    restore(legacy, tmp_path / "restored")
    state = SdkState(sdk_session_id="unicode", files=unpack(legacy))
    assert SdkSessionFiles("unicode", state).snapshot().files == state.files


@pytest.mark.parametrize("malformed", ["\n", " \t\n", "{not-json}\n", "null\n"])
def test_jsonl_blank_or_invalid_records_still_fail_explicitly(malformed):
    fs = SdkSessionFiles("invalid")
    fs.files = {"workspace.yaml": "id: invalid\n",
                "events.jsonl": json.dumps({"type": "session.start", "data": {"sessionId": "invalid"}}) + "\n" + malformed}
    with pytest.raises(Problem):
        fs.snapshot()


@pytest.mark.parametrize("system,machine,selected,accepted", [
    ("win32", "AMD64", "win32-x64", True),
    ("linux", "x86_64", "linux-x64", True),
    ("linux", "AMD64", "linux-x64", True),
    ("linux", "x86_64", "win32-x64", False),
    ("win32", "AMD64", "linux-x64", False),
    ("linux", "aarch64", "linux-x64", False),
    ("darwin", "arm64", "win32-x64", False),
    ("unknown", "x86_64", "linux-x64", False),
])
def test_checkpoint_profile_requires_exact_native_platform(monkeypatch, system, machine, selected, accepted):
    from shared_context import behavior_sdk_files as adapter
    monkeypatch.setattr(adapter.sys, "platform", system)
    monkeypatch.setattr(adapter.platform, "machine", lambda: machine)
    profile = CheckpointProfile(platform=selected)
    if accepted:
        fs = SdkSessionFiles("native-platform", checkpoint_profile=profile)
        assert fs.checkpoint_profile == profile and not fs.restored
    else:
        with pytest.raises(Problem) as denied:
            SdkSessionFiles("native-platform", checkpoint_profile=profile)
        assert denied.value.body["error"]["code"] == "SDK_STATE_UNSUPPORTED_FORMAT"
    ordinary = SdkSessionFiles("ordinary-platform")
    assert ordinary.checkpoint_profile is None


def test_existing_windows_checkpoint_profile_serialization_is_unchanged():
    original = '{"name":"manual-no-custom-v1","platform":"win32-x64","provider":"openai-completions"}'
    assert CheckpointProfile().model_dump_json() == original
    assert CheckpointProfile.model_validate_json(original).model_dump_json() == original
    with pytest.raises(ValueError):
        CheckpointProfile(platform="linux-arm64")
