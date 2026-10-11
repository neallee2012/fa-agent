"""Local signed-token checks; no live human identity or Entra client is configured."""
from __future__ import annotations

import time
from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import Request

from conftest import A, B, C, AUDIENCE, TENANT
from shared_context.auth import Actor, JwtVerifier
from shared_context.errors import Problem
from shared_context.m3_auth import (
    APPROVAL_SCOPE, ApprovalSettings, HumanApprover, M3JwtVerifier,
)


HUMAN_OID = "dddddddd-dddd-dddd-dddd-dddddddddddd"
CLIENT_ID = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
UNKNOWN_OID = "ffffffff-ffff-ffff-ffff-ffffffffffff"


@pytest.fixture(scope="module")
def signing_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def jwk_client(signing_key):
    jwk = jwt.PyJWK.from_dict({
        **jwt.algorithms.RSAAlgorithm.to_jwk(signing_key.public_key(), as_dict=True),
        "kid": "m3-test-key", "alg": "RS256", "use": "sig",
    })

    def lookup(token):
        if jwt.get_unverified_header(token).get("kid") != jwk.key_id:
            raise jwt.PyJWKClientError("unknown synthetic signing key")
        return jwk

    return SimpleNamespace(get_signing_key_from_jwt=lookup)


@pytest.fixture
def approval():
    return ApprovalSettings(HUMAN_OID, CLIENT_ID, "team-one")


def token(signing_key, changes=None, remove=(), *, algorithm="RS256", headers=None):
    claims = {
        "exp": int(time.time()) + 300, "iss": f"https://sts.windows.net/{TENANT}/",
        "aud": AUDIENCE, "tid": TENANT, "oid": HUMAN_OID, "ver": "1.0",
        "appid": CLIENT_ID, "scp": "Artifacts.Approve",
    }
    claims.update(changes or {})
    for field in remove:
        claims.pop(field, None)
    return jwt.encode(
        claims, signing_key, algorithm=algorithm,
        headers={"kid": "m3-test-key"} if headers is None else headers,
    )


def request(encoded):
    return Request({"type": "http", "headers": [(b"authorization", ("Bearer " + encoded).encode())]})


@pytest.mark.parametrize("version,issuer,client_claim", [
    ("1.0", f"https://sts.windows.net/{TENANT}/", "appid"),
    ("2.0", f"https://login.microsoftonline.com/{TENANT}/v2.0", "azp"),
])
def test_exact_human_subject_client_and_scope_yield_human_principal(
    auth, approval, signing_key, jwk_client, version, issuer, client_claim,
):
    encoded = token(signing_key, {
        "ver": version, "iss": issuer, client_claim: CLIENT_ID,
        "scp": "Context.Read Artifacts.Approve", "team": "untrusted-team",
        "preferred_username": "untrusted-profile@example.invalid",
    }, remove=("appid",) if client_claim == "azp" else ())
    principal = M3JwtVerifier(auth, approval, jwk_client)(request(encoded))
    assert APPROVAL_SCOPE == "Artifacts.Approve"
    assert principal == HumanApprover(HUMAN_OID, "team-one")
    assert not isinstance(principal, Actor)
    with pytest.raises(FrozenInstanceError):
        principal.team = "other-team"


@pytest.mark.parametrize("changes", [
    {"idtyp": "user"},
    {"azp": CLIENT_ID},
    {"ver": "2.0", "iss": f"https://login.microsoftonline.com/{TENANT}/v2.0", "azp": CLIENT_ID},
])
def test_consistent_optional_human_claims(auth, approval, signing_key, jwk_client, changes):
    principal = M3JwtVerifier(auth, approval, jwk_client)(request(token(signing_key, changes)))
    assert principal == HumanApprover(HUMAN_OID, "team-one")


def test_exact_issuer_identifies_version_when_ver_is_absent(auth, approval, signing_key, jwk_client):
    principal = M3JwtVerifier(auth, approval, jwk_client)(request(token(signing_key, remove=("ver",))))
    assert principal == HumanApprover(HUMAN_OID, "team-one")


