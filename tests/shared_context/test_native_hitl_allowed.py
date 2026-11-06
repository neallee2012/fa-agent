"""Owner allowed-call log over the original authorization and intent rows; signed HTTP and a Table scan double."""

import copy
from datetime import timedelta

import pytest

from shared_context.native_hitl_wire import (
    NATIVE_HITL_HTTP_BYTES, NativeHitlApprovalReference, NativeHitlAuthorize, NativeHitlReview,
)
from test_m3_auth import jwk_client, signing_key
from test_native_hitl import change, create_pending, decision, error, hitl, post, proposal
from test_native_hitl_attention import attention

PATH = "/native-hitl/v1/allowed"


def authorize(c, call_id, review, *, principal=None, agent_id="native-a", approval=None, action=None):
    principal = c.agent if principal is None else principal
    rules = c.service.rules(principal)
    action = action or proposal(c, call_id=call_id, agent_id=agent_id, principal=principal).action
    command = NativeHitlAuthorize(
        request_id="claim-" + call_id, action=action, action_digest=action.sha256,
        review=review.model_copy(update={"rules_revision": rules.revision}), expected_rules_etag=rules.etag,
        effect_token="effect-" + call_id, approval=approval)
    response = post(c, "/authorize", command, principal)
    assert response.status_code == 200, response.text
    return command


def human_once(c):
    command, card = create_pending(c)
    assert post(c, "/decisions", decision(c, card)).status_code == 200
    approval = NativeHitlApprovalReference(pending_id=card["pending_id"], pending_digest=card["pending_digest"],
                                           call=command.action.call)
    return authorize(c, "tool-one", command.review, action=command.action, approval=approval)


def by_rule(c):
    assert post(c, "/rules", change(c, "allow-echo", text="允許 echo", behavior="allow_automatically")).status_code == 200
    rule_id = c.service.rules(c.owner).rules[0].rule_id
    return rule_id, authorize(c, "tool-rule", NativeHitlReview(
        rules_revision=0, matched_rule_ids=(rule_id,), source="rules", outcome="allow"))


def toolbox(c, call_id="tool-box"):
    return authorize(c, call_id, NativeHitlReview(
        rules_revision=0, source="toolbox", reason="toolbox_never", outcome="allow"))


def collect(c, *, principal=None, **params):
    entries, pages, cursor = [], [], None
    for _ in range(100):
        query = dict(params) if cursor is None else {"cursor": cursor}
        response = c.client.get(PATH, params=query, headers=c.headers(principal, read_only=True))
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store" and "etag" not in response.headers
        value = response.json()
        assert value["schema_version"] == "fa-native-hitl-allowed-v1"
        pages.append((len(response.content), value))
        entries.extend(value["entries"])
        if value["next_cursor"] is None:
            return entries, pages
        assert value["next_cursor"] != cursor
        cursor = value["next_cursor"]
    pytest.fail("the allowed-log cursor did not finish")


def test_owner_reads_rule_human_once_and_toolbox_calls_with_their_original_review_and_action(attention):
    c = attention
    once = human_once(c)
    rule_id, ruled = by_rule(c)
    boxed = toolbox(c)
    before = copy.deepcopy(c.table.backing.rows)
    entries, _pages = collect(c)
    assert c.table.backing.rows == before
    found = {entry["action"]["call"]["tool_call_id"]: entry for entry in entries}
    assert {key: entry["by"] for key, entry in found.items()} == {
        "tool-one": "human_once", "tool-rule": "rule", "tool-box": "toolbox"}
    assert found["tool-rule"]["review"]["matched_rule_ids"] == [rule_id]
    for command in (once, ruled, boxed):
        entry = found[command.action.call.tool_call_id]
        assert entry["action"] == command.action.model_dump(mode="json")
        assert entry["review"] == command.review.model_dump(mode="json")
        assert entry["authorized_at"] == c.now.isoformat().replace("+00:00", "Z")
    assert found["tool-one"]["approval"]["pending_id"] == once.approval.pending_id
    assert found["tool-rule"]["approval"] is None


def test_another_teams_calls_are_absent_and_its_owner_sees_only_its_own(attention):
    c = attention
    toolbox(c)
    authorize(c, "foreign", NativeHitlReview(rules_revision=0, source="toolbox", reason="toolbox_never", outcome="allow"),
              principal=c.other_team, agent_id="native-c")
    mine, _ = collect(c)
    theirs, _ = collect(c, principal=c.owner_two)
    assert [entry["action"]["call"]["tool_call_id"] for entry in mine] == ["tool-box"]
    assert [entry["action"]["call"]["tool_call_id"] for entry in theirs] == ["foreign"]


def test_the_default_window_is_24_hours_and_since_reaches_further(attention):
    c = attention
    toolbox(c, "old-call")
    start = c.now
    c.now += timedelta(hours=25)
    toolbox(c, "new-call")
    recent, pages = collect(c)
    assert [entry["action"]["call"]["tool_call_id"] for entry in recent] == ["new-call"]
    assert pages[0][1]["since"] == (c.now - timedelta(hours=24)).isoformat().replace("+00:00", "Z")
    both, _ = collect(c, since=start.isoformat())
    assert sorted(entry["action"]["call"]["tool_call_id"] for entry in both) == ["new-call", "old-call"]


