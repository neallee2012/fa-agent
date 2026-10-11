from __future__ import annotations

import json
import base64
import re
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from azure.ai.projects import AIProjectClient
from azure.core import MatchConditions
from azure.core.credentials import AccessToken
from azure.core.exceptions import HttpResponseError, ResourceExistsError, ResourceModifiedError
from azure.core.pipeline.transport import HttpResponse, HttpTransport
from azure.core.utils import CaseInsensitiveDict
from azure.data.tables import TableClient, UpdateMode

from shared_context.errors import Problem, safe_json, upstream_problem
from shared_context.memory import FEATURE_HEADERS, FoundryMemory
from shared_context.config import native_memory_scope
from shared_context.models import SyntheticMemory
from shared_context.storage import MAX_PAYLOAD_BYTES, TableRepository, serialize

ENDPOINT = "https://synthetic.services.ai.azure.com/api/projects/poc"


class WireResponse(HttpResponse):
    def __init__(self, request, status, payload=None, headers=None):
        super().__init__(request, None)
        self.status_code = status
        self.headers = CaseInsensitiveDict({"Content-Type": "application/json", **(headers or {})})
        self.content_type = "application/json"
        self.reason = "synthetic response"
        self._body = json.dumps(payload or {}).encode()

    def body(self):
        return self._body

    def read(self):
        return self._body

    def json(self):
        return json.loads(self._body)

    def iter_bytes(self):
        yield self._body


