"""One N6 test-agent operation per invocation, run inside the existing API container by the deployment owner.

No operator token, Azure CLI login, resource/IAM change, retry, redirect, model, or arbitrary URL/command.
The output is append-only JSONL: an intent is flushed before I/O; an existing file is never reused.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import quote
from uuid import NAMESPACE_URL, uuid5

import httpx
from pydantic import TypeAdapter

from shared_context.behavior_code_identity import code_sha256
from shared_context.behavior_server_config import load_configuration
from shared_context.behavior_wire import Digest, Identifier
from shared_context.errors import Problem
from shared_context.responses_probe import (
    HEADER, INPUTS, PROFILE, PROBE_AGENT_ID, PROBE_AGENT_NAME, PROBE_PROJECT_ENDPOINT,
    RID, AgentVersion, PlatformId, ProbeMetadata, ProbeObservation, ProbeRecord, digest_input, http_summary,
)

OPERATIONS = (
    "agent-object-read", "version-read", "session-create", "session-read", "conversation-create", "response-create", "response-read",
    "conversation-read", "items-read", "agent-read", "receipt-read", "interrupt",
)
WRITES = {"session-create", "conversation-create", "response-create", "interrupt"}


def redact_message(value: str) -> str:
    text = re.sub(r"""(?im)\b(?:set-cookie|cookie)["']?\s*[:=]\s*[^\r\n]*""", "cookie=[REDACTED]", value)
    text = re.sub(r"(?i)\b(?:Bearer|Basic)\s+[^\s\"'<>]+", "[REDACTED_AUTH]", text)
    text = re.sub(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b", "[REDACTED_TOKEN]", text)
    text = re.sub(r"(?i)(?:\?|%3f)[^\s\"'<>]*", "?[REDACTED_QUERY]", text)
    text = re.sub(r"(?i)(https?://)[^\s/@]+:[^\s/@]+@", r"\1[REDACTED]@", text)
    fields = (
        r"authorization|access[_-]?token|refresh[_-]?token|client[_-]?secret|api[_-]?key|"
        r"x-agent-foundry-call-id|call[\s_-]?id|token|password|signature|sig"
    )
    separator = r"""(?:\s*[:=]\s*|\s+is\s+|\s+(?=["']))"""
    text = re.sub(
        rf"""(?ix)(["']?(?:{fields})["']?{separator})(?:"[^"]*"|'[^']*'|[^\s,;]+)""",
        r"\1[REDACTED]", text,
    )
    return " ".join(re.sub(r"[\x00-\x1f\x7f]", " ", text).split())[:300]


def summarize_response(response: httpx.Response) -> dict:
    summary = http_summary(response)
    if response.is_success:
        return summary
    try:
        body = response.json()
    except ValueError:
        return summary
    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict):
        return summary
    safe = {}
    code, message = error.get("code"), error.get("message")
    if (isinstance(code, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_.:-]{0,127}", code)
            and redact_message(code) == code):
        safe["code"] = code
    if isinstance(message, str):
        safe["message"] = redact_message(message)
    if safe:
        summary["error"] = safe
    return summary


def probe_output(body: dict) -> dict:
    value = body
    if "output" in body:
        output = body["output"]
        if not isinstance(output, list) or len(output) != 1 or not isinstance(output[0], dict):
            raise ValueError("probe output differs")
        content = output[0].get("content")
        if (not isinstance(content, list) or len(content) != 1 or not isinstance(content[0], dict)
                or content[0].get("type") != "output_text"):
            raise ValueError("probe content differs")
        value = json.loads(content[0]["text"])
    if not isinstance(value, dict):
        raise ValueError("probe object differs")
    record = ProbeRecord.model_validate(value["record"])
    observed = ProbeObservation.model_validate(value["observed"])
    result = {"record": record.model_dump(mode="json"), "observed": observed.model_dump(mode="json")}
    for name in ("is_recovery", "state_write_performed", "cid_fa_identifier"):
        if name in value:
            result[name] = TypeAdapter(bool).validate_python(value[name], strict=True)
    return result


