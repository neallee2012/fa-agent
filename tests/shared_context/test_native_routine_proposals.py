"""Routine from chat: the assistant drafts, only the owner's card click writes, settings disables. Offline, signed."""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import jwt
import pytest

from shared_context import native_sdk
from shared_context.behavior_native_turn_wire import material_digest
from shared_context.errors import Problem
from shared_context.native_capabilities import guard_native_tool
from shared_context.native_hitl_api import install_native_hitl_routes
from shared_context.native_hitl_wire import (
    NativeHitlAction, NativeHitlAuthorize, NativeHitlCall, NativeHitlClose, NativeHitlDecision, NativeHitlEnd,
    NativeHitlPendingCreate, NativeHitlPendingQuery, NativeHitlReview, NativeHitlRuleInput, NativeHitlTurn,
    native_hitl_failure_feedback,
)
from shared_context.m2_copilot import ToolFailure
from shared_context.native_routine_proposal_tool import propose_routine_tool
from shared_context.native_routine_wire import NativeRoutineProposalResult, next_daily_run, routine_proposal_change
from test_conversation_transfer_startup import synthetic_standing_v1  # noqa: F401
from test_instruction_flow import instruction_models  # noqa: F401
from test_m3_auth import jwk_client, signing_key  # noqa: F401
from test_native_hitl import change, hitl, post  # noqa: F401
from test_native_hitl_h2 import SID, human_decide, pending_view, permit, setup_gate, verdict
from test_native_routine_api import routine_api  # noqa: F401
from test_native_routine_context import routine_client, sdk_answer
from test_native_routine_turns import routine_request
from test_native_turn_api import native_api  # noqa: F401
from test_native_turn_wire import submit as native_submit
from test_native_turns import harness, local_env, native_retry_policy  # noqa: F401
from test_web_test_console_native import first, native_console, progress, send  # noqa: F401

HITL = "/native-hitl/v1"
CONTENT = "Check the Foundry what's new page and tell me about new articles."
FLAGS = {"FA_NATIVE_ROUTINES_ENABLED": "1", "FA_NATIVE_ROUTINE_CLOCK": "api",
         "FA_NATIVE_ROUTINE_PROPOSALS": "1", "FA_NATIVE_HITL_ENABLED": "1"}


@pytest.fixture
def proposals(request, monkeypatch):
    for name, value in FLAGS.items():
        monkeypatch.setenv(name, value)
    case = request.getfixturevalue("routine_api")
    install_native_hitl_routes(case.client.app, case.composition)
    case.hitl = case.client.app.state.native_hitl
    due = case.c.h.time.replace(second=0, microsecond=0) + timedelta(minutes=2)
    case.due_at = due
    case.draft = {"local_time": due.strftime("%H:%M"), "time_zone": "UTC", "content": CONTENT}
    return case


def routine_action(case, *, call_id="propose-one", arguments=None, tool="propose_routine", target="conversation:native-cid"):
    call = NativeHitlCall(agent_id=case.foundry.target.agent_id, conversation_id="native-cid", request_id="human-seed",
                          sdk_session_id="sdk-original", tool_call_id=call_id)
    return NativeHitlAction(call=call, category="routine", tool=tool, target=target,
                            arguments=dict(case.draft) if arguments is None else arguments)


def rules(case):
    response = case.client.get(HITL + "/rules", headers=case.headers)
    assert response.status_code == 200, response.text
    return response.json()


def propose(case, *, source="default", **kwargs):
    action = routine_action(case, **kwargs)
    command = NativeHitlPendingCreate(
        request_id="pending-" + action.call.tool_call_id, action=action, action_digest=action.sha256,
        review=NativeHitlReview(rules_revision=rules(case)["revision"], source=source, outcome="ask",
                               **({"reason": "review_unavailable"} if source == "unavailable" else {})))
    return case.client.post(HITL + "/pending", json=command.model_dump(mode="json"), headers=case.agent_headers)


