"""TN-1 Team note sharing: a human's own verbatim suggestion is created once in the Team memory scope; Team Agents
recall Team notes on human turns. Off by default.

Offline: official local chain/store, named SDK and memory-service doubles; no network, model or token service.
"""

import asyncio
import json
import logging
import time
from types import SimpleNamespace

import httpx
import pytest
from starlette.testclient import TestClient

from shared_context import behavior_private_memory as memory
from shared_context import native_memory_candidate as candidates
from shared_context import native_sdk
from shared_context import native_team_memory as team
from shared_context.behavior_assistant_hosted import AssistantSupervisor
from shared_context.behavior_hosted import create_host
from shared_context.behavior_native_capture import TEAM_MEMORY_ENV, team_memory_capture
from shared_context.behavior_native_turn_wire import NativeSubmit, NativeTurnCapture, TeamMemoryCapture, team_memory_scope
from shared_context.m2_copilot import ToolFailure
from shared_context.native_agent_tools import NativeAgentRelayClient
from shared_context.native_checkpoint import open_durable_store
from shared_context.native_private_memory import NativePrivateMemory
from shared_context.native_turns import EFFECTS_STORE, NativeTurnRuntime
from test_native_capabilities import selected_capture
from test_native_turn_wire import agent_receive, capture, receipt_for, submit
from test_native_turns import Harness, IDENTITY, local_env

ENDPOINT = "https://fa-acct.services.ai.azure.com/api/projects/fa-project"
ENV = {memory.STORE_ENV: "fa-memory-store", memory.ENDPOINT_ENV: ENDPOINT, memory.AGENT_ENV: "OFFLINE-agent",
       team.TEAM_ENV: "1"}
TOKEN = "OFFLINE-memory-token"
SCOPE = team_memory_scope("OFFLINE-tenant", "OFFLINE-team")
OTHER = team_memory_scope("OFFLINE-tenant", "OFFLINE-other-team")
TEXT = "Please remember: our release window is Friday 18:00. Thanks!"
QUOTE = "our release window is Friday 18:00"


async def token():
    return TOKEN


def service(sent, *, create_status=200, search_status=200, found=None):
    found = [{"memory_id": "t1", "content": "[OFFLINE-peer, 2026-10-01] Use repo X"}] if found is None else found

    def handle(request):
        sent.append(request)
        if request.url.path.endswith("/items"):
            return httpx.Response(create_status, json={"memory_id": "m1"})
        if request.url.path.endswith(":update_memories"):
            return httpx.Response(202, json={"update_id": "u1", "status": "queued"})
        return httpx.Response(search_status, json={"search_id": "s1", "usage": {"embedding_tokens": 3}, "memories": [
            {"memory_item": item} for item in found]})
    return httpx.MockTransport(handle)


def team_memory(sent, **kwargs):
    return team.NativeTeamMemory.from_environment(token, environ=ENV, transport=service(sent, **kwargs))


def bodies(sent, suffix):
    return [json.loads(request.content) for request in sent if request.url.path.endswith(suffix)]


def with_team(value: NativeTurnCapture, scope=SCOPE) -> NativeTurnCapture:
    return value.model_copy(update={"team_memory": None if scope is None else TeamMemoryCapture(scope=scope)})


def memory_receipt(text="q", *, scope=SCOPE, rid="rid-1", cid="cid-1", status="started", agent=False):
    request = agent_receive(rid, cid) if agent else submit(rid, cid, text=text)
    request = request.model_copy(update={"capture": with_team(request.capture, scope)})
    return receipt_for(request, status)


def stage(decision="candidate", reason="durable_fact", supersedes=None):
    return {"decision": decision, "reason": reason if decision == "candidate" else "no_durable_information",
            **({"quote": QUOTE} if decision == "candidate" else {}),
            **({"supersedes": supersedes} if supersedes is not None else {})}


@pytest.fixture(autouse=True)
def fresh_turn_refs():
    team._TURN_REFS.clear()
    yield
    team._TURN_REFS.clear()