def agent_observation(body: dict) -> dict:
    if (body.get("credential_kind") != "agent_managed_identity"
            or body.get("identity_kind") != "agent_bearer_with_platform_caller_context"
            or body.get("target_metadata_source") != "synthetic_probe_record"
            or body.get("code") not in {"N6_D3_READ_OBSERVED", "N6_D3_STOP"}):
        raise ValueError("agent probe observation differs")
    status = TypeAdapter(int).validate_python(body["http_status"], strict=True)
    if not 100 <= status <= 599:
        raise ValueError("invalid status")
    result = {
        "http_status": status, "body_sha256": TypeAdapter(Digest).validate_python(body["body_sha256"]),
        "credential_kind": body["credential_kind"], "identity_kind": body["identity_kind"],
        "call_id_present": TypeAdapter(bool).validate_python(body["call_id_present"], strict=True),
        "conversation_id": TypeAdapter(PlatformId).validate_python(body["conversation_id"]),
        "request_id": TypeAdapter(RID).validate_python(body["request_id"]),
        "target_metadata": ProbeMetadata.model_validate(body["target_metadata"]).model_dump(mode="json"),
        "target_metadata_source": body["target_metadata_source"], "code": body["code"],
    }
    for name in ("x-ms-request-id", "apim-request-id", "x-request-id"):
        if name in body:
            result[name] = TypeAdapter(PlatformId).validate_python(body[name])
    return result


def bind_conversation(args, team_id):
    if args.operation not in {"response-create", "conversation-read", "items-read"}:
        return None
    if args.conversation_record is None:
        raise ValueError("the original successful synthetic conversation-create record is required")
    raw = args.conversation_record.read_bytes()
    if len(raw) > 16384:
        raise ValueError("the conversation record exceeds its bound")
    rows = [json.loads(line) for line in raw.splitlines()]
    if (len(rows) != 2 or not all(isinstance(row, dict) for row in rows)
            or rows[0].get("operation") != "conversation-create" or rows[0].get("profile") != PROFILE
            or rows[1].get("code") != "N6_OBSERVED" or rows[1].get("http_status") not in {200, 201}
            or rows[1].get("conversation_id") != args.conversation_id
            or rows[1].get("metadata") != {"fa_agent_id": PROBE_AGENT_ID, "fa_team_id": team_id}):
        raise ValueError("this is not the original successful synthetic conversation creation")
    return hashlib.sha256(raw).hexdigest()


def bind_session(args):
    if args.operation != "response-create":
        return None
    if args.session_record is None:
        raise ValueError("the successful API MI session-create record is required")
    raw = args.session_record.read_bytes()
    if len(raw) > 16384:
        raise ValueError("the session record exceeds its bound")
    rows = [json.loads(line) for line in raw.splitlines()]
    if (len(rows) != 2 or not all(isinstance(row, dict) for row in rows)
            or rows[0].get("operation") != "session-create" or rows[0].get("profile") != PROFILE
            or rows[0].get("session_id") != args.session_id
            or rows[0].get("agent_version") != args.agent_version
            or rows[0].get("expected_build") != args.expected_build
            or rows[1].get("code") != "N6_OBSERVED" or rows[1].get("http_status") != 201
            or rows[1].get("credential_kind") != "api_managed_identity"
            or rows[1].get("agent_version") != args.agent_version
            or rows[1].get("agent_session_id") != args.session_id):
        raise ValueError("this is not the original successful API MI session creation")
    return hashlib.sha256(raw).hexdigest()