def proposed(case, **kwargs):
    response = propose(case, **kwargs)
    assert response.status_code == 200, response.text
    return response.json()["result"]


def decision_body(case, pending, choice="allow_once", request_id="owner-decision"):
    current = rules(case)
    return NativeHitlDecision(
        request_id=request_id, pending_id=pending["pending_id"], pending_digest=pending["pending_digest"],
        choice=choice, expected_rules_revision=current["revision"], expected_rules_etag=current["etag"],
        rule=NativeHitlRuleInput(text="Allow schedules like this.", behavior="allow_automatically")
        if choice == "allow_rule" else None).model_dump(mode="json")


def decide(case, pending, choice="allow_once", request_id="owner-decision", headers=None, body=None):
    return case.client.post(HITL + "/decisions", json=body or decision_body(case, pending, choice, request_id),
                            headers=case.headers if headers is None else headers)


def definitions(case):
    response = case.client.get(case.routine_prefix + "/definitions", headers=case.headers)
    assert response.status_code == 200, response.text
    return response.json()["definitions"]


def card(case, pending):
    response = case.client.get(HITL + "/pending/" + pending["pending_id"], headers=case.headers)
    assert response.status_code == 200, response.text
    return response.json()


def routine_receives(case):
    return len([1 for _, command in case.remote.calls if command.operation == "routine_receive"])


def test_owner_allow_writes_only_the_card_draft_once_and_the_api_clock_runs_it(proposals):
    case = proposals
    pending = proposed(case)
    shown = card(case, pending)["pending"]["command"]
    assert shown["review"]["source"] == "default" and shown["review"]["outcome"] == "ask"
    assert shown["action"]["arguments"] == case.draft and definitions(case) == []
    decided = decide(case, pending)
    assert decided.status_code == 200, decided.text
    assert decided.json()["result"]["status"] == "allow"
    listed = definitions(case)
    assert len(listed) == 1
    saved = listed[0]["definition"]
    assert saved["routine_id"] == material_digest({"kind": "fa-native-routine-proposal-v1",
                                                   "pending_id": pending["pending_id"]})
    assert (saved["owner_oid"], saved["agent_id"], saved["conversation_id"]) == (
        case.c.human.oid, case.foundry.target.agent_id, "native-cid")
    assert (saved["kind"], saved["enabled"], saved["content"], saved["expected_version_id"]) == (
        "routine", True, CONTENT, None)
    assert saved["schedule"] == {"kind": "daily", **{key: case.draft[key] for key in ("time_zone", "local_time")}}
    resent = decide(case, pending)
    assert resent.status_code == 200 and resent.json()["result"]["status"] == "allow"
    assert definitions(case) == listed, "a resent decision reads the same definition back"
    case.c.h.time = case.due_at + timedelta(seconds=2)
    body = {"routine_id": saved["routine_id"], "scheduled_for": case.due_at.isoformat()}
    dispatched = case.trigger(body)
    assert dispatched.status_code == 202, dispatched.text
    original = case.remote.records[dispatched.json()["request_id"]]
    # Each run opens its own conversation (2026-10-23); the card's conversation keeps only the definition.
    assert original.command.content == CONTENT and original.command.conversation_id is None
    assert original.conversation_id == original.routine.conversation_id != "native-cid"
    assert original.routine.owner_oid == case.c.human.oid
    assert original.routine.occurrence.approved_version == saved["request_id"]
    case.remote.completed(original.request_id)
    count, turns = routine_receives(case), len(case.remote.records)
    repeated = case.trigger(body)
    assert repeated.json()["request_id"] == original.request_id and repeated.json()["status"] == "completed"
    assert routine_receives(case) == count and len(case.remote.records) == turns


