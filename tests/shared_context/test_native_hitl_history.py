"""Original HITL writers and owner HTTP reader; only model/Foundry/StateStore I/O is synthetic."""

import asyncio
import copy
from dataclasses import replace
from types import SimpleNamespace

import pytest

from shared_context.behavior_native_turn_api import PREFIX, NativeTurnGateway
from shared_context.behavior_native_turn_wire import NativeHitlHistoryRead
from shared_context.errors import Problem
from shared_context.m2_copilot import ToolFailure, ToolOutput
from shared_context.native_effects import NativeEffects
from shared_context.native_hitl_wire import NativeHitlEffectState, NativeHitlReview
from shared_context.native_hitl_runtime import NativeHitlRuntime
from shared_context.native_turns import NativeTurnRuntime
from test_native_hitl import change, hitl, post, read
from test_native_hitl_h2 import EffectStore, human_decide, pending_view, permit, setup_gate, verdict
from test_native_turn_api import instruction_models, jwk_client, native_api, signing_key, synthetic_standing_v1, token


class PagedEffectStore(EffectStore):
    async def list_keys(self, *, limit, after=None):
        keys = sorted(key for key in self.items if after is None or key > after)
        selected = keys[:limit]
        return SimpleNamespace(
            keys=[SimpleNamespace(key=key) for key in selected],
            has_more=len(keys) > limit, last_id=selected[-1] if selected else None,
        )


@pytest.fixture
def review_loop():
    loop = asyncio.new_event_loop()
    try:
        yield loop
    finally:
        loop.close()


def original_case(api, control, tmp_path, answers):
    case = setup_gate(control, tmp_path, answers)
    target = api.remote.target
    resolve = control.config.agent_scope
    control.config.targets = (*control.config.targets, SimpleNamespace(agent_id=target.agent_id))
    control.config.agent_scope = lambda agent_id: resolve("native-a" if agent_id == target.agent_id else agent_id)
    case.runtime.receipt = case.runtime.receipt.model_copy(update={"agent_id": target.agent_id})
    case.runtime.identity.agent_id = target.agent_id
    case.runtime.store = PagedEffectStore()
    case.context.receipt = case.runtime.receipt
    case.context.scope.agent_id = target.agent_id
    case.gate = NativeHitlRuntime(
        case.context, case.client, case.gate.reviewer, clock=lambda: control.now, poll_seconds=0.005)
    case.context.hitl = case.gate
    api.remote.reply_override = lambda command: control.client.portal.call(
        NativeTurnRuntime.dispatch, case.runtime, command)
    case.api = api
    case.path = PREFIX + "/conversations/cid-one/requests/rid-one/hitl-reviews"
    return case


@pytest.mark.parametrize("outcome", ["allow", "deny"])
def test_owner_reads_persisted_automatic_review_without_pending(hitl, review_loop, native_api, tmp_path, outcome):
    case = original_case(native_api, hitl, tmp_path, (verdict(outcome),))
    ran = []

    async def tool(packet):
        ran.append(copy.deepcopy(packet))
        return ToolOutput("OFFLINE private effect output must not be in review history")

    async def write():
        allowed, code = await permit(case)
        assert allowed is (outcome == "allow")
        assert code == (None if allowed else "NATIVE_HITL_REVIEW_DENIED")
        if allowed:
            await case.gate.invoke(case.packet, tool)
        call = case.gate.calls["call-one"]
        persisted = await case.context.effects.read("call-one", call.material)
        assert persisted.hitl.action.arguments == case.packet["arguments"]
        assert persisted.hitl.reviews[-1].outcome == outcome
        assert persisted.hitl.pending is None
        assert persisted.status == ("done" if allowed else "pending")
        return persisted

    persisted = review_loop.run_until_complete(write())
    saved = copy.deepcopy(case.runtime.store.items)
    writes = len(hitl.table.submissions)
    reviews = len(case.sender.observed)
    case.gate.calls.clear()
    for _ in range(2):
        response = native_api.client.get(case.path, headers=native_api.headers)
        assert response.status_code == 200, response.text
        entry, = response.json()["entries"]
        assert entry["action"] == persisted.hitl.action.model_dump(mode="json")
        assert entry["action_digest"] == persisted.hitl.action.sha256
        assert entry["reviews"][-1]["outcome"] == outcome
        assert entry["reviews"][-1]["reason"] == "reviewed"
        assert entry["reviews"][-1]["reviewed_at"] == hitl.now.isoformat().replace("+00:00", "Z")
        assert "private effect output" not in response.text
        assert response.headers["cache-control"] == "no-store"
    assert case.runtime.store.items == saved
    assert len(hitl.table.submissions) == writes
    assert len(case.sender.observed) == reviews == 1
    assert len(ran) == (1 if outcome == "allow" else 0)


