"""OFFLINE signed activity bootstrap; metadata and operator JSON never mint routes."""
import json

import httpx
import pytest
from azure.core.credentials import AccessToken
from fastapi import FastAPI
from fastapi.testclient import TestClient

from shared_context.teams_personal import PersonalRouteEnrollment, PersonalTeamsEntry, TeamsParentSeams, UamiBotToken, ROUTE_HANDSHAKE, compose_personal_channel
from shared_context.teams_identity import BOT_SCOPE
from shared_context.errors import Problem
from shared_context.teams_delivery import TeamsSendContext
from test_behavior_store import FakeTable, SCOPE
from test_behavior_delivery import CONTEXT
from shared_context.behavior_store import BehaviorStore
from test_teams_deployment import config
from test_teams_identity import NOW, ROUTE, activity, identity, token
from test_teams_runtime import runtime_fixture


def enrollment_fixture():
    store = BehaviorStore(FakeTable(), SCOPE)
    store.initialize()
    enrollment = PersonalRouteEnrollment(
        store, config(), clock=lambda: NOW, jwks_transport=identity().jwks_transport,
    )
    return store, enrollment


def test_signed_designated_personal_handshake_establishes_route_not_work_or_native_readiness():
    store, enrollment = enrollment_fixture()
    body = json.dumps(activity(text=ROUTE_HANDSHAKE)).encode()
    result = enrollment.capture("Bearer " + token(), body)
    assert result["status"] == "route_recorded_not_admitted"
    assert result["native_gate"] == "closed"
    assert result["work_admitted"] is False
    assert result["evidence_mode"] == "offline_contract"
    settings = enrollment.settings(require_live=False)
    assert settings.routes == (ROUTE,)
    assert enrollment.capture("Bearer " + token(), body) == result
    restarted = PersonalRouteEnrollment(
        store, config(), clock=lambda: NOW, jwks_transport=identity().jwks_transport,
    )
    assert restarted.settings(require_live=False) == settings


@pytest.mark.parametrize("mirror", ["<p>FA Teams route enrollment</p>", "<div><div>FA Teams route enrollment</div>\n</div>"])
def test_documented_client_info_and_exact_html_mirror_do_not_reject_the_control_handshake(mirror):
    import hashlib
    _, enrollment = enrollment_fixture()
    body = json.dumps(activity(
        text=ROUTE_HANDSHAKE,
        attachments=[{"contentType": "text/html", "content": mirror}],
        entities=[{"type": "clientInfo", "locale": "zh-TW", "country": "TW", "platform": "Web",
                   "timezone": "Asia/Taipei"}],
    )).encode()
    result = enrollment.capture("Bearer " + token(), body)
    assert result["work_admitted"] is False
    assert enrollment._witness().body_digest == hashlib.sha256(body).hexdigest()


@pytest.mark.parametrize("attachment", [
    {"contentType": "text/html", "content": "<p>A different instruction</p>"},
    {"contentType": "text/html", "content": "<img src='https://invalid.test/pixel'>FA Teams route enrollment"},
    {"contentType": "text/html", "content": "<p onclick='run()'>FA Teams route enrollment</p>"},
    {"contentType": "text/html", "contentUrl": "https://invalid.test/content"},
    {"contentType": "application/json", "content": {"approval": True}},
])
def test_setup_metadata_cannot_add_content_links_or_new_commands(attachment):
    _, enrollment = enrollment_fixture()
    body = json.dumps(activity(text=ROUTE_HANDSHAKE, attachments=[attachment])).encode()
    with pytest.raises(Problem):
        enrollment.capture("Bearer " + token(), body)
    with pytest.raises(Problem):
        enrollment.settings(require_live=False)


def test_parent_composition_keeps_capture_and_real_work_resolvers_separate():
    h, previous, _ = runtime_fixture()
    enrollment = PersonalRouteEnrollment(
        h.store, config(team_id="team"), clock=lambda: NOW, jwks_transport=identity().jwks_transport,
    )
    entry = PersonalTeamsEntry(enrollment)
    app = FastAPI()
    app.include_router(entry.router())
    client = TestClient(app)
    blocked = client.post("/behavior/v1/teams/activities", json=activity(),
                          headers={"Authorization": "Bearer " + token()})
    assert blocked.status_code == 403
    capture = client.post("/behavior/v1/teams/activities", json=activity(text=ROUTE_HANDSHAKE),
                          headers={"Authorization": "Bearer " + token()})
    assert capture.json()["work_admitted"] is False
    processed = []
    def work(obligation, context):
        processed.append((obligation, context))
        h.work_result(obligation)
    composed = compose_personal_channel(
        enrollment, h.registration, TeamsParentSeams(
            resolve_admission=previous.resolve_admission, resolve_context=lambda _: CONTEXT,
            process_work=work, resolve_send=lambda _: TeamsSendContext(context=CONTEXT, grant_id="not-approved"),
        ),
        transport=httpx.MockTransport(lambda _: httpx.Response(503)),
        bot_token=lambda _: "unused-offline-credential",
    )
    h.core = composed.core
    entry.attach_runtime(composed.runtime)
    assert composed.runtime.admission_enabled is False
    composed.runtime.admission_enabled = True  # OFFLINE readiness fixture only.
    accepted = client.post("/behavior/v1/teams/activities", json=activity(id="business-2"),
                           headers={"Authorization": "Bearer " + token()})
    assert accepted.status_code == 200
    obligation = accepted.json()["obligation_id"]
    prepared = composed.runtime.prepare(obligation)
    assert prepared["binding"]["route"]["thread_id"] == ROUTE.route_id
    assert processed == [(obligation, CONTEXT)]