def test_a_lost_decision_after_the_saved_definition_resends_to_the_same_single_definition(proposals, monkeypatch):
    case = proposals
    pending = proposed(case)
    original, attempts = case.hitl.submit, []

    def lost_once(principal, command):
        attempts.append(command.request_id)
        if len(attempts) == 1:
            raise Problem(503, "BEHAVIOR_COMMIT_UNKNOWN", "OFFLINE lost decision write")
        return original(principal, command)

    monkeypatch.setattr(case.hitl, "submit", lost_once)
    first_try = decide(case, pending)
    assert first_try.status_code == 503, first_try.text
    saved = definitions(case)
    assert len(saved) == 1, "the definition is saved before the decision"
    assert card(case, pending)["status"] == "pending"
    again = decide(case, pending)
    assert again.status_code == 200, again.text
    assert again.json()["result"]["status"] == "allow"
    assert definitions(case) == saved and attempts == ["owner-decision", "owner-decision"]


@pytest.mark.parametrize("outcome", ["deny", "expired", "allow_rule", "changed_digest", "extra_content",
                                     "turn_closed", "interrupted"])
def test_deny_expiry_a_rule_or_a_changed_body_writes_no_schedule(proposals, outcome):
    """``turn_closed`` / ``interrupted``: the card's turn ended (or was left on an earlier SID, ADR-0009 gate 6) and its
    cards were closed as at a normal turn end; an allow then writes no schedule nobody will confirm."""
    case = proposals
    pending = proposed(case)
    if outcome == "expired":
        case.c.h.time += timedelta(seconds=601)
    if outcome in {"turn_closed", "interrupted"}:
        call = routine_action(case).call
        end = NativeHitlEnd(request_id="end-human-seed", turn=NativeHitlTurn(
            agent_id=call.agent_id, conversation_id=call.conversation_id, request_id=call.request_id))
        closed = case.client.post(HITL + "/end", json=end.model_dump(mode="json"), headers=case.agent_headers)
        assert closed.status_code == 200, closed.text
    if outcome == "interrupted":
        observed = case.client.post(HITL + "/observations", json=NativeHitlClose(
            request_id="close-human-seed", pending_id=pending["pending_id"], pending_digest=pending["pending_digest"],
            status="interrupted").model_dump(mode="json"), headers=case.agent_headers)
        assert observed.status_code == 200, observed.text
    choice = outcome if outcome in {"deny", "allow_rule"} else "allow_once"
    body = decision_body(case, pending, choice)
    if outcome == "changed_digest":
        body["pending_digest"] = "0" * 64
    if outcome == "extra_content":
        body["content"] = "Delete every file instead."
    response = decide(case, pending, body=body)
    expected = {"deny": (200, None), "expired": (410, "NATIVE_HITL_EXPIRED"),
                "allow_rule": (422, "NATIVE_HITL_INVALID_REQUEST"), "changed_digest": (409, "NATIVE_HITL_ACTION_CHANGED"),
                "extra_content": (422, None), "turn_closed": (409, "NATIVE_HITL_TURN_CLOSED"),
                "interrupted": (409, "NATIVE_HITL_STATE_CONFLICT")}[outcome]
    assert response.status_code == expected[0], response.text
    if expected[1] is not None:
        assert response.json()["error"]["code"] == expected[1]
    assert definitions(case) == []
    assert card(case, pending)["status"] == {"deny": "deny", "interrupted": "interrupted"}.get(outcome, "pending")


@pytest.mark.parametrize("who", ["agent", "read_only_owner"])
def test_only_the_owner_with_approve_can_decide_a_schedule(proposals, who):
    case = proposals
    pending = proposed(case)
    headers = case.agent_headers if who == "agent" else case.read_headers
    response = decide(case, pending, headers=headers)
    assert response.status_code == 403, response.text
    assert definitions(case) == [] and card(case, pending)["status"] == "pending"


