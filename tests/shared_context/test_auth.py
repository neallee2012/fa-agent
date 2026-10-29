from __future__ import annotations

import time
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from conftest import A, AUDIENCE, TENANT
from shared_context.api import create_app
from shared_context.auth import AuthSettings, JwtVerifier
from shared_context.config import validate_api_audience


@pytest.fixture(scope="module")
def private_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def claims():
    return {
        "exp": int(time.time()) + 300, "iss": f"https://sts.windows.net/{TENANT}/",
        "aud": AUDIENCE, "tid": TENANT, "oid": A.oid,
    }


def signed(private_key, changes=None, remove=None, headers=None):
    data = {**claims(), **(changes or {})}
    if remove:
        del data[remove]
    return jwt.encode(data, private_key, algorithm="RS256", headers=headers or {"kid": "test-key"})


@pytest.fixture
def authenticated(system, auth, private_key):
    keys = SimpleNamespace(get_signing_key_from_jwt=lambda token: SimpleNamespace(key=private_key.public_key()))
    return TestClient(create_app(system[0], JwtVerifier(auth, keys)))


@pytest.mark.parametrize("issuer", [
    f"https://sts.windows.net/{TENANT}/",
    f"https://login.microsoftonline.com/{TENANT}/v2.0",
])
def test_signed_allowed_actor(authenticated, private_key, issuer):
    token = signed(private_key, {"iss": issuer, "actor": "spoofed", "team": "team-two"})
    response = authenticated.post("/teams/team-one/works/auth-test", json={"goal": "synthetic"},
                                  headers={"Authorization": f"Bearer {token}", "X-Actor": "agent-b"})
    assert response.status_code == 201
    assert response.json()["owner"] == A.actor and response.json()["team"] == A.team


@pytest.mark.parametrize("change", [
    {"exp": int(time.time()) - 30},
    {"aud": "https://ai.azure.com"},
    {"aud": [AUDIENCE, "other-audience"]},
    {"tid": "99999999-9999-9999-9999-999999999999"},
    {"iss": "https://evil.example/"},
    {"iss": f"https://sts.windows.net/{TENANT}"},
    {"nbf": int(time.time()) + 3600},
    {"oid": 123},
])
def test_reject_bad_claims(authenticated, private_key, change):
    response = authenticated.get("/teams/team-one/works/auth-test",
                                  headers={"Authorization": "Bearer " + signed(private_key, change)})
    assert response.status_code == 401


@pytest.mark.parametrize("claim", ["exp", "iss", "aud", "tid", "oid"])
def test_require_claims(authenticated, private_key, claim):
    response = authenticated.get("/teams/team-one/works/auth-test",
                                  headers={"Authorization": "Bearer " + signed(private_key, remove=claim)})
    assert response.status_code == 401


def test_reject_unknown_oid(authenticated, private_key):
    token = signed(private_key, {"oid": "dddddddd-dddd-dddd-dddd-dddddddddddd"})
    assert authenticated.get("/teams/team-one/works/auth-test",
                             headers={"Authorization": "Bearer " + token}).status_code == 403


def test_reject_header_bypass_none_hs256_invalid_signature(authenticated, private_key):
    route = "/teams/team-one/works/auth-test"
    assert authenticated.get(route, headers={"X-Actor": "agent-a", "X-Team": "team-one"}).status_code == 401
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    for token in (
        jwt.encode(claims(), "", algorithm="none", headers={"kid": "test-key"}),
        jwt.encode(claims(), "synthetic-test-key-not-used-in-production", algorithm="HS256", headers={"kid": "test-key"}),
        signed(other_key),
        jwt.encode(claims(), private_key, algorithm="RS256"),
    ):
        assert authenticated.get(route, headers={"Authorization": "Bearer " + token}).status_code == 401


def test_jwks_outage_fails_closed(system, auth, private_key):
    def missing(_token):
        raise jwt.PyJWKClientConnectionError("unavailable")
    client = TestClient(create_app(system[0], JwtVerifier(auth, SimpleNamespace(get_signing_key_from_jwt=missing))))
    response = client.get("/teams/team-one/works/auth-test",
                          headers={"Authorization": "Bearer " + signed(private_key)})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "AUTH_KEYS_UNAVAILABLE"


