"""Owner rule history over the original create-only snapshot and intent rows; signed HTTP, fake Table only."""

from datetime import timedelta
import json

import pytest

from conftest import A, TENANT
from shared_context.native_hitl_wire import (
    NATIVE_HITL_HTTP_BYTES, NativeHitlDecision, NativeHitlPendingCreate, NativeHitlRuleInput, hitl_key,
)
from test_m3_auth import jwk_client, signing_key
from test_native_hitl import change, create_pending, decision, error, hitl, post, proposal

PATH = "/native-hitl/v1/rules/history"


def history(c, principal=None, **params):
    response = c.client.get(PATH, params=params, headers=c.headers(principal, read_only=True))
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store" and "etag" not in response.headers
    return response.json()


def ok(response):
    assert response.status_code == 200, response.text
    return response.json()["result"]


def edit(c, request_id, operation, **kwargs):
    c.now += timedelta(minutes=1)
    return ok(post(c, "/rules", change(c, request_id, operation, **kwargs)))


def four_changes(c):
    first = edit(c, "add-one", "add", text="先問我再寄外部訊息", behavior="ask_first")
    edit(c, "update-one", "update", rule_id=first["rule_id"], text="寄外部訊息前一律先問我", behavior="ask_first")
    edit(c, "delete-one", "delete", rule_id=first["rule_id"])
    command, card = create_pending(c)
    c.now += timedelta(minutes=1)
    added = ok(post(c, "/decisions", decision(c, card, choice="allow_rule")))
    return first["rule_id"], command, card, added["rule_id"]


