"""Team discovery over the original H1 records; signed owner HTTP and named Table/Hosted I/O doubles."""

import copy
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from shared_context.errors import Problem
from shared_context.native_hitl_wire import NativeHitlAnswer, NativeHitlEnd, NativeHitlQuestion, NativeHitlQuestionCreate, NativeHitlTurn
from test_m3_auth import jwk_client, signing_key
from test_native_hitl import decision, hitl, post, proposal, read

PATH = "/native-hitl/v1/attention"
BASE_FILTER = "PartitionKey eq @partition and RowKey ge @start and RowKey lt @end"


@pytest.fixture
def attention(hitl, monkeypatch):
    queries = []
    # The server-maintained Timestamp of every row: the test clock plus a configurable server clock offset.
    hitl.server_offset = timedelta(0)
    stamps = {key: hitl.now for key in hitl.table.backing.rows}
    apply = hitl.table.backing.apply

    def stamped(table, operations):
        result = apply(table, operations)
        for _mode, entity, *_options in operations:
            stamps[(table, entity["PartitionKey"], entity["RowKey"])] = hitl.now + hitl.server_offset
        return result

    monkeypatch.setattr(hitl.table.backing, "apply", stamped)
    windows = {}

    def query_entities(*, query_filter, parameters, select, results_per_page, raw_request_hook, **options):
        assert query_filter in (BASE_FILTER, BASE_FILTER + " and Timestamp ge @since")
        assert ("since" in parameters) == query_filter.endswith("@since")
        assert parameters["start"] == "behavior:v1:row:" and parameters["end"] == "behavior:v1:row;"
        assert set(select) == {"PartitionKey", "RowKey", "Payload"}
        assert options["retry_total"] == options["retry_read"] == 0
        since = parameters.get("since")
        queries.append((parameters.copy(), results_per_page))
        rows = sorted(
            (copy.deepcopy(entity) for (table, partition, key), entity in hitl.table.backing.rows.items()
             if partition == parameters["partition"] and parameters["start"] <= key < parameters["end"]
             and (since is None or stamps[(table, partition, key)] >= since)),
            key=lambda entity: entity["RowKey"],
        )

        class Pages:
            def __init__(self, continuation):
                if continuation is not None:
                    # The Table service needs the original $filter on every continuation request.
                    assert windows[continuation["RowKey"]] == since
                self.offset = 0 if continuation is None else int(continuation["RowKey"])
                self.continuation_token = None
                self.read = False

            def __iter__(self):
                return self

            def __next__(self):
                if self.read:
                    raise StopIteration
                self.read = True
                raw_request_hook(SimpleNamespace(http_request=SimpleNamespace(
                    method="GET", url=hitl.table.url + "/Behavior", body=None)))
                end = min(self.offset + results_per_page, len(rows))
                self.continuation_token = (
                    {"PartitionKey": parameters["partition"], "RowKey": str(end)} if end < len(rows) else None)
                if self.continuation_token is not None:
                    windows[str(end)] = since
                hitl.returned.append(len(rows[self.offset:end]))
                return iter(rows[self.offset:end])

        return SimpleNamespace(by_page=lambda continuation_token=None: Pages(continuation_token))

    def unavailable(_agent_id):
        raise Problem(503, "NATIVE_EXECUTION_UNAVAILABLE", "OFFLINE original reader unavailable")

    monkeypatch.setattr(hitl.table, "query_entities", query_entities, raising=False)
    hitl.composition.for_agent = unavailable
    hitl.queries, hitl.returned, hitl.windows = queries, [], windows
    return hitl


def pending(c, agent_id, principal, rid, *, call_id="tool-one", wait="background"):
    command = proposal(c, agent_id=agent_id, principal=principal, rid=rid, call_id=call_id, request_id="pending-" + rid)
    command = command.model_copy(update={"wait": wait})
    response = post(c, "/pending", command, principal)
    assert response.status_code == 200, response.text
    return command, response.json()["result"]