class WireTransport(HttpTransport):
    """Runs real Azure SDK serialization/policies without making network calls."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def open(self):
        pass

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def send(self, request, **kwargs):
        self.requests.append(request)
        status, payload, headers = self.responses.pop(0)
        return WireResponse(request, status, payload, headers)


class Credential:
    def __init__(self):
        self.scopes = []

    def get_token(self, *scopes, **kwargs):
        self.scopes.append(scopes)
        return AccessToken("synthetic-not-a-real-token", 9999999999)


def test_table_uses_conditional_replace_not_upsert():
    client = Mock()
    client.update_entity.return_value = {"etag": 'W/"new"'}
    adapter = TableRepository(client)
    record = adapter.replace("team-one", "work:fixture", {"revision": 2}, 'W/"old"')
    call = client.update_entity.call_args.kwargs
    assert call["mode"] == UpdateMode.REPLACE
    assert call["match_condition"] == MatchConditions.IfNotModified
    assert call["etag"] == 'W/"old"'
    assert isinstance(call["entity"]["Payload"], bytes)
    assert record.etag == 'W/"new"'
    client.upsert_entity.assert_not_called()


def test_actual_table_sdk_sends_if_match():
    transport = WireTransport([(204, {}, {"etag": 'W/"after"'})])
    client = TableClient("https://synthetic.table.core.windows.net", "PocTable",
                         credential=Credential(), transport=transport, retry_total=0)
    record = TableRepository(client).replace("team-one", "work:fixture", {"revision": 2}, 'W/"before"')
    request = transport.requests[0]
    assert request.method == "PUT"
    assert request.headers["If-Match"] == 'W/"before"'
    assert "work%3Afixture" in request.url or "work:fixture" in request.url
    assert record.etag == 'W/"after"'


def test_actual_table_sdk_create_conflict_and_stale_etag():
    transport = WireTransport([
        (409, {"odata.error": {"code": "EntityAlreadyExists", "message": {"value": "exists"}}},
         {"x-ms-error-code": "EntityAlreadyExists"}),
        (412, {"odata.error": {"code": "UpdateConditionNotSatisfied", "message": {"value": "stale"}}},
         {"x-ms-error-code": "UpdateConditionNotSatisfied"}),
    ])
    client = TableClient("https://synthetic.table.core.windows.net", "PocTable",
                         credential=Credential(), transport=transport, retry_total=0)
    repository = TableRepository(client)
    with pytest.raises(Problem) as create_error:
        repository.create("team-one", "work:fixture", {"revision": 1})
    assert create_error.value.status == 409
    with pytest.raises(Problem) as update_error:
        repository.replace("team-one", "work:fixture", {"revision": 2}, 'W/"stale"')
    assert update_error.value.status == 412


def test_payload_exact_byte_limit():
    overhead = len(serialize({"x": ""}))
    assert len(serialize({"x": "x" * (MAX_PAYLOAD_BYTES - overhead)})) == MAX_PAYLOAD_BYTES
    with pytest.raises(Problem) as exc:
        serialize({"x": "x" * (MAX_PAYLOAD_BYTES - overhead + 1)})
    assert exc.value.status == 413


def test_real_memory_sdk_start_and_search_serialization():
    transport = WireTransport([
        (202, {"update_id": "native-update-1", "status": "queued"}, {}),
        (200, {"search_id": "search-1", "memories": [], "usage": {}}, {}),
        (200, {"update_id": "native-update-1", "status": "completed",
               "result": {"memory_operations": [{"kind": "created"}, {"kind": "updated"}],
                          "usage": {"input_tokens": 1919, "output_tokens": 103, "embedding_tokens": 62,
                                    "total_tokens": 2084}}}, {}),
    ])
    credential = Credential()
    client = AIProjectClient(
        endpoint=ENDPOINT, credential=credential, transport=transport,
        api_version="v1", retry_total=0, credential_scopes=["https://ai.azure.com/.default"],
    )
    memory = FoundryMemory(client, ENDPOINT, "poc-team-memory")
    start = memory.start("team:team-one", [{"role": "user", "type": "message",
                                         "content": [{"type": "input_text", "text": "synthetic fixture amber"}]}])
    assert start == {"update_id": "native-update-1", "status": "queued"}
    assert len(transport.requests) == 1  # no background polling / fake empty completion
    searched = memory.search("private:team-one:agent-a", "synthetic fixture")
    polled = memory.poll("native-update-1")
    assert searched["search_id"] == "search-1" and searched["memories"] == []
    assert polled["status"] == "completed"
    assert polled["result"]["memory_operations"] == [{"kind": "created"}, {"kind": "updated"}]
    assert polled["result"]["usage"]["total_tokens"] == 2084
    assert credential.scopes[0] == ("https://ai.azure.com/.default",)
    for request in transport.requests:
        assert request.headers["Foundry-Features"] == "MemoryStores=V1Preview"
        assert "api-version=v1" in request.url
    update = json.loads(transport.requests[0].body)
    search = json.loads(transport.requests[1].body)
    assert update["scope"] == "sc1-dGVhbTp0ZWFtLW9uZQ" and update["update_delay"] == 0
    assert update["items"][0]["content"][0]["type"] == "input_text"
    assert search["scope"] == "sc1-" + base64.urlsafe_b64encode(b"private:team-one:agent-a").decode().rstrip("=")
    assert search["options"]["max_memories"] == 10
    assert transport.requests[2].url == ENDPOINT + "/memory_stores/poc-team-memory/updates/native-update-1?api-version=v1"


@pytest.mark.parametrize("scope", ["team:one:two", "private:team-one:agent/a", "team/one", "team:../other"])
def test_native_scope_mapping_rejects_ambiguous_or_untrusted_scope_without_calling_sdk(scope):
    client = Mock()
    with pytest.raises(Problem) as exc:
        FoundryMemory(client, ENDPOINT, "poc-team-memory").search(scope, "synthetic")
    assert exc.value.body["error"]["code"] == "MEMORY_PROTOCOL_ERROR"
    client.beta.memory_stores.search_memories.assert_not_called()


def test_native_scope_satisfies_both_frontend_and_extractor_contract_without_collisions():
    scopes = ["team:a-b", "team:a_b", "private:a:b_c", "private:a_b:c",
              "private:" + "a" * 64 + ":" + "b" * 64]
    encoded = [native_memory_scope(scope) for scope in scopes]
    assert len(set(encoded)) == len(scopes)
    for scope, wire in zip(scopes, encoded):
        assert len(wire) <= 256 and re.fullmatch(r"[A-Za-z0-9_-]+", wire)
        raw = wire.removeprefix("sc1-")
        assert base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode() == scope


def test_memory_fixture_describes_a_durable_benign_preference_not_a_verification_code():
    payload = SyntheticMemory(marker="dashboard-example", value="sepia")
    text = payload.chat()[0]["content"][0]["text"]
    assert "verification code" not in text
    assert "color theme" in text and "long-term" in text
    assert payload.marker in text and payload.value in text


def test_memory_errors_keep_service_code_not_tokens():
    client = Mock()
    error = HttpResponseError(message="native unavailable Bearer eyJabc.def.ghi")
    error.status_code = 403
    error.error = SimpleNamespace(code="AuthorizationFailed", message="native unavailable Bearer eyJabc.def.ghi")
    client.beta.memory_stores.search_memories.side_effect = error
    with pytest.raises(Problem) as exc:
        FoundryMemory(client, ENDPOINT, "poc-team-memory").search("team:team-one", "synthetic")
    data = exc.value.body["error"]
    assert exc.value.status == 502 and data["upstream_status"] == 403
    assert data["upstream_code"] == "AuthorizationFailed"
    assert "eyJabc" not in json.dumps(data)
    assert safe_json({"access_token": "synthetic-secret", "Authorization": "foo"}) == {
        "access_token": "[REDACTED]", "Authorization": "[REDACTED]",
    }


def test_both_runtime_manifests_explicitly_pin_httpx_and_sdk_imports():
    from pathlib import Path
    import httpx
    from azure.ai.projects import AIProjectClient
    from azure.ai.agentserver.invocations import InvocationAgentServerHost
    from azure.ai.agentserver.responses import ResponsesAgentServerHost
    from importlib.metadata import version

    source = Path(__file__).resolve().parents[2] / "src" / "fa-shared-context-poc"
    for manifest in ("requirements.txt", "requirements-hosted.txt"):
        assert "httpx==0.28.1" in (source / manifest).read_text().splitlines()
    assert httpx.__version__ == "0.28.1"
    for package, pinned in (
        ("azure-ai-agentserver-core", "2.2.0"), ("azure-ai-agentserver-responses", "2.2.0"),
        ("azure-ai-agentserver-invocations", "1.2.0"), ("github-copilot-sdk", "1.0.13"),
    ):
        assert f"{package}=={pinned}" in (source / "requirements-hosted.txt").read_text().splitlines()
        assert version(package) == pinned
    assert "agentserver" not in (source / "requirements.txt").read_text()
    assert AIProjectClient is not None and InvocationAgentServerHost is not None and ResponsesAgentServerHost is not None
