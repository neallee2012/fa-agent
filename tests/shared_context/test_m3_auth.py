"""Local signed-token checks; no live human identity or Entra client is configured."""
from __future__ import annotations

import time
from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import Request

from conftest import AUDIENCE, TENANT
from shared_context.auth import JwtVerifier
from shared_context.errors import Problem
from shared_context.m3_auth import ApprovalSettings


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


@pytest.mark.parametrize("kind", ["wrong-signature", "none", "HS256", "missing-kid", "unknown-kid"])
def test_shared_validation_requires_real_rs256_signature_and_known_jwk(auth, signing_key, jwk_client, kind):
    if kind == "wrong-signature":
        encoded = token(rsa.generate_private_key(public_exponent=65537, key_size=2048))
    elif kind == "none":
        encoded = token("", algorithm="none")
    elif kind == "HS256":
        encoded = token("synthetic-not-a-production-signing-secret", algorithm="HS256")
    else:
        encoded = token(signing_key, headers={} if kind == "missing-kid" else {"kid": "unknown"})
    with pytest.raises(Problem) as denied:
        JwtVerifier(auth, jwk_client).verified_claims(request(encoded))
    assert denied.value.status == 401


@pytest.mark.parametrize("authorization", [None, "", "Basic synthetic", "Bearer ", "oversized"])
def test_shared_validation_parses_the_bearer_strictly(auth, signing_key, jwk_client, authorization):
    if authorization == "oversized":  # validly signed, so only the length bound can refuse it
        authorization = "Bearer " + token(signing_key, {"pad": "x" * 16384})
    headers = [] if authorization is None else [(b"authorization", authorization.encode())]
    with pytest.raises(Problem) as denied:
        JwtVerifier(auth, jwk_client).verified_claims(Request({"type": "http", "headers": headers}))
    assert denied.value.status == 401


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


def test_approval_settings_are_immutable_and_have_no_placeholder_defaults(approval):
    with pytest.raises(FrozenInstanceError):
        approval.approver_oid = UNKNOWN_OID
    with pytest.raises(TypeError):
        ApprovalSettings()