def question(c, agent_id, principal, rid, *, wait="background"):
    call = proposal(c, agent_id=agent_id, rid=rid).action.call
    value = NativeHitlQuestion(call=call, question="Which original option?", choices=("yes", "no"))
    command = NativeHitlQuestionCreate(
        request_id="question-" + rid, question=value, question_digest=value.sha256, wait=wait)
    response = post(c, "/questions", command, principal)
    assert response.status_code == 200, response.text
    return command, response.json()["result"]


def collect(c, *, principal=None, limit=20):
    entries, cursor = [], None
    for _ in range(100):
        params = {"limit": limit}
        if cursor is not None:
            params["cursor"] = cursor
        response = c.client.get(PATH, params=params, headers=c.headers(principal, read_only=True))
        assert response.status_code == 200, response.text
        value = response.json()
        assert value["schema_version"] == "fa-native-hitl-team-attention-v1"
        assert response.headers["cache-control"] == "no-store"
        entries.extend(value["items"])
        if value["next_cursor"] is None:
            return entries
        assert value["next_cursor"] != cursor
        cursor = value["next_cursor"]
    pytest.fail("the bounded Team discovery cursor did not finish")


def test_owner_discovers_all_team_agents_without_console_threads_or_any_writes(attention):
    c = attention
    first, a = pending(c, "native-a", c.agent, "rid-a")
    second, b = pending(c, "native-b", c.other_agent, "rid-b")
    _question, q = question(c, "native-b", c.other_agent, "rid-b")
    pending(c, "native-c", c.other_team, "foreign-rid")
    before = copy.deepcopy(c.table.backing.rows)
    writes = len(c.table.submissions)
    entries = collect(c, limit=3)
    assert {entry["item"]["item_id"] for entry in entries} == {a["pending_id"], b["pending_id"], q["question_id"]}
    assert {entry["item"]["type"] for entry in entries} == {"approval_required", "question_required"}
    assert all(entry["origin"] == {"state": "unavailable", "root": None, "sender": None} for entry in entries)
    assert read(c, a["pending_id"]).json()["pending"]["command"] == first.model_dump(mode="json")
    assert read(c, b["pending_id"]).json()["pending"]["command"] == second.model_dump(mode="json")
    assert c.table.backing.rows == before and len(c.table.submissions) == writes
    assert c.queries and max(limit for _parameters, limit in c.queries) == 3


@pytest.mark.parametrize("principal", ["agent", "other_agent", "other_team", None])
def test_discovery_requires_a_team_owner_before_any_enumeration(attention, principal):
    c = attention
    headers = {} if principal is None else c.headers(getattr(c, principal))
    response = c.client.get(PATH, headers=headers)
    assert response.status_code == (401 if principal is None else 403)
    assert not c.queries


def test_another_owner_only_discovers_their_own_team(attention):
    c = attention
    pending(c, "native-a", c.agent, "rid-a")
    _command, foreign = pending(c, "native-c", c.other_team, "rid-c")
    entries = collect(c, principal=c.owner_two)
    assert [entry["item"]["item_id"] for entry in entries] == [foreign["pending_id"]]


@pytest.mark.parametrize("query", [
    "?limit=0", "?limit=21", "?limit=1&limit=2", "?team_id=team-two",
    "?agent_id=native-b", "?store=arbitrary", "?cursor=not-a-valid-cursor", "?cursor=",
])
def test_discovery_rejects_unbounded_or_forged_selectors(attention, query):
    c = attention
    response = c.client.get(PATH + query, headers=c.headers())
    assert response.status_code == 422, response.text
    assert not c.queries


def test_discovery_rejects_a_body_without_writing(attention):
    c = attention
    response = c.client.request("GET", PATH, content=b"{}", headers=c.headers())
    assert response.status_code == 422
    assert not c.queries


def test_empty_filtered_pages_keep_a_cursor_and_cross_team_cursor_is_refused(attention):
    c = attention
    pending(c, "native-c", c.other_team, "rid-c")
    response = c.client.get(PATH, params={"limit": 1}, headers=c.headers())
    assert response.status_code == 200
    assert response.json()["items"] == [] and response.json()["next_cursor"] is not None
    before = len(c.queries)
    wrong_team = c.client.get(PATH, params={"cursor": response.json()["next_cursor"]},
                             headers=c.headers(c.owner_two))
    assert wrong_team.status_code == 422 and len(c.queries) == before
    assert collect(c, limit=2) == []