def test_an_empty_page_with_a_cursor_is_not_the_end(attention):
    c = attention
    for index in range(25):
        assert post(c, "/rules", change(c, f"rule-{index}", text=f"規則 {index}")).status_code == 200
        c.service.submit(c.owner, change(c, f"del-{index}", "delete", rule_id=c.service.rules(c.owner).rules[0].rule_id))
    toolbox(c)
    entries, pages = collect(c)
    assert [entry["action"]["call"]["tool_call_id"] for entry in entries] == ["tool-box"]
    assert any(value["entries"] == [] and value["next_cursor"] is not None for _size, value in pages)


def test_maximum_size_actions_page_under_the_http_limit_and_resume_inside_one_scan_page(attention):
    c = attention
    for index in range(3):
        base = proposal(c, call_id=f"big-{index}")
        action = base.action.model_copy(update={"arguments": {"command": "echo big", "payload": "x" * 190000}})
        authorize(
            c, f"big-{index}", NativeHitlReview(rules_revision=0, source="toolbox", reason="toolbox_never",
                                                outcome="allow"), action=action)
    entries, pages = collect(c)
    assert sorted(entry["action"]["call"]["tool_call_id"] for entry in entries) == ["big-0", "big-1", "big-2"]
    assert all(entry["action"]["arguments"]["payload"] == "x" * 190000 for entry in entries)
    assert all(size < NATIVE_HITL_HTTP_BYTES for size, _value in pages), [size for size, _ in pages]
    assert max(len(value["entries"]) for _size, value in pages) == 2
    assert len({query[0]["partition"] for query in c.queries}) == 1


@pytest.mark.parametrize("principal", ["agent", "other_agent", "other_team"])
def test_only_a_team_owner_reads_the_log_and_never_scans_first(attention, principal):
    c = attention
    error(c.client.get(PATH, headers=c.headers(getattr(c, principal))), 403)
    assert c.client.get(PATH).status_code == 401
    assert not c.queries


@pytest.mark.parametrize("query", ["?since=2026-10-02T09:00:00", "?since=2030-01-01T00:00:00+00:00",
                                   "?since=yesterday", "?cursor=not-a-cursor", "?team_id=team-two", "?limit=5",
                                   "?since=2026-10-02T09:00:00%2B00:00&since=2026-10-02T09:00:00%2B00:00"])
def test_forged_or_unbounded_selectors_are_refused(attention, query):
    c = attention
    error(c.client.get(PATH + query, headers=c.headers()), 422)
    assert not c.queries


def test_a_cursor_keeps_its_window_and_team(attention):
    c = attention
    for index in range(25):
        assert post(c, "/rules", change(c, f"rule-{index}", text=f"規則 {index}")).status_code == 200
        c.service.submit(c.owner, change(c, f"del-{index}", "delete", rule_id=c.service.rules(c.owner).rules[0].rule_id))
    first = c.client.get(PATH, headers=c.headers()).json()
    assert first["next_cursor"] is not None
    error(c.client.get(PATH, params={"cursor": first["next_cursor"], "since": first["since"]},
                       headers=c.headers()), 422)
    error(c.client.get(PATH, params={"cursor": first["next_cursor"]}, headers=c.headers(c.owner_two)), 422)


def test_an_authorization_that_differs_from_its_intent_is_invalid(attention):
    import json
    c = attention
    command = toolbox(c)
    key = next(stored for (_, _, stored), entity in c.table.backing.rows.items()
               if json.loads(entity["Payload"]).get("data", {}).get("kind") == "native_hitl_authorization")
    entity = next(value for (_, _, stored), value in c.table.backing.rows.items() if stored == key)
    payload = json.loads(entity["Payload"])
    payload["data"]["authorization"]["effect_token"] = "another-token"
    entity["Payload"] = json.dumps(payload).encode()
    assert command.effect_token != "another-token"
    error(c.client.get(PATH, headers=c.headers()), 409, "NATIVE_HITL_RECORD_INVALID")


def test_a_shifted_reread_after_a_full_page_never_repeats_a_shown_call(attention, monkeypatch):
    c = attention
    for index in range(3):
        action = proposal(c, call_id=f"big-{index}").action.model_copy(
            update={"arguments": {"command": "echo big", "payload": "x" * 190000}})
        authorize(c, f"big-{index}", NativeHitlReview(rules_revision=0, source="toolbox", reason="toolbox_never",
                                                      outcome="allow"), action=action)
    store = c.service.store
    rows, token = store.read_record_page(limit=20)
    while token is not None:
        more, token = store.read_record_page(limit=20, continuation=token)
        rows += more
    calls = sorted((identifier, record) for identifier, record in rows
                   if record.payload["data"].get("kind") == "native_hitl_authorization")
    assert len(calls) == 3
    # The first read fills on size after two calls; the re-read comes back short (rows landed before it).
    pages, token = {None: [[*calls], [calls[0]]], "T": [calls[1:]]}, {"NextRowKey": "T"}
    monkeypatch.setattr(store, "read_record_page", lambda *, limit, continuation, modified_since: (
        pages[continuation and continuation["NextRowKey"]].pop(0),
        token if continuation is None and not pages[None] else None))
    entries, _pages = collect(c)
    ids = [entry["authorization_id"] for entry in entries]
    assert ids == [identifier for identifier, _ in calls]