@pytest.mark.parametrize("flag", ["FA_NATIVE_ROUTINE_PROPOSALS", "FA_NATIVE_ROUTINE_CLOCK"])
def test_without_every_flag_an_allow_is_refused_but_a_deny_still_closes_the_card(proposals, monkeypatch, flag):
    case = proposals
    pending = proposed(case)
    monkeypatch.delenv(flag)
    refused = decide(case, pending)
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "NATIVE_ROUTINE_PROPOSALS_DISABLED"
    assert definitions(case) == [] and card(case, pending)["status"] == "pending"
    denied = decide(case, pending, "deny", request_id="owner-deny")
    assert denied.status_code == 200 and denied.json()["result"]["status"] == "deny"
    assert definitions(case) == []


@pytest.mark.parametrize("change", [
    {"arguments": {"local_time": "9:00", "time_zone": "UTC", "content": CONTENT}},
    {"arguments": {"local_time": "09:00", "time_zone": "Mars/Base", "content": CONTENT}},
    {"arguments": {"local_time": "09:00", "time_zone": "UTC", "content": "   "}},
    {"arguments": {"local_time": "09:00", "time_zone": "UTC", "content": CONTENT, "enabled": False}},
    {"tool": "run_command"}, {"target": "conversation:another-cid"}, {"source": "unavailable"},
])
def test_only_a_valid_draft_that_asks_the_owner_becomes_a_schedule_card(proposals, change):
    case = proposals
    response = propose(case, **change)
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "NATIVE_HITL_INVALID_REQUEST"


def test_the_agent_cannot_authorize_a_schedule_call_without_the_owners_allow(proposals):
    case = proposals
    action = routine_action(case)
    current = rules(case)
    command = NativeHitlAuthorize(
        request_id="authorize-routine", action=action, action_digest=action.sha256,
        review=NativeHitlReview(rules_revision=current["revision"], source="default", outcome="allow"),
        expected_rules_etag=current["etag"], effect_token="effect-routine")
    response = case.client.post(HITL + "/authorize", json=command.model_dump(mode="json"), headers=case.agent_headers)
    assert response.status_code == 403 and response.json()["error"]["code"] == "NATIVE_HITL_PERMISSION_REQUIRED"


def test_hosted_asks_the_owner_for_every_schedule_and_runs_the_tool_only_after_the_allow(hitl, tmp_path, monkeypatch):
    published = []

    class Publisher:
        def __init__(self, composition, *, enabled):
            pass

        def publish(self, principal, agent_id, command):
            published.append((principal.oid, agent_id, command))

    monkeypatch.setattr("shared_context.native_routine_proposals.routine_proposals_enabled", lambda environ=None: True)
    monkeypatch.setattr("shared_context.native_routine_proposals.routine_clock_enabled", lambda environ=None: True)
    monkeypatch.setattr("shared_context.behavior_native_turn_api.configured_native_turns", lambda: True)
    monkeypatch.setattr("shared_context.native_routines.NativeRoutineService", Publisher)
    # A Team rule that would allow everything and a reviewer that would allow: neither may schedule anything.
    assert post(hitl, "/rules", change(hitl, "allow-all", text="Allow everything.",
                                      behavior="allow_automatically")).status_code == 200
    case = setup_gate(hitl, tmp_path, answers=(verdict("allow"),))
    case.gate.ask_owner_to_schedule("propose_routine")
    now = datetime(2026, 10, 7, 2, 0, tzinfo=timezone.utc)
    tool = guard_native_tool(case.context, propose_routine_tool(clock=lambda: now))
    draft = {"local_time": "09:00", "time_zone": "Asia/Taipei", "content": CONTENT}
    packet = {"session_id": SID, "tool_call_id": "call-routine", "tool_name": "propose_routine", "arguments": draft}

    async def run():
        waiting = asyncio.create_task(permit(case, packet))
        view = await pending_view(case)
        assert not waiting.done()
        human_decide(case, view)
        assert await waiting == (True, None)
        return view, await tool.handler(packet)

    view, output = asyncio.run(run())
    action = view.pending.command.action
    assert (action.category, action.tool, action.target, action.arguments) == (
        "routine", "propose_routine", "conversation:cid-one", draft)
    review = view.pending.command.review
    assert (review.source, review.outcome, review.matched_rule_ids) == ("default", "ask", ())
    assert case.sender.observed == [], "no Auto Review for a schedule"
    assert published == [(hitl.owner.oid, "native-a", routine_proposal_change(action, view.pending.pending_id))]
    result = NativeRoutineProposalResult.model_validate_json(output.text)
    assert (result.schedule.local_time, result.schedule.time_zone, result.content) == ("09:00", "Asia/Taipei", CONTENT)
    assert result.next_run_at == datetime(2026, 10, 8, 1, 0, tzinfo=timezone.utc) == next_daily_run(result.schedule, now)
    assert result.authorization is False