def test_owner_acts_on_discovered_b_cards_using_only_original_h1_routes(attention):
    c = attention
    original, b = pending(c, "native-b", c.other_agent, "rid-b")
    asked, q = question(c, "native-b", c.other_agent, "rid-b")
    items = {entry["item"]["item_id"]: entry["item"] for entry in collect(c)}
    assert items[b["pending_id"]]["approval"]["call"] == original.action.call.model_dump(mode="json")
    assert items[q["question_id"]]["question"]["call"] == asked.question.call.model_dump(mode="json")
    no_approve = post(c, "/decisions", decision(c, b), read_only=True)
    assert no_approve.status_code == 403
    chosen = decision(c, b, choice="allow_once")
    assert post(c, "/decisions", chosen).status_code == 200
    answer = NativeHitlAnswer(
        request_id="owner-b-answer", question_id=q["question_id"], question_digest=q["question_digest"],
        answer="yes", was_freeform=False)
    assert post(c, "/answers", answer, read_only=True).status_code == 200
    assert collect(c) == []
    view = read(c, b["pending_id"]).json()
    assert view["pending"]["command"] == original.model_dump(mode="json")
    assert view["grant"]["uses"] == 1 and view.get("authorization") is None
    assert view["decision"] == chosen.model_dump(mode="json")


def test_expired_closed_and_decided_cards_are_not_returned_as_attention(attention):
    c = attention
    _old, expired = pending(c, "native-a", c.agent, "old-rid")
    question(c, "native-a", c.agent, "old-rid")
    c.now += timedelta(seconds=601)
    _closed, closed = pending(c, "native-b", c.other_agent, "closed-rid")
    question(c, "native-b", c.other_agent, "closed-rid")
    assert post(c, "/end", NativeHitlEnd(
        request_id="close-b", turn=NativeHitlTurn(
            agent_id="native-b", conversation_id="cid-one", request_id="closed-rid")), c.other_agent).status_code == 200
    _denied, denied = pending(c, "native-b", c.other_agent, "denied-rid")
    assert post(c, "/decisions", decision(c, denied, choice="deny")).status_code == 200
    _active, active = pending(c, "native-a", c.agent, "active-rid")
    before = copy.deepcopy(c.table.backing.rows)
    entries = collect(c)
    assert [entry["item"]["item_id"] for entry in entries] == [active["pending_id"]]
    assert c.table.backing.rows == before
    assert read(c, expired["pending_id"]).json()["expired"] is True
    assert read(c, closed["pending_id"]).json()["turn_closed"] is True


def test_a_wait_from_the_owners_own_turn_outlasts_the_background_ttl_and_stays_listed(attention):
    c = attention
    start = c.now
    _bg, background = pending(c, "native-a", c.agent, "scheduled-rid")
    _bq, background_question = question(c, "native-a", c.agent, "scheduled-rid")
    _ia, interactive = pending(c, "native-b", c.other_agent, "owner-rid", wait="interactive")
    _iq, interactive_question = question(c, "native-b", c.other_agent, "owner-rid", wait="interactive")
    assert read(c, background["pending_id"]).json()["pending"]["expires_at"] == "2026-10-02T09:10:00Z"
    assert read(c, interactive["pending_id"]).json()["pending"]["expires_at"] == "2026-10-03T09:00:00Z"
    c.now = start + timedelta(hours=2)
    entries = {entry["item"]["item_id"]: entry["item"] for entry in collect(c)}
    assert set(entries) == {interactive["pending_id"], interactive_question["question_id"]}
    assert entries[interactive_question["question_id"]]["wait"] == "interactive"
    late = post(c, "/decisions", decision(c, background, request_id="late-background"))
    assert late.status_code == 410 and late.json()["error"]["code"] == "NATIVE_HITL_EXPIRED"
    assert post(c, "/decisions", decision(c, interactive, request_id="late-owner")).status_code == 200
    assert read(c, interactive["pending_id"]).json()["status"] == "allow"
    answers = {qid: post(c, "/answers", NativeHitlAnswer(
        request_id="answer-" + qid[:8], question_id=qid, question_digest=item["question_digest"],
        answer="yes", was_freeform=False)) for qid, item in (
            (background_question["question_id"], background_question),
            (interactive_question["question_id"], interactive_question))}
    assert answers[background_question["question_id"]].status_code == 410
    assert answers[interactive_question["question_id"]].status_code == 200