def run_turn(monkeypatch, tmp_path, sent, *, decision="candidate", scope=SCOPE, memories=None, reason="durable_fact",
             supersedes=None):
    """One human native turn with the C5 candidate tool; the SDK double stages ``decision``."""
    memories = team_memory(sent) if memories is None else memories
    prompts, failures = [], []

    async def sdk(**options):
        prompts.append(options["prompt"])
        spec = next(tool for tool in options["tools"] if tool.name == candidates.TOOL)
        try:
            await spec.handler({"session_id": options["sdk_session_id"], "tool_call_id": "OFFLINE-call",
                                "tool_name": spec.name, "arguments": stage(decision, reason, supersedes)})
        except ToolFailure as failure:
            failures.append(failure.public_code)
        options["session_files"].files.update({
            "events.jsonl": json.dumps({"type": "session.start", "data": {"sessionId": options["sdk_session_id"]}}) + "\n",
            "workspace.yaml": "id: " + options["sdk_session_id"] + "\n",
        })
        return {"final_message": "OFFLINE answer",
                "sdk_stop": {"pid": 123, "exit_code": 0, "graceful": True, "runtime_version": "1.0.83"}}

    monkeypatch.setattr(native_sdk, "run_copilot", sdk)
    executor = native_sdk.NativeSdkExecutor(
        root=tmp_path / "native", runtime_path=tmp_path / "OFFLINE-not-executed", api_url="https://api.offline.invalid",
        model_url=ENDPOINT + "/openai/v1", token_provider=lambda: "OFFLINE-model-token",
        capability_providers=(candidates.PROVIDER,), team_memory=memories)

    async def completed(receipt, _result):
        await memories.completed(receipt)
    runtime = NativeTurnRuntime(IDENTITY, executor, preflight=executor.preflight, on_completed=completed)
    executor.reader = runtime
    request = NativeSubmit(command=submit(text=TEXT).command, capture=with_team(selected_capture(candidates.ENTRY_ID),
                                                                                 scope))
    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=runtime)) as client:
        harness = Harness(runtime, SimpleNamespace(controls={}), client)
        assert harness.invoke(request).status_code == 202
        receipt = harness.wait("rid-1", "completed")
        deadline = time.monotonic() + 8
        while decision == "candidate" and not failures and not bodies(sent, "/items") and time.monotonic() < deadline:
            time.sleep(0.01)
        harness.call(memories.completed, receipt)  # the official terminal task may retry every post-turn action
    return SimpleNamespace(prompts=prompts, receipt=receipt, failures=failures)


def test_the_api_captures_the_team_scope_only_when_enabled_and_old_bytes_stay_unchanged():
    assert team_memory_capture("t", "team-a", environ={}) is None
    assert team_memory_capture("t", "team-a", environ={TEAM_MEMORY_ENV: "true"}) is None
    enabled = team_memory_capture("t", "team-a", environ={TEAM_MEMORY_ENV: "1"})
    assert enabled.scope == team_memory_scope("t", "team-a") != team_memory_scope("t", "team-b")
    assert enabled.scope != team_memory_scope("other-tenant", "team-a")
    original = capture()
    assert "team_memory" not in original.model_dump_json()
    assert with_team(original, None).sha256 == original.sha256
    restored = NativeTurnCapture.model_validate_json(with_team(original).model_dump_json())
    assert restored.team_memory.scope == SCOPE
    with pytest.raises(ValueError):
        TeamMemoryCapture(scope="fap1-" + "0" * 48)