def test_ask_card_and_owner_history_share_original_action_and_review(hitl, review_loop, native_api, tmp_path):
    case = original_case(native_api, hitl, tmp_path, (verdict("ask"),))

    async def check():
        waiting = asyncio.create_task(permit(case))
        original = await pending_view(case)
        saved = copy.deepcopy(case.runtime.store.items)
        writes = len(hitl.table.submissions)
        guard = native_api.c.control.store._guard()
        response = native_api.client.get(case.path, headers=native_api.headers)
        assert response.status_code == 200, response.text
        entry, = response.json()["entries"]
        assert entry["action"] == original.pending.command.action.model_dump(mode="json")
        assert entry["action_digest"] == original.pending.command.action_digest
        assert entry["reviews"] == [original.pending.command.review.model_dump(mode="json")]
        assert (await case.client.read(original.approval.pending_id)) == original
        assert not waiting.done() and case.runtime.store.items == saved
        assert len(hitl.table.submissions) == writes and native_api.c.control.store._guard() == guard
        human_decide(case, original, "deny")
        assert await waiting == (False, "NATIVE_HITL_HUMAN_DENIED")
        assert read(hitl, original.approval.pending_id).json()["status"] == "deny"

    review_loop.run_until_complete(check())


@pytest.mark.parametrize("identity", ["anonymous", "agent", "other_agent", "non_owner"])
def test_review_history_is_owner_only_before_forwarding(
        hitl, review_loop, native_api, tmp_path, signing_key, caplog, identity):
    case = original_case(native_api, hitl, tmp_path, (verdict("allow"),))
    if identity == "anonymous":
        headers = {}
    elif identity == "non_owner":
        headers = {"Authorization": "Bearer " + token(signing_key, {
            "oid": hitl.owner_two.oid, "scp": "Behavior.Read",
            "appid": native_api.composition.config.identity.admin_client_id,
        })}
    else:
        agent = native_api.composition.config.identity.agents[0 if identity == "agent" else 1]
        headers = {"Authorization": "Bearer " + token(signing_key, {
            "oid": agent.oid, "roles": ["Behavior.Agent"], "idtyp": "app",
        }, remove=("scp",))}
    before = len(native_api.remote.calls)
    response = native_api.client.get(case.path, headers=headers)
    assert response.status_code == (401 if identity == "anonymous" else 403)
    if identity in {"agent", "other_agent"}:
        assert response.json()["error"]["code"] == "BEHAVIOR_ACCESS_DENIED"
        assert "WORK_POLICY_HUMAN_REQUIRED" in caplog.text
    assert len(native_api.remote.calls) == before
    assert not case.runtime.store.items and not case.sender.observed


@pytest.mark.parametrize("suffix", [
    "?agent_id=another-agent", "?team_id=another-team", "?key=some-record", "?store=another-store",
    "?limit=0", "?limit=6", "?after=", "?limit=1&limit=2",
])
def test_review_history_accepts_only_bounded_original_read_selectors(hitl, review_loop, native_api, tmp_path, suffix):
    case = original_case(native_api, hitl, tmp_path, (verdict("allow"),))
    before = len(native_api.remote.calls)
    response = native_api.client.get(case.path + suffix, headers=native_api.headers)
    assert response.status_code == 422
    assert len(native_api.remote.calls) == before


