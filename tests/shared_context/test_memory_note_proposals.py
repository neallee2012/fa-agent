"""Human-selected saved-turn fragments become inactive proposals, not effective memory."""

import hashlib
import importlib
import json
from types import SimpleNamespace

import pytest

from test_behavior_assistant import assistant
from test_instruction_flow import instruction_models, runtime_path
from test_m3_auth import jwk_client, signing_key
from test_sdk_compaction_product import configured, open_turn, ordinary, settle


@pytest.fixture
def proposal_case(assistant):
    h, control, human = configured(assistant)
    turn = open_turn(h, control, human, "note-proposal-source")
    settle(turn, ordinary(turn.assignment.sdk_session_id))
    try:
        yield turn
    finally:
        turn.client.close()


def install_case(case):
    module = importlib.import_module("shared_context.memory_note_proposal_api")
    module.install_note_proposal_routes(case.h.app, case.h.runtime, case.h.verifier)
    text = case.assignment.task.instruction.content
    raw = text.encode("utf-8")
    selected = b"original inventory"
    start = raw.index(selected)
    command = {
        "schema_version": "fa-note-proposal-command-v1",
        "request_id": "human-note-proposal",
        "conversation_id": case.assignment.task.instruction.conversation_id,
        "turn_number": 1,
        "speaker": "user",
        "source_sha256": hashlib.sha256(raw).hexdigest(),
        "start_byte": start,
        "end_byte": start + len(selected),
        "share_team_id": case.binding.team_id,
        "share_work_id": case.binding.work.work_id,
        "share_confirmed": True,
    }
    return "/behavior/v1/bindings/" + case.binding.binding_id + "/note-proposals", command


def test_explicit_fragment_proposal_replays_one_receipt_and_stays_inactive(proposal_case):
    c = proposal_case
    path, command = install_case(c)
    claim_path = "/behavior/v1/bindings/" + c.binding.binding_id + "/claims/" + c.binding.claim_ids[0]
    prior = c.h.client.get(claim_path, headers=c.h.headers())
    assert prior.status_code == 200
    before_claims = prior.json()
    response = c.h.client.post(path, json=command, headers=c.h.headers("read-only"))
    assert response.status_code == 200, response.text
    assert response.json()["result"]["state"] == "pending_review"
    assert response.json()["result"]["effective"] is False
    guard = c.h.store._guard()
    assert c.h.client.post(path, json=command, headers=c.h.headers("read-only")).json() == response.json()
    assert c.h.client.post(path + "/reconcile", json=command, headers=c.h.headers("read-only")).json() == response.json()
    assert c.h.store._guard() == guard
    viewed = c.h.client.get(path + "/" + command["request_id"], headers=c.h.headers("read-only"))
    assert viewed.status_code == 200
    value = viewed.json()
    assert value["text"] == "original inventory"
    assert value["state"] == "pending_review" and value["effective"] is False
    assert value["bridge_available"] is False
    assert value["source"]["speaker"] == "user"
    assert value["source"]["original_request_id"] == "note-proposal-source"
    assert value["source"]["source_sha256"] == command["source_sha256"]
    assert "Use the original inventory source." not in json.dumps(value)
    assert c.h.client.get(claim_path, headers=c.h.headers()).json() == before_claims
    assert c.h.endpoint.requests == []


@pytest.mark.parametrize("change,status", [
    ({"share_confirmed": False}, 422),
    ({"share_confirmed": 1}, 422),
    ({"start_byte": True}, 422),
    ({"end_byte": 9000}, 422),
    ({"share_team_id": "other-team"}, 403),
    ({"share_work_id": "other-work"}, 403),
    ({"source_sha256": "0" * 64}, 409),
    ({"text": "caller-supplied replacement"}, 422),
])
def test_proposal_rejects_unconfirmed_or_mismatched_selection_without_writes(proposal_case, change, status):
    c = proposal_case
    path, command = install_case(c)
    guard = c.h.store._guard()
    response = c.h.client.post(path, json={**command, **change}, headers=c.h.headers("read-only"))
    assert response.status_code == status
    assert c.h.store._guard() == guard and c.h.endpoint.requests == []