def test_a_human_suggestion_is_shared_once_verbatim_and_recalled_as_labeled_data(local_env, monkeypatch, tmp_path):
    sent = []
    turn = run_turn(monkeypatch, tmp_path, sent)
    searches, creates = bodies(sent, ":search_memories"), bodies(sent, "/items")
    # First turn: the official static read (no items) plus the message search, both in the captured Team scope.
    assert [body["scope"] for body in searches] == [SCOPE, SCOPE]
    assert sorted("items" in body for body in searches) == [False, True]
    assert next(body for body in searches if "items" in body)["items"][0]["content"][0]["text"] == TEXT
    # One item both searches return is listed once, with its short reference.
    assert turn.prompts == [team.PREFIX + "\n- note:t1 [OFFLINE-peer, 2026-10-01] Use repo X\n\n" + TEXT]
    # One verbatim item in the captured Team scope, even though the completion hook ran twice.
    assert len(creates) == 1 and creates[0]["scope"] == SCOPE and creates[0]["kind"] == "chat_summary"
    assert creates[0]["content"].startswith("[OFFLINE-agent, ") and creates[0]["content"].endswith(
        ", src rid-1] " + QUOTE)
    assert all(request.headers["authorization"] == "Bearer " + TOKEN for request in sent)
    assert all(request.headers["foundry-features"] == "MemoryStores=V1Preview" for request in sent)
    assert not bodies(sent, ":update_memories")


@pytest.mark.parametrize("reason, kind", [("stable_preference", "user_profile"), ("durable_fact", "chat_summary")])
def test_a_preference_note_is_a_user_profile_item_and_a_fact_is_a_chat_summary(local_env, monkeypatch, tmp_path,
                                                                                 reason, kind):
    sent = []
    run_turn(monkeypatch, tmp_path, sent, reason=reason)
    assert [body["kind"] for body in bodies(sent, "/items")] == [kind]
    description = candidates.bind(SimpleNamespace(receipt=SimpleNamespace(source=None)), ())
    description = asyncio.run(description).tools[0].description
    assert "stable_preference" in description and "durable_fact" in description
    assert "must share, review" not in description


@pytest.mark.parametrize("decision", ["no_candidate"])
def test_a_skip_shares_nothing(local_env, monkeypatch, tmp_path, decision):
    sent = []
    run_turn(monkeypatch, tmp_path, sent, decision=decision)
    assert bodies(sent, "/items") == [] and len(bodies(sent, ":search_memories")) == 2


def test_a_failed_team_search_is_named_unavailable_and_the_turn_continues(local_env, monkeypatch, tmp_path):
    sent = []
    turn = run_turn(monkeypatch, tmp_path, sent, memories=team_memory(sent, search_status=503))
    assert turn.prompts == [f"({team.UNAVAILABLE})\n\n" + TEXT]
    assert turn.receipt.status == "completed" and len(bodies(sent, "/items")) == 1


OCT_1 = 1790812800  # 2026-10-01T00:00:00Z
OCT_2 = OCT_1 + 86400
PREFERENCE = {"memory_id": "n1", "kind": "user_profile", "content": "[agent-a, 2026-10-01 00:00 UTC] conclusion first",
              "updated_at": OCT_1}
FACT = {"memory_id": "n2", "kind": "chat_summary", "content": "[agent-b, 2026-10-02 00:00 UTC] Lyra uses Postgres",
        "updated_at": OCT_2}


def test_the_first_turn_reads_the_teams_preferences_and_later_turns_only_relevant_notes(local_env):
    create_effects_store()
    sent = []

    def handle(request):
        sent.append(json.loads(request.content))
        found = [PREFERENCE] if "items" not in sent[-1] else [PREFERENCE, FACT]
        return httpx.Response(200, json={"search_id": "s1", "memories": [{"memory_item": item} for item in found]})
    memories = team.NativeTeamMemory(memory.PrivateMemory.from_environment(
        token, environ=ENV, transport=httpx.MockTransport(handle)), "OFFLINE-agent")
    receipt = memory_receipt("what database")
    preference = "note:n1 [preference] " + PREFERENCE["content"]
    fact = "note:n2 [team note] " + FACT["content"]
    assert list(asyncio.run(memories.recall(receipt, first_turn=True))) == [preference, fact]
    assert list(asyncio.run(memories.recall(receipt))) == [fact, preference]
    assert ["items" in body for body in sent] == [False, True, True] or ["items" in body for body in sent] == [
        True, False, True]
    # The static read asks for more preference notes than it shows; message searches ask for the shown bound.
    assert sorted(body["options"]["max_memories"] for body in sent) == [
        team.MAX_TEAM_MEMORIES, team.MAX_TEAM_MEMORIES, team.MAX_TEAM_STATIC_FETCH]
    assert next(body for body in sent if "items" not in body)["options"] == {"max_memories": team.MAX_TEAM_STATIC_FETCH}