def test_no_arbitrary_issuer_configuration(auth):
    with pytest.raises(ValueError, match="exact tenant-specific"):
        AuthSettings(TENANT, AUDIENCE, ("https://evil.example/",), auth.actors)


def test_resource_uri_audience_is_matched_exactly(system, auth, private_key):
    audience = "api://00000000-0000-0000-0000-000000000013/fa-context-poc-api"
    settings = AuthSettings(TENANT, audience, (f"https://sts.windows.net/{TENANT}/",), auth.actors)
    keys = SimpleNamespace(get_signing_key_from_jwt=lambda token: SimpleNamespace(key=private_key.public_key()))
    client = TestClient(create_app(system[0], JwtVerifier(settings, keys)))
    for token_audience in (audience.rsplit("/", 1)[0], audience + "/.default", audience + "-other"):
        response = client.post("/teams/team-one/works/resource-uri", json={"goal": "synthetic"},
                               headers={"Authorization": "Bearer " + signed(private_key, {"aud": token_audience})})
        assert response.status_code == 401
    accepted = client.post("/teams/team-one/works/resource-uri", json={"goal": "synthetic"},
                           headers={"Authorization": "Bearer " + signed(private_key, {"aud": audience})})
    assert accepted.status_code == 201


@pytest.mark.parametrize("audience", [
    "https://00000000-0000-0000-0000-000000000013/fa-context-poc-api",
    "api://------------------------------------/fa-context-poc-api",
    "api://00000000-0000-0000-0000-000000000013/",
    "api://00000000-0000-0000-0000-000000000013/fa-context-poc-api/.default",
    "api://00000000-0000-0000-0000-000000000013/fa-context-poc-api?other",
    "api://00000000-0000-0000-0000-000000000013/fa-context-poc-api#other",
])
def test_resource_uri_configuration_is_bounded(audience):
    with pytest.raises(ValueError):
        validate_api_audience(audience)


class _CountingKeys:
    """The production key client with its network fetch replaced by a local key set that counts downloads."""

    def __init__(self, monkeypatch, keys, clock):
        from shared_context.auth import tenant_key_client
        self.fetches, self.keys = 0, keys
        self.client = tenant_key_client(TENANT)
        self.client._clock = clock
        monkeypatch.setattr(self.client, "fetch_data", self._fetch)

    def _fetch(self):
        self.fetches += 1
        data = {"keys": [{**jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True),
                          "kid": kid, "alg": "RS256", "use": "sig"} for kid, key in self.keys.items()]}
        self.client.jwk_set_cache.put(data)
        return data


@pytest.fixture
def clock():
    from shared_context.auth import _reset_forced_refresh_window
    _reset_forced_refresh_window()
    now = SimpleNamespace(value=1000.0)
    yield now
    _reset_forced_refresh_window()


def test_unknown_kids_force_at_most_one_key_download_per_window(system, auth, private_key, monkeypatch, clock):
    import uuid
    keys = _CountingKeys(monkeypatch, {"test-key": private_key}, lambda: clock.value)
    client = TestClient(create_app(system[0], JwtVerifier(auth, keys.client)))
    route = "/teams/team-one/works/auth-test"
    assert client.post(route, json={"goal": "synthetic"},
                       headers={"Authorization": "Bearer " + signed(private_key)}).status_code == 201
    assert keys.fetches == 1  # the cold fill
    for _ in range(25):
        forged = signed(private_key, headers={"kid": uuid.uuid4().hex})
        response = client.get(route, headers={"Authorization": "Bearer " + forged})
        assert response.status_code == 401 and response.json()["error"]["code"] == "UNAUTHENTICATED"
    assert keys.fetches == 2, "one forced refresh per window, however many unknown kids arrive"
    assert client.get(route, headers={"Authorization": "Bearer " + signed(private_key)}).status_code == 200
    assert keys.fetches == 2

    rotated = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    keys.keys["rotated-key"] = rotated
    new = {"Authorization": "Bearer " + signed(rotated, headers={"kid": "rotated-key"})}
    clock.value += 30
    assert client.get(route, headers=new).status_code == 401 and keys.fetches == 2
    clock.value += 31
    assert client.get(route, headers=new).status_code == 200, "a rotated key is picked up after the window"
    assert keys.fetches == 3


