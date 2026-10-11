"""Human operation decisions and original receipt lookup; no execution or provisioning."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import ssl
import sys
from urllib.parse import urlsplit

import httpx
from azure.core.exceptions import ClientAuthenticationError
from azure.identity import InteractiveBrowserCredential
from pydantic import TypeAdapter, ValidationError

from .backup_models import canonical_oid
from .behavior_auth import APPROVAL_SCOPE, READ_SCOPE
from .behavior_operations import Effect, OperationDescription, control_key
from .behavior_store import InlineMaterial
from .behavior_wire import Digest, Identifier, OperationApproval, OperationDecisionState, OperationRejection, OperationRevocation
from .config import validate_api_audience
from .errors import Problem
from .human_cli_logging import private_auth_logging as _private_auth_logging


_ATTEMPT_VERSIONS = {
    "approve": "fa-operation-approval-cli-attempt-v1",
    "reject": "fa-operation-rejection-cli-attempt-v2",
    "revoke": "fa-operation-revocation-cli-attempt-v1",
}
_RECONCILE_PATHS = {
    "approve": "/approval-reconcile", "reject": "/rejection-reconcile", "revoke": "/revocation-reconcile",
}


def _pairs(items):
    result = {}
    for name, value in items:
        if name in result:
            raise ValueError("duplicate JSON field")
        result[name] = value
    return result


def _invalid_constant(_value):
    raise ValueError("non-finite JSON value")


def _json(raw):
    return json.loads(raw, object_pairs_hook=_pairs, parse_constant=_invalid_constant)


def _canonical(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _terminal_json(value):
    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2)
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    result = []
    for character in encoded:
        if character == "\n":
            result.append(character)
            continue
        if character.isprintable():
            try:
                character.encode(encoding)
            except UnicodeEncodeError:
                pass
            else:
                result.append(character)
                continue
        result.append(json.dumps(character, ensure_ascii=True)[1:-1])
    return "".join(result)


def _path(value):
    path = Path(value).absolute()
    if any(item.is_symlink() or getattr(item, "is_junction", lambda: False)() for item in (path, *path.parents)):
        raise ValueError("request and attempt paths cannot follow symbolic links or junctions")
    return path


def _read_file(path):
    with path.open("rb") as stream:
        raw = stream.read(8193)
    if len(raw) > 8192:
        raise ValueError("the original request or attempt file exceeds 8192 bytes")
    return _json(raw)


def _request(http, token, method, path, body=None):
    try:
        with http.stream(method, path, headers={
            "Authorization": "Bearer " + token(), "Content-Type": "application/json",
        }, content=None if body is None else _canonical(body), follow_redirects=False) as response:
            raw = bytearray()
            for chunk in response.iter_bytes():
                raw.extend(chunk)
                if len(raw) > 128 * 1024:
                    raise Problem(502, "OPERATION_RESPONSE_INVALID", "response exceeds the complete response bound")
            if response.status_code != 200:
                raise Problem(response.status_code, "OPERATION_REQUEST_UNCONFIRMED",
                              "no confirmed result; preserve the original request",
                              upstream_status=response.status_code)
            data = _json(bytes(raw))
            if not isinstance(data, dict):
                raise ValueError("object response required")
            return data
    except Problem:
        raise
    except httpx.HTTPError:
        raise Problem(503, "OPERATION_REQUEST_UNKNOWN", "transport outcome unknown; do not resubmit") from None
    except (ValueError, TypeError, UnicodeError):
        raise Problem(502, "OPERATION_RESPONSE_INVALID", "response cannot confirm the original request") from None


def _byte_view(value):
    result = {"byte_length": len(value), "sha256": hashlib.sha256(value).hexdigest()}
    try:
        return {**result, "encoding": "utf-8", "text": value.decode("utf-8")}
    except UnicodeDecodeError:
        return {**result, "encoding": "base64", "base64": base64.b64encode(value).decode("ascii")}


def _preview(value, *, complete=False):
    operation_id = TypeAdapter(Identifier).validate_python(value["operation_id"], strict=True)
    material = InlineMaterial.model_validate_json(json.dumps(value["content"]))
    description = OperationDescription.model_validate_json(material.data)
    if value["description_digest"] != material.sha256:
        raise ValueError("prepared operation digest differs")
    contract = TypeAdapter(Digest).validate_python(value["adapter_contract_digest"], strict=True)
    effect = TypeAdapter(Effect).validate_python(value["effect_state"], strict=True)
    if complete:
        execution = TypeAdapter(Identifier).validate_python(value["execution_window_contract"], strict=True)
        if (value["canonicalization_version"] != description.canonicalization_version
                or value["strict_service_commit_time_expiry_supported"] is not False):
            raise ValueError("the complete original execution contract is required")
        return operation_id, {
            "operation_id": operation_id, "description": description.model_dump(mode="json"),
            "content_view": _byte_view(description.content),
            "dependency_views": [
                {"resource_id": dependency.resource_id, "version": dependency.version,
                 **_byte_view(dependency.content)} for dependency in description.dependencies
            ],
            "material_byte_length": len(material.data), "description_digest": material.sha256,
            "adapter_contract_digest": contract, "effect_state": effect,
            "rejection_recorded": value.get("human_rejection") is not None,
            "execution_window_contract": execution,
            "strict_service_commit_time_expiry_supported": False,
        }
    return operation_id, {
        "operation_id": operation_id, "adapter_id": description.adapter_id,
        "method": description.method, "target": description.target,
        "content_utf8": description.content.decode("utf-8"),
        "description_digest": material.sha256, "adapter_contract_digest": contract,
        "effect_state": effect, "rejection_recorded": value.get("human_rejection") is not None,
    }


def _receipt(value, command, operation_id):
    result = value["result"]
    if (not isinstance(result, dict) or result.get("rejected") is not True or result.get("operation_id") != operation_id
            or result.get("decision_id") != command.decision_id
            or result.get("description_digest") != command.description_digest):
        raise ValueError("receipt does not match the original rejection")
    return {name: result[name] for name in ("operation_id", "decision_id", "description_digest", "rejected")}


def _decision_receipt_result(value, command, binding, decision):
    if set(value) != {"result", "control_request_id", "control_request_digest"}:
        raise ValueError("a complete original decision receipt is required")
    TypeAdapter(Digest).validate_python(value["control_request_digest"], strict=True)
    if value["control_request_id"] != control_key("runtime-operation-" + decision, binding, command.decision_id):
        raise ValueError("the decision receipt belongs to another original request")
    return value["result"]


def _approval_receipt(value, command, binding, operation_id):
    result = _decision_receipt_result(value, command, binding, "approve")
    if (not isinstance(result, dict)
            or set(result) != {"approval_id", "grant_id", "description_digest", "approved_uses"}
            or result["approval_id"] != command.decision_id
            or result["description_digest"] != command.description_digest
            or type(result["approved_uses"]) is not int or result["approved_uses"] != command.uses):
        raise ValueError("receipt does not match the original approval")
    TypeAdapter(Identifier).validate_python(result["grant_id"], strict=True)
    return {"operation_id": operation_id, **result}


def _revocation_receipt(value, command, binding, operation_id, grant_id):
    result = _decision_receipt_result(value, command, binding, "revoke")
    if (not isinstance(result, dict) or set(result) != {"grant_id", "revoked", "revocation_revision"}
            or result["grant_id"] != grant_id or result["revoked"] is not True
            or type(result["revocation_revision"]) is not int
            or result["revocation_revision"] != command.expected_revocation_revision + 1):
        raise ValueError("receipt does not match the original grant revocation")
    return {"operation_id": operation_id, **result}


def _decision_command(value, *, decision):
    if decision == "approve":
        if not isinstance(value, dict) or any(
                name in value and type(value[name]) is not int for name in ("uses", "units")):
            raise ValueError("approval limits must be exact JSON integers")
        return OperationApproval.model_validate_json(_canonical(value))
    if decision == "revoke":
        return OperationRevocation.model_validate(value)
    return OperationRejection.model_validate(value)


def _observation(data, binding):
    if (data.get("schema_version") != "fa-operation-decision-state-v1"
            or data.get("purpose") != "observation-only-not-operation-permission"
            or data.get("binding_id") != binding):
        raise ValueError("the observation must identify the original binding and supported purpose")
    return OperationDecisionState.model_validate_json(json.dumps(data))


def parser():
    result = argparse.ArgumentParser(description="Read or decide an original operation once; reconcile decisions, never execute.")
    for option, environment in (
        ("api", "CONTEXT_API_URL"), ("tenant", "CONTEXT_TENANT_ID"),
        ("audience", "CONTEXT_API_AUDIENCE"), ("client-id", "CONTEXT_BEHAVIOR_CLIENT_ID"),
    ):
        result.add_argument("--" + option, default=os.environ.get(environment))
    actions = result.add_subparsers(dest="action", required=True)
    for action in ("show", "state", *_ATTEMPT_VERSIONS, "reconcile"):
        command = actions.add_parser(action)
        command.add_argument("binding")
        if action in _ATTEMPT_VERSIONS or action == "reconcile":
            command.add_argument("--request-file", type=Path, required=True)
    return result


def main(argv=None):
    options = parser()
    args = options.parse_args(argv)
    if not all((args.api, args.tenant, args.audience, args.client_id)):
        options.error("configure the API origin, tenant, audience and existing delegated behavior client ID")
    phase = "validation"
    try:
        origin = urlsplit(args.api)
        if (origin.scheme != "https" or not origin.hostname or not origin.hostname.endswith(".azurecontainerapps.io")
                or origin.username or origin.password or origin.query or origin.fragment
                or origin.path not in ("", "/") or origin.port not in (None, 443)):
            raise ValueError("the configured HTTPS Azure Container Apps origin is required")
        canonical_oid(args.tenant)
        canonical_oid(args.client_id)
        validate_api_audience(args.audience)
        TypeAdapter(Identifier).validate_python(args.binding, strict=True)
        api = args.api.rstrip("/")
        if args.action in _ATTEMPT_VERSIONS and not sys.stdin.isatty():
            raise ValueError("a human decision requires an interactive terminal; there is no automatic confirmation")
        command, attempt, frozen, operation_id, grant_id = None, None, None, None, None
        decision = args.action if args.action in _ATTEMPT_VERSIONS else None
        target = {}
        if decision is not None or args.action == "reconcile":
            original = _path(args.request_file)
            attempt = _path(original.with_name(original.name + ".attempt.json"))
            if decision is not None and attempt.exists():
                raise Problem(409, "RECONCILE_REQUIRED", "original attempt exists; only reconcile, never resubmit")
            version = _ATTEMPT_VERSIONS.get(decision)
            if args.action == "reconcile":
                saved = _read_file(attempt)
                version = saved["schema_version"]
                decision = next((name for name, schema in _ATTEMPT_VERSIONS.items() if schema == version), None)
                if decision is not None:
                    command = _decision_command(saved["command"], decision=decision)
                    if original.exists() and _decision_command(_read_file(original), decision=decision) != command:
                        raise ValueError("the request file differs from its immutable original command")
                elif version == "fa-operation-rejection-cli-attempt-v1":
                    decision = "reject"
                    command = OperationRejection.model_validate(_read_file(original))
                else:
                    raise ValueError("unknown attempt format; preserve the original files")
            else:
                command = _decision_command(_read_file(original), decision=decision)
            frozen = {
                "schema_version": version,
                "api": api, "tenant": args.tenant, "audience": args.audience, "client_id": args.client_id,
                "binding_id": args.binding,
                "command_sha256": hashlib.sha256(_canonical(command.model_dump(mode="json"))).hexdigest(),
            }
            if version in _ATTEMPT_VERSIONS.values():
                frozen["command"] = command.model_dump(mode="json")
            if args.action == "reconcile":
                operation_id = TypeAdapter(Identifier).validate_python(saved["operation_id"], strict=True)
                target = {"operation_id": operation_id}
                if decision == "revoke":
                    grant_id = TypeAdapter(Identifier).validate_python(saved["grant_id"], strict=True)
                    target["grant_id"] = grant_id
                if saved != {**frozen, **target}:
                    raise ValueError("original request or connection differs from its saved attempt")
        scopes = [args.audience + "/" + READ_SCOPE]
        if args.action in _ATTEMPT_VERSIONS:
            scopes.append(args.audience + "/" + APPROVAL_SCOPE)
        with _private_auth_logging(), InteractiveBrowserCredential(
            tenant_id=args.tenant, client_id=args.client_id, redirect_uri="http://localhost:8400",
            disable_automatic_authentication=True,
        ) as credential, httpx.Client(
            base_url=api, trust_env=False, follow_redirects=False, timeout=30,
            transport=httpx.HTTPTransport(retries=0, trust_env=False,
                verify=ssl.create_default_context(cafile=os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE"))),
        ) as http:
            phase = "authentication"
            credential.authenticate(scopes=scopes)
            token = lambda: credential.get_token(*scopes).token
            path = "/behavior/v1/bindings/" + args.binding + "/operation"
            if args.action == "state":
                phase = "state"
                data = _request(http, token, "GET", path + "/decision-state")
                observed = _observation(data, args.binding)
                print(_terminal_json(observed.model_dump(mode="json")))
                return 0
            if args.action != "reconcile":
                phase = "preview"
                operation_id, preview = _preview(_request(http, token, "GET", path), complete=decision in ("approve", "revoke"))
                print(_terminal_json(preview))
                if args.action == "show":
                    return 0
                if preview["description_digest"] != command.description_digest:
                    raise ValueError("the exact original operation must match the request file")
                target = {"operation_id": operation_id}
                if decision == "revoke":
                    phase = "state"
                    observed = _observation(_request(http, token, "GET", path + "/decision-state"), args.binding)
                    if (observed.operation_id != operation_id
                            or observed.description_digest != command.description_digest
                            or observed.adapter_contract_digest != preview["adapter_contract_digest"]
                            or observed.grant is None or observed.grant.revoked
                            or observed.grant.revocation_revision != command.expected_revocation_revision):
                        raise ValueError("revocation requires the exact observed original grant and revision")
                    grant_id = observed.grant.grant_id
                    target["grant_id"] = grant_id
                    print(_terminal_json(observed.model_dump(mode="json")))
                elif (preview["rejection_recorded"]
                        or preview["adapter_contract_digest"] != command.adapter_contract_digest):
                    raise ValueError("the exact original un-rejected operation must match the request file")
                print(_terminal_json(command.model_dump(mode="json")))
                phase = "confirmation"
                prompt = {
                    "approve":
                    "輸入 approve 批准以上精確操作及UTC期限／次數／單位；後續代理可依原grant執行。"
                    "此CLI不執行、不退款，也不把unknown改為未發生：\n",
                    "reject": "輸入 reject 拒絕以上精確操作；不取消工作、不退款，也不宣稱效果未發生：\n",
                    "revoke": "輸入 revoke 撤銷以上原grant及revision；不取消工作、不退款或逆轉已發生／未知效果：\n",
                }[decision]
                if input(prompt) != args.action:
                    raise ValueError("confirmation did not match; no decision submitted")
                phase = "persist-attempt"
                with attempt.open("xb") as stream:
                    stream.write(_canonical({**frozen, **target}) + b"\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                if os.name != "nt":
                    descriptor = os.open(attempt.parent, os.O_RDONLY | os.O_DIRECTORY)
                    try:
                        os.fsync(descriptor)
                    finally:
                        os.close(descriptor)
            phase = args.action
            action = _RECONCILE_PATHS[decision] if args.action == "reconcile" else "/" + decision
            value = _request(http, token, "POST", path + action, command.model_dump(mode="json"))
            if decision == "approve":
                result = _approval_receipt(value, command, args.binding, operation_id)
                output = {"state": "confirmed", "purpose": "original-approval-receipt-not-current-permission", "result": result}
            elif decision == "revoke":
                result = _revocation_receipt(value, command, args.binding, operation_id, grant_id)
                output = {"state": "confirmed", "purpose": "original-revocation-receipt-not-effect-outcome", "result": result}
            else:
                output = {"state": "confirmed", "result": _receipt(value, command, operation_id)}
            print(_terminal_json(output))
        return 0
    except Problem as error:
        print(json.dumps({**error.body, "phase": phase}, ensure_ascii=True), file=sys.stderr)
    except (ValueError, OSError, EOFError, KeyError, TypeError, ValidationError, ClientAuthenticationError) as error:
        print(json.dumps({
            "error": {"code": "OPERATION_CLI_UNCONFIRMED", "type": type(error).__name__,
                      "message": "未取得確認結果；保留原 request／attempt 檔。若已有 attempt，只能使用 reconcile。"},
            "phase": phase,
        }, ensure_ascii=True), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