def test_original_request_cannot_be_reused_for_a_different_fragment(proposal_case):
    c = proposal_case
    path, command = install_case(c)
    assert c.h.client.post(path, json=command, headers=c.h.headers("read-only")).status_code == 200
    guard = c.h.store._guard()
    changed = {**command, "end_byte": command["end_byte"] - 1}
    for suffix in ("", "/reconcile"):
        result = c.h.client.post(path + suffix, json=changed, headers=c.h.headers("read-only"))
        assert result.status_code == 409
    assert c.h.store._guard() == guard


@pytest.mark.parametrize("identity", ["agent", "old-m3"])
def test_only_original_human_read_can_create_or_inspect_proposals(proposal_case, identity):
    c = proposal_case
    path, command = install_case(c)
    guard = c.h.store._guard()
    assert c.h.client.post(path, json=command, headers=c.h.headers(identity)).status_code == 403
    assert c.h.client.get(path + "/" + command["request_id"], headers=c.h.headers(identity)).status_code == 403
    assert c.h.store._guard() == guard


def test_proposal_ack_loss_reads_original_receipt_without_another_transaction(proposal_case, monkeypatch):
    from shared_context.memory_note_proposals import proposal_key
    from shared_context.behavior_operations import control_key

    c = proposal_case
    path, command = install_case(c)
    key = proposal_key(c.h.store._scope.authority_domain, c.human.oid, command["request_id"])
    request_id = control_key("memory-note-proposal-write", key)
    original, lost = c.h.store._submit, []

    def lose(operations, request):
        if request is not None and request.request_id == request_id and not lost:
            lost.append(True)
            c.h.transport.after_apply = "lost"
        try:
            return original(operations, request)
        finally:
            c.h.transport.after_apply = None

    monkeypatch.setattr(c.h.store, "_submit", lose)
    response = c.h.client.post(path, json=command, headers=c.h.headers("read-only"))
    assert response.status_code == 503 and lost == [True]
    guard = c.h.store._guard()

    def forbidden(*args, **kwargs):
        pytest.fail("original receipt lookup must not transact")

    monkeypatch.setattr(c.h.store, "transact", forbidden)
    readback = c.h.client.post(path + "/reconcile", json=command, headers=c.h.headers("read-only"))
    assert readback.status_code == 200
    assert readback.json()["result"]["proposal_id"] == key
    assert c.h.store._guard() == guard


def test_missing_receipt_stays_unknown_and_does_not_create_a_proposal(proposal_case, monkeypatch):
    c = proposal_case
    path, command = install_case(c)
    guard = c.h.store._guard()
    monkeypatch.setattr(c.h.store, "transact", lambda *_: pytest.fail("lookup cannot write"))
    response = c.h.client.post(path + "/reconcile", json=command, headers=c.h.headers("read-only"))
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "BEHAVIOR_COMMIT_UNKNOWN"
    assert response.json()["error"]["reconcile_original"] is True
    assert response.json()["error"]["automatic_retry"] is False
    assert c.h.store._guard() == guard


def test_public_assistant_reply_is_a_separate_non_authorizing_source(proposal_case):
    c = proposal_case
    path, command = install_case(c)
    text = c.control.read_reply(c.human, command["conversation_id"], 1).text.encode("utf-8")
    selected = b"inventory"
    start = text.index(selected)
    command.update(speaker="assistant", source_sha256=hashlib.sha256(text).hexdigest(),
                   start_byte=start, end_byte=start + len(selected))
    assert c.h.client.post(path, json=command, headers=c.h.headers("read-only")).status_code == 200
    result = c.h.client.get(path + "/" + command["request_id"], headers=c.h.headers("read-only")).json()
    assert result["text"] == "inventory" and result["source"]["speaker"] == "assistant"
    assert result["effective"] is False and result["bridge_available"] is False


def test_unsaved_reply_cannot_supply_a_memory_proposal(assistant):
    h, control, human = configured(assistant)
    turn = open_turn(h, control, human, "unfinished-note-source")
    try:
        path, command = install_case(turn)
        guard = h.store._guard()
        response = h.client.post(path, json=command, headers=h.headers("read-only"))
        assert response.status_code == 404
        assert h.store._guard() == guard
    finally:
        turn.client.close()