class ProbeClient:
    def __init__(self, credential, *, transport=None):
        self.credential = credential
        self.http = httpx.Client(
            transport=transport or httpx.HTTPTransport(retries=0),
            timeout=httpx.Timeout(30, connect=10), follow_redirects=False, trust_env=False,
        )

    def close(self):
        self.http.close()

    def request(self, method, path, *, correlation, params=None, body=None, headers=None):
        token = self.credential.get_token("https://ai.azure.com/.default")
        return self.http.request(
            method, PROBE_PROJECT_ENDPOINT + path,
            params={"api-version": "v1", **(params or {})}, json=body,
            headers={"Authorization": "Bearer " + token.token, "x-ms-client-request-id": correlation, **(headers or {})},
        )

    def require_version(self, version, build, correlation):
        response = self.request(
            "GET", "/agents/" + PROBE_AGENT_NAME + "/versions/" + version, correlation=correlation,
        )
        summary = summarize_response(response)
        if response.status_code != 200:
            raise Problem(409, "N6_VERSION_READ_STOP", "do not change identity or retry", observation=summary)
        try:
            body = response.json()
            if not isinstance(body, dict):
                raise ValueError()
            definition, metadata = body.get("definition"), body.get("metadata")
            environment = definition.get("environment_variables") if isinstance(definition, dict) else None
            guid = body.get("agent_guid")
            if not isinstance(environment, dict) or not isinstance(metadata, dict) or not isinstance(guid, str):
                raise ValueError()
        except ValueError:
            raise Problem(502, "N6_VERSION_SHAPE_UNKNOWN", "the version read is not usable", observation=summary) from None
        if (guid.lower() != PROBE_AGENT_ID or body.get("version") != version
                or body.get("status") != "active" or metadata.get("fa_build") != build
                or environment.get("FA_RESPONSES_ENABLED") != "1"
                or environment.get("FA_N6_RESPONSES_PROBE") != PROFILE):
            raise Problem(409, "N6_VERSION_MISMATCH", "only the approved same-build test version may be invoked")
        return summary, environment

    def require_agent(self, correlation):
        response = self.request("GET", "/agents/" + PROBE_AGENT_NAME, correlation=correlation)
        summary = summarize_response(response)
        if response.status_code != 200:
            raise Problem(409, "N6_AGENT_READ_STOP", "do not change identity or retry", observation=summary)
        try:
            body = response.json()
            if (not isinstance(body, dict) or body.get("object") != "agent"
                    or body.get("name") != PROBE_AGENT_NAME or body.get("state") not in {"enabled", "disabled"}):
                raise ValueError()
            endpoint = body.get("agent_endpoint")
            protocols = endpoint.get("protocol_configuration") if isinstance(endpoint, dict) else None
            if not isinstance(protocols, dict) or not protocols:
                raise ValueError()
            known = {"responses", "invocations", "invocations_ws", "activity", "a2a", "mcp"}
            if set(protocols) - known or any(not isinstance(value, dict) for value in protocols.values()):
                raise ValueError()
            summary.update(agent_state=body["state"], configured_protocols=sorted(protocols))
            if body["state"] == "disabled":
                raise Problem(409, "N6_AGENT_DISABLED", "the agent is disabled; no PATCH", observation=summary)
            if "responses" not in protocols:
                raise Problem(409, "N6_RESPONSES_PROTOCOL_DISABLED", "the endpoint does not enable Responses; no PATCH",
                              observation=summary)
            if protocols["responses"] != {}:
                raise ValueError()
        except ValueError:
            raise Problem(502, "N6_AGENT_PROTOCOL_UNKNOWN", "the actual endpoint configuration is not confirmed",
                          observation=summary) from None
        return {**summary, "responses_enabled": True}

    def execute(self, args, team_id):
        correlation = uuid5(NAMESPACE_URL, PROFILE + ":" + args.request_id + ":" + args.operation).hex
        root = "/agents/" + PROBE_AGENT_NAME
        openai = root + "/endpoint/protocols/openai"
        extra = {"credential_kind": "api_managed_identity", "call_id_present": False, "correlation_id": correlation}
        if args.operation == "agent-object-read":
            return {**extra, **self.require_agent(correlation), "code": "N6_AGENT_CONFIRMED"}
        if args.operation in WRITES:
            extra["agent_read"] = self.require_agent(correlation)
        if args.operation in WRITES or args.operation in {"agent-read", "receipt-read"}:
            checked, environment = self.require_version(args.agent_version, args.expected_build, correlation)
            extra["version_read"] = checked
            if args.operation == "interrupt" or args.operation == "response-create" and args.mode == "hold":
                if (environment.get("FA_N6_RESPONSES_INTERRUPT_SESSION_ID") != args.session_id
                        or environment.get("FA_NATIVE_TURNS")):
                    raise Problem(409, "N6_INTERRUPT_SESSION_MISMATCH", "hold/interrupt need the dedicated no-native session")
        if args.operation == "version-read":
            summary, _ = self.require_version(args.agent_version, args.expected_build, correlation)
            return {**extra, **summary, "code": "N6_VERSION_CONFIRMED"}
        if args.operation == "session-create":
            # Projects 2.3.0 AgentsOperations.create_session uses this exact version-ref body and route.
            response = self.request("POST", root + "/endpoint/sessions", correlation=correlation, body={
                "agent_session_id": args.session_id,
                "version_indicator": {"type": "version_ref", "agent_version": args.agent_version},
            })
        elif args.operation == "session-read":
            response = self.request("GET", root + "/endpoint/sessions/" + quote(args.session_id, safe=""), correlation=correlation)
        elif args.operation == "conversation-create":
            response = self.request("POST", openai + "/conversations", correlation=correlation, body={
                "metadata": {"fa_agent_id": PROBE_AGENT_ID, "fa_team_id": team_id},
            })
        elif args.operation == "response-create":
            text = INPUTS[args.mode]
            response = self.request("POST", openai + "/responses", correlation=correlation, body={
                "input": text, "conversation": args.conversation_id, "agent_session_id": args.session_id,
                "background": True, "store": True, "stream": False,
                "metadata": {"fa_agent_id": PROBE_AGENT_ID, "fa_team_id": team_id},
            }, headers={
                HEADER: PROFILE, "x-client-fa-rid": args.request_id, "x-client-fa-cid": args.conversation_id,
                "x-client-fa-submit-digest": digest_input(text, args.conversation_id),
            })
        elif args.operation == "response-read":
            response = self.request("GET", openai + "/responses/" + quote(args.response_id, safe=""), correlation=correlation)
        elif args.operation in {"conversation-read", "items-read"}:
            # Cross-endpoint D3 is read-only, same fixed project; never use this name for a write or invocation.
            reader = args.reader_agent_name or PROBE_AGENT_NAME
            path = ("/agents/" + quote(reader, safe="") + "/endpoint/protocols/openai/conversations/"
                    + quote(args.conversation_id, safe=""))
            response = self.request("GET", path + ("/items" if args.operation == "items-read" else ""),
                                    correlation=correlation, params={"limit": 1} if args.operation == "items-read" else None)
            extra["reader_agent_name"] = reader
        else:
            operation = {"agent-read": "read_conversation", "receipt-read": "read_receipt", "interrupt": "interrupt"}[
                args.operation]
            command = {"operation": operation, "request_id": args.request_id}
            if operation == "interrupt":
                command.update(session_id=args.session_id, process_id=args.process_id)
            response = self.request(
                "POST", root + "/endpoint/protocols/invocations", correlation=correlation,
                params={"agent_session_id": args.session_id}, body=command, headers={HEADER: PROFILE},
            )
        result = {**extra, **summarize_response(response), "code": "N6_OBSERVED" if response.is_success else "N6_STOP"}
        if args.operation == "session-create" and (response.status_code == 408 or response.status_code >= 500):
            result["code"] = "N6_OUTCOME_UNKNOWN"
        try:
            return self.observe(args, response, result)
        except (ValueError, KeyError, TypeError):
            return {**result, "code": "N6_OUTCOME_UNKNOWN"}

    @staticmethod
    def observe(args, response, result):
        if not response.is_success and args.operation != "agent-read":
            return result
        body = response.json()
        if not isinstance(body, dict):
            raise ValueError("the operation did not return an object")
        if args.operation == "agent-read":
            if "identity_kind" in body:
                result["agent_read"] = agent_observation(body)
                if body["code"] != "N6_D3_READ_OBSERVED":
                    result["code"] = "N6_STOP"
            else:
                result["code"] = "N6_STOP"
            return result
        if not response.is_success:
            return result
        if args.operation in {"session-create", "session-read"}:
            result["agent_session_id"] = TypeAdapter(PlatformId).validate_python(body["agent_session_id"])
            indicator = body.get("version_indicator") or {}
            result["agent_version"] = TypeAdapter(AgentVersion).validate_python(indicator["agent_version"])
            if args.operation == "session-create" and (
                response.status_code != 201 or result["agent_session_id"] != args.session_id
                or result["agent_version"] != args.agent_version or indicator.get("type") != "version_ref"
            ):
                raise ValueError("session creation did not confirm the original SID/version")
        elif args.operation in {"conversation-create", "conversation-read"}:
            result["conversation_id"] = TypeAdapter(PlatformId).validate_python(body["id"])
        elif args.operation in {"response-create", "response-read"}:
            result["response_id"] = TypeAdapter(PlatformId).validate_python(body["id"])
            if (body.get("status") not in {"queued", "in_progress", "completed", "failed", "cancelled", "incomplete"}
                    or args.operation == "response-read" and body["id"] != args.response_id):
                raise ValueError("the original response status or ID differs")
            if body.get("status") == "completed":
                result["probe"] = probe_output(body)
            elif body.get("status") in {"failed", "cancelled", "incomplete"}:
                result["code"] = "N6_STOP"
        elif args.operation == "receipt-read":
            result["probe"] = probe_output(body)
        elif args.operation == "items-read" and not isinstance(body.get("data"), list):
            raise ValueError("the item list differs")
        elif args.operation == "interrupt" and (
            body.get("status") != "interrupt_scheduled" or body.get("process_id") != args.process_id
            or body.get("request_id") != args.request_id or body.get("session_id") != args.session_id
        ):
            raise ValueError("interruption acceptance differs")
        if "probe" in result:
            record = result["probe"]["record"]
            if record["request_id"] != args.request_id or record["agent_id"] != PROBE_AGENT_ID:
                raise ValueError("the returned probe belongs to another RID/agent")
        return result


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=OPERATIONS)
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--session-id", required=True, help="Fixed synthetic SID, created once by this script's API MI")
    parser.add_argument("--session-record", type=Path, help="Successful same-API-MI session-create JSONL for response-create")
    parser.add_argument("--agent-version", required=True)
    parser.add_argument("--expected-build", required=True, help="Exact fa_build/code_sha256, not a git commit")
    parser.add_argument("--conversation-id")
    parser.add_argument("--conversation-record", type=Path, help="Successful synthetic conversation-create JSONL, not an old CID")
    parser.add_argument("--response-id")
    parser.add_argument("--reader-agent-name", help="D3 only: another explicitly approved existing test-agent endpoint")
    parser.add_argument("--process-id", type=int)
    parser.add_argument("--mode", choices=tuple(INPUTS), default="observe")
    parser.add_argument("--output", type=Path, required=True, help="New JSONL file; never reuse an uncertain operation's file")
    args = parser.parse_args(argv)
    for value, kind in ((args.request_id, RID), (args.session_id, Identifier),
                        (args.agent_version, AgentVersion), (args.expected_build, Digest)):
        TypeAdapter(kind).validate_python(value)
    needs_cid = args.operation in {"response-create", "conversation-read", "items-read"}
    if needs_cid or args.conversation_id is not None:
        TypeAdapter(PlatformId).validate_python(args.conversation_id)
    if args.operation == "response-read" or args.response_id is not None:
        TypeAdapter(PlatformId).validate_python(args.response_id)
    if args.reader_agent_name:
        TypeAdapter(Identifier).validate_python(args.reader_agent_name)
        if args.operation not in {"conversation-read", "items-read"}:
            raise ValueError("the alternate test endpoint is read-only")
    if args.operation == "interrupt" and (args.process_id is None or args.process_id <= 0):
        raise ValueError("the observed exact process ID is required")
    return args


