"""Signed local JWT checks; these tests do not provision Entra scopes or roles."""
from dataclasses import FrozenInstanceError

import pytest

from conftest import A, TENANT
from shared_context.behavior_auth import (
    AGENT_ROLE, APPROVAL_SCOPE, BACKUP_SCOPE, READ_SCOPE, RESTORE_SCOPE, BehaviorAuthSettings, BehaviorJwtVerifier,
)
from shared_context.errors import Problem
from shared_context.m3_auth import HumanApprover, M3JwtVerifier
from test_m3_auth import (
    CLIENT_ID, HUMAN_OID, UNKNOWN_OID, approval, jwk_client, request, signing_key, token,
)


@pytest.fixture
def verifier(auth, jwk_client):
    return BehaviorJwtVerifier(auth, BehaviorAuthSettings(HUMAN_OID, CLIENT_ID, "team-one"), jwk_client)


@pytest.mark.parametrize("version,issuer,client_claim", [
    ("1.0", f"https://sts.windows.net/{TENANT}/", "appid"),
    ("2.0", f"https://login.microsoftonline.com/{TENANT}/v2.0", "azp"),
])
def test_exact_delegated_human_can_read_and_explicitly_approve(verifier, signing_key, version, issuer, client_claim):
    value = token(signing_key, {
        "ver": version, "iss": issuer, client_claim: CLIENT_ID,
        "scp": f"{READ_SCOPE} {APPROVAL_SCOPE}", "team": "untrusted-team",
        "approved": True, "actor": "agent-b",
    }, remove=("appid",) if client_claim == "azp" else ())
    principal = verifier(request(value))
    assert principal.oid == HUMAN_OID and principal.team == "team-one" and principal.kind == "human"
    assert principal.actor_id is None
    assert principal.source_actor_id == "human-" + HUMAN_OID
    for action in ("read", "submit", "acknowledge", "approve"):
        principal.require("team-one", action)
    for team, action in (("team-two", "read"), ("team-one", "execute"), ("team-one", "consume")):
        with pytest.raises(Problem) as denied:
            principal.require(team, action)
        assert denied.value.status == 403
    with pytest.raises(FrozenInstanceError):
        principal.kind = "agent"


def test_read_consent_does_not_grant_approval(verifier, signing_key):
    principal = verifier(request(token(signing_key, {"scp": READ_SCOPE, "roles": [APPROVAL_SCOPE]})))
    principal.require("team-one", "acknowledge")
    with pytest.raises(Problem):
        principal.require("team-one", "approve")


def test_m3_consent_is_not_silently_broadened(auth, approval, jwk_client, verifier, signing_key):
    value = request(token(signing_key))
    assert M3JwtVerifier(auth, approval, jwk_client)(value) == HumanApprover(HUMAN_OID, "team-one")
    with pytest.raises(Problem) as denied:
        verifier(value)
    assert denied.value.body["error"]["code"] == "BEHAVIOR_HUMAN_NOT_ALLOWED"


def test_agent_role_does_not_grant_human_approval(verifier, signing_key):
    principal = verifier(request(token(signing_key, {
        "oid": A.oid, "idtyp": "app", "roles": [AGENT_ROLE, APPROVAL_SCOPE], "team": "team-two",
    }, remove=("scp",))))
    assert principal.actor_id == A.actor and principal.team == A.team and principal.kind == "agent"
    assert principal.source_actor_id == A.actor
    for action in ("read", "submit", "execute", "consume"):
        principal.require(A.team, action)
    for action in ("approve", "acknowledge", "backup", "restore"):
        with pytest.raises(Problem):
            principal.require(A.team, action)


@pytest.mark.parametrize("scope,allowed,denied", [
    (BACKUP_SCOPE, "backup", "restore"), (RESTORE_SCOPE, "restore", "backup"),
])
def test_backup_and_quarantine_restore_require_separate_current_consent(verifier, signing_key, scope, allowed, denied):
    principal = verifier(request(token(signing_key, {"scp": READ_SCOPE + " " + scope})))
    principal.require("team-one", allowed)
    for action in (denied, "approve", "execute"):
        with pytest.raises(Problem):
            principal.require("team-one", action)
    ordinary = verifier(request(token(signing_key, {"scp": READ_SCOPE + " " + APPROVAL_SCOPE})))
    with pytest.raises(Problem):
        ordinary.require("team-one", allowed)


