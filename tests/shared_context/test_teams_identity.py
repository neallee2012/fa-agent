"""OFFLINE: real RS256 validation and HTTP transport, never Teams native proof."""
from datetime import datetime, timedelta, timezone

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from shared_context.errors import Problem
from shared_context.teams_identity import ConnectorIdentity, PublicCloudTransport, TeamsRouteRecord, TeamsSettings

NOW = datetime(2026, 9, 16, 14, tzinfo=timezone.utc)
TENANT = "00000000-0000-0000-0000-000000000013"
BOT = "22222222-2222-2222-2222-222222222222"
HUMAN = "00000000-0000-0000-0000-00000000001b"
SERVICE = "https://smba.trafficmanager.net/amer/"
CONVERSATION = "19:" + "opaque-conversation-" * 20 + "@thread" + ".v2"
ROUTE = TeamsRouteRecord(
    tenant_id=TENANT, bot_app_id=BOT, bot_id="28:" + BOT,
    user_id="29:opaque-teams-user", aad_object_id=HUMAN,
    conversation_id=CONVERSATION, conversation_type="personal",
    service_url=SERVICE,
)
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def activity(**updates):
    return {
        "type": "message", "id": "provider-incoming-1", "channelId": "msteams",
        "serviceUrl": SERVICE,
        "from": {"id": ROUTE.user_id, "aadObjectId": HUMAN},
        "recipient": {"id": ROUTE.bot_id},
        "conversation": {"id": CONVERSATION, "conversationType": "personal", "tenantId": TENANT},
        "channelData": {"tenant": {"id": TENANT}}, "text": "Please answer the bounded question.",
        **updates,
    }


def token(**updates):
    return jwt.encode({
        "iss": "https://api.botframework.com", "aud": BOT,
        "nbf": int((NOW - timedelta(minutes=1)).timestamp()),
        "exp": int((NOW + timedelta(minutes=5)).timestamp()),
        "serviceurl": SERVICE, **updates,
    }, KEY, algorithm="RS256", headers={"kid": "offline-key"})


def identity():
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(KEY.public_key(), as_dict=True)
    jwk.update(kid="offline-key", use="sig", alg="RS256", endorsements=["msteams"])
    def provider(request):
        assert str(request.url) == "https://login.botframework.com/v1/.well-known/keys"
        return httpx.Response(200, json={"keys": [jwk]})
    return ConnectorIdentity(
        TeamsSettings(tenant_id=TENANT, bot_app_id=BOT, team_id="team-one", routes=(ROUTE,)),
        clock=lambda: NOW, jwks_transport=httpx.MockTransport(provider),
    )


def test_authenticated_connector_preserves_opaque_route_without_delegated_approval():
    verified = identity().verify("Bearer " + token(), activity())
    assert verified.route.conversation_id == CONVERSATION
    assert verified.route.service_url == SERVICE
    assert verified.principal.oid == HUMAN
    assert verified.principal.actions == frozenset({"submit"})
    try:
        verified.principal.require("team-one", "approve")
    except Problem as error:
        assert error.body["error"]["code"] == "BEHAVIOR_ACCESS_DENIED"
    else:
        raise AssertionError("a Connector service token granted delegated approval")


@pytest.mark.parametrize("padding,accepted", [(771860, True), (2 * 1024 * 1024, False)])
def test_current_sized_signing_catalog_is_supported_but_remains_bounded(padding, accepted):
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(KEY.public_key(), as_dict=True)
    jwk.update(kid="offline-key", use="sig", alg="RS256", endorsements=["msteams"])
    auth = ConnectorIdentity(
        identity().settings, clock=lambda: NOW,
        jwks_transport=httpx.MockTransport(lambda _: httpx.Response(
            200, json={"keys": [jwk], "publisher_metadata": "x" * padding},
        )),
    )
    if accepted:
        verified = auth.verify("Bearer " + token(), activity())
        assert verified.principal.actions == frozenset({"submit"})
    else:
        with pytest.raises(Problem):
            auth.verify("Bearer " + token(), activity())


@pytest.mark.parametrize("claims", [
    {"aud": "api://wrong-app"}, {"aud": [BOT]}, {"iss": "https://evil.invalid"},
    {"exp": int(NOW.timestamp())}, {"nbf": int(NOW.timestamp()) + 1},
    {"exp": "9999999999"}, {"serviceurl": SERVICE + "other/"},
    {"serviceurl": None}, {"iat": int(NOW.timestamp()) + 10},
])
def test_invalid_service_jwt_cannot_authenticate(claims):
    with pytest.raises(Problem) as error:
        identity().verify("Bearer " + token(**claims), activity())
    assert error.value.body["error"]["code"] == "TEAMS_SERVICE_AUTH_DENIED"


