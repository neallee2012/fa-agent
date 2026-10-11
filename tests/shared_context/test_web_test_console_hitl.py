"""Signed H1 control API, native/Responses receipt doubles, and the original console journal."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import json
from types import SimpleNamespace

import httpx
import jwt
import pytest
from fastapi.testclient import TestClient

from shared_context.native_hitl_api import install_native_hitl_routes
from shared_context.behavior_auth import BehaviorPrincipal
from shared_context.behavior_team_registry import resolve_agent_scope
from shared_context.native_hitl_wire import (
    NativeHitlAction, NativeHitlCall, NativeHitlEnd, NativeHitlPending, NativeHitlPendingCreate,
    NativeHitlReview, NativeHitlTurn,
)
from web_test_console.adapter import ConsoleError
from web_test_console.bridge import Journal, create_app
from web_test_console.remote import EasyAuthTokenProvider, RemoteBoundary
from test_m3_auth import jwk_client, signing_key
from test_native_turn_api import native_api
from test_conversation_transfer_startup import synthetic_standing_v1
from test_instruction_flow import instruction_models
from test_web_test_console_native import native_console
from test_web_test_console_native import first, send, progress
from test_web_test_console_responses import responses_console, responses_api


@pytest.fixture
def hitl_console(request, monkeypatch, signing_key):
    reopened_loop = asyncio.new_event_loop()
    monkeypatch.setenv("FA_NATIVE_HITL_ENABLED", "1")
    c = request.getfixturevalue("native_console")
    if getattr(request, "param", None) == "foundry-responses-v1":
        c, c.responses_fixture = request.getfixturevalue("responses_console")
    original = c.api.headers["Authorization"].removeprefix("Bearer ")
    claims = jwt.decode(original, options={"verify_signature": False})
    claims["scp"] += " Behavior.Approve"
    c.api.headers["Authorization"] = "Bearer " + jwt.encode(
        claims, signing_key, algorithm="RS256", headers=jwt.get_unverified_header(original))
    assert c.ui.client.post("/api/authenticate", json={}, headers=c.ui.headers).status_code == 200
    install_native_hitl_routes(c.api.client.app, c.api.composition, environ={"FA_NATIVE_HITL_ENABLED": "1"})
    c.hitl = SimpleNamespace(service=c.api.client.app.state.native_hitl, owner=c.c.human)
    open_ui = c.open
    c.open = lambda **options: open_ui(loop_factory=lambda: reopened_loop, **options)
    c.reopened_loop = reopened_loop
    yield c
    if not reopened_loop.is_running() and not reopened_loop.is_closed():
        reopened_loop.close()


def ui_post(c, path, body):
    return c.ui.client.post(path, json=body, headers=c.ui.headers)


def route(c, suffix):
    thread = c.ui.console.instructions.store.current()["thread_id"]
    return "/api/native-hitl/" + thread + "/" + suffix


def rule_input(rules, *, operation="add", rule_id=None, text="Ask before changing the synthetic workspace."):
    return {
        "operation": operation, "rule_id": rule_id,
        "rule": None if operation == "delete" else {"text": text, "behavior": "ask_first"},
        "expected_rules_revision": rules["revision"], "expected_rules_etag": rules["etag"],
    }


def pending_call(c, *, call_id="original-call", arguments=None, text="Original native instruction"):
    draft = first(c)
    if draft["request_id"] not in c.api.remote.records:
        assert send(c, draft, text=text).status_code == 200
    original = c.api.remote.records[draft["request_id"]]
    c.api.remote.records[draft["request_id"]] = original.model_copy(update={
        "status": "started", "turn_number": 1, "dispatch": None,
        "response_id": "resp-platform" if original.state_profile == "foundry-responses-v1" else None})
    scope = resolve_agent_scope(c.api.composition.runtime.store, c.api.composition.config, original.agent_id)
    agent = BehaviorPrincipal(str(scope.tenant_id), str(scope.identity.oid), scope.team_id, "agent",
                              scope.identity.actor, frozenset({"read", "execute", "consume", "submit"}))
    c.hitl.agent = agent
    action = NativeHitlAction(
        call=NativeHitlCall(agent_id=original.agent_id, conversation_id=original.conversation_id,
                           request_id=original.request_id, sdk_session_id="offline-sdk", tool_call_id=call_id),
        category="shell", tool="run_command", target="OFFLINE workspace",
        arguments=arguments or {"command": "echo <script>synthetic</script>"})
    rules = c.hitl.service.rules(c.hitl.owner)
    command = NativeHitlPendingCreate(
        request_id="pending-" + call_id, action=action, action_digest=action.sha256,
        review=NativeHitlReview(rules_revision=rules.revision, source="default", outcome="ask"))
    receipt = c.hitl.service.submit(agent, command)
    return draft, receipt.payload["result"]["pending_id"]


def decision_input(value, choice):
    return {
        "approval": value["readback"]["approval"], "choice": choice,
        "expected_rules_revision": value["rules"]["revision"],
        "expected_rules_etag": value["rules"]["etag"],
        "rule": {"text": "Allow echo only in the OFFLINE workspace.", "behavior": "allow_automatically"}
        if choice == "allow_rule" else None,
    }

def test_pending_attention_uses_its_original_rid_text_not_the_first_thread_label(hitl_console):
    c = hitl_console
    store = c.ui.console.instructions.store
    original = first(c)
    for index in range(3):
        draft = store.current(original["thread_id"])
        store.prepare({"request_id": draft["request_id"], "content": f"Earlier message {index + 1}."},
                      freeze=True, state_profile=original["state_profile"])
        store.new(original["thread_id"])
    wanted = "Fourth message: run echo hitl-ok-offline."
    draft, pid = pending_call(c, text=wanted)
    assert draft["request_id"] != original["request_id"]
    response = progress(c, original["thread_id"])
    assert response.status_code == 200, response.text
    item = next(entry for entry in response.json()["composer"]["attention"]
                if entry["item"]["approval"]["pending_id"] == pid)
    assert item["item"]["approval"]["call"]["request_id"] == draft["request_id"]
    assert item["label"] == wanted
    assert len(store.all(original["thread_id"])) == 4


def test_rule_click_saves_full_original_before_one_post_and_reconciles_after_restart(hitl_console):
    c = hitl_console
    current = c.ui.client.get(route(c, "rules"))
    assert current.status_code == 200, current.text
    body = rule_input(current.json()["rules"])
    observed = []

    def before_post(request):
        if request.url.path == "/native-hitl/v1/rules" and request.method == "POST":
            command = json.loads(request.content)
            entry = c.ui.console.journal.get(command["request_id"], "native-hitl")
            assert entry["state"] == "UNKNOWN"
            assert entry["intent"]["body"] == command
            assert command["expected_rules_revision"] == 0 and command["expected_rules_etag"] is None
            observed.append(command)

    c.before[0] = before_post
    saved = ui_post(c, route(c, "rules"), body)
    assert saved.status_code == 200, saved.text
    assert len(observed) == 1
    identifier = saved.json()["request_id"]
    original = c.ui.console.journal.get(identifier, "native-hitl")
    assert original["state"] == "CONFIRMED" and original["intent"]["body"]["rule"] == body["rule"]
    assert ui_post(c, route(c, "rules"), body).status_code == 200
    c.ui.client.__exit__(None, None, None)
    c.ui = c.open()
    assert ui_post(c, route(c, "rules"), body).status_code == 200
    result = ui_post(c, route(c, "reconcile"), {"request_id": identifier})
    assert result.status_code == 200 and result.json()["original_request_resent"] is False
    assert c.ui.console.journal.get(identifier, "native-hitl") == original
    assert len(observed) == 1
    rules = c.ui.client.get(route(c, "rules")).json()["rules"]
    assert rules["revision"] == 1 and len(rules["rules"]) == 1


@pytest.mark.parametrize("hitl_console", ["foundry-native-v1", "foundry-responses-v1"], indirect=True)
@pytest.mark.parametrize("choice", ["allow_once", "allow_rule", "deny"])
def test_persisted_native_pending_joins_attention_and_three_owner_choices_keep_original_call(hitl_console, choice):
    c = hitl_console
    draft, pid = pending_call(c)
    state = progress(c, draft["thread_id"])
    assert state.status_code == 200, state.text
    attention = state.json()["composer"]["attention"]
    assert len(attention) == 1 and attention[0]["item"]["type"] == "approval_required"
    assert attention[0]["thread_id"] == draft["thread_id"] and attention[0]["item"]["approval"]["pending_id"] == pid
    original = c.ui.client.get(route(c, "pending/" + pid))
    assert original.status_code == 200, original.text
    value = original.json()
    assert value["readback"]["pending"]["command"]["action"]["arguments"]["command"] == "echo <script>synthetic</script>"
    c.ui.client.__exit__(None, None, None)
    c.ui = c.open()
    restored = progress(c, draft["thread_id"])
    assert restored.json()["composer"]["attention"] == attention
    body = decision_input(value, choice)
    saved = ui_post(c, route(c, "decisions"), body)
    assert saved.status_code == 200, saved.text
    identifier = saved.json()["request_id"]
    entry = c.ui.console.journal.get(identifier, "native-hitl")
    assert entry["intent"]["selection"] == body["approval"]
    assert entry["intent"]["body"]["pending_digest"] == body["approval"]["pending_digest"]
    assert ui_post(c, route(c, "decisions"), body).status_code == 200
    current = c.ui.client.get(route(c, "pending/" + pid)).json()["readback"]
    assert current["status"] == ("deny" if choice == "deny" else "allow")
    assert current["approval"] == body["approval"] and current.get("authorization") is None
    assert current["decision"] == entry["intent"]["body"]
    assert len([item for item in c.calls if item[0] == "POST" and item[1] == "/native-hitl/v1/decisions"]) == 1
    assert len(c.hitl.service.rules(c.hitl.owner).rules) == int(choice == "allow_rule")
    assert progress(c, draft["thread_id"]).json()["composer"]["attention"] == []


def test_a_decision_keeps_what_its_card_showed_for_the_record_after_a_reload(hitl_console):
    # Neal 2026-10-07: the decided card stays as "助理要執行：…／你允許了這次"; the console keeps that short display
    # beside its saved original, outside the command, so it is never sent or compared.
    c = hitl_console
    long_command = "echo " + "x" * 700
    draft, pid = pending_call(c, arguments={"command": long_command, "cwd": "/w"})
    value = c.ui.client.get(route(c, "pending/" + pid)).json()
    saved = ui_post(c, route(c, "decisions"), decision_input(value, "allow_once"))
    assert saved.status_code == 200, saved.text
    identifier = saved.json()["request_id"]
    c.ui.client.__exit__(None, None, None)
    c.ui = c.open()
    commands = progress(c, draft["thread_id"]).json()["composer"]["native_hitl"]["commands"]
    entry = next(item for item in commands if item["request_id"] == identifier)
    assert entry["display"] == {"tool": "run_command", "category": "shell", "target": "OFFLINE workspace",
                                "command": long_command[:500], "toolbox": False}
    assert entry["created_at"] == c.ui.console.journal.get(identifier, "native-hitl")["intent"]["created_at"]
    posted = next(item for item in c.calls if item[:2] == ("POST", "/native-hitl/v1/decisions"))
    assert "display" not in json.loads(posted[2])


def test_twenty_rules_update_delete_and_stale_revision_keep_the_old_human_decision(hitl_console):
    c = hitl_console
    _, pid = pending_call(c)
    value = c.ui.client.get(route(c, "pending/" + pid)).json()
    assert ui_post(c, route(c, "decisions"), decision_input(value, "allow_rule")).status_code == 200
    original = c.ui.client.get(route(c, "pending/" + pid)).json()["readback"]
    for number in range(19):
        rules = c.ui.client.get(route(c, "rules")).json()["rules"]
        assert ui_post(c, route(c, "rules"), rule_input(rules, text=f"Ask for synthetic operation {number}.")).status_code == 200
    rules = c.ui.client.get(route(c, "rules")).json()["rules"]
    assert len(rules["rules"]) == rules["revision"] == 20
    extra = ui_post(c, route(c, "rules"), rule_input(rules, text="The 21st rule must not overwrite another."))
    assert extra.status_code == 409 and extra.json()["error"]["code"] == "NATIVE_HITL_RULE_LIMIT"
    rule_id = rules["rules"][0]["rule_id"]
    update = rule_input(rules, operation="update", rule_id=rule_id, text="Updated exact scope.")
    assert ui_post(c, route(c, "rules"), update).status_code == 200
    changed = c.ui.client.get(route(c, "rules")).json()["rules"]
    assert changed["revision"] == 21 and len(changed["rules"]) == 20
    assert changed["rules"][0]["rule_id"] == rule_id and changed["rules"][0]["text"] == "Updated exact scope."
    assert ui_post(c, route(c, "rules"), rule_input(changed, operation="delete", rule_id=rule_id)).status_code == 200
    final = c.ui.client.get(route(c, "rules")).json()["rules"]
    assert final["revision"] == 22 and len(final["rules"]) == 19
    before = len(c.calls)
    stale = ui_post(c, route(c, "rules"), {**update, "rule": {"text": "Stale update", "behavior": "ask_first"}})
    assert stale.json()["error"]["code"] == "NATIVE_HITL_RULES_CHANGED"
    assert all(method == "GET" for method, _, _ in c.calls[before:])
    saved = c.ui.client.get(route(c, "pending/" + pid)).json()["readback"]
    assert saved["decision"] == original["decision"] and saved["grant"] == original["grant"]


def test_lost_decision_ack_reopens_the_original_journal_and_only_reconciles(hitl_console):
    c = hitl_console
    _, pid = pending_call(c)
    value = c.ui.client.get(route(c, "pending/" + pid)).json()
    body = decision_input(value, "allow_once")

    def lose(request, response):
        if request.url.path == "/native-hitl/v1/decisions":
            assert response.status_code == 200
            raise httpx.ReadTimeout("OFFLINE lost decision response", request=request)
        return response

    c.mutate[0] = lose
    assert ui_post(c, route(c, "decisions"), body).status_code != 200
    identifier, original = c.ui.console.journal.entries("native-hitl")[0]
    assert original["state"] == "UNKNOWN" and original["intent"]["selection"] == body["approval"]
    c.ui.client.__exit__(None, None, None)
    c.ui = c.open()
    repeated = ui_post(c, route(c, "decisions"), body)
    assert repeated.status_code == 200 and repeated.json()["commands"][0]["state"] == "UNKNOWN"
    result = ui_post(c, route(c, "reconcile"), {"request_id": identifier})
    assert result.status_code == 200, result.text
    entry = result.json()["commands"][0]
    assert entry["state"] == "UNKNOWN" and entry["observation"]["result"]["status"] == "allow"
    assert c.ui.console.journal.get(identifier, "native-hitl") == original
    assert len([v for v in c.calls if v[:2] == ("POST", "/native-hitl/v1/decisions")]) == 1
    assert json.loads(next(v[2] for v in c.calls if v[1].endswith("/decisions/reconcile"))) == original["intent"]["body"]


def test_two_clients_share_the_same_durable_once_key(hitl_console):
    c = hitl_console
    rules = c.ui.client.get(route(c, "rules")).json()["rules"]
    second = c.open()
    body, path = rule_input(rules), route(c, "rules")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda browser: browser.client.post(path, json=body, headers=browser.headers),
                                [c.ui, second]))
    assert all(result.status_code in {200, 409} for result in results)
    assert any(result.status_code == 200 for result in results)
    assert len([v for v in c.calls if v[:2] == ("POST", "/native-hitl/v1/rules")]) == 1
    assert len(c.ui.console.journal.entries("native-hitl")) == 1


@pytest.mark.parametrize("boundary", ["expired", "closed", "digest", "call", "read_only", "no_login", "forged_actor"])
def test_invalid_or_unavailable_human_decisions_never_post(hitl_console, boundary, signing_key):
    c = hitl_console
    _, pid = pending_call(c)
    value = c.ui.client.get(route(c, "pending/" + pid)).json()
    body = decision_input(value, "allow_once")
    if boundary == "expired":
        c.c.h.time += timedelta(seconds=601)
    elif boundary == "closed":
        c.hitl.service.submit(c.hitl.agent, NativeHitlEnd(
            request_id="close-original", turn=NativeHitlTurn(**{
                key: body["approval"]["call"][key] for key in ("agent_id", "conversation_id", "request_id")})))
    elif boundary == "digest":
        body["approval"]["pending_digest"] = "f" * 64
    elif boundary == "call":
        body["approval"]["call"]["sdk_session_id"] = "different-sdk"
    elif boundary == "read_only":
        raw = c.api.headers["Authorization"][7:]
        claims = jwt.decode(raw, options={"verify_signature": False})
        claims["scp"] = "Behavior.Read"
        c.ui.console.vault.set(jwt.encode(claims, signing_key, algorithm="RS256", headers=jwt.get_unverified_header(raw)))
    elif boundary == "no_login":
        c.ui.console.vault.clear()
    else:
        body["actor"] = {"kind": "human", "system": True}
    before = len(c.calls)
    refused = ui_post(c, route(c, "decisions"), body)
    assert refused.status_code in {400, 401, 403, 409, 410, 502}, refused.text
    assert not any(method == "POST" for method, _, _ in c.calls[before:])
    assert c.ui.console.journal.entries("native-hitl") == []


def test_complete_large_pending_is_read_without_the_legacy_preview_limit(hitl_console):
    c = hitl_console
    text = "z" * (192 * 1024)
    _, pid = pending_call(c, arguments={"command": text})
    response = c.ui.client.get(route(c, "pending/" + pid))
    assert response.status_code == 200, response.text[:200]
    assert response.json()["readback"]["pending"]["command"]["action"]["arguments"]["command"] == text


@pytest.mark.parametrize("damage", ["digest", "team", "missing_api"])
def test_bad_or_missing_readback_is_explicit_not_an_empty_policy_or_an_approval(hitl_console, damage):
    c = hitl_console
    _, pid = pending_call(c)

    def damage_reply(request, response):
        if not request.url.path.startswith("/native-hitl/v1/"):
            return response
        if damage == "missing_api":
            return httpx.Response(404, json={"detail": "OFFLINE unavailable"})
        if request.url.path.endswith("/pending/" + pid):
            data = response.json()
            if damage == "digest":
                data["pending"]["command"]["action"]["arguments"]["command"] = "Changed without a matching hash"
            else:
                data["pending"]["team_id"] = "another-team"
                data["approval"]["pending_digest"] = NativeHitlPending.model_validate_json(json.dumps(data["pending"])).sha256
            return httpx.Response(200, json=data, headers=response.headers)
        return response

    c.mutate[0] = damage_reply
    refused = c.ui.client.get(route(c, "pending/" + pid))
    assert refused.status_code != 200 and "readback" not in refused.json()
    if damage == "missing_api":
        assert refused.json()["error"]["code"] == "NATIVE_HITL_UNAVAILABLE"
        state = progress(c, first(c)["thread_id"]).json()["composer"]
        assert state["native_hitl"]["error"]["code"] == "NATIVE_HITL_UNAVAILABLE"


@pytest.mark.parametrize("disabled", ["flag", "legacy"])
def test_default_off_or_legacy_adds_no_hitl_reads_or_writes(hitl_console, monkeypatch, disabled):
    c = hitl_console
    if disabled == "flag":
        draft, _ = pending_call(c)
        monkeypatch.setenv("FA_NATIVE_HITL_ENABLED", "0")
    c.ui = c.open(enabled=disabled != "legacy", path=c.tmp / (disabled + ".sqlite3") if disabled == "legacy" else None)
    before = len(c.calls)
    state = c.ui.client.get("/api/state").json()
    if disabled == "flag":
        assert progress(c, draft["thread_id"]).status_code == 200
    assert "native_hitl" not in state["composer"]
    refused = c.ui.client.get(route(c, "rules"))
    assert refused.status_code != 200
    assert not any(path.startswith("/native-hitl/v1/") for _, path, _ in c.calls[before:])


def test_remote_failed_commit_and_stale_cookie_prevent_the_hitl_writer(hitl_console, monkeypatch):
    c = hitl_console
    origin = "https://hitl.offline.example.org"
    app = create_app(c.config, c.tmp / "remote.sqlite3", backend=c.ui.console.backend,
        clock=lambda: c.c.h.time, instruction_connection=c.connection, token_provider=EasyAuthTokenProvider(),
        remote=RemoteBoundary(origin, c.config.human_oid))
    signed = {"x-ms-client-principal-id": c.config.human_oid, "x-ms-client-principal-idp": "aad",
              "x-ms-token-aad-access-token": c.api.headers["Authorization"][7:]}
    with TestClient(app, base_url=origin, backend_options={"loop_factory": lambda: c.reopened_loop}) as ui:
        page = ui.get("/", headers=signed)
        assert "__Host-fa_console=" in page.headers["set-cookie"]
        csrf = ui.get("/api/state", headers=signed).json()["csrf"]
        headers = {**signed, "Origin": origin, "X-Console-CSRF": csrf}
        assert ui.post("/api/authenticate", json={}, headers=headers).status_code == 200
        thread = app.state.console.instructions.store.current()["thread_id"]
        path = "/api/native-hitl/" + thread + "/rules"
        rules = ui.get(path, headers=signed).json()["rules"]

        def fail_flush(_path):
            raise ConsoleError("REMOTE_STATE_UNAVAILABLE", 503)

        with monkeypatch.context() as scoped:
            scoped.setattr(Journal, "after_commit", fail_flush)
            before = len(c.calls)
            refused = ui.post(path, json=rule_input(rules), headers=headers)
            assert refused.status_code == 503 and refused.json()["error"]["code"] == "REMOTE_STATE_UNAVAILABLE"
            original = app.state.console.journal.entries("native-hitl")
            assert len(original) == 1 and original[0][1]["state"] == "UNKNOWN"
            assert not any(method == "POST" for method, _, _ in c.calls[before:])
            stale = ui.post(path, json=rule_input(rules), headers={**headers, "Cookie": "__Host-fa_console=old-replica"})
            assert stale.status_code == 403 and stale.json()["error"]["code"] == "REMOTE_SESSION_CHANGED"
            assert app.state.console.journal.entries("native-hitl") == original


@pytest.mark.parametrize("body", [
    b'{"operation":"add","operation":"delete"}', b"[]", b'{"actor":"system"}', b"x" * 8193,
])
def test_new_control_body_is_bounded_strict_json_without_a_fallback_writer(hitl_console, body):
    c = hitl_console
    before = len(c.calls)
    result = c.ui.client.post(route(c, "rules"), content=body,
                             headers={**c.ui.headers, "Content-Type": "application/json"})
    assert result.status_code in {400, 413}, result.text
    assert c.calls[before:] == []
    assert c.ui.console.journal.entries("native-hitl") == []


def test_new_confirmed_rule_intent_does_not_replace_an_older_unknown(hitl_console):
    c = hitl_console
    initial = c.ui.client.get(route(c, "rules")).json()["rules"]
    first_body = rule_input(initial, text="Original rule whose ACK will be lost.")

    def lose(request, response):
        if request.url.path == "/native-hitl/v1/rules" and request.method == "POST":
            raise httpx.ReadTimeout("OFFLINE original ACK lost", request=request)
        return response

    c.mutate[0] = lose
    assert ui_post(c, route(c, "rules"), first_body).status_code != 200
    identifier, original = c.ui.console.journal.entries("native-hitl")[0]
    c.mutate[0] = lambda _request, response: response
    latest = c.ui.client.get(route(c, "rules")).json()["rules"]
    assert latest["revision"] == 1
    next_body = rule_input(latest, text="A distinct newly confirmed human rule.")
    result = ui_post(c, route(c, "rules"), next_body)
    assert result.status_code == 200 and result.json()["request_id"] != identifier
    assert c.ui.console.journal.get(identifier, "native-hitl") == original and original["state"] == "UNKNOWN"
    assert len(c.ui.console.journal.entries("native-hitl")) == 2


def test_pending_discovery_follows_the_original_twenty_item_pages(hitl_console):
    c = hitl_console
    for number in range(21):
        draft, _ = pending_call(c, call_id="call-" + str(number))
    before = len(c.calls)
    result = progress(c, draft["thread_id"])
    assert result.status_code == 200, result.text
    assert len(result.json()["composer"]["attention"]) == 21
    queries = [json.loads(raw) for method, path, raw in c.calls[before:]
               if (method, path) == ("POST", "/native-hitl/v1/turn-pending")]
    assert [query["after"] for query in queries] == [0, 20]
    assert all(query["turn"]["request_id"] == draft["request_id"] for query in queries)


def test_login_loss_between_the_reader_and_writer_retains_once_without_post(hitl_console):
    c = hitl_console
    rules = c.ui.client.get(route(c, "rules")).json()["rules"]

    def lose_login(request, response):
        if request.url.path == "/native-hitl/v1/rules" and request.method == "GET":
            c.ui.console.vault.clear()
        return response

    c.mutate[0] = lose_login
    before = len(c.calls)
    denied = ui_post(c, route(c, "rules"), rule_input(rules))
    assert denied.status_code == 401
    assert not any(method == "POST" for method, _, _ in c.calls[before:])
    assert c.ui.console.journal.entries("native-hitl") == []


def test_rules_from_another_team_cannot_be_presented_as_this_agents_policy(hitl_console):
    c = hitl_console

    def another_team(request, response):
        if request.url.path == "/native-hitl/v1/rules":
            data = response.json()
            data["team_id"] = "another-team"
            return httpx.Response(200, json=data, headers=response.headers)
        return response

    c.mutate[0] = another_team
    result = c.ui.client.get(route(c, "rules"))
    assert result.status_code == 403 and result.json()["error"]["code"] == "NATIVE_HITL_SCOPE_DENIED"
