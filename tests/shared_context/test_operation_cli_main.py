"""The operation CLI's exact arguments, output, phases, exit codes and attempt bytes, offline."""

from datetime import datetime, timezone
import hashlib
import importlib
import json
import logging
import os
from pathlib import Path
import ssl
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

from azure.core.exceptions import ClientAuthenticationError
import httpx
import pytest

from shared_context.behavior_operations import OperationDescription, ResolvedResource, control_key
from shared_context.behavior_runtime import material
from shared_context.behavior_wire import OperationApproval, OperationRejection, OperationRevocation

API = "https://poc.example.westus3.azurecontainerapps.io"
TENANT = "11111111-1111-1111-1111-111111111111"
AUDIENCE = "api://22222222-2222-2222-2222-222222222222"
CLIENT = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
BINDING, OPERATION, GRANT, DECISION = "binding-one", "operation-one", "grant-one", "decision-one"
PATH = f"/behavior/v1/bindings/{BINDING}/operation"
READ, APPROVE = AUDIENCE + "/Behavior.Read", AUDIENCE + "/Behavior.Approve"
TOKEN = "SYNTHETIC_TOKEN"
CONTRACT = "a" * 64
UNCONFIRMED_MESSAGE = "未取得確認結果；保留原 request／attempt 檔。若已有 attempt，只能使用 reconcile。"
DESCRIPTION = OperationDescription(
    adapter_id="reviewed-adapter", adapter_version="adapter-v1", kind="request", method="POST",
    target="logical-target", content=b"original", resources=(ResolvedResource(resource_id="resource", version="v1"),),
)
MATERIAL = material(DESCRIPTION.model_dump(mode="json"))
PREVIEW = {
    "operation_id": OPERATION, "content": MATERIAL.model_dump(mode="json"), "description_digest": MATERIAL.sha256,
    "adapter_contract_digest": CONTRACT, "effect_state": "unknown", "execution_window_contract": "window-v1",
    "canonicalization_version": "fa-operation-v1", "strict_service_commit_time_expiry_supported": False,
}
SHORT_VIEW = {
    "operation_id": OPERATION, "adapter_id": "reviewed-adapter", "method": "POST", "target": "logical-target",
    "content_utf8": "original", "description_digest": MATERIAL.sha256, "adapter_contract_digest": CONTRACT,
    "effect_state": "unknown", "rejection_recorded": False,
}
FULL_VIEW = {
    "operation_id": OPERATION, "description": DESCRIPTION.model_dump(mode="json"),
    "content_view": {"byte_length": 8, "sha256": hashlib.sha256(b"original").hexdigest(),
                     "encoding": "utf-8", "text": "original"},
    "dependency_views": [], "material_byte_length": len(MATERIAL.data), "description_digest": MATERIAL.sha256,
    "adapter_contract_digest": CONTRACT, "effect_state": "unknown", "rejection_recorded": False,
    "execution_window_contract": "window-v1", "strict_service_commit_time_expiry_supported": False,
}
OBSERVED = {
    "schema_version": "fa-operation-decision-state-v1", "purpose": "observation-only-not-operation-permission",
    "binding_id": BINDING, "operation_id": OPERATION, "description_digest": MATERIAL.sha256,
    "adapter_contract_digest": CONTRACT, "observed_at": "2026-11-01T00:00:00Z", "current_policy_revision": 1,
    "effect_state": "unknown", "business_state": "pending", "current_attempt_id": None, "unsettled_attempt_ids": [],
    "grant": {
        "grant_id": GRANT, "approval_id": DECISION, "not_before": "2026-11-01T00:00:00Z",
        "expires_at": "2026-12-01T00:00:00Z", "policy_revision": 1, "revocation_revision": 0, "revoked": False,
        "remaining_uses": 1, "remaining_units": 1,
    },
    "rejection": None,
}
COMMANDS = {
    "approve": OperationApproval(
        request_id="request-one", decision_id=DECISION, description_digest=MATERIAL.sha256,
        adapter_contract_digest=CONTRACT, expires_at=datetime(2026, 12, 1, tzinfo=timezone.utc)),
    "reject": OperationRejection(
        request_id="request-one", schema_version="fa-operation-rejection-v1", decision_id=DECISION,
        description_digest=MATERIAL.sha256, adapter_contract_digest=CONTRACT),
    "revoke": OperationRevocation(
        request_id="request-one", decision_id=DECISION, description_digest=MATERIAL.sha256,
        expected_revocation_revision=0),
}
ATTEMPT_VERSIONS = {
    "approve": "fa-operation-approval-cli-attempt-v1",
    "reject": "fa-operation-rejection-cli-attempt-v2",
    "revoke": "fa-operation-revocation-cli-attempt-v1",
}
RECEIPTS = {
    "approve": {
        "result": {"approval_id": DECISION, "grant_id": GRANT, "description_digest": MATERIAL.sha256, "approved_uses": 1},
        "control_request_id": control_key("runtime-operation-approve", BINDING, DECISION),
        "control_request_digest": "b" * 64,
    },
    "reject": {"result": {
        "rejected": True, "operation_id": OPERATION, "decision_id": DECISION, "description_digest": MATERIAL.sha256,
    }},
    "revoke": {
        "result": {"grant_id": GRANT, "revoked": True, "revocation_revision": 1},
        "control_request_id": control_key("runtime-operation-revoke", BINDING, DECISION),
        "control_request_digest": "b" * 64,
    },
}
CONFIRMED = {
    "approve": {
        "state": "confirmed", "purpose": "original-approval-receipt-not-current-permission",
        "result": {"operation_id": OPERATION, **RECEIPTS["approve"]["result"]},
    },
    "reject": {"state": "confirmed", "result": RECEIPTS["reject"]["result"]},
    "revoke": {
        "state": "confirmed", "purpose": "original-revocation-receipt-not-effect-outcome",
        "result": {"operation_id": OPERATION, **RECEIPTS["revoke"]["result"]},
    },
}
PROMPTS = {
    "approve": ("輸入 approve 批准以上精確操作及UTC期限／次數／單位；後續代理可依原grant執行。"
                "此CLI不執行、不退款，也不把unknown改為未發生：\n"),
    "reject": "輸入 reject 拒絕以上精確操作；不取消工作、不退款，也不宣稱效果未發生：\n",
    "revoke": "輸入 revoke 撤銷以上原grant及revision；不取消工作、不退款或逆轉已發生／未知效果：\n",
}
RECONCILE_ROUTES = {
    "approve": "/approval-reconcile", "reject": "/rejection-reconcile", "revoke": "/revocation-reconcile",
}
CHECKED_PREVIEWS = {"approve": FULL_VIEW, "reject": SHORT_VIEW, "revoke": FULL_VIEW}