def test_large_original_card_uses_existing_packed_readback_without_a_second_schema(attention):
    c = attention
    command = proposal(c, agent_id="native-b", principal=c.other_agent, rid="large")
    action = command.action.model_copy(update={"arguments": {"command": "echo " + "x" * 100_000}})
    command = command.model_copy(update={"action": action, "action_digest": action.sha256})
    response = post(c, "/pending", command, c.other_agent)
    assert response.status_code == 200, response.text
    original = response.json()["result"]
    entries = collect(c)
    assert len(entries) == 1 and entries[0]["item"]["approval"]["pending_digest"] == original["pending_digest"]
    returned = read(c, original["pending_id"])
    assert returned.status_code == 200
    assert returned.json()["pending"]["command"]["action"]["arguments"] == action.arguments


def test_discovery_failure_is_not_an_empty_success(attention, monkeypatch):
    from azure.core.exceptions import ServiceRequestError

    def unavailable(**_):
        raise ServiceRequestError("OFFLINE table query unavailable")

    monkeypatch.setattr(attention.table, "query_entities", unavailable)
    response = attention.client.get(PATH, headers=attention.headers())
    assert response.status_code == 503
    assert "items" not in response.json()


@pytest.mark.parametrize("damage", ["partition", "row_key", "binding", "payload"])
def test_discovery_refuses_invalid_physical_rows(attention, damage):
    c = attention
    pending(c, "native-a", c.agent, "rid-a")
    entity = next(entity for (_table, _partition, key), entity in c.table.backing.rows.items()
                  if key.startswith("behavior:v1:row:"))
    if damage == "partition":
        entity["PartitionKey"] = "another-partition"
    elif damage == "row_key":
        entity["RowKey"] = "another-namespace"
    elif damage == "payload":
        entity["Payload"] = b'{"private":"OFFLINE-sensitive-not-for-response"}'
    else:
        data = json.loads(entity["Payload"])
        data["binding"] = "0" * 64
        entity["Payload"] = json.dumps(data).encode("utf-8")
    response = c.client.get(PATH, headers=c.headers())
    assert response.status_code == 502, response.text
    assert "items" not in response.json() and "OFFLINE-sensitive" not in response.text


def test_record_discovery_cannot_enumerate_inside_a_pure_point_read(attention):
    c = attention
    with pytest.raises(Problem) as rejected:
        c.store.consistent_read(lambda: c.store.read_record_page())
    assert rejected.value.body["error"]["code"] == "BEHAVIOR_READ_SCOPE_INVALID"
    assert not c.queries