def test_review_history_get_rejects_a_body(hitl, review_loop, native_api, tmp_path):
    case = original_case(native_api, hitl, tmp_path, (verdict("allow"),))
    before = len(native_api.remote.calls)
    response = native_api.client.request("GET", case.path, headers=native_api.headers, content=b"{}")
    assert response.status_code == 422 and len(native_api.remote.calls) == before


def test_review_history_preserves_full_card_arguments_not_effect_output(hitl, review_loop, native_api, tmp_path):
    case = original_case(native_api, hitl, tmp_path, (verdict("allow"),))
    case.packet["arguments"]["command"] = "echo synthetic\r\n# " + "\u539f\u6587" * 12_000

    async def write():
        assert await permit(case) == (True, None)

        async def tool(_):
            return ToolOutput("OFFLINE output is not approval material")

        await case.gate.invoke(case.packet, tool)

    review_loop.run_until_complete(write())
    saved = copy.deepcopy(case.runtime.store.items)
    guard = native_api.c.control.store._guard()
    calls = len(native_api.remote.calls)
    response = native_api.client.get(case.path, headers=native_api.headers)
    assert response.status_code == 200, response.text
    entry, = response.json()["entries"]
    assert len(entry["action"]["arguments"]["command"].encode("utf-8")) > 64 * 1024
    assert entry["action"]["arguments"] == case.packet["arguments"]
    assert set(entry) == {"tool_call_id", "action", "action_digest", "reviews", "status", "time_status"}
    assert entry["status"] == entry["time_status"] == "recorded"
    assert response.json()["purpose"] == "readback-only-not-execution-permission"
    assert "OFFLINE output" not in response.text
    assert {command.operation for _, command in native_api.remote.calls[calls:]} <= {"hitl-reviews", "session_read"}
    assert case.runtime.store.items == saved and native_api.c.control.store._guard() == guard


def test_old_effects_and_review_times_remain_unrecorded_without_backfill(hitl, review_loop, native_api, tmp_path):
    case = original_case(native_api, hitl, tmp_path, (verdict("allow"),))
    original_review = NativeHitlReview(rules_revision=0, source="default", outcome="deny", reason="reviewed")

    async def seed():
        await case.context.effects.reserve("legacy-without-hitl", {"kind": "OFFLINE legacy effect"})
        action = case.gate._action(case.packet)
        claim = await case.context.effects.reserve("call-one", case.gate._material(case.context.receipt, action))
        await case.context.effects.annotate_hitl(claim, NativeHitlEffectState(action=action, reviews=(original_review,)))

    review_loop.run_until_complete(seed())
    saved = copy.deepcopy(case.runtime.store.items)
    assert all("reviewed_at" not in str(item.value) for item in saved.values())
    case.runtime.receipt = case.runtime.receipt.model_copy(update={"status": "unknown"})
    for _ in range(2):
        response = native_api.client.get(case.path, headers=native_api.headers)
        assert response.status_code == 200, response.text
        entries = {entry["tool_call_id"]: entry for entry in response.json()["entries"]}
        assert entries["legacy-without-hitl"] == {
            "tool_call_id": "legacy-without-hitl", "action": None, "action_digest": None, "reviews": [],
            "status": "not_recorded", "time_status": "unknown",
        }
        assert entries["call-one"]["reviews"] == [original_review.model_dump(mode="json")]
        assert entries["call-one"]["time_status"] == "unknown"
    assert case.runtime.store.items == saved and not case.sender.observed
    assert case.runtime.receipt.status == "unknown"


