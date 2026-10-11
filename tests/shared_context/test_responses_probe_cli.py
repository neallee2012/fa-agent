"""The operator script is executed only against synthetic HTTP/MI boundaries."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from shared_context.responses_probe import (
    HEADER, INPUTS, PROFILE, PROBE_AGENT_ID, PROBE_AGENT_NAME,     digest_input,
)

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "n6-responses-probe.py"
spec = importlib.util.spec_from_file_location("n6_probe_cli", SCRIPT)
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)
BUILD = "b" * 64
REQUEST = "n6-" + "2" * 32
CID = "conv_synthetic"
SID = "n6-synthetic-session"


class Credential:
    def __init__(self, **kwargs):
        assert set(kwargs) <= {"client_id", "retry_total"}
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def get_token(self, scope):
        assert scope == "https://ai.azure.com/.default"
        return SimpleNamespace(token="PRIVATE-api-mi-token")


def version(**changes):
    return {
        "agent_guid": PROBE_AGENT_ID, "version": "12", "status": "active", "metadata": {"fa_build": BUILD},
        "definition": {"environment_variables": {"FA_RESPONSES_ENABLED": "1", "FA_N6_RESPONSES_PROBE": PROFILE,
                                                "FA_N6_RESPONSES_INTERRUPT_SESSION_ID": SID}},
        **changes,
    }


def agent(protocols=None):
    return {
        "object": "agent", "name": PROBE_AGENT_NAME, "id": PROBE_AGENT_NAME, "state": "enabled",
        "agent_endpoint": {"protocol_configuration": {"responses": {}} if protocols is None else protocols},
    }


def test_agent_object_preflight_is_one_read_and_does_not_patch():
    calls = []

    def server(request):
        calls.append(request)
        assert request.method == "GET" and request.url.path.endswith("/agents/" + PROBE_AGENT_NAME)
        return httpx.Response(200, json=agent({"invocations": {}, "responses": {}}))

    observed = execute("agent-object-read", server)
    assert len(calls) == 1 and observed["code"] == "N6_AGENT_CONFIRMED"
    assert observed["responses_enabled"] is True


def test_session_create_uses_one_api_mi_post_with_the_pinned_sdk_payload_and_path():
    from azure.ai.projects.models import VersionRefIndicator
    from azure.ai.projects.operations._operations import build_agents_create_session_request

    expected = build_agents_create_session_request(PROBE_AGENT_NAME)
    calls = []

    def server(request):
        calls.append(request)
        assert request.headers["Authorization"] == "Bearer PRIVATE-api-mi-token"
        assert "x-ms-user-identity" not in request.headers
        if request.url.path.endswith("/agents/" + PROBE_AGENT_NAME):
            return httpx.Response(200, json=agent())
        if request.url.path.endswith("/versions/12"):
            return httpx.Response(200, json=version())
        assert request.method == "POST" and request.url.path.endswith(httpx.URL(expected.url).path)
        assert request.url.params == httpx.URL(expected.url).params
        assert json.loads(request.content) == {
            "agent_session_id": SID,
            "version_indicator": VersionRefIndicator(agent_version="12").as_dict(),
        }
        return httpx.Response(201, json={
            "agent_session_id": SID, "status": "active",
            "version_indicator": VersionRefIndicator(agent_version="12").as_dict(),
        })

    observed = execute("session-create", server)
    assert [request.method for request in calls] == ["GET", "GET", "POST"]
    assert observed["credential_kind"] == "api_managed_identity"
    assert observed["agent_session_id"] == SID and observed["agent_version"] == "12"


def args(operation, *more):
    return cli.arguments([
        operation, "--request-id", REQUEST, "--session-id", SID, "--agent-version", "12",
        "--expected-build", BUILD, "--output", "unused.jsonl", *more,
    ])


def execute(operation, server, *more):
    def with_endpoint_preflight(request):
        if (operation in {"conversation-create", "response-create", "interrupt"}
                and request.url.path.endswith("/agents/" + PROBE_AGENT_NAME)):
            return httpx.Response(200, json=agent())
        return server(request)

    client = cli.ProbeClient(Credential(), transport=httpx.MockTransport(with_endpoint_preflight))
    try:
        return client.execute(args(operation, *more), "synthetic-team")
    finally:
        client.close()


def test_response_wire_has_flat_session_and_exact_headers_not_extra_body():
    requests = []

    def server(request):
        requests.append(request)
        if len(requests) == 1:
            assert request.method == "GET" and request.url.path.endswith("/versions/12")
            return httpx.Response(200, json=version())
        assert request.method == "POST" and request.url.path.endswith("/protocols/openai/responses")
        assert json.loads(request.content) == {
            "input": INPUTS["observe"], "conversation": CID, "agent_session_id": SID,
            "background": True, "store": True, "stream": False,
            "metadata": {"fa_agent_id": PROBE_AGENT_ID, "fa_team_id": "synthetic-team"},
        }
        assert request.headers[HEADER] == PROFILE
        assert request.headers["x-client-fa-rid"] == REQUEST
        assert request.headers["x-client-fa-cid"] == CID
        assert request.headers["x-client-fa-submit-digest"] == digest_input(INPUTS["observe"], CID)
        assert "x-agent-foundry-call-id" not in request.headers
        return httpx.Response(200, json={"id": "resp_original", "status": "in_progress"})

    observed = execute("response-create", server, "--conversation-id", CID)
    assert len(requests) == 2 and observed["response_id"] == "resp_original"
    assert observed["credential_kind"] == "api_managed_identity" and observed["call_id_present"] is False


@pytest.mark.parametrize("operation", ["conversation-read", "items-read"])
@pytest.mark.parametrize("status", [200, 401, 403, 404, 302, 502])
def test_d3_api_reads_once_without_session_routing_retry_redirect_or_caller_header(operation, status):
    requests = []

    def server(request):
        requests.append(request)
        assert request.method == "GET" and request.content == b""
        assert "/agents/approved-test-x/endpoint/protocols/openai/conversations/" + CID in request.url.path
        assert "agent_session_id" not in request.url.params
        assert "x-agent-foundry-call-id" not in request.headers
        return httpx.Response(status, json={
            "id": CID, "metadata": {"fa_agent_id": PROBE_AGENT_ID, "fa_team_id": "synthetic-team",
                                   "private": "PRIVATE-metadata"},
            "data": [{"content": "PRIVATE-conversation"}],
        }, headers={"Location": "https://never-follow.invalid", "x-ms-request-id": "original-http-request"})

    observed = execute(operation, server, "--conversation-id", CID, "--reader-agent-name", "approved-test-x")
    assert len(requests) == 1 and observed["http_status"] == status
    assert "PRIVATE-" not in json.dumps(observed)
    assert observed["code"] == ("N6_OBSERVED" if status == 200 else "N6_STOP")
    assert observed["metadata"] == {"fa_agent_id": PROBE_AGENT_ID, "fa_team_id": "synthetic-team"}


def test_d3_agent_refusal_retains_credential_and_call_id_presence_not_opaque_value():
    requests = []

    def server(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(200, json=version())
        assert json.loads(request.content) == {"operation": "read_conversation", "request_id": REQUEST}
        return httpx.Response(403, json={
            "code": "N6_D3_STOP", "http_status": 403, "body_sha256": "a" * 64,
            "request_id": REQUEST, "conversation_id": CID, "credential_kind": "agent_managed_identity",
            "identity_kind": "agent_bearer_with_platform_caller_context", "call_id_present": True,
            "target_metadata": {"fa_agent_id": PROBE_AGENT_ID, "fa_team_id": "synthetic-team"},
            "target_metadata_source": "synthetic_probe_record", "private": "PRIVATE-opaque-value",
        })

    observed = execute("agent-read", server)
    assert len(requests) == 2 and observed["code"] == "N6_STOP"
    assert observed["agent_read"]["call_id_present"] is True
    assert observed["agent_read"]["identity_kind"] == "agent_bearer_with_platform_caller_context"
    assert "PRIVATE-" not in json.dumps(observed)


@pytest.mark.parametrize("message", [
    "http_status=429; call_id_present=true; identity_kind=not_observed",
    "exception_type=ServiceRequestError; call_id_present=false; identity_kind=not_observed",
])
def test_d3_agent_unknown_keeps_safe_reason_and_stops_without_another_post(message):
    requests = []

    def server(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(200, json=version())
        return httpx.Response(503, json={"error": {
            "code": "N6_PROBE_READ_UNKNOWN", "message": message,
            "reason": message.split(";")[0], "identity_kind": "not_observed",
            "call_id_present": "call_id_present=true" in message,
        }})

    observed = execute("agent-read", server)
    assert [request.method for request in requests] == ["GET", "POST"]
    assert observed["http_status"] == 503 and observed["code"] == "N6_STOP"
    assert observed["error"] == {"code": "N6_PROBE_READ_UNKNOWN", "message": message}
    assert "agent_read" not in observed
    assert observed["call_id_present"] is False and observed["credential_kind"] == "api_managed_identity"


@pytest.mark.parametrize("change", [
    {"agent_guid": "another-agent"}, {"version": "13"}, {"status": "creating"},
    {"metadata": {"fa_build": "c" * 64}}, {"definition": {"environment_variables": {}}},
])
def test_write_requires_exact_test_agent_active_version_same_build_and_explicit_flags(change):
    requests = []

    def server(request):
        requests.append(request)
        return httpx.Response(200, json=version(**change))

    with pytest.raises(cli.Problem) as error:
        execute("conversation-create", server)
    assert error.value.body["error"]["code"] in {"N6_VERSION_MISMATCH", "N6_VERSION_SHAPE_UNKNOWN"}
    assert len(requests) == 1


def test_fixed_write_target_cannot_be_replaced_by_a_reader_name():
    with pytest.raises(ValueError):
        args("response-create", "--conversation-id", CID, "--reader-agent-name", "another-agent")


def test_output_intent_precedes_write_unknown_is_retained_and_same_file_prevents_resend(tmp_path, monkeypatch, capsys):
    import azure.identity

    output = tmp_path / "one-operation.jsonl"
    created = tmp_path / "conversation.jsonl"
    created.write_text("\n".join(json.dumps(row) for row in (
        {"operation": "conversation-create", "profile": PROFILE},
        {"code": "N6_OBSERVED", "http_status": 200, "conversation_id": CID,
         "metadata": {"fa_agent_id": PROBE_AGENT_ID, "fa_team_id": "synthetic-team"}},
    )), encoding="utf-8")
    session = tmp_path / "session.jsonl"
    session.write_text("\n".join(json.dumps(row) for row in (
        {"operation": "session-create", "profile": PROFILE, "session_id": SID,
         "agent_version": "12", "expected_build": BUILD},
        {"code": "N6_OBSERVED", "http_status": 201, "agent_session_id": SID,
         "credential_kind": "api_managed_identity", "agent_version": "12"},
    )), encoding="utf-8")
    requests = []
    base_client = cli.ProbeClient
    argv = [
        "response-create", "--request-id", REQUEST, "--session-id", SID, "--agent-version", "12",
        "--expected-build", BUILD, "--conversation-id", CID, "--conversation-record", str(created),
        "--session-record", str(session), "--output", str(output),
    ]

    def server(request):
        intent = json.loads(output.read_text(encoding="utf-8").splitlines()[0])
        assert intent["status"] == "intent" and intent["request_id"] == REQUEST
        requests.append(request)
        if request.url.path.endswith("/agents/" + PROBE_AGENT_NAME):
            return httpx.Response(200, json=agent())
        if request.method == "GET":
            return httpx.Response(200, json=version())
        raise httpx.ReadTimeout("PRIVATE-lost-ack", request=request)

    monkeypatch.setattr(cli, "code_sha256", lambda: BUILD)
    monkeypatch.setattr(cli, "load_configuration", lambda: SimpleNamespace(
        identity=SimpleNamespace(managed_identity_client_id="11111111-1111-1111-1111-111111111111",
                                 team_id="synthetic-team")))
    monkeypatch.setattr(azure.identity, "ManagedIdentityCredential", Credential)
    monkeypatch.setattr(cli, "ProbeClient", lambda credential: base_client(credential, transport=httpx.MockTransport(server)))
    assert cli.main(argv) == 2 and len(requests) == 3
    original = output.read_bytes()
    rows = [json.loads(line) for line in original.splitlines()]
    assert rows[-1] == {"code": "N6_OUTCOME_UNKNOWN", "status": "unknown"}
    assert cli.main(argv) == 2 and len(requests) == 3
    assert output.read_bytes() == original
    assert "PRIVATE-" not in capsys.readouterr().out and b"PRIVATE-" not in original


def test_old_or_unknown_conversation_cannot_be_used_as_a_probe_target(tmp_path):
    command = args("response-create", "--conversation-id", CID)
    with pytest.raises(ValueError):
        cli.bind_conversation(command, "synthetic-team")
    record = tmp_path / "unknown.jsonl"
    record.write_text('{"operation":"conversation-create","profile":"' + PROFILE
                      + '"}\n{"code":"N6_OUTCOME_UNKNOWN"}', encoding="utf-8")
    command.conversation_record = record
    with pytest.raises(ValueError):
        cli.bind_conversation(command, "synthetic-team")


def test_session_read_uses_the_pinned_projects_sdk_endpoint_builder():
    from azure.ai.projects.operations._operations import build_agents_get_session_request

    expected = build_agents_get_session_request(PROBE_AGENT_NAME, SID)
    calls = []

    def server(request):
        calls.append(request)
        assert request.url.path.endswith(httpx.URL(expected.url).path)
        assert request.url.params == httpx.URL(expected.url).params
        return httpx.Response(200, json={
            "agent_session_id": SID, "status": "idle",
            "version_indicator": {"type": "version_ref", "agent_version": "12"},
        })

    observed = execute("session-read", server)
    assert len(calls) == 1 and observed["agent_session_id"] == SID and observed["agent_version"] == "12"


def test_interrupt_requires_observed_pid_and_dedicated_session():
    with pytest.raises(ValueError):
        args("interrupt")
    requests = []

    def server(request):
        requests.append(request)
        body = version()
        body["definition"]["environment_variables"]["FA_NATIVE_TURNS"] = "foundry-state-v1"
        return httpx.Response(200, json=body)

    with pytest.raises(cli.Problem) as error:
        execute("interrupt", server, "--process-id", "1234")
    assert error.value.body["error"]["code"] == "N6_INTERRUPT_SESSION_MISMATCH"
    assert len(requests) == 1


@pytest.mark.parametrize("body", [[], {}, {"id": "resp_original"}, {"id": "resp_original", "status": "completed", "output": [None]}])
def test_malformed_success_is_unknown_with_http_hash_not_success_or_private_dump(body):
    requests = []

    def server(request):
        requests.append(request)
        return httpx.Response(200, json=body)

    observed = execute("response-read", server, "--response-id", "resp_original")
    assert len(requests) == 1 and observed["code"] == "N6_OUTCOME_UNKNOWN"
    assert observed["http_status"] == 200 and len(observed["body_sha256"]) == 64


@pytest.mark.parametrize("body", [[], {"definition": []}, {"agent_guid": 1, "metadata": {}, "definition": {}}])
def test_malformed_version_read_stops_before_post_and_retains_its_http_hash(body):
    requests = []

    def server(request):
        requests.append(request)
        return httpx.Response(200, json=body)

    with pytest.raises(cli.Problem) as error:
        execute("conversation-create", server)
    assert len(requests) == 1 and error.value.body["error"]["code"] == "N6_VERSION_SHAPE_UNKNOWN"
    assert error.value.body["error"]["observation"]["http_status"] == 200


@pytest.mark.parametrize("protocols,code", [
    ({"invocations": {}, "invocations_ws": {}}, "N6_RESPONSES_PROTOCOL_DISABLED"),
    ({}, "N6_AGENT_PROTOCOL_UNKNOWN"),
    ([], "N6_AGENT_PROTOCOL_UNKNOWN"),
    ({"responses": None}, "N6_AGENT_PROTOCOL_UNKNOWN"),
    ({"responses": {"enabled": True}}, "N6_AGENT_PROTOCOL_UNKNOWN"),
])
def test_session_create_stops_at_an_invocations_only_or_unknown_endpoint(protocols, code):
    calls = []
    body = agent()
    body["agent_endpoint"]["protocol_configuration"] = protocols

    def server(request):
        calls.append(request)
        return httpx.Response(200, json=body)

    with pytest.raises(cli.Problem) as refused:
        execute("session-create", server)
    assert refused.value.body["error"]["code"] == code
    assert len(calls) == 1 and calls[0].method == "GET"
    assert "protocol_configuration" not in json.dumps(refused.value.body)


@pytest.mark.parametrize("status", [401, 403, 404, 409, 429, 502])
def test_session_refusal_has_safe_error_details_and_never_reposts(status):
    calls = []
    secret = "PRIVATE-access-token"
    call_id = "PRIVATE-platform-call"
    message = ('The request was refused; Bearer ' + secret
               + '; x-agent-foundry-call-id="' + call_id + '"'
               + '; URL=https://example.invalid/path?api-key=PRIVATE-query&call_id=other '
               + "diagnostic " * 100)

    def server(request):
        calls.append(request)
        if request.url.path.endswith("/agents/" + PROBE_AGENT_NAME):
            return httpx.Response(200, json=agent())
        if request.url.path.endswith("/versions/12"):
            return httpx.Response(200, json=version())
        return httpx.Response(status, json={"error": {
            "code": "session_not_accessible", "message": message, "details": {"secret": "PRIVATE-detail"}}},
            headers={"apim-request-id": "original-apim", "x-request-id": "original-request",
                     "Set-Cookie": "PRIVATE-cookie"})

    observed = execute("session-create", server)
    assert [request.method for request in calls] == ["GET", "GET", "POST"]
    assert observed["code"] == ("N6_OUTCOME_UNKNOWN" if status >= 500 else "N6_STOP")
    assert observed["http_status"] == status and observed["error"]["code"] == "session_not_accessible"
    assert 0 < len(observed["error"]["message"]) <= 300
    assert observed["apim-request-id"] == "original-apim" and observed["x-request-id"] == "original-request"
    assert "PRIVATE-" not in json.dumps(observed) and "details" not in observed["error"]


@pytest.mark.parametrize("message", [
    'prefix "Cookie": "one=PRIVATE-one; two=PRIVATE-two"',
    "prefix Set-Cookie: one=PRIVATE-one; two=PRIVATE-two",
    'prefix access_token="PRIVATE-token" call_id is "PRIVATE-call" https://u:PRIVATE-pass@host/x?a=PRIVATE-query',
    "prefix Basic PRIVATE-basic; bearer PRIVATE-bearer; eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJzZWNyZXQifQ.PRIVATE_signature",
])
def test_error_redaction_precedes_truncation_and_covers_quoted_headers(message):
    response = httpx.Response(403, json={"error": {"code": "forbidden", "message": message + " filler" * 100}})
    observed = cli.summarize_response(response)
    assert len(observed["error"]["message"]) <= 300 and "PRIVATE" not in json.dumps(observed)
    assert set(observed) == {"http_status", "body_sha256", "error"}


@pytest.mark.parametrize("change", ["operator", "get-only", "unknown", "other-sid"])
def test_response_requires_same_api_mi_session_creation_not_just_a_session_get(tmp_path, change):
    command = args("response-create", "--conversation-id", CID)
    rows = [
        {"operation": "session-create", "profile": PROFILE, "session_id": SID,
         "agent_version": "12", "expected_build": BUILD},
        {"code": "N6_OBSERVED", "http_status": 201, "agent_session_id": SID,
         "credential_kind": "api_managed_identity", "agent_version": "12"},
    ]
    if change == "operator":
        rows[1]["credential_kind"] = "AzureCliCredential"
    elif change == "get-only":
        rows[0]["operation"], rows[1]["http_status"] = "session-read", 200
    elif change == "unknown":
        rows[1]["code"] = "N6_OUTCOME_UNKNOWN"
    else:
        rows[1]["agent_session_id"] = "another-session"
    command.session_record = tmp_path / "session.jsonl"
    command.session_record.write_text("\n".join(map(json.dumps, rows)), encoding="utf-8")
    with pytest.raises(ValueError):
        cli.bind_session(command)


def test_session_create_unknown_preserves_intent_and_allows_no_second_post(tmp_path, monkeypatch, capsys):
    import azure.identity

    output = tmp_path / "session-create.jsonl"
    argv = ["session-create", "--request-id", REQUEST, "--session-id", SID, "--agent-version", "12",
            "--expected-build", BUILD, "--output", str(output)]
    requests = []
    real = cli.ProbeClient

    def server(request):
        row = json.loads(output.read_text(encoding="utf-8").splitlines()[0])
        assert row["session_id"] == SID and row["credential_kind"] == "api_managed_identity"
        requests.append(request)
        if request.url.path.endswith("/agents/" + PROBE_AGENT_NAME):
            return httpx.Response(200, json=agent())
        if request.url.path.endswith("/versions/12"):
            return httpx.Response(200, json=version())
        raise httpx.ReadTimeout("PRIVATE-uncertain-create", request=request)

    monkeypatch.setattr(cli, "code_sha256", lambda: BUILD)
    monkeypatch.setattr(cli, "load_configuration", lambda: SimpleNamespace(identity=SimpleNamespace(
        managed_identity_client_id="11111111-1111-1111-1111-111111111111", team_id="synthetic-team")))
    monkeypatch.setattr(azure.identity, "ManagedIdentityCredential", Credential)
    monkeypatch.setattr(cli, "ProbeClient", lambda credential: real(credential, transport=httpx.MockTransport(server)))
    assert cli.main(argv) == 2
    before = output.read_bytes()
    assert json.loads(before.splitlines()[-1])["code"] == "N6_OUTCOME_UNKNOWN"
    assert cli.main(argv) == 2 and output.read_bytes() == before
    assert [request.method for request in requests] == ["GET", "GET", "POST"]
    assert "PRIVATE" not in capsys.readouterr().out


def test_direct_quoted_call_id_is_redacted_before_any_output():
    response = httpx.Response(403, json={"error": {
        "code": "session_not_accessible",
        "message": "The call ID 'PRIVATE-call-one' and x-agent-foundry-call-id \"PRIVATE-call-two\" were refused",
    }})
    observed = cli.summarize_response(response)
    assert "PRIVATE" not in json.dumps(observed)
    assert observed["error"]["code"] == "session_not_accessible"


@pytest.mark.parametrize("mismatch", ["version", "build"])
def test_session_origin_version_and_build_must_match_this_probe_before_any_post(tmp_path, mismatch):
    command = args("response-create", "--conversation-id", CID)
    rows = [
        {"operation": "session-create", "profile": PROFILE, "session_id": SID,
         "agent_version": "12", "expected_build": BUILD},
        {"code": "N6_OBSERVED", "http_status": 201, "agent_session_id": SID,
         "agent_version": "12", "credential_kind": "api_managed_identity"},
    ]
    command.session_record = tmp_path / "session.jsonl"
    command.session_record.write_text("\n".join(map(json.dumps, rows)), encoding="utf-8")
    if mismatch == "version":
        command.agent_version = "13"
    else:
        command.expected_build = "c" * 64
    with pytest.raises(ValueError):
        cli.bind_session(command)