def test_the_static_read_keeps_the_newest_preferences_so_a_newer_correction_is_not_left_out(local_env):
    """S3 (2026-10-03): with more preference notes than shown, the newest three by updated_at are kept."""
    create_effects_store()
    notes = [{"memory_id": f"p{index}", "kind": "user_profile", "updated_at": OCT_1 + index * 60,
              "content": f"[agent-a, 2026-10-01 00:{index:02d} UTC] preference {index}"} for index in range(6)]

    def handle(request):
        body = json.loads(request.content)
        # Which items the service returns past the limit is unspecified: this double honors the limit, oldest first.
        found = notes[:body["options"]["max_memories"]] if "items" not in body else []
        return httpx.Response(200, json={"search_id": "s1", "memories": [{"memory_item": item} for item in found]})
    memories = team.NativeTeamMemory(memory.PrivateMemory.from_environment(
        token, environ=ENV, transport=httpx.MockTransport(handle)), "OFFLINE-agent")
    receipt = memory_receipt("hello")
    recalled = asyncio.run(memories.recall(receipt, first_turn=True))
    assert list(recalled) == [f"note:p{index} [preference] " + notes[index]["content"] for index in (5, 4, 3)]
    assert recalled.omitted == 3


def test_the_candidate_tool_says_a_changed_way_of_working_is_a_stable_preference():
    description = asyncio.run(candidates.bind(SimpleNamespace(receipt=SimpleNamespace(source=None)), ()))
    assert "changes or corrects how they want work done, that is also a stable_preference" in (
        description.tools[0].description)


def test_a_correction_names_its_source_and_a_note_this_turn_showed_and_never_updates_or_deletes(
        local_env, monkeypatch, tmp_path):
    """MEM-02: the new note names its source RID and the exact earlier note it corrects; the old note is untouched."""
    sent = []
    turn = run_turn(monkeypatch, tmp_path, sent, reason="stable_preference", supersedes="note:t1")
    creates = bodies(sent, "/items")
    assert turn.failures == [] and turn.receipt.status == "completed"
    assert len(creates) == 1 and creates[0]["kind"] == "user_profile"
    assert creates[0]["content"].endswith(", src rid-1, corrects t1] " + QUOTE)
    assert all(request.method == "POST" and request.url.path.endswith(("/items", ":search_memories"))
               for request in sent)
    tool = asyncio.run(candidates.bind(SimpleNamespace(receipt=SimpleNamespace(source=None)), ())).tools[0]
    assert "never invent a reference" in tool.description


def test_an_invented_reference_saves_nothing(local_env, monkeypatch, tmp_path, caplog):
    sent = []
    with caplog.at_level(logging.WARNING):
        turn = run_turn(monkeypatch, tmp_path, sent, supersedes="note:zz")
    assert turn.failures == ["CAPABILITY_ARGUMENTS"] and turn.receipt.status == "completed"
    assert bodies(sent, "/items") == []
    assert "FA_TEAM_MEMORY_CREATE status=not_sent reason=supersedes_unknown" in caplog.text


@pytest.mark.parametrize("supersedes", ["t1", "note:", "note:" + "t" * 9, "note:t1]"])
def test_a_malformed_reference_saves_nothing(local_env, monkeypatch, tmp_path, supersedes):
    sent = []
    turn = run_turn(monkeypatch, tmp_path, sent, supersedes=supersedes)
    assert turn.failures == ["CAPABILITY_ARGUMENTS"] and bodies(sent, "/items") == []