def test_review_history_preserves_each_review_and_recorded_time(hitl, review_loop, native_api, tmp_path):
    from datetime import timedelta

    case = original_case(native_api, hitl, tmp_path, (verdict("allow"), verdict("deny")))
    first_time = hitl.now

    async def check():
        assert await permit(case) == (True, None)
        assert post(hitl, "/rules", change(hitl, "new-review-rules")).status_code == 200
        hitl.now += timedelta(seconds=3)
        with pytest.raises(ToolFailure):
            await case.gate.invoke(case.packet, lambda _: pytest.fail("new deny executed a tool"))

    review_loop.run_until_complete(check())
    before = len(case.sender.observed)
    response = native_api.client.get(case.path, headers=native_api.headers)
    assert response.status_code == 200, response.text
    entry, = response.json()["entries"]
    assert [review["outcome"] for review in entry["reviews"]] == ["allow", "deny"]
    assert [review["reviewed_at"] for review in entry["reviews"]] == [
        first_time.isoformat().replace("+00:00", "Z"), hitl.now.isoformat().replace("+00:00", "Z")]
    assert len(case.sender.observed) == before == 2


def test_review_pages_filter_other_turns_and_image_segments_without_writes(hitl, review_loop, native_api, tmp_path):
    case = original_case(native_api, hitl, tmp_path, (verdict("deny"),))

    async def seed():
        assert await permit(case) == (False, "NATIVE_HITL_REVIEW_DENIED")
        original = case.gate.calls["call-one"].claim.record
        for number in range(5):
            foreign = original.model_copy(update={
                "request_id": "other-rid-" + str(number), "conversation_id": "other-cid", "hitl": None,
            })
            await case.runtime.store.create_item(NativeEffects._key(foreign), foreign.model_dump(mode="json"))
        await case.runtime.store.create_item("fx-img:OFFLINE-segment:0", {"data": "not public review content"})

    review_loop.run_until_complete(seed())
    saved = copy.deepcopy(case.runtime.store.items)
    entries, cursor = [], None
    for _ in range(len(saved) + 1):
        params = {"limit": 1}
        if cursor is not None:
            params["after"] = cursor
        response = native_api.client.get(case.path, headers=native_api.headers, params=params)
        assert response.status_code == 200, response.text
        assert "other-cid" not in response.text and "not public review content" not in response.text
        entries.extend(response.json()["entries"])
        next_after = response.json()["next_after"]
        if next_after is None:
            break
        assert next_after != cursor
        cursor = next_after
    else:
        pytest.fail("the bounded review page cursor did not finish")
    assert [entry["tool_call_id"] for entry in entries] == ["call-one"]
    assert case.runtime.store.items == saved


@pytest.mark.parametrize("damage", ["conversation", "agent", "action", "arguments", "key"])
def test_review_reader_refuses_foreign_scope_or_wrong_original_key(hitl, review_loop, native_api, tmp_path, damage):
    case = original_case(native_api, hitl, tmp_path, (verdict("deny"),))
    assert review_loop.run_until_complete(permit(case)) == (False, "NATIVE_HITL_REVIEW_DENIED")
    path = case.path
    key = next(iter(case.runtime.store.items))
    if damage == "conversation":
        path = path.replace("cid-one", "foreign-cid")
    elif damage == "key":
        case.runtime.store.items["fx:" + "0" * 64] = case.runtime.store.items.pop(key)
    elif damage == "agent":
        case.runtime.store.items[key].value["agent_id"] = "foreign-agent"
    elif damage == "arguments":
        case.runtime.store.items[key].value["hitl"]["action"]["arguments"] = {"command": "not the original command"}
    else:
        case.runtime.store.items[key].value["hitl"]["action"]["call"]["agent_id"] = "foreign-agent"
    saved = copy.deepcopy(case.runtime.store.items)
    response = native_api.client.get(path, headers=native_api.headers)
    assert response.status_code in {403, 409}, response.text
    assert "action" not in response.json() and case.runtime.store.items == saved