@pytest.mark.parametrize("changes,remove", [
    ({"oid": UNKNOWN_OID, "email": "admin@example.invalid", "upn": "admin@example.invalid",
      "preferred_username": "admin@example.invalid", "roles": ["Global Administrator", "Artifacts.Approve"]}, ()),
    ({"oid": CLIENT_ID}, ()),
    ({"idtyp": "app", "roles": ["Artifacts.Approve"]}, ()),
    ({"idtyp": "app", "roles": ["Artifacts.Approve"]}, ("scp",)),
    ({"roles": ["Artifacts.Approve"]}, ("scp",)),
    ({"idtyp": "user"}, ("scp",)),
    ({"idtyp": None}, ()),
    ({"idtyp": "human"}, ()),
    ({"idtyp": True}, ()),
    ({"scp": None}, ()),
    ({"scp": ["Artifacts.Approve"]}, ()),
    ({"scp": ""}, ()),
    ({"scp": "Artifacts.Read"}, ()),
    ({"scp": "artifacts.approve"}, ()),
    ({"scp": "Artifacts.Approve.Extra"}, ()),
    ({"scp": "Artifacts.Approve\tContext.Read"}, ()),
    ({"appid": UNKNOWN_OID}, ()),
    ({"appid": CLIENT_ID.upper()}, ()),
    ({"appid": [CLIENT_ID]}, ()),
    ({"azp": CLIENT_ID}, ("appid",)),
    ({}, ("appid",)),
    ({"azp": UNKNOWN_OID}, ()),
    ({"azp": None}, ()),
    ({"appid": UNKNOWN_OID, "azp": CLIENT_ID}, ()),
    ({"ver": "2.0"}, ()),
    ({"ver": "unknown"}, ()),
    ({"ver": None}, ()),
    ({"ver": "2.0", "iss": f"https://login.microsoftonline.com/{TENANT}/v2.0"}, ()),
    ({"ver": "2.0", "iss": f"https://login.microsoftonline.com/{TENANT}/v2.0",
      "azp": CLIENT_ID, "appid": UNKNOWN_OID}, ()),
])
def test_human_authorization_fails_closed(
    auth, approval, signing_key, jwk_client, changes, remove,
):
    with pytest.raises(Problem) as denied:
        M3JwtVerifier(auth, approval, jwk_client)(request(token(signing_key, changes, remove)))
    assert denied.value.status == 403
    assert denied.value.body["error"]["code"] == "APPROVER_NOT_ALLOWED"


@pytest.mark.parametrize("version,issuer,client_claim", [
    ("1.0", f"https://sts.windows.net/{TENANT}/", "appid"),
    ("2.0", f"https://login.microsoftonline.com/{TENANT}/v2.0", "azp"),
])
@pytest.mark.parametrize("audience", [
    CLIENT_ID, AUDIENCE.removeprefix("api://"), UNKNOWN_OID,
    AUDIENCE + "/.default", [AUDIENCE], [AUDIENCE, CLIENT_ID],
])
def test_human_audience_is_never_relaxed_for_v2_or_id_tokens(
    auth, approval, signing_key, jwk_client, version, issuer, client_claim, audience,
):
    encoded = token(signing_key, {
        "ver": version, "iss": issuer, client_claim: CLIENT_ID, "aud": audience,
    })
    with pytest.raises(Problem) as denied:
        M3JwtVerifier(auth, approval, jwk_client)(request(encoded))
    assert denied.value.status == 401
    assert denied.value.body["error"]["code"] == "UNAUTHENTICATED"


@pytest.mark.parametrize("changes", [
    {"tid": UNKNOWN_OID},
    {"iss": f"https://sts.windows.net/{UNKNOWN_OID}/"},
    {"iss": "https://login.microsoftonline.com/common/v2.0"},
    {"iss": f"https://sts.windows.net/{TENANT}"},
    {"exp": 1},
    {"nbf": "one-hour-from-now"},
    {"oid": 123},
    {"oid": None},
])
def test_human_reuses_shared_token_validation(auth, approval, signing_key, jwk_client, changes):
    # A future nbf is computed per test: a collection-time value is already past in a run longer than an hour.
    if changes.get("nbf") == "one-hour-from-now":
        changes = {"nbf": int(time.time()) + 3600}
    with pytest.raises(Problem) as denied:
        M3JwtVerifier(auth, approval, jwk_client)(request(token(signing_key, changes)))
    assert denied.value.status == 401


@pytest.mark.parametrize("missing", ["exp", "iss", "aud", "tid", "oid"])
def test_human_requires_original_mandatory_claims(auth, approval, signing_key, jwk_client, missing):
    with pytest.raises(Problem) as denied:
        M3JwtVerifier(auth, approval, jwk_client)(request(token(signing_key, remove=(missing,))))
    assert denied.value.status == 401