def encoded_cursor(team, after, **extra):
    import base64
    raw = json.dumps({"team": team, "after": after, **extra}, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def test_window_skips_rows_written_before_every_open_wait_and_returns_the_same_cards(attention):
    c = attention
    for index in range(6):
        pending(c, "native-a", c.agent, f"old-{index}")
        question(c, "native-b", c.other_agent, f"old-{index}")
        c.now += timedelta(seconds=120)
    c.now += timedelta(seconds=86400)
    data_rows = lambda: {key for key in c.table.backing.rows if key[2].startswith("behavior:v1:row:")}
    old_rows = data_rows()
    # The Table server's clock runs up to 4 minutes behind the API host; the margin still covers it.
    c.server_offset = -timedelta(minutes=4)
    _first, edge = pending(c, "native-a", c.agent, "edge")
    c.now += timedelta(seconds=300)
    _second, b = pending(c, "native-b", c.other_agent, "fresh-b")
    _asked, q = question(c, "native-b", c.other_agent, "fresh-b")
    c.now += timedelta(seconds=299)
    entries = collect(c)
    assert {entry["item"]["item_id"] for entry in entries} == {edge["pending_id"], b["pending_id"], q["question_id"]}
    assert all(parameters["since"] == c.now - timedelta(seconds=86700) for parameters, _limit in c.queries)
    assert sum(c.returned) == len(data_rows() - old_rows), "older rows are not sent back at all"
    assert len(old_rows) > 3 * sum(c.returned) and len(c.queries) == 1
    c.now += timedelta(seconds=2)
    assert {entry["item"]["item_id"] for entry in collect(c)} == {b["pending_id"], q["question_id"]}


def test_continuation_pages_keep_the_first_pages_window_while_time_passes(attention):
    c = attention
    for index in range(3):
        pending(c, "native-a", c.agent, f"rid-{index}")
    found, cursor, first = set(), None, None
    for _ in range(100):
        params = {"limit": 1, **({} if cursor is None else {"cursor": cursor})}
        value = c.client.get(PATH, params=params, headers=c.headers(read_only=True)).json()
        found.update(entry["item"]["item_id"] for entry in value["items"])
        first = first or c.queries[0][0]["since"]
        cursor = value["next_cursor"]
        if cursor is None:
            break
        c.now += timedelta(seconds=5)
    assert len(found) == 3 and len(c.queries) > 3
    assert {parameters["since"] for parameters, _limit in c.queries} == {first}


def test_a_cursor_from_before_the_window_resumes_its_unfiltered_scan(attention):
    c = attention
    _first, a = pending(c, "native-a", c.agent, "rid-a")
    c.now += timedelta(seconds=10)
    _command, b = pending(c, "native-b", c.other_agent, "rid-b")
    c.windows["0"] = None  # the old scan's own continuation, issued before any window existed
    found, cursor = set(), encoded_cursor(c.owner.team, {"PartitionKey": "unused", "RowKey": "0"})
    for _ in range(100):
        response = c.client.get(PATH, params={"cursor": cursor}, headers=c.headers(read_only=True))
        assert response.status_code == 200, response.text
        found.update(entry["item"]["item_id"] for entry in response.json()["items"])
        cursor = response.json()["next_cursor"]
        if cursor is None:
            break
    assert found == {a["pending_id"], b["pending_id"]}
    assert c.queries and all("since" not in parameters for parameters, _limit in c.queries)


@pytest.mark.parametrize("since", ["2026-10-02T09:00:00", "2026-10-02T17:00:00+08:00", "2026-10-02 09:00:00+00:00",
                                   "2099-01-01T00:00:00+00:00", 1, None])
def test_a_forged_discovery_window_is_refused_before_any_query(attention, since):
    c = attention
    forged = encoded_cursor(c.owner.team, {"PartitionKey": "unused", "RowKey": "0"}, since=since)
    response = c.client.get(PATH, params={"cursor": forged}, headers=c.headers(read_only=True))
    assert response.status_code == 422, response.text
    assert not c.queries


@pytest.mark.parametrize("since", [datetime(2026, 10, 2, 9), datetime(2026, 10, 2, 17, tzinfo=timezone(timedelta(hours=8))),
                                   "2026-10-02T09:00:00+00:00"])
def test_record_discovery_takes_only_a_utc_modification_bound(attention, since):
    c = attention
    with pytest.raises(Problem) as rejected:
        c.store.read_record_page(modified_since=since)
    assert rejected.value.body["error"]["code"] == "BEHAVIOR_INVALID_DATA"
    assert not c.queries


def test_expiry_during_source_read_does_not_extend_the_pending_window(attention, monkeypatch):
    from shared_context.native_hitl_wire import NativeHitlAttentionOrigin

    c = attention
    _command, saved = pending(c, "native-a", c.agent, "rid-a")
    original = copy.deepcopy(c.table.backing.rows)

    def source(_call):
        c.now += timedelta(seconds=601)
        return NativeHitlAttentionOrigin()

    monkeypatch.setattr("shared_context.native_hitl_api.owner_origin_reader", lambda *_: source)
    assert collect(c) == []
    assert c.table.backing.rows == original
    assert read(c, saved["pending_id"]).json()["expired"] is True


def test_real_table_sdk_reads_one_page_and_preserves_its_continuation():
    import base64
    from urllib.parse import parse_qs, urlsplit
    from azure.data.tables import TableClient
    from shared_context.behavior_store import BehaviorStore
    from test_behavior_store import Credential, SCOPE, URL, WireTransport, changes, json_response, request, write

    class QueryTransport(WireTransport):
        def send(self, outgoing, **options):
            query = parse_qs(urlsplit(outgoing.url).query)
            if outgoing.method != "GET" or "$filter" not in query:
                return super().send(outgoing, **options)
            self.requests.append(outgoing)
            assert query["$filter"] == [
                "PartitionKey eq 'control-one' and RowKey ge 'behavior:v1:row:' and RowKey lt 'behavior:v1:row;'"]
            assert set(query["$select"][0].split(",")) == {"PartitionKey", "RowKey", "Payload"}
            limit = int(query["$top"][0])
            after = query.get("NextRowKey", [""])[0]
            rows = sorted(
                (entity for (_table, partition, key), entity in self.backing.rows.items()
                 if partition == SCOPE.partition_key and key.startswith("behavior:v1:row:") and key >= after),
                key=lambda entity: entity["RowKey"],
            )
            values = [
                {**entity, "Payload": base64.b64encode(entity["Payload"]).decode("ascii"),
                 "Payload@odata.type": "Edm.Binary", "odata.etag": entity.metadata["etag"]}
                for entity in rows[:limit]
            ]
            headers = ({"x-ms-continuation-NextPartitionKey": SCOPE.partition_key,
                        "x-ms-continuation-NextRowKey": rows[limit]["RowKey"]} if len(rows) > limit else {})
            return json_response(outgoing, 200, {"value": values}, headers)

    transport = QueryTransport()
    client = TableClient(URL, SCOPE.table_name, credential=Credential(), transport=transport)
    try:
        store = BehaviorStore(client, SCOPE)
        store.initialize()
        store.transact(request("seed-page"), lambda _: changes(*(write(str(n), {"value": n}) for n in range(3))))
        saved = copy.deepcopy(transport.backing.rows)
        first, continuation = store.read_record_page(limit=2)
        assert [key for key, _row in first] == ["0", "1"] and continuation
        second, end = store.read_record_page(limit=2, continuation=continuation)
        assert [key for key, _row in second] == ["2"] and end is None
        assert len([item for item in transport.requests if "$filter" in item.url]) == 2
        assert transport.backing.rows == saved
    finally:
        client.close()


def test_real_table_sdk_sends_the_timestamp_window_unchanged_on_every_page():
    from urllib.parse import parse_qs, urlsplit
    from azure.data.tables import TableClient
    from shared_context.behavior_store import BehaviorStore
    from test_behavior_store import Credential, SCOPE, URL, WireTransport, changes, json_response, request, write

    filters = []

    class QueryTransport(WireTransport):
        def send(self, outgoing, **options):
            query = parse_qs(urlsplit(outgoing.url).query)
            if outgoing.method != "GET" or "$filter" not in query:
                return super().send(outgoing, **options)
            filters.append((query["$filter"], query.get("NextRowKey")))
            if len(filters) == 1:
                return json_response(outgoing, 200, {"value": []}, {
                    "x-ms-continuation-NextPartitionKey": SCOPE.partition_key,
                    "x-ms-continuation-NextRowKey": "behavior:v1:row:next"})
            return json_response(outgoing, 200, {"value": []})

    transport = QueryTransport()
    client = TableClient(URL, SCOPE.table_name, credential=Credential(), transport=transport)
    try:
        store = BehaviorStore(client, SCOPE)
        store.initialize()
        store.transact(request("seed-window"), lambda _: changes(write("one", {"value": 1})))
        since = datetime(2026, 10, 2, 8, 45, tzinfo=timezone.utc)
        empty, continuation = store.read_record_page(modified_since=since)
        assert empty == () and continuation
        assert store.read_record_page(modified_since=since, continuation=continuation) == ((), None)
        expected = ["PartitionKey eq 'control-one' and RowKey ge 'behavior:v1:row:' and RowKey lt 'behavior:v1:row;'"
                    " and Timestamp ge datetime'2026-10-02T08:45:00.000000Z'"]
        assert filters == [(expected, None), (expected, ["behavior:v1:row:next"])]
    finally:
        client.close()