@pytest.mark.parametrize("header", ["", "Basic secret", "Bearer invalid.jwt.value"])
def test_missing_or_malformed_bearer_is_not_logged(header):
    with pytest.raises(Problem) as error:
        identity().verify(header, activity())
    assert header not in str(error.value.body) or not header


def test_jwt_signature_and_algorithms_are_not_chosen_by_the_sender():
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    claims = jwt.decode(token(), options={"verify_signature": False})
    wrong = jwt.encode(claims, other, algorithm="RS256", headers={"kid": "offline-key"})
    unsigned = jwt.encode(claims, "", algorithm="none", headers={"kid": "offline-key"})
    for value in (wrong, unsigned):
        with pytest.raises(Problem):
            identity().verify("Bearer " + value, activity())


def test_service_token_claiming_delegated_scopes_still_cannot_approve():
    verified = identity().verify("Bearer " + token(scp="Behavior.Read Behavior.Approve",
                                                 oid=HUMAN, tid=TENANT), activity())
    with pytest.raises(Problem):
        verified.principal.require("team-one", "approve")


@pytest.mark.parametrize("update", [
    {"channelId": "emulator"}, {"channelData": {"tenant": {"id": "other-tenant"}}},
    {"from": {"id": "29:other", "aadObjectId": HUMAN}},
    {"from": {"id": ROUTE.user_id, "aadObjectId": "other-oid"}},
    {"recipient": {"id": "other-bot"}}, {"replyToId": "unapproved-thread"},
    {"conversation": {"id": CONVERSATION, "conversationType": "personal", "tenantId": "wrong-tenant"}},
    {"conversation": {"id": CONVERSATION[:64], "conversationType": "personal"}},
])
def test_exact_tenant_actor_bot_conversation_and_thread_scope_are_required(update):
    with pytest.raises(Problem):
        identity().verify("Bearer " + token(), activity(**update))


@pytest.mark.parametrize("url", [
    "http://smba.trafficmanager.net/amer/", "https://127.0.0.1/amer/",
    "https://169.254.169.254/amer/", "https://[::1]/amer/",
    "https://smba.trafficmanager.net.evil.invalid/amer/",
    "https://smba.trafficmanager.net:8443/amer/",
    "https://user:secret@smba.trafficmanager.net/amer/",
    "https://smba.trafficmanager.net/amer/../", "https://smba.trafficmanager.net/amer/%2e%2e/",
    "https://smba.trafficmanager.net/amer/?callback=evil", "https://smba.trafficmanager.net/other/",
])
def test_authenticated_service_url_still_cannot_select_an_ssrf_destination(url):
    with pytest.raises(Problem):
        identity().verify("Bearer " + token(serviceurl=url), activity(serviceUrl=url))


def test_jwks_redirect_is_not_followed():
    seen = []
    def redirect(request):
        seen.append(str(request.url))
        return httpx.Response(302, headers={"Location": "https://127.0.0.1/secret"})
    auth = ConnectorIdentity(identity().settings, clock=lambda: NOW,
                             jwks_transport=httpx.MockTransport(redirect))
    with pytest.raises(Problem):
        auth.verify("Bearer " + token(), activity())
    assert seen == ["https://login.botframework.com/v1/.well-known/keys"]


def test_private_dns_resolution_cannot_receive_a_request_or_credentials(monkeypatch):
    import socket
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.7", 443)),
    ])
    with PublicCloudTransport() as transport:
        with pytest.raises(Problem) as error:
            transport.handle_request(httpx.Request("POST", SERVICE + "v3/conversations/id/activities"))
    assert error.value.body["error"]["code"] == "TEAMS_NETWORK_DENIED"


def test_new_channel_root_and_reply_keep_exact_provider_thread_without_truncation():
    root = "root-" + "opaque" * 40
    channel_route = ROUTE.model_copy(update={"conversation_type": "channel", "thread_id": root})
    auth = identity()
    auth.settings = auth.settings.model_copy(update={"routes": (channel_route,)})
    incoming = activity(id=root, conversation={
        "id": CONVERSATION, "conversationType": "channel", "tenantId": TENANT,
    })
    verified = auth.verify("Bearer " + token(), incoming)
    assert verified.route.thread_id == root
    reply = auth.verify("Bearer " + token(), {**incoming, "id": "reply-1", "replyToId": root})
    assert reply.route == verified.route
    assert reply.event_id != verified.event_id


def test_routes_over_existing_operation_url_limit_are_rejected_not_truncated():
    large = ROUTE.model_copy(update={"conversation_id": "opaque-" * 300})
    with pytest.raises(Problem) as error:
        TeamsSettings(tenant_id=TENANT, bot_app_id=BOT, team_id="team", routes=(large,))
    assert error.value.body["error"]["code"] == "TEAMS_ROUTE_LIMIT"
    assert len(large.conversation_id) == 2100