@pytest.mark.parametrize("kind", ["wrong-signature", "none", "HS256", "missing-kid", "unknown-kid"])
def test_human_requires_real_rs256_signature_and_known_jwk(
    auth, approval, signing_key, jwk_client, kind,
):
    if kind == "wrong-signature":
        encoded = token(rsa.generate_private_key(public_exponent=65537, key_size=2048))
    elif kind == "none":
        encoded = token("", algorithm="none")
    elif kind == "HS256":
        encoded = token("synthetic-not-a-production-signing-secret", algorithm="HS256")
    else:
        encoded = token(signing_key, headers={} if kind == "missing-kid" else {"kid": "unknown"})
    with pytest.raises(Problem) as denied:
        M3JwtVerifier(auth, approval, jwk_client)(request(encoded))
    assert denied.value.status == 401


def test_jwk_outage_remains_explicitly_unavailable(auth, approval, signing_key):
    def unavailable(_encoded):
        raise jwt.PyJWKClientConnectionError("synthetic outage")

    verifier = M3JwtVerifier(auth, approval, SimpleNamespace(get_signing_key_from_jwt=unavailable))
    with pytest.raises(Problem) as denied:
        verifier(request(token(signing_key)))
    assert denied.value.status == 503
    assert denied.value.body["error"]["code"] == "AUTH_KEYS_UNAVAILABLE"


@pytest.mark.parametrize("authorization", [None, "", "Basic synthetic", "Bearer ", "Bearer " + "x" * 16385])
def test_m3_reuses_strict_bearer_parsing(auth, approval, jwk_client, authorization):
    headers = [] if authorization is None else [(b"authorization", authorization.encode())]
    with pytest.raises(Problem) as denied:
        M3JwtVerifier(auth, approval, jwk_client)(Request({"type": "http", "headers": headers}))
    assert denied.value.status == 401


@pytest.mark.parametrize("agent", [A, B, C])
@pytest.mark.parametrize("scopes", [False, True])
def test_agent_app_tokens_never_become_human_approvers(
    auth, approval, signing_key, jwk_client, agent, scopes,
):
    encoded = token(signing_key, {
        "oid": agent.oid, "idtyp": "app", "roles": ["Artifacts.Approve", "Admin"],
        "team": "untrusted-team", "actor": "spoofed",
    }, remove=() if scopes else ("scp",))
    principal = M3JwtVerifier(auth, approval, jwk_client)(request(encoded))
    assert principal == agent
    assert isinstance(principal, Actor)
    assert not isinstance(principal, HumanApprover)
    assert JwtVerifier(auth, jwk_client)(request(encoded)) == principal


def test_human_is_not_added_to_existing_agent_routes(auth, approval, signing_key, jwk_client):
    encoded = token(signing_key, {"actor": A.actor, "team": A.team})
    assert isinstance(M3JwtVerifier(auth, approval, jwk_client)(request(encoded)), HumanApprover)
    with pytest.raises(Problem) as denied:
        JwtVerifier(auth, jwk_client)(request(encoded))
    assert denied.value.status == 403
    assert denied.value.body["error"]["code"] == "ACTOR_NOT_ALLOWED"


@pytest.mark.parametrize("field,value", [
    ("approver_oid", "admin@example.invalid"),
    ("approver_oid", HUMAN_OID.upper()),
    ("approver_oid", HUMAN_OID.replace("-", "")),
    ("approver_oid", None),
    ("client_id", ""),
    ("client_id", CLIENT_ID.upper()),
    ("client_id", "{" + CLIENT_ID + "}"),
    ("client_id", 123),
    ("team", ""),
    ("team", "../team-one"),
    ("team", "team one"),
    ("team", "x" * 65),
    ("team", None),
])
def test_approval_settings_require_canonical_ids_and_identifier(field, value):
    values = {"approver_oid": HUMAN_OID, "client_id": CLIENT_ID, "team": "team-one"}
    values[field] = value
    with pytest.raises(ValueError):
        ApprovalSettings(**values)


@pytest.mark.parametrize("agent", [A, B, C])
def test_human_configuration_cannot_overlap_any_agent_oid(auth, jwk_client, agent):
    with pytest.raises(ValueError, match="overlap"):
        M3JwtVerifier(auth, ApprovalSettings(agent.oid, CLIENT_ID, "team-one"), jwk_client)


def test_approval_settings_are_immutable_and_have_no_placeholder_defaults(approval):
    with pytest.raises(FrozenInstanceError):
        approval.approver_oid = UNKNOWN_OID
    with pytest.raises(TypeError):
        ApprovalSettings()