def test_a_reference_shown_only_to_another_request_saves_nothing(local_env, monkeypatch, tmp_path, caplog):
    team._remember_refs("rid-0", memory.Recalled(("note",), ids=("t1",)))
    sent = []
    with caplog.at_level(logging.WARNING):
        turn = run_turn(monkeypatch, tmp_path, sent, supersedes="note:t1",
                        memories=team.NativeTeamMemory.from_environment(token, environ=ENV, transport=service(
                            sent, found=[{"memory_id": "t2", "content": "[OFFLINE-peer, 2026-10-01] Use repo Y"}])))
    assert turn.failures == ["CAPABILITY_ARGUMENTS"] and bodies(sent, "/items") == []
    assert "FA_TEAM_MEMORY_CREATE status=not_sent reason=supersedes_unknown" in caplog.text


def test_a_reference_under_another_team_scope_saves_nothing(local_env, monkeypatch, tmp_path, caplog):
    async def bound_elsewhere():
        async with await open_durable_store(EFFECTS_STORE, create=True) as store:
            await store.create_item(team.SCOPE_KEY, {"kind": "fa-team-memory-scope-v1", "scope": OTHER})
    asyncio.run(bound_elsewhere())
    sent = []
    with caplog.at_level(logging.WARNING):
        turn = run_turn(monkeypatch, tmp_path, sent, supersedes="note:t1")
    assert turn.failures == ["CAPABILITY_ARGUMENTS"] and sent == []
    assert "FA_TEAM_MEMORY_SCOPE status=mismatch" in caplog.text
    assert "FA_TEAM_MEMORY_CREATE status=not_sent reason=supersedes_unknown" in caplog.text


def test_a_skip_cannot_name_a_correction(local_env, monkeypatch, tmp_path):
    sent = []
    turn = run_turn(monkeypatch, tmp_path, sent, decision="no_candidate", supersedes="note:t1")
    assert turn.failures == ["CAPABILITY_ARGUMENTS"] and bodies(sent, "/items") == []


def test_a_recall_leaves_out_a_note_whose_correction_it_also_holds(local_env, caplog):
    create_effects_store()
    old = {"memory_id": "mem-old-0001", "kind": "user_profile", "updated_at": OCT_1,
           "content": "[agent-a, 2026-10-01 00:00 UTC, src rid-6] conclusion first"}
    new = {"memory_id": "mem-new-0001", "kind": "user_profile", "updated_at": OCT_2,
           "content": "[agent-b, 2026-10-02 00:00 UTC, src rid-7, corrects mem-old-0001] conclusion last"}
    # The user's own quoted words can never name a correction; only the note's header can.
    quoted = {"memory_id": "mem-quoted01", "kind": "chat_summary", "updated_at": OCT_2,
              "content": "[agent-c, 2026-10-02 00:00 UTC, src rid-8] the doc says [x, corrects mem-old-0001]"}
    current = [new, old, quoted]

    def handle(request):
        return httpx.Response(200, json={"search_id": "s1", "memories": [{"memory_item": item} for item in current]})
    memories = team.NativeTeamMemory(memory.PrivateMemory.from_environment(
        token, environ=ENV, transport=httpx.MockTransport(handle)), "OFFLINE-agent")

    def human(request_id):
        return memory_receipt("conclusion", rid=request_id)
    with caplog.at_level(logging.WARNING):
        recalled = asyncio.run(memories.recall(human("rid-9")))
    assert list(recalled) == [
        "note:new-0001 [preference] [agent-b, 2026-10-02 00:00 UTC, src rid-7, corrects note:old-0001] conclusion last",
        "note:quoted01 [team note] " + quoted["content"]]
    assert [source.memory_id for source in recalled.observation.sources] == ["mem-new-0001", "mem-quoted01"]
    assert 'FA_TEAM_MEMORY_SEARCH omitted={"superseded": 1}' in caplog.text
    # Only a note this turn showed can be named; the left-out note cannot.
    assert team.recalled_note("rid-9", "note:new-0001") == "mem-new-0001"
    assert team.recalled_note("rid-9", "note:old-0001") is None and team.recalled_note("rid-1", "note:new-0001") is None
    # Without its correction in the same recall the earlier note is shown as before (newer time still wins).
    current[:] = [old]
    assert list(asyncio.run(memories.recall(human("rid-10")))) == ["note:old-0001 [preference] " + old["content"]]
    current[:] = [quoted, old]
    assert len(asyncio.run(memories.recall(human("rid-11")))) == 2