def main(argv=None):
    from azure.core.exceptions import AzureError
    from azure.identity import ManagedIdentityCredential

    try:
        args = arguments(argv)
        if code_sha256() != args.expected_build:
            raise ValueError("API build differs")
        config = load_configuration()
        client_id = config.identity.managed_identity_client_id
        if client_id is None:
            raise ValueError("the configured API managed identity is required; no credential fallback")
        conversation_record_hash = bind_conversation(args, config.identity.team_id)
        session_record_hash = bind_session(args)
    except (ValueError, OSError):
        print(json.dumps({"code": "N6_LOCAL_PREFLIGHT_STOP"}))
        return 2
    intent = {
        "status": "intent", "operation": args.operation, "profile": PROFILE, "request_id": args.request_id,
        "session_id": args.session_id, "agent_version": args.agent_version, "expected_build": args.expected_build,
        "conversation_id": args.conversation_id, "response_id": args.response_id, "mode": args.mode,
        "process_id": args.process_id, "reader_agent_name": args.reader_agent_name,
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "conversation_record_sha256": conversation_record_hash,
        "session_record_sha256": session_record_hash, "credential_kind": "api_managed_identity",
        "command": ["n6-responses-probe.py", *(sys.argv[1:] if argv is None else argv)],
    }
    try:
        with args.output.open("x", encoding="utf-8") as output:
            output.write(json.dumps(intent, sort_keys=True) + "\n")
            output.flush()
            os.fsync(output.fileno())
            try:
                with ManagedIdentityCredential(client_id=str(client_id), retry_total=0) as credential:
                    client = ProbeClient(credential)
                    try:
                        observed = client.execute(args, config.identity.team_id)
                    finally:
                        client.close()
            except Problem as error:
                observed = {"code": error.body["error"]["code"], "status": "stopped"}
                if "observation" in error.body["error"]:
                    observed["observation"] = error.body["error"]["observation"]
            except (AzureError, httpx.HTTPError, OSError, ValueError, KeyError, TypeError):
                observed = {"code": "N6_OUTCOME_UNKNOWN", "status": "unknown"}
            output.write(json.dumps(observed, sort_keys=True) + "\n")
            output.flush()
            os.fsync(output.fileno())
    except OSError:
        print(json.dumps({"code": "N6_OUTPUT_UNAVAILABLE", "request_id": args.request_id}))
        return 2
    print(json.dumps(observed, sort_keys=True))
    return 0 if observed["code"] in {"N6_OBSERVED", "N6_VERSION_CONFIRMED", "N6_AGENT_CONFIRMED"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