def test_metadata_or_fabricated_operator_route_cannot_create_settings():
    _, enrollment = enrollment_fixture()
    with pytest.raises(Problem) as error:
        enrollment.settings()
    assert error.value.body["error"]["code"] == "TEAMS_PERSONAL_ROUTE_REQUIRED"


@pytest.mark.parametrize("updates", [
    {"from": {"id": ROUTE.user_id, "aadObjectId": "other-user"}},
    {"channelData": {"tenant": {"id": "other-tenant"}}},
    {"recipient": {"id": "other-bot"}},
    {"conversation": {"id": ROUTE.conversation_id, "conversationType": "groupChat"}},
    {"conversation": {"id": ROUTE.conversation_id, "conversationType": "channel"}},
    {"replyToId": "arbitrary-thread"},
    {"text": "Please perform real work now"},
])
def test_enrollment_is_only_for_the_designated_personal_control_handshake(updates):
    _, enrollment = enrollment_fixture()
    with pytest.raises(Problem):
        enrollment.capture("Bearer " + token(), json.dumps(activity(text=ROUTE_HANDSHAKE) | updates).encode())
    with pytest.raises(Problem):
        enrollment.settings(require_live=False)


@pytest.mark.parametrize("bearer", ["", "Bearer invalid", "Bearer " + token(aud="wrong-app"),
                                    "Bearer " + token(exp=int(NOW.timestamp()))])
def test_unsigned_or_invalid_connector_activity_never_establishes_a_route(bearer):
    _, enrollment = enrollment_fixture()
    with pytest.raises(Problem):
        enrollment.capture(bearer, json.dumps(activity(text=ROUTE_HANDSHAKE)).encode())
    with pytest.raises(Problem):
        enrollment.settings(require_live=False)


def test_offline_route_evidence_cannot_be_promoted_by_reconstructing_native_dependencies():
    store, enrollment = enrollment_fixture()
    enrollment.capture("Bearer " + token(), json.dumps(activity(text=ROUTE_HANDSHAKE)).encode())
    reconstructed = PersonalRouteEnrollment(store, config(), clock=lambda: NOW)
    with pytest.raises(Problem) as error:
        reconstructed.settings(require_live=True)
    assert error.value.body["error"]["code"] == "TEAMS_PERSONAL_LIVE_ROUTE_REQUIRED"


def test_same_provider_id_changed_body_and_changed_conversation_do_not_replace_enrollment():
    _, enrollment = enrollment_fixture()
    enrollment.capture("Bearer " + token(), json.dumps(activity(text=ROUTE_HANDSHAKE)).encode())
    for update in ({"extra": "changed bytes"}, {
        "id": "different-event", "conversation": {"id": "other-conversation", "conversationType": "personal"},
    }):
        with pytest.raises(Problem):
            enrollment.capture("Bearer " + token(), json.dumps(activity(text=ROUTE_HANDSHAKE) | update).encode())
    assert enrollment.settings(require_live=False).routes == (ROUTE,)


def test_uami_bot_credential_is_lazy_scope_bound_and_never_uses_user_token_fallback():
    class Credential:
        def __init__(self):
            self.scopes = []
        def get_token(self, *scopes):
            self.scopes.append(scopes)
            return AccessToken("SYNTHETIC-BOT-TOKEN", int(NOW.timestamp()) + 300)
    credential = Credential()
    dependency = UamiBotToken(config(), clock=lambda: NOW, credential=credential)
    assert credential.scopes == []
    with pytest.raises(Problem):
        dependency("https://graph.microsoft.com/.default")
    assert credential.scopes == []
    assert dependency(BOT_SCOPE) == "SYNTHETIC-BOT-TOKEN"
    assert credential.scopes == [("https://api.botframework.com/.default",)]


def test_credential_failure_and_expiry_do_not_expose_observed_values(capsys):
    class Credential:
        def get_token(self, *_):
            raise RuntimeError("SYNTHETIC-SECRET-DO-NOT-LOG")
    dependency = UamiBotToken(config(), clock=lambda: NOW, credential=Credential())
    with pytest.raises(Problem) as error:
        dependency(BOT_SCOPE)
    assert "SYNTHETIC-SECRET-DO-NOT-LOG" not in str(error.value.body)
    assert capsys.readouterr().out == ""
    class ExpiredCredential:
        def get_token(self, *_):
            return AccessToken("SYNTHETIC-EXPIRED-SECRET", int(NOW.timestamp()))
    with pytest.raises(Problem) as error:
        UamiBotToken(config(), clock=lambda: NOW, credential=ExpiredCredential())(BOT_SCOPE)
    assert "SYNTHETIC-EXPIRED-SECRET" not in str(error.value.body)


def test_managed_identity_constructor_uses_client_id_not_principal_or_default_chain(monkeypatch):
    import shared_context.teams_personal as personal
    constructed = []
    class Credential:
        def __init__(self, **kwargs):
            constructed.append(kwargs)
        def get_token(self, *_):
            raise AssertionError("construction must not acquire any token")
    monkeypatch.setattr(personal, "ManagedIdentityCredential", Credential)
    UamiBotToken(config(), clock=lambda: NOW)
    assert constructed == [{"client_id": "22222222-2222-2222-2222-222222222222", "logging_enable": False}]
