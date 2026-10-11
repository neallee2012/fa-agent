"""ADR-0006 §5 A1 live check: one invalid Responses request with the Hosted identity, logging only the status.

Offline: the credential and HTTP transport are local doubles; no Azure call or model run.
"""

import json
import logging
from types import SimpleNamespace

import httpx
import pytest

from shared_context import behavior_model_direct_probe as direct

URL = "https://fa-acct.services.ai.azure.com/api/projects/fa-project/openai/v1/responses"
TOKEN = "secret-token-value"


class Credential:
    def __init__(self, fail=False):
        self.scopes, self.fail = [], fail

    def get_token(self, scope):
        self.scopes.append(scope)
        if self.fail:
            raise RuntimeError("Synthetic credential failure")
        return SimpleNamespace(token=TOKEN)


def transport(status, sent):
    def handle(request):
        sent.append(request)
        return httpx.Response(status, json={"error": {"code": "invalid_value", "message": "echo " + TOKEN}})
    return httpx.MockTransport(handle)


def test_off_without_configuration_sends_nothing():
    credential, sent = Credential(), []
    assert direct.probe(credential, environ={}, transport=transport(400, sent)) is None
    assert direct.start(credential, environ={}) is None
    assert credential.scopes == [] and sent == []


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_one_invalid_request_logs_only_the_status(status, caplog):
    credential, sent = Credential(), []
    environ = {direct.URL_ENV: URL, direct.MODEL_ENV: "gpt-5.4-mini"}
    with caplog.at_level(logging.INFO):
        assert direct.probe(credential, environ=environ, transport=transport(status, sent)) == status
    assert credential.scopes == ["https://ai.azure.com/.default"]
    assert len(sent) == 1 and sent[0].method == "POST" and str(sent[0].url) == URL
    assert sent[0].headers["authorization"] == "Bearer " + TOKEN
    body = json.loads(sent[0].content)
    assert body["model"] == "gpt-5.4-mini" and body["temperature"] == 5 and body["store"] is False
    assert f"FA_MODEL_DIRECT_PROBE status={status}" in caplog.text
    # Neither the token nor any response text reaches the log.
    assert TOKEN not in caplog.text and "invalid_value" not in caplog.text and "echo" not in caplog.text


@pytest.mark.parametrize("url", [
    "https://evil.example.com/api/projects/fa-project/openai/v1/responses",
    "https://fa-acct.services.ai.azure.com.evil.example/api/projects/p/openai/v1/responses",
    "http://fa-acct.services.ai.azure.com/api/projects/fa-project/openai/v1/responses",
    "https://fa-acct.services.ai.azure.com/api/projects/fa-project/openai/v1/chat/completions",
    "https://fa-acct.services.ai.azure.com/api/projects/fa-project/openai/v1/responses?x=1",
])
def test_the_token_is_never_sent_outside_a_project_responses_endpoint(url, caplog):
    credential, sent = Credential(), []
    with caplog.at_level(logging.INFO):
        result = direct.probe(credential, environ={direct.URL_ENV: url, direct.MODEL_ENV: "gpt-5.4-mini"},
                              transport=transport(400, sent))
    assert result == "config_invalid" and credential.scopes == [] and sent == []
    assert "FA_MODEL_DIRECT_PROBE status=config_invalid" in caplog.text


def test_a_credential_or_transport_failure_is_logged_without_a_request_or_secret(caplog):
    environ = {direct.URL_ENV: URL, direct.MODEL_ENV: "gpt-5.4-mini"}
    sent = []
    with caplog.at_level(logging.INFO):
        assert direct.probe(Credential(fail=True), environ=environ, transport=transport(400, sent)) == "token_unavailable"

        def unreachable(request):
            raise httpx.ConnectError("Synthetic connect failure " + TOKEN)
        assert direct.probe(Credential(), environ=environ,
                            transport=httpx.MockTransport(unreachable)) == "transport_error"
    assert sent == [] and TOKEN not in caplog.text
    assert "status=token_unavailable" in caplog.text and "status=transport_error" in caplog.text


def test_start_runs_once_in_the_background(monkeypatch):
    calls = []
    monkeypatch.setattr(direct, "probe", lambda credential, *, environ: calls.append(environ))
    environ = {direct.URL_ENV: URL, direct.MODEL_ENV: "gpt-5.4-mini"}
    thread = direct.start(Credential(), environ=environ)
    thread.join(5)
    assert not thread.is_alive() and thread.daemon and calls == [environ]