def test_the_forced_refresh_window_is_shared_by_every_verifier_in_the_process(private_key, monkeypatch, clock):
    first, second = (_CountingKeys(monkeypatch, {"test-key": private_key}, lambda: clock.value) for _ in range(2))
    for keys in (first, second):
        with pytest.raises(jwt.PyJWKClientError):
            keys.client.get_signing_key("unknown-kid")
    assert (first.fetches, second.fetches) == (2, 1), "the second client's miss does not force another download"


def test_a_failed_refresh_after_the_ttl_keeps_the_last_tenant_keys_for_a_bounded_time(private_key, monkeypatch, clock):
    keys = _CountingKeys(monkeypatch, {"test-key": private_key}, lambda: clock.value)
    keys.client.get_signing_key("test-key")
    held = keys.client.jwk_set_cache.jwk_set_with_timestamp
    attempts = []

    def down():
        attempts.append(clock.value)
        raise jwt.PyJWKClientConnectionError("synthetic outage")

    monkeypatch.setattr(keys.client, "fetch_data", down)
    held.timestamp -= 301
    for _ in range(5):
        assert keys.client.get_signing_key("test-key").key_id == "test-key"
    assert len(attempts) == 1, "one refresh attempt per retry window, not one per request"
    clock.value += 31
    keys.client.get_signing_key("test-key")
    assert len(attempts) == 2
    with pytest.raises(jwt.PyJWKClientConnectionError):
        keys.client.get_signing_key("unknown-kid")  # a forced refresh never falls back to the old set
    held.timestamp -= 24 * 3600
    clock.value += 31
    with pytest.raises(jwt.PyJWKClientConnectionError):
        keys.client.get_signing_key("test-key")  # the stale set is bounded


@pytest.mark.parametrize("body", [b"<html>gateway error</html>", b"[]"], ids=["not-json", "not-a-key-set"])
def test_a_refresh_answered_without_a_key_set_keeps_the_last_tenant_keys(private_key, monkeypatch, clock, body):
    import io
    keys = _CountingKeys(monkeypatch, {"test-key": private_key}, lambda: clock.value)
    keys.client.get_signing_key("test-key")
    keys.client.jwk_set_cache.jwk_set_with_timestamp.timestamp -= 301
    # The real download path: a 200 whose body is not a JSON key set.
    monkeypatch.setattr(keys.client, "fetch_data", type(keys.client).fetch_data.__get__(keys.client))
    monkeypatch.setattr(jwt.jwks_client.urllib.request, "urlopen", lambda *_args, **_kwargs: io.BytesIO(body))
    assert keys.client.get_signing_key("test-key").key_id == "test-key"
    clock.value += 31
    assert keys.client.get_signing_key("test-key").key_id == "test-key", "the bad reply never replaces the held set"


def test_a_forced_refresh_answered_without_a_key_set_never_replaces_the_held_tenant_keys(private_key, monkeypatch, clock):
    import io
    keys = _CountingKeys(monkeypatch, {"test-key": private_key}, lambda: clock.value)
    keys.client.get_signing_key("test-key")
    monkeypatch.setattr(keys.client, "fetch_data", type(keys.client).fetch_data.__get__(keys.client))
    monkeypatch.setattr(jwt.jwks_client.urllib.request, "urlopen", lambda *_args, **_kwargs: io.BytesIO(b"[]"))
    with pytest.raises(jwt.PyJWKClientError):
        keys.client.get_signing_key("unknown-kid")  # a forced refresh still never falls back
    assert keys.client.get_signing_key("test-key").key_id == "test-key", "the refused body was not cached"