def test_two_shown_notes_with_the_same_short_reference_name_neither():
    team._remember_refs("rid-1", memory.Recalled(("a", "b", "c"), ids=("x-ref00001", "y-ref00001", None)))
    assert team.recalled_note("rid-1", "note:ref00001") is None


def test_without_the_api_scope_nothing_is_searched_or_written(local_env, monkeypatch, tmp_path):
    sent = []
    run_turn(monkeypatch, tmp_path, sent, scope=None)
    assert sent == []


def create_effects_store():
    async def effects():
        async with await open_durable_store(EFFECTS_STORE, create=True):
            pass
    asyncio.run(effects())


def test_agent_messages_and_unfinished_turns_neither_read_nor_write(local_env):
    create_effects_store()  # a usable Team binding, so only the human-turn rule keeps an Agent message out
    sent = []
    memories = team_memory(sent)
    receipt = memory_receipt("human", status="completed")
    agent = memory_receipt("human", status="completed", agent=True)
    unknown = memory_receipt("human", status="unknown")
    assert asyncio.run(memories.recall(agent)) == ()
    asyncio.run(memories.completed(agent))
    asyncio.run(memories.completed(unknown))
    assert sent == []
    assert asyncio.run(memories.recall(receipt)) != ()  # the same human receipt does search


def test_a_different_later_team_scope_is_never_searched_or_written(local_env, caplog):
    create_effects_store()
    sent = []
    memories = team_memory(sent)
    human = memory_receipt()
    assert asyncio.run(memories.recall(human)) != ()
    # A new process re-reads the create-only binding; configuration drift cannot reach another Team.
    later = team_memory(sent)
    drifted = memory_receipt(scope=OTHER)
    with caplog.at_level(logging.WARNING):
        assert asyncio.run(later.recall(drifted)) == ()
        asyncio.run(later.completed(memory_receipt(scope=OTHER, rid="rid-9", cid="cid-9", status="completed")))
    assert [body["scope"] for body in bodies(sent, ":search_memories")] == [SCOPE]
    assert bodies(sent, "/items") == []
    assert "FA_TEAM_MEMORY_SCOPE status=mismatch" in caplog.text


def test_an_unknown_create_is_not_resent(local_env, monkeypatch, tmp_path, caplog):
    sent = []
    with caplog.at_level(logging.WARNING):
        run_turn(monkeypatch, tmp_path, sent, memories=team_memory(sent, create_status=500))
    assert len(bodies(sent, "/items")) == 1
    assert "FA_TEAM_MEMORY_CREATE status=500" in caplog.text and "status=already_claimed" in caplog.text