def test_add_update_delete_allow_rule_read_back_newest_first_with_texts_times_and_source_card(hitl):
    c = hitl
    start = c.now
    rule_id, command, card, allowed = four_changes(c)
    page = history(c)
    assert (page["schema_version"], page["team_id"], page["head_revision"], page["next_before"]) == (
        "fa-native-hitl-rule-history-v1", A.team, 4, None)
    entries = page["entries"]
    assert [entry["revision"] for entry in entries] == [4, 3, 2, 1]
    assert [entry["change"] for entry in entries] == ["allow_rule", "delete", "update", "add"]
    assert [entry["changed_at"] for entry in entries] == [
        (start + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z") for minutes in (4, 3, 2, 1)]
    assert {entry["changed_by_oid"] for entry in entries} == {c.owner.oid}
    assert {entry["purpose"] for entry in entries} == {"readback-only-not-execution-permission"}
    asked, updated = ({"text": text, "behavior": "ask_first"} for text in ("先問我再寄外部訊息", "寄外部訊息前一律先問我"))
    assert [(entry["rule"], entry["previous_rule"]) for entry in entries] == [
        ({"text": "Allow echo in the synthetic workspace.", "behavior": "allow_automatically"}, None),
        (None, updated), (updated, asked), (asked, None)]
    assert [entry["rule_id"] for entry in entries] == [allowed, rule_id, rule_id, rule_id]
    assert rule_id == hitl_key("rule", TENANT, A.team, entries[3]["intent_id"])
    assert allowed == hitl_key("rule", TENANT, A.team, entries[0]["intent_id"])
    assert entries[0]["source_pending_id"] == card["pending_id"]
    assert entries[0]["source_call"] == command.action.call.model_dump(mode="json")
    assert all(entry["source_pending_id"] is None and entry["source_call"] is None for entry in entries[1:])


def test_pages_continue_before_the_oldest_shown_revision(hitl):
    c = hitl
    four_changes(c)
    first = history(c, limit=3)
    assert [entry["revision"] for entry in first["entries"]] == [4, 3, 2] and first["next_before"] == 2
    rest = history(c, before=2)
    assert [entry["revision"] for entry in rest["entries"]] == [1] and rest["next_before"] is None
    assert history(c, before=5)["entries"] == first["entries"] + rest["entries"]
    assert history(c, before=1)["entries"] == [] and history(c, before=1)["next_before"] is None


def test_a_missing_intent_row_is_invalid_not_skipped(hitl):
    c = hitl
    edit(c, "add-one", "add", text="先問我")
    snapshot = json.loads(entity(c, hitl_key("rules-revision", TENANT, A.team, "1"))["Payload"])["data"]
    del c.table.backing.rows[next(key for key in c.table.backing.rows
                                  if key[2] == "behavior:v1:row:" + snapshot["intent_id"])]
    error(c.client.get(PATH, headers=c.headers()), 409, "NATIVE_HITL_RECORD_INVALID")


@pytest.mark.parametrize("query", ["?before=6", "?before=0", "?limit=21", "?limit=0", "?before=1&before=2",
                                   "?team_id=team-two", "?after=1"])
def test_unbounded_or_forged_selectors_are_refused(hitl, query):
    c = hitl
    four_changes(c)
    error(c.client.get(PATH + query, headers=c.headers()), 422)


def test_only_the_team_owner_reads_history_and_another_owner_sees_only_their_team(hitl):
    c = hitl
    four_changes(c)
    for principal in (c.agent, c.other_agent, c.other_team):
        error(c.client.get(PATH, headers=c.headers(principal)), 403)
    assert c.client.get(PATH).status_code == 401
    assert history(c, c.owner_two)["entries"] == []


def entity(c, key):
    return next(value for (_, _, stored), value in c.table.backing.rows.items() if stored == "behavior:v1:row:" + key)


@pytest.mark.parametrize("field", ["text", "rule_id"])
def test_a_snapshot_that_differs_from_its_intent_is_invalid_not_guessed(hitl, field):
    c = hitl
    four_changes(c)
    row = entity(c, hitl_key("rules-revision", TENANT, A.team, "2"))
    payload = json.loads(row["Payload"])
    payload["data"]["rules"][0][field] = "f" * 64 if field == "rule_id" else "被改過的規則"
    row["Payload"] = json.dumps(payload).encode()
    error(c.client.get(PATH, headers=c.headers()), 409, "NATIVE_HITL_RECORD_INVALID")


def test_an_add_whose_stable_id_is_not_derived_from_its_intent_is_invalid(hitl):
    c = hitl
    edit(c, "add-one", "add", text="先問我")
    row = entity(c, hitl_key("rules-revision", TENANT, A.team, "1"))
    payload = json.loads(row["Payload"])
    payload["data"]["intent_id"] = c.service._intent_key(c.owner, change(c, "another-intent"))
    row["Payload"] = json.dumps(payload).encode()
    error(c.client.get(PATH, headers=c.headers()), 409, "NATIVE_HITL_RECORD_INVALID")


def test_an_intent_refused_by_a_racing_rule_change_is_not_in_history(hitl, monkeypatch):
    c = hitl
    edit(c, "add-one", "add", text="原本的規則")
    stale = change(c, "stale-add", text="被搶先的規則")
    original, raced = c.store.transact, []

    def transact(request, changes):
        result = original(request, changes)
        if request.request_id == c.service._intent_key(c.owner, stale) and not raced:
            raced.append(c.service.submit(c.owner, change(c, "racer", text="搶先的規則")))
        return result

    monkeypatch.setattr(c.store, "transact", transact)
    error(post(c, "/rules", stale), 409, "NATIVE_HITL_RULES_CHANGED")
    assert raced and c.store.read_record(c.service._intent_key(c.owner, stale)) is not None
    texts = [entry["rule"]["text"] for entry in history(c)["entries"]]
    assert texts == ["搶先的規則", "原本的規則"]


def test_a_full_page_of_packed_allow_rule_cards_stays_within_the_read_and_response_bounds(hitl, monkeypatch):
    c = hitl
    first = edit(c, "add-one", "add", text="先問我")
    edit(c, "delete-one", "delete", rule_id=first["rule_id"])
    for index in range(20):
        base = proposal(c, call_id=f"tool-{index}", request_id=f"pending-{index}")
        action = base.action.model_copy(update={"arguments": {**base.action.arguments, "payload": "x" * 40000}})
        card = ok(post(c, "/pending", NativeHitlPendingCreate(
            request_id=base.request_id, action=action, action_digest=action.sha256, review=base.review), c.agent))
        assert json.loads(entity(c, card["pending_id"])["Payload"])["data"]["kind"] == "native_hitl_packed_record"
        rules = c.service.rules(c.owner)
        ok(post(c, "/decisions", NativeHitlDecision(
            request_id=f"allow-{index}", pending_id=card["pending_id"], pending_digest=card["pending_digest"],
            choice="allow_rule", expected_rules_revision=rules.revision, expected_rules_etag=rules.etag,
            rule=NativeHitlRuleInput(text="允" * 512, behavior="allow_automatically"))))
    keys, read = set(), c.store.read_record

    def counted(key):
        keys.add(key)
        return read(key)

    monkeypatch.setattr(c.store, "read_record", counted)
    response = c.client.get(PATH, headers=c.headers(read_only=True))
    assert response.status_code == 200, response.text
    page = response.json()
    assert [entry["revision"] for entry in page["entries"]] == list(range(22, 2, -1)) and page["next_before"] == 3
    assert all(entry["source_call"]["tool_call_id"] == f"tool-{entry['revision'] - 3}" for entry in page["entries"])
    # head + 21 snapshots + 20 intents + 20 card rows (never their parts) + the owner's registration.
    assert len(keys) == 63 <= 98, len(keys)
    assert len(response.content) < NATIVE_HITL_HTTP_BYTES, len(response.content)