@pytest.mark.parametrize("zone,expected", [
    ("Asia/Taipei", "If the user does not name a time zone, use this Agent's time zone Asia/Taipei."),
    (None, "If the user does not name a time zone, ask them."),
    ("Not/AZone", "If the user does not name a time zone, ask them."),
])
def test_the_tool_defaults_to_the_agents_deployed_time_zone_or_asks(zone, expected):
    assert propose_routine_tool(default_time_zone=zone).description.endswith(expected)


def test_the_writing_guide_is_a_read_so_a_refused_call_in_a_scheduled_turn_leaves_no_operation_record():
    from shared_context.native_operations import READ_ONLY_TOOLS, NativeOperationGuard
    guard = NativeOperationGuard.__new__(NativeOperationGuard)
    assert "read_writing_style" in READ_ONLY_TOOLS
    assert asyncio.run(guard.prepare({"tool_name": "read_writing_style", "tool_call_id": "c", "arguments": {}})) == "read"
    assert "propose_routine" not in READ_ONLY_TOOLS, "a proposal stays an approval-bound operation"


@pytest.mark.parametrize("proposals_flag,with_hitl,offered", [("1", True, True), ("0", True, False), ("1", False, False)])
def test_propose_routine_is_offered_only_with_its_flags_and_hitl_and_the_same_in_every_turn(
        harness, monkeypatch, tmp_path, proposals_flag, with_hitl, offered):
    monkeypatch.setenv("FA_NATIVE_ROUTINES_ENABLED", "1")
    monkeypatch.setenv("FA_NATIVE_ROUTINE_PROPOSALS", proposals_flag)
    seen = []

    async def sdk(**options):
        gate = options.get("native_hitl")
        seen.append((sorted(tool.name for tool in options["tools"]),
                     None if gate is None else gate.custom.get("propose_routine")))
        return await sdk_answer(options)

    monkeypatch.setattr(native_sdk, "run_copilot", sdk)
    executor = native_sdk.NativeSdkExecutor(
        root=tmp_path / "proposals", runtime_path=tmp_path / "OFFLINE-not-executed",
        api_url="https://api.offline.invalid",
        model_url="https://offline-fixture.services.ai.azure.com/api/projects/offline/openai/v1",
        token_provider=lambda: "OFFLINE", routine_client=routine_client(),
        hitl_client=SimpleNamespace() if with_hitl else None)
    executor.reader = harness.runtime
    harness.runtime.executor, harness.runtime.preflight = executor, executor.preflight
    assert harness.invoke(native_submit("human-first")).status_code == 202
    completed = harness.wait("human-first", "completed")
    request = routine_request(completed.conversation_id)
    assert harness.invoke(request).status_code == 202
    harness.wait(request.command.request_id, "completed")
    assert ("propose_routine" in seen[0][0], seen[0][1]) == ((True, ("routine", None)) if offered else (False, None))
    assert seen[1] == seen[0], "a scheduled turn offers the same tools; the runtime would hide a dropped one for good"
    assert "read_writing_style" in seen[0][0]