@pytest.mark.parametrize("changes,remove", [
    ({"scp": "Artifacts.Approve"}, ()),
    ({"scp": APPROVAL_SCOPE}, ()),
    ({"scp": READ_SCOPE + ".Extra"}, ()),
    ({"scp": READ_SCOPE.lower()}, ()),
    ({"scp": READ_SCOPE + "\t" + APPROVAL_SCOPE}, ()),
    ({"scp": [READ_SCOPE, APPROVAL_SCOPE]}, ()),
    ({"scp": None}, ()),
    ({"scp": READ_SCOPE, "oid": UNKNOWN_OID}, ()),
    ({"scp": READ_SCOPE, "idtyp": "app"}, ()),
    ({"scp": READ_SCOPE, "idtyp": True}, ()),
    ({"scp": READ_SCOPE, "appid": UNKNOWN_OID}, ()),
    ({"scp": READ_SCOPE, "azp": UNKNOWN_OID}, ()),
    ({"scp": READ_SCOPE}, ("appid",)),
    ({"oid": A.oid, "roles": [AGENT_ROLE]}, ()),
    ({"oid": A.oid, "roles": [AGENT_ROLE], "idtyp": "user"}, ("scp",)),
])
def test_missing_or_confused_grants_fail_closed(verifier, signing_key, changes, remove):
    with pytest.raises(Problem) as denied:
        verifier(request(token(signing_key, changes, remove)))
    assert denied.value.status == 403


# ADR-0006 gate C: a configured actor's app-only token is admitted by its oid alone. These cases were
# denied while the configured path still demanded the Behavior.Agent role; any roles claim is now ignored.
@pytest.mark.parametrize("changes", [
    {}, {"idtyp": "app"}, {"roles": [AGENT_ROLE]}, {"roles": ["Other.Role"]},
    {"roles": AGENT_ROLE}, {"roles": [AGENT_ROLE.lower()]},
])
def test_configured_actor_app_token_is_admitted_by_oid_without_role(verifier, signing_key, caplog, changes):
    principal = verifier(request(token(signing_key, {"oid": A.oid, **changes}, ("scp",))))
    assert (principal.oid, principal.team, principal.kind, principal.actor_id) == (A.oid, A.team, "agent", A.actor)
    assert principal.actions == frozenset({"read", "submit", "execute", "consume"})
    assert "BEHAVIOR_AGENT_AUTH_DENIED" not in caplog.text


@pytest.mark.parametrize("changes,remove,reason", [
    ({"oid": A.oid, "scp": READ_SCOPE}, (), "delegated_token"),
    ({"oid": A.oid, "scp": READ_SCOPE, "idtyp": "app", "roles": [AGENT_ROLE]}, (), "delegated_token"),
    ({"oid": A.oid, "idtyp": "user"}, ("scp",), "not_app_only"),
    ({"oid": A.oid, "idtyp": None}, ("scp",), "not_app_only"),
    ({"oid": UNKNOWN_OID, "idtyp": "app"}, ("scp",), "agent_unregistered"),
    ({"oid": UNKNOWN_OID, "roles": [AGENT_ROLE]}, ("scp",), "agent_unregistered"),
])
def test_agent_denials_log_only_a_fixed_reason(verifier, signing_key, caplog, changes, remove, reason):
    with pytest.raises(Problem) as denied:
        verifier(request(token(signing_key, changes, remove)))
    assert denied.value.status == 403 and denied.value.body["error"]["code"] == "BEHAVIOR_ACTOR_NOT_ALLOWED"
    assert [r.getMessage() for r in caplog.records] == ["BEHAVIOR_AGENT_AUTH_DENIED reason=" + reason]
    assert changes["oid"] not in caplog.text and TENANT not in caplog.text


@pytest.mark.parametrize("changes", [
    {"exp": 1}, {"tid": UNKNOWN_OID}, {"aud": CLIENT_ID}, {"iss": f"https://sts.windows.net/{UNKNOWN_OID}/"},
])
def test_configured_actor_app_token_keeps_signature_tenant_audience_and_expiry(verifier, signing_key, changes):
    with pytest.raises(Problem) as denied:
        verifier(request(token(signing_key, {"oid": A.oid, "idtyp": "app", **changes}, ("scp",))))
    assert denied.value.status == 401


def test_configured_actor_app_token_signed_by_another_key_is_rejected(verifier):
    from cryptography.hazmat.primitives.asymmetric import rsa
    forged = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(Problem) as denied:
        verifier(request(token(forged, {"oid": A.oid}, ("scp",))))
    assert denied.value.status == 401


@pytest.mark.parametrize("changes", [
    {"exp": 1}, {"tid": UNKNOWN_OID}, {"aud": CLIENT_ID}, {"iss": "https://login.microsoftonline.com/common/v2.0"},
])
def test_shared_signature_tenant_audience_and_expiry_checks_are_preserved(verifier, signing_key, changes):
    with pytest.raises(Problem) as denied:
        verifier(request(token(signing_key, {"scp": READ_SCOPE, **changes})))
    assert denied.value.status == 401