def test_api_rechecks_hosted_review_scope(hitl, review_loop, native_api, tmp_path):
    case = original_case(native_api, hitl, tmp_path, (verdict("deny"),))
    assert review_loop.run_until_complete(permit(case)) == (False, "NATIVE_HITL_REVIEW_DENIED")
    command = NativeHitlHistoryRead(conversation_id="cid-one", request_id="rid-one")
    page = hitl.client.portal.call(NativeTurnRuntime.dispatch, case.runtime, command)
    native_api.remote.reply_override = lambda _: page.model_copy(update={"agent_id": "another-agent", "entries": ()})
    response = native_api.client.get(case.path, headers=native_api.headers)
    assert response.status_code == 502 and response.json()["error"]["code"] == "NATIVE_STATE_SCOPE"
    with pytest.raises(Problem):
        NativeTurnGateway(native_api.composition, enabled=True).read(hitl.agent, command)
    before = len(native_api.remote.calls)
    with pytest.raises(Problem):
        NativeTurnGateway(native_api.composition, enabled=True).read(
            replace(native_api.c.human, team="foreign-team"), command)
    assert len(native_api.remote.calls) == before


def test_empty_review_history_creates_nothing(hitl, review_loop, native_api, tmp_path):
    case = original_case(native_api, hitl, tmp_path, (verdict("deny"),))
    before = len(hitl.table.submissions)
    response = native_api.client.get(case.path, headers=native_api.headers)
    assert response.status_code == 200, response.text
    assert response.json()["entries"] == [] and response.json()["next_after"] is None
    assert not case.runtime.store.items and not case.sender.observed
    assert len(hitl.table.submissions) == before


def test_unconfirmed_effect_read_is_not_an_empty_success(hitl, review_loop, native_api, tmp_path, monkeypatch):
    case = original_case(native_api, hitl, tmp_path, (verdict("deny"),))
    assert review_loop.run_until_complete(permit(case)) == (False, "NATIVE_HITL_REVIEW_DENIED")
    saved = copy.deepcopy(case.runtime.store.items)

    async def lost_read(_key):
        return None

    monkeypatch.setattr(case.runtime.store, "get_item", lost_read)
    response = native_api.client.get(case.path, headers=native_api.headers)
    assert response.status_code == 503 and response.json()["error"]["code"] == "NATIVE_EFFECT_UNKNOWN"
    assert case.runtime.store.items == saved and "entries" not in response.json()


def test_delegation_history_uses_its_original_effect_material(hitl, review_loop, native_api, tmp_path):
    from shared_context.native_delegation import delegation_material

    case = original_case(native_api, hitl, tmp_path, (verdict("deny"),))
    action = case.gate._action(case.packet).model_copy(update={
        "category": "delegation", "tool": "task", "target": "fa-delegate-v1",
        "arguments": {"prompt": "OFFLINE synthetic delegated action"},
    })
    original = NativeHitlReview(rules_revision=0, source="default", outcome="deny", reason="reviewed")

    async def seed():
        material = delegation_material(case.context.receipt, action.call.sdk_session_id, action.arguments)
        claim = await case.context.effects.reserve(action.call.tool_call_id, material)
        await case.context.effects.annotate_hitl(claim, NativeHitlEffectState(action=action, reviews=(original,)))

    review_loop.run_until_complete(seed())
    response = native_api.client.get(case.path, headers=native_api.headers)
    assert response.status_code == 200, response.text
    assert response.json()["entries"][0]["action"] == action.model_dump(mode="json")
    assert not case.sender.observed


