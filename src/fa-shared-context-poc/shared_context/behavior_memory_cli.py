"""Inspect a current reviewed Team note; never create memory or operation authority."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import ssl
import sys
from urllib.parse import urlsplit

from azure.core.exceptions import ClientAuthenticationError
from azure.identity import InteractiveBrowserCredential
import httpx
from pydantic import TypeAdapter

from .backup_models import canonical_oid
from .behavior_auth import READ_SCOPE
from .behavior_wire import Identifier
from .config import validate_api_audience
from .errors import Problem
from .human_cli_logging import private_auth_logging
from .memory_governance import ReviewedClaim, claim_digest
from .memory_candidates import CandidateView


MAX_RESPONSE_BYTES = 128 * 1024
REVIEW_CHECKS = frozenset({"necessity", "support", "contradiction", "sensitivity", "consent"})


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


def _read(http, token, path):
    try:
        with http.stream("GET", path, headers={"Authorization": "Bearer " + token()}, follow_redirects=False) as response:
            if response.status_code != 200:
                raise Problem(response.status_code, "MEMORY_NOTE_UNAVAILABLE",
                              "no current reviewed note was confirmed", upstream_status=response.status_code)
            raw = bytearray()
            for chunk in response.iter_bytes():
                if len(raw) + len(chunk) > MAX_RESPONSE_BYTES:
                    raise Problem(502, "MEMORY_NOTE_RESPONSE_INVALID", "the complete note response exceeds its bound")
                raw.extend(chunk)
            value = _json(bytes(raw))
            if not isinstance(value, dict):
                raise ValueError("a complete object response is required")
            return value
    except httpx.HTTPError:
        raise Problem(503, "MEMORY_NOTE_READ_UNKNOWN", "note lookup was not confirmed; no cached note was used") from None
    except (ValueError, TypeError):
        raise Problem(502, "MEMORY_NOTE_RESPONSE_INVALID", "the response cannot confirm a reviewed note") from None


def _view(value, binding, tenant, claim_id):
    if type(value.get("schema_version")) is not int or value["schema_version"] != 1:
        raise ValueError("a known explicit reviewed-claim schema is required")
    claim = ReviewedClaim.model_validate_json(json.dumps(value, allow_nan=False))
    candidate, reference, review = claim.candidate, claim.reference, claim.review
    scope = reference.scope
    if (scope.tenant_id != tenant or scope.visibility != "team" or candidate.visibility != "team"
            or not claim.human_reviewed
            or reference.claim_id != claim_id or candidate.claim_id != claim_id
            or reference.version != candidate.version or reference.subject != candidate.subject
            or reference.sha256 != claim_digest(candidate, scope)
            or review.candidate_sha256 != reference.sha256
            or len(review.reviewed_checks) != 5 or set(review.reviewed_checks) != REVIEW_CHECKS
            or any(source.scope != scope for source in candidate.supporting)):
        raise ValueError("the response differs from the original reviewed Team note")
    times = candidate.model_dump(mode="json", include={"effective_from", "expires_at"})
    return {
        "usage": "knowledge-data-not-permission",
        "binding_id": binding,
        "reference": reference.model_dump(mode="json", include={"claim_id", "version", "sha256"}),
        "team_id": scope.team_id, "subject": candidate.subject, "kind": candidate.kind,
        "purpose": candidate.purpose, "text": candidate.text, **times,
        "human_reviewed": True,
        "supporting": [source.model_dump(mode="json", exclude={"scope"}) for source in candidate.supporting],
    }


def _read_original(http, token, binding, reference):
    path = ("/behavior/v1/bindings/" + binding + "/sources/" + reference["source_id"]
            + "/versions/" + str(reference["version"]))
    try:
        with http.stream("GET", path, headers={"Authorization": "Bearer " + token()}, follow_redirects=False) as response:
            if response.status_code != 200:
                raise Problem(response.status_code, "MEMORY_NOTE_SOURCE_UNAVAILABLE",
                              "an original source was not available", upstream_status=response.status_code)
            media_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if (response.headers.get_list("x-source-version") != [str(reference["version"])]
                    or response.headers.get_list("x-source-sha256") != [reference["sha256"]]
                    or media_type not in {"text/plain", "text/markdown", "application/json"}):
                raise ValueError("source version, hash or text media type differs")
            raw = bytearray()
            limit = min(reference["byte_length"], MAX_RESPONSE_BYTES)
            for chunk in response.iter_bytes():
                if len(raw) + len(chunk) > limit:
                    raise ValueError("the complete source exceeds its expected byte length")
                raw.extend(chunk)
            if len(raw) != reference["byte_length"] or hashlib.sha256(raw).hexdigest() != reference["sha256"]:
                raise ValueError("source bytes differ from the original reference")
            return {"source": reference, "text": raw.decode("utf-8")}
    except httpx.HTTPError:
        raise Problem(503, "MEMORY_NOTE_SOURCE_UNKNOWN", "source lookup was not confirmed; nothing was displayed") from None
    except (ValueError, TypeError):
        raise Problem(502, "MEMORY_NOTE_SOURCE_INVALID", "a complete exact original text source is required") from None


def parser():
    result = argparse.ArgumentParser(description="Inspect reviewed notes or this turn's inactive suggestion; never create memory.")
    for option, environment in (
        ("api", "CONTEXT_API_URL"), ("tenant", "CONTEXT_TENANT_ID"),
        ("audience", "CONTEXT_API_AUDIENCE"), ("client-id", "CONTEXT_BEHAVIOR_CLIENT_ID"),
    ):
        result.add_argument("--" + option, default=os.environ.get(environment))
    actions = result.add_subparsers(dest="action", required=True)
    show = actions.add_parser("show")
    show.add_argument("binding")
    show.add_argument("claim_id")
    show.add_argument("--with-sources", action="store_true", help="Read the note's exact direct text sources as well.")
    candidate = actions.add_parser("candidate", help="Read this turn's inactive suggestion; never share or activate it.")
    candidate.add_argument("binding")
    return result


def _check_arguments(args):
    origin = urlsplit(args.api)
    if (origin.scheme != "https" or not origin.hostname or not origin.hostname.endswith(".azurecontainerapps.io")
            or origin.username or origin.password or origin.query or origin.fragment
            or origin.path not in ("", "/") or origin.port not in (None, 443)):
        raise ValueError("the configured HTTPS Azure Container Apps origin is required")
    canonical_oid(args.tenant)
    canonical_oid(args.client_id)
    validate_api_audience(args.audience)
    TypeAdapter(Identifier).validate_python(args.binding, strict=True)
    if args.action == "show":
        TypeAdapter(Identifier).validate_python(args.claim_id, strict=True)


def _scopes(args):
    return [args.audience + "/" + READ_SCOPE]


def _http_client(args):
    return httpx.Client(
        base_url=args.api.rstrip("/"), trust_env=False, follow_redirects=False, timeout=30,
        transport=httpx.HTTPTransport(retries=0, trust_env=False,
            verify=ssl.create_default_context(cafile=os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE"))),
    )


def _candidate_action(args, http, token):
    value = CandidateView.model_validate_json(_canonical(_request(
        http, token, "GET", "/behavior/v1/bindings/" + args.binding + "/memory-candidate")))
    if value.binding_id != args.binding:
        raise ValueError("the candidate belongs to another original binding")
    print(_terminal_json(value.model_dump(mode="json")))


def _show_action(args, http, token, phase):
    phase[0] = "read"
    path = "/behavior/v1/bindings/" + args.binding + "/claims/" + args.claim_id
    value = _read(http, token, path)
    phase[0] = "response"
    result = _view(value, args.binding, args.tenant, args.claim_id)
    if args.with_sources:
        phase[0] = "sources"
        originals = [_read_original(http, token, args.binding, reference) for reference in result["supporting"]]
        phase[0] = "recheck"
        current = _view(_read(http, token, path), args.binding, args.tenant, args.claim_id)
        if current != result:
            raise Problem(409, "MEMORY_NOTE_CHANGED", "the current note changed while its sources were read")
        result = {**result, "originals": originals}
    print(_terminal_json(result))


def _run_action(args, http, token, phase):
    """Run the chosen action; ``phase[0]`` is the step a failure is reported in."""
    if args.action == "candidate":
        phase[0] = "candidate-read"
        _candidate_action(args, http, token)
    else:
        _show_action(args, http, token, phase)


def main(argv=None):
    options = parser()
    args = options.parse_args(argv)
    if not all((args.api, args.tenant, args.audience, args.client_id)):
        options.error("configure the API origin, tenant, audience and existing delegated behavior client ID")
    phase = ["validation"]
    try:
        _check_arguments(args)
        scopes = _scopes(args)
        with private_auth_logging(), InteractiveBrowserCredential(
            tenant_id=args.tenant, client_id=args.client_id, redirect_uri="http://localhost:8400",
            disable_automatic_authentication=True,
        ) as credential, _http_client(args) as http:
            phase[0] = "authentication"
            credential.authenticate(scopes=scopes)
            token = lambda: credential.get_token(*scopes).token
            _run_action(args, http, token, phase)
        return 0
    except Problem as error:
        print(json.dumps({**error.body, "phase": phase[0]}, ensure_ascii=True), file=sys.stderr)
    except ClientAuthenticationError:
        print(json.dumps({"error": {"code": "MEMORY_NOTE_AUTHENTICATION_FAILED",
                                   "message": "未取得憑證；若已有提案attempt，保留原件並查回條。"},
                          "phase": phase[0]}, ensure_ascii=True), file=sys.stderr)
    except (ValueError, OSError, KeyError, TypeError):
        print(json.dumps({"error": {"code": "MEMORY_NOTE_CLI_UNCONFIRMED",
                                   "message": "未取得確認結果；不使用舊內容代替，已有提案attempt時只查原回條。"},
                          "phase": phase[0]}, ensure_ascii=True), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