def test_a_follow_up_with_the_scope_newly_enabled_continues_the_original_conversation(local_env, monkeypatch, tmp_path):
    sent = []
    memories = team_memory(sent)

    async def sdk(**options):
        options["session_files"].files.update({
            "events.jsonl": json.dumps({"type": "session.start", "data": {"sessionId": options["sdk_session_id"]}}) + "\n",
            "workspace.yaml": "id: " + options["sdk_session_id"] + "\n",
        })
        return {"final_message": "OFFLINE answer",
                "sdk_stop": {"pid": 123, "exit_code": 0, "graceful": True, "runtime_version": "1.0.83"}}
    monkeypatch.setattr(native_sdk, "run_copilot", sdk)
    executor = native_sdk.NativeSdkExecutor(
        root=tmp_path / "native", runtime_path=tmp_path / "OFFLINE-not-executed", api_url="https://api.offline.invalid",
        model_url=ENDPOINT + "/openai/v1", token_provider=lambda: "OFFLINE-model-token", team_memory=memories)
    runtime = NativeTurnRuntime(IDENTITY, executor, preflight=executor.preflight)
    with TestClient(create_host(SimpleNamespace(identity=IDENTITY), native_runtime=runtime)) as client:
        harness = Harness(runtime, SimpleNamespace(controls={}), client)
        assert harness.invoke(submit()).status_code == 202
        first = harness.wait("rid-1", "completed")
        second = NativeSubmit(command=submit("rid-2", first.conversation_id, text="Next").command,
                              capture=with_team(capture()))
        assert harness.invoke(second).status_code == 202
        assert harness.wait("rid-2", "completed").turn_number == 2
    assert [body["scope"] for body in bodies(sent, ":search_memories")] == [SCOPE]


def test_the_factory_shares_after_private_memory_and_a_failure_never_blocks_the_relay(local_env, monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    runtime_path = tmp_path / "OFFLINE-not-executed"
    runtime_path.write_text("OFFLINE SDK boundary only", encoding="utf-8")
    hosted = SimpleNamespace(
        identity=IDENTITY, supervisor=AssistantSupervisor(home / ".fa-assistant-v12", runtime_path=runtime_path),
        api=SimpleNamespace(api_url="https://api.offline.invalid", audience="api://OFFLINE"),
    )
    order = []

    async def remembered(self, receipt, reader=None):
        order.append("private-memory")

    async def shared(self, receipt):
        order.append("team-note")
        raise OSError("OFFLINE store transport")

    async def relayed(self, receipt, result):
        order.append("relay")

    async def settled(*_args):
        return None
    monkeypatch.setattr(NativePrivateMemory, "completed", remembered)
    monkeypatch.setattr(team.NativeTeamMemory, "_share", shared)
    monkeypatch.setattr(NativeAgentRelayClient, "completed", relayed)
    monkeypatch.setattr(native_sdk.NativeUsageReporter, "__call__", settled)
    monkeypatch.setattr(native_sdk.NativeSdkExecutor, "notify_terminal", settled)
    receipt = SimpleNamespace(status="completed", result=None, request_id="OFFLINE-rid")

    async def finished(current):
        await current.on_terminal(receipt)  # the peer result goes out ahead of usage and memory
        await current.on_completed(receipt, None)
    with monkeypatch.context() as environment:
        for name, value in {"FA_NATIVE_TURNS": "foundry-native-v1", "FOUNDRY_HOSTING_ENVIRONMENT": "OFFLINE-platform",
                            "AGENTSERVER_TASKS_BACKEND": "hosted", "HOME": str(home),
                            "BEHAVIOR_ASSISTANT_RUNTIME_PATH": str(runtime_path), "FOUNDRY_PROJECT_ENDPOINT": ENDPOINT,
                            "FOUNDRY_AGENT_NAME": "OFFLINE-agent", memory.STORE_ENV: "fa-memory-store",
                            team.TEAM_ENV: "1"}.items():
            environment.setenv(name, value)
        runtime = native_sdk.native_runtime_from_environment(hosted)
        environment.delenv(team.TEAM_ENV)
        off = native_sdk.native_runtime_from_environment(hosted)
    assert isinstance(runtime.executor.team_memory, team.NativeTeamMemory)
    asyncio.run(finished(runtime))
    assert order == ["relay", "private-memory", "team-note"]
    assert off.executor.team_memory is None
    asyncio.run(finished(off))
    assert order == ["relay", "private-memory", "team-note", "relay", "private-memory"]