def test_mixed_memory_claims_do_not_block_tool_history(hitl, review_loop, native_api, tmp_path, monkeypatch):
    from shared_context import native_private_memory
    from shared_context.native_private_memory import claim_key as private_key, claim_once
    from shared_context.native_team_memory import claim_key as team_key

    case = original_case(native_api, hitl, tmp_path, (verdict("deny"),))
    monkeypatch.setattr(native_private_memory, "agent_store", lambda name, _credential: case.runtime._store(name))

    async def seed():
        assert await permit(case) == (False, "NATIVE_HITL_REVIEW_DENIED")
        for rid in ("rid-one", "0-other-rid"):
            await claim_once(private_key(rid), {
                "kind": "fa-private-memory-update-claim-v1", "request_id": rid,
            }, None, "OFFLINE_PRIVATE_CLAIM")
            await claim_once(team_key(rid), {
                "kind": "fa-team-note-claim-v1", "request_id": rid, "scope": "OFFLINE-team",
            }, None, "OFFLINE_TEAM_CLAIM")
        foreign = case.gate.calls["call-one"].claim.record.model_copy(update={
            "request_id": "another-tool-rid", "conversation_id": "another-cid", "hitl": None,
        })
        await case.runtime.store.create_item(NativeEffects._key(foreign), foreign.model_dump(mode="json"))

    review_loop.run_until_complete(seed())
    saved = copy.deepcopy(case.runtime.store.items)
    writes = len(hitl.table.submissions)
    reviews = len(case.sender.observed)
    entries, cursor = [], None
    for page_number in range(len(saved) + 1):
        params = {"limit": 1}
        if cursor is not None:
            params["after"] = cursor
        response = native_api.client.get(case.path, headers=native_api.headers, params=params)
        assert response.status_code == 200, response.text
        page = response.json()
        if page_number == 0:
            assert page["entries"] == [] and page["next_after"] is not None
        entries.extend(page["entries"])
        if page["next_after"] is None:
            break
        assert page["next_after"] != cursor
        cursor = page["next_after"]
    else:
        pytest.fail("the mixed effect page cursor did not finish")
    assert [entry["tool_call_id"] for entry in entries] == ["call-one"]
    assert case.runtime.store.items == saved
    assert len(hitl.table.submissions) == writes and len(case.sender.observed) == reviews


@pytest.mark.parametrize("damage", [
    "unknown_kind", "private_kind_on_tool_key", "team_kind_on_tool_key", "missing_field",
    "extra_field", "pending_result", "tool_on_memory_key", "memory_key_mismatch", "input_digest",
])
def test_memory_filter_keeps_invalid_tool_and_unknown_records_strict(
        hitl, review_loop, native_api, tmp_path, damage):
    from shared_context.native_private_memory import claim_key

    case = original_case(native_api, hitl, tmp_path, (verdict("deny"),))
    assert review_loop.run_until_complete(permit(case)) == (False, "NATIVE_HITL_REVIEW_DENIED")
    key = next(iter(case.runtime.store.items))
    item = case.runtime.store.items[key]
    if damage in {"unknown_kind", "private_kind_on_tool_key", "team_kind_on_tool_key"}:
        item.value["kind"] = {
            "unknown_kind": "unknown-effect-kind",
            "private_kind_on_tool_key": "fa-private-memory-update-claim-v1",
            "team_kind_on_tool_key": "fa-team-note-claim-v1",
        }[damage]
    elif damage == "missing_field":
        del item.value["agent_id"]
    elif damage == "extra_field":
        item.value["unknown_field"] = "not a supported tool record"
    elif damage == "pending_result":
        item.value["result"] = "a pending effect cannot claim a result"
    elif damage == "input_digest":
        item.value["input_digest"] = "0" * 64
    else:
        case.runtime.store.items[claim_key("rid-one")] = case.runtime.store.items.pop(key)
        if damage == "memory_key_mismatch":
            item.value = {"kind": "fa-private-memory-update-claim-v1", "request_id": "different-rid"}
    saved = copy.deepcopy(case.runtime.store.items)
    before = len(hitl.table.submissions), len(case.sender.observed)
    response = native_api.client.get(case.path, headers=native_api.headers)
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == (
        "NATIVE_EFFECT_CONFLICT" if damage in {"tool_on_memory_key", "input_digest"} else "NATIVE_STATE_INVALID")
    assert "entries" not in response.json()
    assert case.runtime.store.items == saved
    assert (len(hitl.table.submissions), len(case.sender.observed)) == before