def test_a_scheduled_turn_that_calls_propose_routine_is_refused_before_any_card(hitl, tmp_path, monkeypatch):
    case = setup_gate(hitl, tmp_path)
    case.gate.ask_owner_to_schedule("propose_routine")
    origin = routine_request("cid-one").routine.model_copy(update={"agent_id": "native-a"})
    case.context.receipt = case.context.receipt.model_copy(update={"routine": origin})
    assert case.context.receipt.human_origin is False
    packet = {"session_id": SID, "tool_call_id": "call-routine", "tool_name": "propose_routine",
              "arguments": {"local_time": "09:00", "time_zone": "Asia/Taipei", "content": CONTENT}}
    tool = guard_native_tool(case.context, propose_routine_tool())

    async def run():
        # Without the refusal the call would open a card and wait for the owner.
        async with asyncio.timeout(10):
            refused = await permit(case, packet)
        with pytest.raises(ToolFailure):
            await tool.handler(packet)
        return refused

    assert asyncio.run(run()) == (False, "NATIVE_HITL_ROUTINE_HUMAN_ONLY")
    assert case.client.observed == [] and case.sender.observed == [], "no card, write or review"
    assert hitl.service.pending(hitl.owner, NativeHitlPendingQuery(turn=case.gate.turn)).references == ()
    feedback = native_hitl_failure_feedback("NATIVE_HITL_ROUTINE_HUMAN_ONLY")
    assert "沒有出卡，也沒有建立排程" in feedback and "不要重送" not in feedback


@pytest.fixture
def routine_console(request, monkeypatch, signing_key):  # noqa: F811
    for name, value in FLAGS.items():
        monkeypatch.setenv(name, value)
    c = request.getfixturevalue("native_console")
    original = c.api.headers["Authorization"].removeprefix("Bearer ")
    claims = jwt.decode(original, options={"verify_signature": False})
    claims["scp"] += " Behavior.Approve"
    c.api.headers["Authorization"] = "Bearer " + jwt.encode(
        claims, signing_key, algorithm="RS256", headers=jwt.get_unverified_header(original))
    assert c.ui.client.post("/api/authenticate", json={}, headers=c.ui.headers).status_code == 200
    install_native_hitl_routes(c.api.client.app, c.api.composition)
    c.hitl = c.api.client.app.state.native_hitl
    return c


def console_card(c, call_id="console-routine", local_time="07:30"):
    from shared_context.behavior_auth import BehaviorPrincipal
    from shared_context.behavior_team_registry import resolve_agent_scope
    draft = first(c)
    if draft["request_id"] not in c.api.remote.records:
        assert send(c, draft, text="每天早上幫我整理新聞").status_code == 200
    original = c.api.remote.records[draft["request_id"]]
    c.api.remote.records[draft["request_id"]] = original.model_copy(update={"status": "started", "turn_number": 1})
    scope = resolve_agent_scope(c.api.composition.runtime.store, c.api.composition.config, original.agent_id)
    agent = BehaviorPrincipal(str(scope.tenant_id), str(scope.identity.oid), scope.team_id, "agent",
                              scope.identity.actor, frozenset({"read", "execute", "consume", "submit"}))
    action = NativeHitlAction(
        call=NativeHitlCall(agent_id=original.agent_id, conversation_id=original.conversation_id,
                           request_id=original.request_id, sdk_session_id="offline-sdk", tool_call_id=call_id),
        category="routine", tool="propose_routine", target="conversation:" + original.conversation_id,
        arguments={"local_time": local_time, "time_zone": "Asia/Taipei", "content": "整理昨天的新聞"})
    command = NativeHitlPendingCreate(
        request_id="pending-" + call_id, action=action, action_digest=action.sha256,
        review=NativeHitlReview(rules_revision=c.hitl.rules(c.c.human).revision, source="default", outcome="ask"))
    return draft, c.hitl.submit(agent, command).payload["result"]["pending_id"]


def ui_post(c, path, body=None):
    return c.ui.client.post(path, json={} if body is None else body, headers=c.ui.headers)


