"""OFFLINE authenticated HTTP -> real admission ledger; no models or Azure."""
import json
from dataclasses import replace
from datetime import timedelta

import pytest
import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from shared_context.behavior_delivery import ChannelContract, DeliveryCore, WorkDefinition
from shared_context.errors import Problem
from shared_context.teams_identity import TEAMS_VERSION
from shared_context.teams_delivery import TeamsDeliveryAdapter, TeamsSendContext
from shared_context.teams_runtime import TeamsAdmission, TeamsRuntime, teams_router
from test_behavior_delivery import CONTEXT, Harness
from test_teams_identity import HUMAN, NOW, ROUTE, activity, identity, token


def runtime_fixture(*, route=ROUTE):
    h = Harness(channel=ChannelContract(
        channel_id="msteams", version=TEAMS_VERSION,
        supported_levels=("provider_accepted",), max_confirmation_level="provider_accepted",
    ))
    h.time = NOW
    auth = identity()
    auth.settings = auth.settings.model_copy(update={"team_id": "team", "routes": (route,)})
    bridge = TeamsRuntime(
        h.store, auth, clock=lambda: h.time,
        resolve_admission=lambda incoming: TeamsAdmission(
            context=CONTEXT, root_request_id="root-request",
            work=WorkDefinition(work_id="work-" + incoming.event_id[:32], goal="Answer the question",
                                criteria_version="answer-v1", result_kind="answer",
                                required_checks=("answer-checked",)),
        ),
    )
    proof = h.readiness()
    adapter = TeamsDeliveryAdapter(
        h.store, auth.settings, clock=lambda: h.time,
        resolve_send=lambda _: TeamsSendContext(context=CONTEXT, grant_id="unapproved"),
        bot_token=lambda _: "synthetic-credential",
        transport=httpx.MockTransport(lambda _: httpx.Response(503)),
    )
    h.registration = replace(h.registration, verify_ingress=bridge.verify_ingress,
                             channel_adapter=adapter,
                             verify_readiness=lambda: proof.model_copy(update={
                                 "observed_at": NOW, "expires_at": NOW + timedelta(hours=2)}))
    h.core = h.broker(h.store)
    bridge.attach(h.core)
    adapter.attach(h.core)
    # Current server owner is a worker, not the asserted Teams user.
    h.mutate("policy", "policy", revision=1)
    bridge.admission_enabled = True
    app = FastAPI()
    app.include_router(teams_router(bridge))
    return h, bridge, TestClient(app)


def test_http_incoming_commits_existing_admission_and_replays_same_provider_id():
    h, bridge, client = runtime_fixture()
    response = client.post("/behavior/v1/teams/activities", json=activity(),
                           headers={"Authorization": "Bearer " + token()})
    assert response.status_code == 200, response.text
    admitted = response.json()
    assert admitted["status"] == "queued"
    assert admitted["actual_environment"] == "NOT_RUN"
    assert bridge.inspect(admitted["obligation_id"])["fulfillment"] == "pending"
    assert bridge.route(ROUTE.route_id).conversation_id == ROUTE.conversation_id
    replay = client.post("/behavior/v1/teams/activities", json=activity(),
                         headers={"Authorization": "Bearer " + token()})
    assert replay.json() == admitted
    changed = client.post("/behavior/v1/teams/activities", json=activity(text="Different request"),
                          headers={"Authorization": "Bearer " + token()})
    assert changed.status_code == 409
    assert changed.json()["error"]["code"] == "TEAMS_SOURCE_CONFLICT"


def test_teams_identity_proofs_do_not_enable_admission_without_concrete_adapter():
    h, bridge, client = runtime_fixture()
    h.registration = replace(h.registration, channel_adapter=None)
    h.core = h.broker(h.store)
    bridge.attach(h.core)
    response = client.post("/behavior/v1/teams/activities", json=activity(),
                           headers={"Authorization": "Bearer " + token()})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "TEAMS_REGISTRATION_DENIED"


@pytest.mark.parametrize("kind", ["messageReaction", "typing", "conversationUpdate", "event"])
def test_transport_activity_never_creates_work_even_with_another_pending_obligation(kind):
    h, bridge, client = runtime_fixture()
    first = client.post("/behavior/v1/teams/activities", json=activity(),
                        headers={"Authorization": "Bearer " + token()}).json()
    quiet = client.post("/behavior/v1/teams/activities", json=activity(id="receipt-event", type=kind),
                        headers={"Authorization": "Bearer " + token()})
    assert quiet.json()["status"] == "silence"
    assert bridge.inspect(first["obligation_id"])["fulfillment"] == "pending"


@pytest.mark.parametrize("content", [
    {"type": "invoke", "value": {"action": "approve"}},
    {"value": {"grant_id": "model-selected", "approved": True}},
    {"attachments": [{"contentType": "application/octet-stream", "content": "unreviewed"}]},
])
def test_teams_cards_and_unreviewed_content_cannot_supply_approval(content):
    _, _, client = runtime_fixture()
    response = client.post("/behavior/v1/teams/activities", json=activity(**content),
                           headers={"Authorization": "Bearer " + token(scp="Behavior.Approve")})
    assert response.status_code == 422


def test_known_credentials_never_reach_persisted_incoming_or_problem_output():
    h, _, client = runtime_fixture()
    secret = "SYNTHETIC-SECRET-DO-NOT-RETAIN"
    response = client.post("/behavior/v1/teams/activities", json=activity(text="client_secret=" + secret),
                           headers={"Authorization": "Bearer " + token()})
    assert response.status_code == 422
    assert secret not in response.text
    assert all(secret.encode() not in op[1].get("Payload", b"") for batch in h.client.submissions for op in batch)


def test_body_limit_and_duplicate_json_fields_are_rejected_before_admission():
    _, _, client = runtime_fixture()
    for body, status in ((b"x" * 32769, 413), (b'{"id":"first","id":"second"}', 422)):
        response = client.post("/behavior/v1/teams/activities", content=body,
                               headers={"Authorization": "Bearer " + token(), "Content-Type": "application/json"})
        assert response.status_code == status