def test_human_agent_overlap_and_unknown_team_are_configuration_errors(auth, jwk_client):
    with pytest.raises(ValueError):
        BehaviorJwtVerifier(auth, BehaviorAuthSettings(A.oid, CLIENT_ID, "team-one"), jwk_client)
    with pytest.raises(ValueError):
        BehaviorJwtVerifier(auth, BehaviorAuthSettings(HUMAN_OID, CLIENT_ID, "unknown-team"), jwk_client)


REMOTE_CLIENT = "cccccccc-1111-2222-3333-cccccccccccc"


@pytest.fixture
def remote_verifier(auth, jwk_client):
    return BehaviorJwtVerifier(auth, BehaviorAuthSettings(HUMAN_OID, CLIENT_ID, "team-one", REMOTE_CLIENT), jwk_client)


@pytest.mark.parametrize("version,issuer,client_claim", [
    ("1.0", f"https://sts.windows.net/{TENANT}/", "appid"),
    ("2.0", f"https://login.microsoftonline.com/{TENANT}/v2.0", "azp"),
])
def test_extra_remote_console_client_gets_the_same_human_principal(
        verifier, remote_verifier, signing_key, version, issuer, client_claim):
    def bearer(client, scopes):
        return request(token(signing_key, {"ver": version, "iss": issuer, client_claim: client, "scp": scopes},
                             remove=("appid",) if client_claim == "azp" else ()))
    for client in (CLIENT_ID, REMOTE_CLIENT):
        principal = remote_verifier(bearer(client, READ_SCOPE))
        assert (principal.oid, principal.team, principal.kind) == (HUMAN_OID, "team-one", "human")
        with pytest.raises(Problem):
            principal.require("team-one", "approve")
        remote_verifier(bearer(client, READ_SCOPE + " " + APPROVAL_SCOPE)).require("team-one", "approve")
    with pytest.raises(Problem) as denied:
        verifier(bearer(REMOTE_CLIENT, READ_SCOPE))
    assert denied.value.body["error"]["code"] == "BEHAVIOR_HUMAN_NOT_ALLOWED"


@pytest.mark.parametrize("changes,remove", [
    ({"scp": READ_SCOPE, "appid": UNKNOWN_OID}, ()),
    ({"scp": READ_SCOPE, "appid": REMOTE_CLIENT, "azp": CLIENT_ID}, ()),
    ({"scp": READ_SCOPE, "appid": CLIENT_ID, "azp": REMOTE_CLIENT}, ()),
    ({"scp": READ_SCOPE, "appid": [REMOTE_CLIENT]}, ()),
    ({"scp": READ_SCOPE, "appid": REMOTE_CLIENT, "oid": UNKNOWN_OID}, ()),
    ({"scp": APPROVAL_SCOPE, "appid": REMOTE_CLIENT}, ()),
    ({"scp": READ_SCOPE, "appid": REMOTE_CLIENT, "idtyp": "app"}, ()),
    ({"scp": READ_SCOPE}, ("appid",)),
])
def test_extra_client_keeps_every_other_human_check(remote_verifier, signing_key, changes, remove):
    with pytest.raises(Problem) as denied:
        remote_verifier(request(token(signing_key, changes, remove)))
    assert denied.value.status == 403


@pytest.mark.parametrize("extra", [CLIENT_ID, REMOTE_CLIENT.upper(), "not-a-uuid", ""])
def test_malformed_or_duplicate_extra_client_is_a_configuration_error(extra):
    with pytest.raises(ValueError):
        BehaviorAuthSettings(HUMAN_OID, CLIENT_ID, "team-one", extra)


@pytest.mark.parametrize("value,expected", [(None, (CLIENT_ID,)), ("", (CLIENT_ID,)),
                                            (REMOTE_CLIENT, (CLIENT_ID, REMOTE_CLIENT))])
def test_server_reads_the_remote_console_client_only_from_its_setting(monkeypatch, auth, jwk_client, value, expected):
    from types import SimpleNamespace
    from shared_context import behavior_server
    identity = SimpleNamespace(human_oid=HUMAN_OID, admin_client_id=CLIENT_ID, team_id="team-one", auth=lambda: auth)
    if value is None:
        monkeypatch.delenv(behavior_server.REMOTE_CONSOLE_CLIENT_ENV, raising=False)
    else:
        monkeypatch.setenv(behavior_server.REMOTE_CONSOLE_CLIENT_ENV, value)
    settings = behavior_server._verifier(identity, jwk_client).settings
    assert settings.client_id == CLIENT_ID and settings.delegated_clients == expected
    monkeypatch.setenv(behavior_server.REMOTE_CONSOLE_CLIENT_ENV, "not-a-uuid")
    with pytest.raises(ValueError):
        behavior_server._verifier(identity, jwk_client)