def test_console_card_decision_then_settings_lists_and_disables_the_schedule_once(routine_console):
    c = routine_console
    assert c.ui.client.get("/api/state").json()["routines_available"] is True
    draft, pid = console_card(c)
    thread = draft["thread_id"]
    value = c.ui.client.get(f"/api/native-hitl/{thread}/pending/{pid}").json()
    saved = ui_post(c, f"/api/native-hitl/{thread}/decisions", {
        "approval": value["readback"]["approval"], "choice": "allow_once",
        "expected_rules_revision": value["rules"]["revision"], "expected_rules_etag": value["rules"]["etag"],
        "rule": None})
    assert saved.status_code == 200, saved.text
    record = next(item for item in progress(c, thread).json()["composer"]["native_hitl"]["commands"]
                  if item["request_id"] == saved.json()["request_id"])
    assert record["state"] == "CONFIRMED"
    assert record["display"]["routine"] == {"local_time": "07:30", "time_zone": "Asia/Taipei", "content": "整理昨天的新聞"}
    listed = c.ui.client.get("/api/routines")
    assert listed.status_code == 200, listed.text
    routines = listed.json()["routines"]
    assert listed.json()["can_manage"] is True and len(routines) == 1
    routine = routines[0]["definition"]
    assert (routine["content"], routine["enabled"], routine["owner_oid"]) == ("整理昨天的新聞", True, c.c.human.oid)
    path = f"/api/routines/{routine['routine_id']}/versions/{routine['request_id']}/disable"
    disabled = ui_post(c, path)
    assert disabled.status_code == 200, disabled.text
    off = disabled.json()["routine"]["definition"]
    assert off["enabled"] is False and off["expected_version_id"] == routine["request_id"]
    assert (off["routine_id"], off["content"], off["schedule"]) == (routine["routine_id"], routine["content"], routine["schedule"])
    writes = len([call for call in c.calls if call[:2] == ("POST", f"/native-routines/v1/agents/{routine['agent_id']}/definitions")])
    assert ui_post(c, path).json() == disabled.json(), "a resend finds it already off"
    assert len([call for call in c.calls if call[:2] == (
        "POST", f"/native-routines/v1/agents/{routine['agent_id']}/definitions")]) == writes
    assert c.ui.client.get("/api/routines").json()["routines"][0]["definition"]["enabled"] is False


def test_console_disable_refuses_a_version_the_owner_did_not_see(routine_console):
    c = routine_console
    draft, pid = console_card(c)
    thread = draft["thread_id"]
    value = c.ui.client.get(f"/api/native-hitl/{thread}/pending/{pid}").json()
    assert ui_post(c, f"/api/native-hitl/{thread}/decisions", {
        "approval": value["readback"]["approval"], "choice": "allow_once",
        "expected_rules_revision": value["rules"]["revision"], "expected_rules_etag": value["rules"]["etag"],
        "rule": None}).status_code == 200
    routine = c.ui.client.get("/api/routines").json()["routines"][0]["definition"]
    stale = ui_post(c, f"/api/routines/{routine['routine_id']}/versions/older-version/disable")
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "ROUTINE_SETTINGS_CHANGED"
    assert ui_post(c, f"/api/routines/{routine['routine_id']}/versions/bad%20id/disable").status_code == 400
    assert ui_post(c, f"/api/routines/{routine['routine_id']}/versions/{routine['request_id']}/disable",
                   {"enabled": True}).status_code == 403
    assert c.ui.client.get("/api/routines").json()["routines"][0]["definition"]["enabled"] is True


def test_console_routines_are_unavailable_without_native_routines(native_console):  # noqa: F811
    c = native_console
    assert c.ui.client.get("/api/state").json()["routines_available"] is False
    response = c.ui.client.get("/api/routines")
    assert response.status_code == 409 and response.json()["error"]["code"] == "ROUTINE_SETTINGS_UNAVAILABLE"