def canonical(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")


def body(decision):
    return COMMANDS[decision].model_dump(mode="json")


def attempt_material(decision):
    target = {"operation_id": OPERATION, **({"grant_id": GRANT} if decision == "revoke" else {})}
    return {
        "schema_version": ATTEMPT_VERSIONS[decision], "api": API, "tenant": TENANT, "audience": AUDIENCE,
        "client_id": CLIENT, "binding_id": BINDING,
        "command_sha256": hashlib.sha256(canonical(body(decision))).hexdigest(), "command": body(decision), **target,
    }


def printed(text):
    """Every JSON document on stdout, in order."""
    decoder, position, documents = json.JSONDecoder(), 0, []
    while position < len(text):
        value, position = decoder.raw_decode(text, position)
        assert text[position] == "\n"
        position += 1
        documents.append(value)
    return documents


def unconfirmed(kind, phase):
    return {"error": {"code": "OPERATION_CLI_UNCONFIRMED", "type": kind, "message": UNCONFIRMED_MESSAGE}, "phase": phase}


def unavailable(status, phase):
    return {
        "error": {"code": "OPERATION_REQUEST_UNCONFIRMED", "message": "no confirmed result; preserve the original request",
                  "upstream_status": status},
        "phase": phase,
    }


def exactly(document):
    return json.dumps(document, ensure_ascii=True) + "\n"


class Operator:
    """The real main() against a recorded in-memory API and credential; nothing leaves the process."""

    def __init__(self, cli, monkeypatch, tmp_path):
        self.cli, self.request = cli, tmp_path / "original.json"
        self.attempt = tmp_path / "original.json.attempt.json"
        self.calls, self.bodies, self.events, self.prompts = [], [], [], []
        self.client_options, self.credential_options = [], []
        self.answer = None
        self.routes = {("GET", PATH): PREVIEW, ("GET", PATH + "/decision-state"): OBSERVED}
        self.credential = MagicMock()
        self.credential.__enter__.return_value = self.credential
        self.credential.authenticate.side_effect = lambda **_options: self.events.append("authenticate")
        self.credential.get_token.side_effect = self.token
        native = httpx.Client

        def client(**options):
            self.client_options.append(options)
            return native(base_url=options["base_url"], transport=httpx.MockTransport(self.reply))

        def credential(**options):
            self.credential_options.append(options)
            return self.credential

        def confirm(prompt):
            self.prompts.append(prompt)
            self.events.append("confirm")
            if isinstance(self.answer, Exception):
                raise self.answer
            return self.answer

        monkeypatch.setattr(cli, "InteractiveBrowserCredential", credential)
        monkeypatch.setattr(cli.httpx, "Client", client)
        monkeypatch.setattr("builtins.input", confirm)
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        for name in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"):
            monkeypatch.delenv(name, raising=False)

    def token(self, *scopes):
        self.events.append(("token", scopes))
        return SimpleNamespace(token=TOKEN)

    def reply(self, request):
        method, path = request.method, request.url.path
        self.calls.append((method, path))
        self.events.append((method, path))
        assert request.headers["authorization"] == "Bearer " + TOKEN
        if method == "POST":
            self.bodies.append(request.content)
            self.events.append(("attempt saved", self.attempt.exists()))
        value = self.routes[method, path]
        if isinstance(value, Exception):
            raise value
        return value if isinstance(value, httpx.Response) else httpx.Response(200, json=value)

    def arguments(self, action):
        files = ["--request-file", str(self.request)] if action in (*ATTEMPT_VERSIONS, "reconcile") else []
        return ["--api", API, "--tenant", TENANT, "--audience", AUDIENCE, "--client-id", CLIENT, action, BINDING, *files]

    def run(self, action):
        return self.cli.main(self.arguments(action))

    def prepare(self, decision, *, receipt=True):
        self.request.write_bytes(canonical(body(decision)))
        self.answer = decision
        if receipt:
            self.routes["POST", PATH + "/" + decision] = RECEIPTS[decision]

    def saved(self, decision, **changes):
        self.attempt.write_bytes(canonical({**attempt_material(decision), **changes}))
        self.routes["POST", PATH + RECONCILE_ROUTES[decision]] = RECEIPTS[decision]


@pytest.fixture
def cli():
    return importlib.import_module("shared_context.behavior_operation_cli")


@pytest.fixture
def operator(cli, monkeypatch, tmp_path):
    return Operator(cli, monkeypatch, tmp_path)


@pytest.fixture
def terminal(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["behavior_operation_cli"])
    monkeypatch.setenv("COLUMNS", "80")
    for name in ("CONTEXT_API_URL", "CONTEXT_TENANT_ID", "CONTEXT_API_AUDIENCE", "CONTEXT_BEHAVIOR_CLIENT_ID"):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("argv,expected", [
    (["show", BINDING], {"action": "show", "binding": BINDING}),
    (["state", BINDING], {"action": "state", "binding": BINDING}),
    *[([name, BINDING, "--request-file", "request.json"],
       {"action": name, "binding": BINDING, "request_file": Path("request.json")}) for name in (*ATTEMPT_VERSIONS, "reconcile")],
])
def test_each_action_parses_to_exactly_its_fields(cli, terminal, argv, expected):
    assert vars(cli.parser().parse_args([*CONFIGURED, *argv])) == {
        "api": API, "tenant": TENANT, "audience": AUDIENCE, "client_id": CLIENT, **expected,
    }


def test_connection_options_default_to_none_without_the_environment(cli, terminal):
    assert vars(cli.parser().parse_args(["show", BINDING])) == {
        "api": None, "tenant": None, "audience": None, "client_id": None, "action": "show", "binding": BINDING,
    }


def test_connection_options_default_to_their_environment_variables(cli, terminal, monkeypatch):
    for name, value in (("API_URL", "a"), ("TENANT_ID", "t"), ("API_AUDIENCE", "u"), ("BEHAVIOR_CLIENT_ID", "c")):
        monkeypatch.setenv("CONTEXT_" + name, value)
    assert vars(cli.parser().parse_args(["show", BINDING])) == {
        "api": "a", "tenant": "t", "audience": "u", "client_id": "c", "action": "show", "binding": BINDING,
    }


CONFIGURED = ["--api", API, "--tenant", TENANT, "--audience", AUDIENCE, "--client-id", CLIENT]
CONFIGURATION_ERROR = (
    "behavior_operation_cli: error: configure the API origin, tenant, audience and existing delegated behavior client ID\n"
)


@pytest.mark.parametrize("argv,expected", [
    ([], "required: action"),
    ([*CONFIGURED, "show"], "required: binding"),
    ([*CONFIGURED, "reject", BINDING], "required: --request-file"),
    ([*CONFIGURED, "show", BINDING, "--request-file", "unused.json"], "unrecognized arguments: --request-file unused.json"),
    (["show", BINDING], CONFIGURATION_ERROR),
    *[pytest.param([*CONFIGURED[:index], *CONFIGURED[index + 2:], "show", BINDING], CONFIGURATION_ERROR,
                   id="missing " + CONFIGURED[index]) for index in range(0, 8, 2)],
])
def test_usage_errors_exit_two_on_stderr_before_any_connection(cli, terminal, operator, capsys, argv, expected):
    with pytest.raises(SystemExit) as stopped:
        cli.main(argv)
    output = capsys.readouterr()
    assert stopped.value.code == 2
    assert output.out == "" and expected in output.err
    assert operator.credential_options == [] and operator.calls == []


def test_an_unknown_action_is_a_usage_error(cli, terminal, capsys):
    with pytest.raises(SystemExit) as stopped:
        cli.main([*CONFIGURED, "execute", BINDING])
    assert stopped.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_connection_defaults_come_from_the_environment_and_flags_override_them(cli, operator, monkeypatch, capsys):
    monkeypatch.setenv("CONTEXT_API_URL", "https://environment.azurecontainerapps.io")
    monkeypatch.setenv("CONTEXT_TENANT_ID", "33333333-3333-3333-3333-333333333333")
    monkeypatch.setenv("CONTEXT_API_AUDIENCE", "api://44444444-4444-4444-4444-444444444444")
    monkeypatch.setenv("CONTEXT_BEHAVIOR_CLIENT_ID", "55555555-5555-5555-5555-555555555555")
    assert cli.main(["show", BINDING]) == 0
    assert operator.client_options[0]["base_url"] == "https://environment.azurecontainerapps.io"
    assert operator.credential_options[0]["tenant_id"] == "33333333-3333-3333-3333-333333333333"
    assert operator.credential_options[0]["client_id"] == "55555555-5555-5555-5555-555555555555"
    assert operator.credential.authenticate.call_args.kwargs["scopes"] == [
        "api://44444444-4444-4444-4444-444444444444/Behavior.Read",
    ]
    assert operator.run("show") == 0
    assert operator.client_options[1]["base_url"] == API
    assert operator.credential_options[1]["tenant_id"] == TENANT
    assert operator.credential_options[1]["client_id"] == CLIENT


def test_the_client_and_credential_are_built_exactly_once_with_the_private_settings(cli, operator, monkeypatch, capsys):
    trust = []
    native = ssl.create_default_context

    def context(*args, **kwargs):
        trust.append((args, kwargs))
        return native()

    monkeypatch.setattr(cli.ssl, "create_default_context", context)
    assert operator.run("show") == 0
    assert operator.credential_options == [{
        "tenant_id": TENANT, "client_id": CLIENT, "redirect_uri": "http://localhost:8400",
        "disable_automatic_authentication": True,
    }]
    [options] = operator.client_options
    transport = options.pop("transport")
    assert options == {"base_url": API, "trust_env": False, "follow_redirects": False, "timeout": 30}
    assert isinstance(transport, httpx.HTTPTransport) and transport._pool._retries == 0
    assert trust == [((), {"cafile": None})]


def test_authentication_runs_with_azure_and_msal_logging_made_private(cli, operator):
    children = [logging.getLogger(name + ".child") for name in ("azure", "msal")]
    seen = []
    operator.credential.authenticate.side_effect = lambda **_options: seen.append(
        [(logging.getLogger(name).propagate, child.disabled) for name, child in zip(("azure", "msal"), children)])
    assert operator.run("show") == 0
    assert seen == [[(False, True), (False, True)]]
    assert logging.getLogger("azure").propagate and not children[0].disabled


@pytest.mark.parametrize("environment,expected", [
    ({"SSL_CERT_FILE": "first.pem"}, "first.pem"),
    ({"REQUESTS_CA_BUNDLE": "second.pem"}, "second.pem"),
    ({"SSL_CERT_FILE": "first.pem", "REQUESTS_CA_BUNDLE": "second.pem"}, "first.pem"),
    ({"SSL_CERT_FILE": "", "REQUESTS_CA_BUNDLE": "second.pem"}, "second.pem"),
])
def test_the_ca_bundle_comes_from_the_environment_in_order(cli, operator, monkeypatch, capsys, environment, expected):
    trust = []
    native = ssl.create_default_context

    def context(cafile):
        trust.append(cafile)
        return native()

    monkeypatch.setattr(cli.ssl, "create_default_context", context)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    assert operator.run("show") == 0
    assert trust == [expected]


@pytest.mark.parametrize("api", [
    "http://poc.example.azurecontainerapps.io",
    "https://",
    "https://example.com",
    "https://poc.example.azurecontainerapps.io.outside.invalid",
    "https://user@poc.example.azurecontainerapps.io",
    "https://:secret@poc.example.azurecontainerapps.io",
    "https://poc.example.azurecontainerapps.io?query=1",
    "https://poc.example.azurecontainerapps.io#fragment",
    "https://poc.example.azurecontainerapps.io/base",
    "https://poc.example.azurecontainerapps.io:8443",
    "https://poc.example.azurecontainerapps.io:99999",
])
def test_only_the_exact_container_apps_origin_is_accepted(operator, capsys, api):
    args = operator.arguments("show")
    args[args.index("--api") + 1] = api
    assert operator.cli.main(args) == 1
    output = capsys.readouterr()
    assert output.out == "" and output.err == exactly(unconfirmed("ValueError", "validation"))
    assert operator.credential_options == [] and operator.calls == []


@pytest.mark.parametrize("api,base", [
    (API + "/", API), (API + ":443", API + ":443"), (API + ":443/", API + ":443"),
])
def test_the_equivalent_origin_forms_are_accepted_without_the_trailing_slash(operator, capsys, api, base):
    args = operator.arguments("show")
    args[args.index("--api") + 1] = api
    assert operator.cli.main(args) == 0
    assert operator.client_options[0]["base_url"] == base


@pytest.mark.parametrize("option,value,kind", [
    ("--tenant", "not-a-uuid", "ValueError"),
    ("--tenant", TENANT.upper().replace("1", "A"), "ValueError"),
    ("--client-id", "not-a-uuid", "ValueError"),
    ("--audience", "api://not-a-uuid", "ValueError"),
])
def test_invalid_connection_identity_stops_before_login(operator, capsys, option, value, kind):
    args = operator.arguments("show")
    args[args.index(option) + 1] = value
    assert operator.cli.main(args) == 1
    assert capsys.readouterr().err == exactly(unconfirmed(kind, "validation"))
    assert operator.credential_options == []


def test_an_invalid_binding_stops_before_login(operator, capsys):
    args = operator.arguments("show")
    args[args.index(BINDING)] = "binding/other"
    assert operator.cli.main(args) == 1
    assert capsys.readouterr().err == exactly(unconfirmed("ValidationError", "validation"))
    assert operator.credential_options == []


def test_the_terminal_check_follows_connection_checks_and_precedes_the_request_file(operator, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    args = operator.arguments("reject")
    args[args.index(BINDING)] = "binding/other"
    assert operator.cli.main(args) == 1
    assert capsys.readouterr().err == exactly(unconfirmed("ValidationError", "validation"))
    assert operator.run("reject") == 1  # the request file does not exist, so reading it first would be an OSError
    assert capsys.readouterr().err == exactly(unconfirmed("ValueError", "validation"))
    operator.attempt.write_bytes(b"retained")
    assert operator.run("reject") == 1
    assert capsys.readouterr().err == exactly(unconfirmed("ValueError", "validation"))
    assert operator.credential_options == []


def test_show_prints_the_short_preview_with_only_the_read_scope(operator, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert operator.run("show") == 0
    output = capsys.readouterr()
    assert output.out == json.dumps(SHORT_VIEW, ensure_ascii=False, indent=2) + "\n" and output.err == ""
    assert operator.calls == [("GET", PATH)]
    assert operator.credential.authenticate.call_args.kwargs == {"scopes": [READ]}
    assert operator.events == ["authenticate", ("token", (READ,)), ("GET", PATH)]


def test_state_prints_the_validated_observation_with_only_the_read_scope(operator, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert operator.run("state") == 0
    output = capsys.readouterr()
    assert printed(output.out) == [OBSERVED] and output.err == ""
    assert operator.calls == [("GET", PATH + "/decision-state")]
    assert operator.credential.authenticate.call_args.kwargs == {"scopes": [READ]}


@pytest.mark.parametrize("decision", ATTEMPT_VERSIONS)
def test_a_decision_shows_confirms_saves_then_posts_once(operator, capsys, decision):
    operator.prepare(decision)
    assert operator.run(decision) == 0
    output = capsys.readouterr()
    scopes = (READ, APPROVE)
    reads = [("GET", PATH)] + ([("GET", PATH + "/decision-state")] if decision == "revoke" else [])
    assert printed(output.out) == [
        CHECKED_PREVIEWS[decision], *([OBSERVED] if decision == "revoke" else []), body(decision), CONFIRMED[decision],
    ]
    assert output.err == ""
    assert operator.credential.authenticate.call_args.kwargs == {"scopes": list(scopes)}
    assert operator.prompts == [PROMPTS[decision]]
    assert operator.attempt.read_bytes() == canonical(attempt_material(decision)) + b"\n"
    assert operator.bodies == [canonical(body(decision))]
    assert operator.events == [
        "authenticate",
        *[event for read in reads for event in (("token", scopes), read)],
        "confirm", ("token", scopes), ("POST", PATH + "/" + decision), ("attempt saved", True),
    ]


@pytest.mark.parametrize("decision", ATTEMPT_VERSIONS)
@pytest.mark.parametrize("keep_request", [True, False])
def test_reconcile_reads_the_saved_receipt_without_terminal_or_prompt(operator, monkeypatch, capsys, decision, keep_request):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    operator.saved(decision)
    retained = operator.attempt.read_bytes()
    if keep_request:
        operator.request.write_bytes(canonical(body(decision)))
    assert operator.run("reconcile") == 0
    output = capsys.readouterr()
    assert printed(output.out) == [CONFIRMED[decision]] and output.err == ""
    assert operator.calls == [("POST", PATH + RECONCILE_ROUTES[decision])]
    assert operator.bodies == [canonical(body(decision))]
    assert operator.credential.authenticate.call_args.kwargs == {"scopes": [READ]}
    assert operator.prompts == [] and operator.attempt.read_bytes() == retained


def test_reconcile_accepts_the_legacy_hash_only_rejection_attempt(operator, capsys):
    value = attempt_material("reject")
    value.pop("command")
    operator.saved("reject")
    operator.attempt.write_bytes(canonical({**value, "schema_version": "fa-operation-rejection-cli-attempt-v1"}))
    operator.request.write_bytes(canonical(body("reject")))
    assert operator.run("reconcile") == 0
    assert printed(capsys.readouterr().out) == [CONFIRMED["reject"]]
    assert operator.calls == [("POST", PATH + "/rejection-reconcile")]


def test_a_legacy_attempt_without_its_request_file_stays_unconfirmed(operator, capsys):
    value = attempt_material("reject")
    value.pop("command")
    operator.attempt.write_bytes(canonical({**value, "schema_version": "fa-operation-rejection-cli-attempt-v1"}))
    assert operator.run("reconcile") == 1
    assert capsys.readouterr().err == exactly(unconfirmed("FileNotFoundError", "validation"))


def test_a_new_format_attempt_cannot_lose_its_saved_command(operator, capsys):
    value = attempt_material("reject")
    value.pop("command")
    operator.attempt.write_bytes(canonical(value))
    operator.request.write_bytes(canonical(body("reject")))
    assert operator.run("reconcile") == 1
    assert capsys.readouterr().err == exactly(unconfirmed("KeyError", "validation"))


@pytest.mark.parametrize("decision,changes,kind", [
    ("reject", {"schema_version": "unknown-future-format"}, "ValueError"),
    ("reject", {"operation_id": "operation/other"}, "ValidationError"),
    ("revoke", {"grant_id": "grant/other"}, "ValidationError"),
    ("reject", {"binding_id": "other-binding"}, "ValueError"),
    ("reject", {"tenant": "33333333-3333-3333-3333-333333333333"}, "ValueError"),
    ("reject", {"audience": "api://33333333-3333-3333-3333-333333333333"}, "ValueError"),
    ("reject", {"client_id": "33333333-3333-3333-3333-333333333333"}, "ValueError"),
    ("reject", {"command_sha256": "0" * 64}, "ValueError"),
    ("reject", {"extra": True}, "ValueError"),
])
def test_changed_attempt_material_never_reaches_login(operator, capsys, decision, changes, kind):
    operator.saved(decision, **changes)
    assert operator.run("reconcile") == 1
    output = capsys.readouterr()
    assert output.out == "" and output.err == exactly(unconfirmed(kind, "validation"))
    assert operator.credential_options == []


def test_a_rejection_attempt_never_carries_a_grant(operator, capsys):
    operator.saved("reject", grant_id=GRANT)
    assert operator.run("reconcile") == 1
    assert capsys.readouterr().err == exactly(unconfirmed("ValueError", "validation"))


@pytest.mark.parametrize("decision", ATTEMPT_VERSIONS)
def test_a_request_file_with_the_same_command_in_other_bytes_still_reconciles(operator, capsys, decision):
    operator.saved(decision)
    operator.request.write_bytes(json.dumps(body(decision), indent=4, sort_keys=False).encode("utf-8") + b"\n")
    assert operator.request.read_bytes() != canonical(body(decision))
    assert operator.run("reconcile") == 0
    assert printed(capsys.readouterr().out) == [CONFIRMED[decision]]


def test_a_changed_request_file_never_reaches_login(operator, capsys):
    operator.saved("reject")
    operator.request.write_bytes(canonical({**body("reject"), "request_id": "other-request"}))
    assert operator.run("reconcile") == 1
    assert capsys.readouterr().err == exactly(unconfirmed("ValueError", "validation"))
    assert operator.credential_options == []


def test_reconcile_requires_the_saved_attempt(operator, capsys):
    assert operator.run("reconcile") == 1
    assert capsys.readouterr().err == exactly(unconfirmed("FileNotFoundError", "validation"))


def test_an_existing_attempt_is_a_reconcile_problem_before_the_request_file(operator, capsys):
    operator.attempt.write_bytes(b"retained")
    assert operator.run("reject") == 1
    assert capsys.readouterr().err == exactly({"error": {
        "code": "RECONCILE_REQUIRED", "message": "original attempt exists; only reconcile, never resubmit",
    }, "phase": "validation"})
    assert operator.attempt.read_bytes() == b"retained" and operator.credential_options == []


def test_a_missing_request_file_stops_a_decision_during_validation(operator, capsys):
    assert operator.run("reject") == 1
    assert capsys.readouterr().err == exactly(unconfirmed("FileNotFoundError", "validation"))
    assert operator.credential_options == []


@pytest.mark.parametrize("failure,kind", [
    (ClientAuthenticationError("PRIVATE"), "ClientAuthenticationError"), (OSError("PRIVATE"), "OSError"),
    (KeyError("PRIVATE"), "KeyError"), (TypeError("PRIVATE"), "TypeError"), (EOFError("PRIVATE"), "EOFError"),
])
def test_expected_failures_during_login_report_the_authentication_phase(operator, capsys, failure, kind):
    operator.credential.authenticate.side_effect = failure
    assert operator.run("show") == 1
    output = capsys.readouterr()
    assert output.out == "" and output.err == exactly(unconfirmed(kind, "authentication"))
    assert operator.calls == []


def test_an_unexpected_failure_is_not_turned_into_an_operator_result(operator):
    operator.credential.authenticate.side_effect = RuntimeError("programming error")
    with pytest.raises(RuntimeError, match="programming error"):
        operator.run("show")


@pytest.mark.parametrize("action,route,phase", [
    ("state", "/decision-state", "state"), ("show", "", "preview"),
])
def test_a_refused_read_reports_its_phase_and_problem(operator, capsys, action, route, phase):
    operator.routes["GET", PATH + route] = httpx.Response(403, json={"error": {"message": "PRIVATE"}})
    assert operator.run(action) == 1
    output = capsys.readouterr()
    assert output.out == "" and output.err == exactly(unavailable(403, phase))


@pytest.mark.parametrize("decision", ATTEMPT_VERSIONS)
def test_a_failed_preview_stops_a_decision_in_the_preview_phase(operator, capsys, decision):
    operator.prepare(decision)
    operator.routes["GET", PATH] = httpx.ReadTimeout("PRIVATE")
    assert operator.run(decision) == 1
    assert capsys.readouterr().err == exactly({"error": {
        "code": "OPERATION_REQUEST_UNKNOWN", "message": "transport outcome unknown; do not resubmit",
    }, "phase": "preview"})
    assert not operator.attempt.exists()


@pytest.mark.parametrize("decision", ATTEMPT_VERSIONS)
def test_a_changed_description_is_printed_then_refused_before_anything_else(operator, capsys, decision):
    operator.prepare(decision)
    changed = {**DESCRIPTION.model_dump(mode="json"), "target": "other-target"}
    prepared = material(changed)
    operator.routes["GET", PATH] = {
        **PREVIEW, "content": prepared.model_dump(mode="json"), "description_digest": prepared.sha256,
    }
    assert operator.run(decision) == 1
    output = capsys.readouterr()
    assert len(printed(output.out)) == 1
    assert output.err == exactly(unconfirmed("ValueError", "preview"))
    assert operator.calls == [("GET", PATH)] and operator.prompts == []


@pytest.mark.parametrize("decision", ["approve", "reject"])
@pytest.mark.parametrize("change", [
    {"human_rejection": {"decision_id": "earlier"}}, {"adapter_contract_digest": "0" * 64},
])
def test_a_rejected_or_changed_contract_is_refused_after_the_preview(operator, capsys, decision, change):
    operator.prepare(decision)
    operator.routes["GET", PATH] = {**PREVIEW, **change}
    assert operator.run(decision) == 1
    output = capsys.readouterr()
    assert len(printed(output.out)) == 1 and output.err == exactly(unconfirmed("ValueError", "preview"))
    assert operator.calls == [("GET", PATH)] and operator.prompts == []


def test_a_recorded_rejection_does_not_stop_revoking_a_grant(operator, capsys):
    operator.prepare("revoke")
    operator.routes["GET", PATH] = {**PREVIEW, "human_rejection": {"decision_id": "earlier"}}
    assert operator.run("revoke") == 0
    assert printed(capsys.readouterr().out)[0]["rejection_recorded"] is True


@pytest.mark.parametrize("change", [
    {"operation_id": "other-operation"},
    {"description_digest": "0" * 64},
    {"adapter_contract_digest": "0" * 64},
    {"grant": None},
    {"grant": {**OBSERVED["grant"], "revoked": True}},
    {"grant": {**OBSERVED["grant"], "revocation_revision": 1}},
])
def test_revocation_needs_the_exact_observed_grant(operator, capsys, change):
    operator.prepare("revoke")
    operator.routes["GET", PATH + "/decision-state"] = {**OBSERVED, **change}
    assert operator.run("revoke") == 1
    output = capsys.readouterr()
    assert printed(output.out) == [FULL_VIEW]  # the mismatched observation is never presented as the target
    assert output.err == exactly(unconfirmed("ValueError", "state"))
    assert operator.prompts == [] and not operator.attempt.exists()


def test_a_failed_observation_stops_revocation_in_the_state_phase(operator, capsys):
    operator.prepare("revoke")
    operator.routes["GET", PATH + "/decision-state"] = httpx.Response(503)
    assert operator.run("revoke") == 1
    assert capsys.readouterr().err == exactly(unavailable(503, "state"))


@pytest.mark.parametrize("decision", ATTEMPT_VERSIONS)
@pytest.mark.parametrize("answer,kind", [
    ("cancel", "ValueError"), ("", "ValueError"), (EOFError(), "EOFError"),
])
def test_only_the_exact_word_confirms_a_decision(operator, capsys, decision, answer, kind):
    operator.prepare(decision)
    operator.answer = answer
    assert operator.run(decision) == 1
    assert capsys.readouterr().err == exactly(unconfirmed(kind, "confirmation"))
    assert not operator.attempt.exists() and operator.bodies == []


def test_another_decisions_word_does_not_confirm(operator, capsys):
    operator.prepare("reject")
    operator.answer = "approve"
    assert operator.run("reject") == 1
    assert capsys.readouterr().err == exactly(unconfirmed("ValueError", "confirmation"))
    assert not operator.attempt.exists()


def test_an_attempt_that_appears_during_confirmation_is_kept_and_nothing_is_sent(operator, monkeypatch, capsys):
    operator.prepare("reject")

    def raced(_prompt):
        operator.attempt.write_bytes(b"another attempt")
        return "reject"

    monkeypatch.setattr("builtins.input", raced)
    assert operator.run("reject") == 1
    assert capsys.readouterr().err == exactly(unconfirmed("FileExistsError", "persist-attempt"))
    assert operator.attempt.read_bytes() == b"another attempt" and operator.bodies == []


def test_a_failed_flush_reports_the_save_phase_and_sends_nothing(operator, monkeypatch, capsys):
    operator.prepare("reject")

    def failed(_descriptor):
        raise OSError("PRIVATE")

    monkeypatch.setattr(operator.cli.os, "fsync", failed)
    assert operator.run("reject") == 1
    assert capsys.readouterr().err == exactly(unconfirmed("OSError", "persist-attempt"))
    assert operator.bodies == []


class PosixOs:
    """The module's view of a POSIX os, without changing how anything else in the process sees os."""

    name, O_RDONLY, O_DIRECTORY = "posix", 1, 2

    def __init__(self, directory_failure=None):
        self.log, self.directory_failure = [], directory_failure

    def __getattr__(self, name):
        return getattr(os, name)

    def fsync(self, descriptor):
        self.log.append(("fsync", "directory" if descriptor == 77 else "file"))
        if descriptor == 77 and self.directory_failure:
            raise self.directory_failure

    def open(self, path, flags):
        self.log.append(("open", path, flags))
        return 77

    def close(self, descriptor):
        self.log.append(("close", descriptor))


def test_a_posix_attempt_also_flushes_its_directory_before_posting(operator, monkeypatch, capsys):
    operator.prepare("reject")
    system = PosixOs()
    monkeypatch.setattr(operator.cli, "os", system)
    assert operator.run("reject") == 0
    assert system.log == [("fsync", "file"), ("open", operator.attempt.parent, 3), ("fsync", "directory"), ("close", 77)]


def test_a_failed_directory_flush_closes_the_descriptor_and_sends_nothing(operator, monkeypatch, capsys):
    operator.prepare("reject")
    system = PosixOs(OSError("PRIVATE"))
    monkeypatch.setattr(operator.cli, "os", system)
    assert operator.run("reject") == 1
    assert capsys.readouterr().err == exactly(unconfirmed("OSError", "persist-attempt"))
    assert system.log[-1] == ("close", 77) and operator.bodies == []


@pytest.mark.parametrize("decision", ATTEMPT_VERSIONS)
def test_a_lost_decision_keeps_the_attempt_and_reports_the_action_phase(operator, capsys, decision):
    operator.prepare(decision)
    operator.routes["POST", PATH + "/" + decision] = httpx.ConnectError("PRIVATE")
    assert operator.run(decision) == 1
    assert capsys.readouterr().err.endswith(exactly({"error": {
        "code": "OPERATION_REQUEST_UNKNOWN", "message": "transport outcome unknown; do not resubmit",
    }, "phase": decision}))
    assert operator.attempt.read_bytes() == canonical(attempt_material(decision)) + b"\n"


@pytest.mark.parametrize("decision", ATTEMPT_VERSIONS)
def test_a_lost_reconcile_reports_the_reconcile_phase(operator, capsys, decision):
    operator.saved(decision)
    operator.routes["POST", PATH + RECONCILE_ROUTES[decision]] = httpx.Response(404)
    assert operator.run("reconcile") == 1
    assert capsys.readouterr().err == exactly(unavailable(404, "reconcile"))


@pytest.mark.parametrize("decision", ATTEMPT_VERSIONS)
@pytest.mark.parametrize("action", ["decide", "reconcile"])
def test_a_mismatched_receipt_is_unconfirmed_in_the_action_phase(operator, capsys, decision, action):
    if action == "decide":
        operator.prepare(decision)
        route = PATH + "/" + decision
    else:
        operator.saved(decision)
        route = PATH + RECONCILE_ROUTES[decision]
    operator.routes["POST", route] = {**RECEIPTS[decision], "result": None}
    assert operator.run(decision if action == "decide" else "reconcile") == 1
    output = capsys.readouterr()
    assert '"state"' not in output.out
    assert output.err == exactly(unconfirmed("ValueError", decision if action == "decide" else "reconcile"))


@pytest.mark.parametrize("decision,field,value", [
    ("reject", "operation_id", "other-operation"),
    ("approve", "grant_id", "grant/other"),
    ("revoke", "grant_id", "other-grant"),
])
def test_receipts_are_checked_against_the_saved_target(operator, capsys, decision, field, value):
    operator.saved(decision)
    receipt = {**RECEIPTS[decision], "result": {**RECEIPTS[decision]["result"], field: value}}
    operator.routes["POST", PATH + RECONCILE_ROUTES[decision]] = receipt
    assert operator.run("reconcile") == 1
    assert '"state"' not in capsys.readouterr().out


def test_stdout_is_left_exactly_as_printed_before_a_later_failure(operator, capsys):
    operator.prepare("reject")
    operator.routes["POST", PATH + "/reject"] = httpx.Response(503)
    assert operator.run("reject") == 1
    output = capsys.readouterr()
    assert printed(output.out) == [SHORT_VIEW, body("reject")]
    assert output.err == exactly(unavailable(503, "reject"))