def test_exact_utf8_boundaries_are_kept_and_mid_character_selection_is_rejected(assistant):
    h, control, human = configured(assistant)
    turn = open_turn(h, control, human, "unicode-note-source")
    try:
        text = "前😀後"
        state = turn.client.save_sdk_state(ordinary(turn.assignment.sdk_session_id), turn.stop)
        result = turn.client.save_text_reply(text, state, turn.stop)
        h.executor._complete(turn.start, result.model_dump(mode="json"))
        assert h.runtime.controller.step(turn.binding.binding_id).state == "succeeded"
        path, command = install_case(turn)
        command.update(speaker="assistant", source_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                       start_byte=3, end_byte=7)
        guard = h.store._guard()
        invalid = h.client.post(path, json={**command, "request_id": "mid-character", "start_byte": 4},
                                headers=h.headers("read-only"))
        assert invalid.status_code == 422 and h.store._guard() == guard
        assert h.client.post(path, json=command, headers=h.headers("read-only")).status_code == 200
        value = h.client.get(path + "/" + command["request_id"], headers=h.headers("read-only")).json()
        assert value["text"] == "😀" and value["source"]["end_byte"] - value["source"]["start_byte"] == 4
    finally:
        turn.client.close()


def test_production_application_registers_proposal_routes_without_other_new_capabilities(proposal_case):
    from shared_context.behavior_server import application

    c = proposal_case
    composition = SimpleNamespace(
        runtime=c.h.runtime, verifier=c.h.verifier, assistant=None, teams=None,
        installation=lambda: None, catalog=SimpleNamespace(require_activation=lambda: None),
        registered_actor=None, close=lambda: None, agent_id=None,
        instruction_ready=lambda *_: pytest.fail("route construction cannot wake work"),
        launch_instruction=lambda *_: pytest.fail("route construction cannot launch work"),
    )
    app = application(composition)
    paths = {route.path for route in app.routes}
    prefix = "/behavior/v1/bindings/{binding_id}/note-proposals"
    assert {prefix, prefix + "/preview", prefix + "/reconcile", prefix + "/{request_id}"} <= paths
    assert prefix + "/commit" not in paths and prefix + "/activate" not in paths
    assert prefix + "/{proposal_request_id}/bridge/commit" in paths


def test_both_note_families_keep_route_order_and_request_errors(proposal_case, monkeypatch):
    from shared_context import memory_note_proposal_api as api

    c = proposal_case
    install_case(c)
    raised = []

    class Recorded(api.Problem):
        def __init__(self, *args):
            super().__init__(*args)
            raised.append(self.body["error"])

    monkeypatch.setattr(api, "Problem", Recorded)
    expected = []
    for segment in ("note-proposals", "note-syntheses"):
        prefix = "/behavior/v1/bindings/{binding_id}/" + segment
        expected += [(prefix + path, {"POST"}) for path in ("/preview", "/reconcile", "")]
        expected += [(prefix + "/{proposal_request_id}/bridge/" + stage, {"POST"})
                     for stage in ("prepare", "preview", "review", "commit", "reconcile")]
        expected.append((prefix + "/{request_id}", {"GET"}))
    routes = [(route.path, route.methods) for route in c.h.app.routes
              if "/note-proposals" in route.path or "/note-syntheses" in route.path]
    assert routes == expected
    for segment, code, noun in (("note-proposals", "NOTE_PROPOSAL_INVALID_REQUEST", "proposal"),
                                ("note-syntheses", "NOTE_SYNTHESIS_INVALID_REQUEST", "synthesis")):
        base = "/behavior/v1/bindings/" + c.binding.binding_id + "/" + segment
        identifiers = {"code": code, "message": "only the original identifiers are supported"}
        for method, path in (("POST", "/preview?x=1"), ("POST", "/request-a/bridge/review?x=1"),
                             ("GET", "/request-a?x=1")):
            raised.clear()
            response = c.h.client.request(method, base + path, headers=c.h.headers())
            assert (response.status_code, raised) == (422, [identifiers]), (method, path)
        raised.clear()
        response = c.h.client.request("GET", base + "/request-a", content=b"{}", headers=c.h.headers())
        assert (response.status_code, raised) == (
            422, [{"code": code, "message": noun + " reads do not accept a body"}])
