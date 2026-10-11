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
from .behavior_auth import APPROVAL_SCOPE, READ_SCOPE
from .behavior_operation_cli import _canonical, _json, _path, _read_file, _request, _terminal_json
from .behavior_operations import control_key
from .behavior_wire import Digest, ExactSourceRef, Identifier
from .config import validate_api_audience
from .errors import Problem
from .human_cli_logging import private_auth_logging
from .memory_governance import ClaimReference, ReviewedClaim, claim_digest
from .memory_note_proposal_bridge import NoteBridgeCommand, ReconcileNoteBridge, ReviewNoteBridge
from .memory_bridge_settings import MemoryBridgeSettingsView, SelectMemoryBridgeSettings
from .behavior_work_memory import WorkMemorySource
from .memory_note_proposals import NoteProposalCommand, NoteProposalView
from .memory_candidates import CandidateView
from .memory_note_synthesis import NoteSynthesisCommand, NoteSynthesisView


MAX_RESPONSE_BYTES = 128 * 1024
REVIEW_CHECKS = frozenset({"necessity", "support", "contradiction", "sensitivity", "consent"})


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
    result = argparse.ArgumentParser(description="Inspect reviewed notes or explicitly share an inactive fragment proposal.")
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
    for action in ("propose", "proposal-reconcile"):
        command = actions.add_parser(action)
        command.add_argument("binding")
        command.add_argument("--request-file", required=True)
        command.add_argument("--synthesis", action="store_true", help="Use the two-turn original-evidence proposal path.")
    proposal = actions.add_parser("proposal")
    proposal.add_argument("binding")
    proposal.add_argument("request_id")
    proposal.add_argument("--synthesis", action="store_true")
    bridge = actions.add_parser("bridge")
    bridge.add_argument("binding")
    bridge.add_argument("request_id", help="The original pending proposal request ID.")
    bridge.add_argument("--stage", choices=("prepare", "preview", "review", "commit"), required=True)
    bridge.add_argument("--request-file", required=True)
    bridge.add_argument("--reconcile", action="store_true", help="Only read the original stage result.")
    bridge.add_argument("--synthesis", action="store_true", help="Review a synthesis against its original evidence bundle.")
    actions.add_parser("memory-settings")
    for action in ("select-memory-settings", "memory-settings-reconcile"):
        command = actions.add_parser(action)
        command.add_argument("--request-file", required=True)
    return result


def _proposal_input(args):
    original = _path(args.request_file)
    synthesis = getattr(args, "synthesis", False)
    model = NoteSynthesisCommand if synthesis else NoteProposalCommand
    attempt = _path(original.with_name(original.name + (".synthesis" if synthesis else "") + ".attempt.json"))
    if args.action == "propose":
        if not sys.stdin.isatty():
            raise ValueError("sharing requires an interactive terminal")
        if attempt.exists():
            raise Problem(409, "NOTE_PROPOSAL_RECONCILE_REQUIRED", "use the original proposal receipt lookup")
        command = model.model_validate_json(_canonical(_read_file(original)))
    else:
        saved = _read_file(attempt)
        command = model.model_validate_json(_canonical(saved["command"]))
        if original.exists() and model.model_validate_json(_canonical(_read_file(original))) != command:
            raise ValueError("the original proposal command differs")
    frozen = {
        "schema_version": "fa-note-synthesis-cli-attempt-v1" if synthesis else "fa-note-proposal-cli-attempt-v1",
        "api": args.api.rstrip("/"),
        "tenant": args.tenant, "audience": args.audience, "client_id": args.client_id,
        "binding_id": args.binding, "command": command.model_dump(mode="json"),
    }
    if args.action == "proposal-reconcile" and saved != frozen:
        raise ValueError("the original proposal or connection differs")
    return command, attempt, frozen


def _proposal_action(args, http, token, command, attempt, frozen):
    synthesis = getattr(args, "synthesis", False)
    model = NoteSynthesisView if synthesis else NoteProposalView
    prefix = "/behavior/v1/bindings/" + args.binding + ("/note-syntheses" if synthesis else "/note-proposals")
    if args.action == "proposal":
        value = model.model_validate_json(_canonical(_request(
            http, token, "GET", prefix + "/" + args.request_id)))
        if value.binding_id != args.binding or value.request_id != args.request_id or value.state != "pending_review":
            raise ValueError("the original pending proposal differs")
        print(_terminal_json(value.model_dump(mode="json")))
        return
    body = command.model_dump(mode="json")
    if args.action == "propose":
        preview = model.model_validate_json(_canonical(_request(http, token, "POST", prefix + "/preview", body)))
        if (preview.binding_id != args.binding or preview.request_id != command.request_id
                or preview.team_id != command.share_team_id or preview.work_id != command.share_work_id
                or preview.state != "not_submitted"):
            raise ValueError("the server preview does not match the exact sharing command")
        if synthesis:
            sources = (preview.summary_source, *(fragment.source for fragment in preview.evidence.fragments))
            selections = (command.summary, *command.supporting)
            if any(source.conversation_id != command.conversation_id
                   or any(getattr(source, field) != getattr(selection, field)
                          for field in ("turn_number", "source_sha256", "start_byte", "end_byte"))
                   for source, selection in zip(sources, selections, strict=True)):
                raise ValueError("the synthesis preview differs from the selected originals")
        elif any(getattr(preview.source, name) != getattr(command, name) for name in
                 ("conversation_id", "turn_number", "speaker", "source_sha256", "start_byte", "end_byte")):
            raise ValueError("the server preview does not match the exact sharing command")
        print(_terminal_json(preview.model_dump(mode="json")))
        confirmation = "share-synthesis" if synthesis else "share-note"
        if input("輸入 " + confirmation + " 將以上精確片段分享給原Team/work並建立未生效提案；目前不會成為有效記憶：\n") != confirmation:
            raise ValueError("sharing was not confirmed")
        with attempt.open("xb") as stream:
            stream.write(_canonical(frozen) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        if os.name != "nt":
            descriptor = os.open(attempt.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    response = _request(http, token, "POST", prefix + ("/reconcile" if args.action == "proposal-reconcile" else ""), body)
    if set(response) != {"result", "control_request_id", "control_request_digest"}:
        raise ValueError("a complete original proposal receipt is required")
    TypeAdapter(Digest).validate_python(response["control_request_digest"], strict=True)
    result = response["result"]
    fields = {"proposal_id", "request_id", "state", "effective"} | (set() if synthesis else {"bridge_available"})
    if (not isinstance(result, dict) or set(result) != fields
            or result.get("request_id") != command.request_id
            or result.get("state") != "pending_review" or result.get("effective") is not False
            or not synthesis and result.get("bridge_available") is not False):
        raise ValueError("no original inactive proposal receipt was confirmed")
    TypeAdapter(Identifier).validate_python(result["proposal_id"], strict=True)
    kind = "memory-note-synthesis-write" if synthesis else "memory-note-proposal-write"
    if response["control_request_id"] != control_key(kind, result["proposal_id"]):
        raise ValueError("proposal receipt identity differs")
    print(_terminal_json({"state": "confirmed", "result": result}))


def _bridge_input(args):
    if args.stage == "preview" and args.reconcile:
        raise ValueError("a read-only preview has no submitted stage to reconcile")
    original = _path(args.request_file)
    synthesis = getattr(args, "synthesis", False)
    family = ".synthesis-bridge-" if synthesis else ".bridge-"
    attempt = _path(original.with_name(original.name + family + args.stage + ".attempt.json"))
    model = ReviewNoteBridge if args.stage == "review" else NoteBridgeCommand
    expected = {}
    if args.reconcile:
        saved = _read_file(attempt)
        command = model.model_validate_json(_canonical(saved["command"]))
        if original.exists() and model.model_validate_json(_canonical(_read_file(original))) != command:
            raise ValueError("the original bridge command differs")
        expected = saved["expected"]
    else:
        if args.stage != "preview":
            if not sys.stdin.isatty():
                raise ValueError("preparing, reviewing and committing a note require an interactive human")
            if attempt.exists():
                raise Problem(409, "NOTE_BRIDGE_RECONCILE_REQUIRED", "only reconcile the original attempted stage")
        command = model.model_validate_json(_canonical(_read_file(original)))
    frozen = {
        "schema_version": "fa-note-synthesis-bridge-cli-attempt-v1" if synthesis else "fa-note-bridge-cli-attempt-v1",
        "api": args.api.rstrip("/"), "tenant": args.tenant, "audience": args.audience,
        "client_id": args.client_id, "binding_id": args.binding, "proposal_request_id": args.request_id,
        "stage": args.stage, "command": command.model_dump(mode="json"),
    }
    if args.reconcile and saved != {**frozen, "expected": expected}:
        raise ValueError("the original bridge target or connection differs")
    return command, attempt, frozen, expected


def _bridge_preview(value, proposal, mapping, bridge_id, binding, tenant):
    source = ExactSourceRef.model_validate_json(_canonical(value["source"]))
    claim = ClaimReference.model_validate_json(_canonical(value["claim"]))
    returned_mapping = NoteBridgeCommand.model_validate_json(_canonical(value["mapping"]))
    synthesis = isinstance(proposal, NoteSynthesisView)
    text = proposal.evidence.original() if synthesis else proposal.text.encode("utf-8")
    if (value["bridge_id"] != bridge_id or value["proposal_id"] != proposal.proposal_id
            or value["binding_id"] != binding or returned_mapping != mapping
            or value["text"] != proposal.text or value["effective"] is not False
            or source.source_id != mapping.source_id or source.version != 1
            or source.sha256 != hashlib.sha256(text).hexdigest() or source.byte_length != len(text)
            or claim.claim_id != mapping.claim_id or claim.version != mapping.claim_version
            or claim.scope.visibility != "team" or claim.scope.team_id != proposal.team_id
            or claim.scope.tenant_id != tenant
            or claim.sha256 != value["review"]["candidate_sha256"]):
        raise ValueError("the review preview does not match the original proposal and mapping")
    if synthesis and (value.get("evidence") != proposal.evidence.model_dump(mode="json")
                      or value.get("summary_source") != proposal.summary_source.model_dump(mode="json")):
        raise ValueError("the review must show the original evidence, not just the summary")
    return source, claim


def _bridge_result(value, stage, expected):
    bridge_id = TypeAdapter(Digest).validate_python(expected["bridge_id"], strict=True)
    if stage == "commit":
        if set(value) != {"result", "control_request_id", "control_request_digest"}:
            raise ValueError("a complete original claim commit receipt is required")
        claim = ClaimReference.model_validate_json(_canonical(value["result"]["claim"]))
        if (claim.model_dump(mode="json") != expected["claim"]
                or value["control_request_id"] != control_key("memory-review", claim.key)
                or type(value["result"]["sequence"]) is not int or value["result"]["sequence"] < 1):
            raise ValueError("the original claim commit receipt differs")
        TypeAdapter(Digest).validate_python(value["control_request_digest"], strict=True)
        return {"bridge_id": bridge_id, "state": "committed", "claim": claim.model_dump(mode="json"),
                "usage": "original-claim-commit-not-operation-permission"}
    state = "source_ready" if stage == "prepare" else "reviewed"
    if value["bridge_id"] != bridge_id or value["state"] != state or value["effective"] is not False:
        raise ValueError("the original bridge stage result differs")
    if stage == "prepare":
        source = ExactSourceRef.model_validate_json(_canonical(value["source"]))
        if source.model_dump(mode="json") != expected["source"]:
            raise ValueError("the prepared source differs from the original proposal")
    return {"bridge_id": bridge_id, "state": state, "effective": False}


def _bridge_action(args, http, token, command, attempt, frozen, expected):
    mapping = command.mapping if args.stage == "review" else command
    synthesis = getattr(args, "synthesis", False)
    family = "/note-syntheses/" if synthesis else "/note-proposals/"
    prefix = "/behavior/v1/bindings/" + args.binding + family + args.request_id
    body = command.model_dump(mode="json")
    if args.reconcile:
        lookup = ReconcileNoteBridge(
            mapping=mapping, stage=args.stage, review=command if args.stage == "review" else None)
        value = _request(http, token, "POST", prefix + "/bridge/reconcile", lookup.model_dump(mode="json"))
        print(_terminal_json(_bridge_result(value, args.stage, expected)))
        return
    model = NoteSynthesisView if synthesis else NoteProposalView
    proposal = model.model_validate_json(_canonical(_request(http, token, "GET", prefix)))
    if (proposal.binding_id != args.binding or proposal.request_id != args.request_id
            or proposal.state != "pending_review"):
        raise ValueError("the original pending proposal differs")
    bridge_id = control_key("memory-note-bridge", proposal.proposal_id, mapping.request_id)
    expected = {"bridge_id": bridge_id}
    if args.stage == "prepare":
        raw = proposal.evidence.original() if synthesis else proposal.text.encode("utf-8")
        expected["source"] = ExactSourceRef(source_id=mapping.source_id, version=1,
            sha256=hashlib.sha256(raw).hexdigest(), byte_length=len(raw)).model_dump(mode="json")
        print(_terminal_json({"proposal": proposal.model_dump(mode="json"), "mapping": body}))
    else:
        preview = _request(http, token, "POST", prefix + "/bridge/preview", mapping.model_dump(mode="json"))
        _, claim = _bridge_preview(preview, proposal, mapping, bridge_id, args.binding, args.tenant)
        if args.stage == "review" and any(getattr(command, name) != preview["review"][name] for name in
                ("candidate_sha256", "evidence_sha256", "policy_version")):
            raise ValueError("human judgments must name the exact current preview")
        print(_terminal_json(preview))
        if args.stage == "preview":
            return
        if args.stage == "commit":
            expected["claim"] = claim.model_dump(mode="json")
        else:
            print(_terminal_json({"reviewed_checks": list(command.reviewed_checks)}))
    confirmation = {"prepare": "prepare-note", "review": "review-note", "commit": "commit-note"}[args.stage]
    if input("輸入 " + confirmation + " 確認以上精確步驟；只在原允許範圍執行，未確認結果不得重送：\n") != confirmation:
        raise ValueError("the bridge stage was not confirmed")
    with attempt.open("xb") as stream:
        stream.write(_canonical({**frozen, "expected": expected}) + b"\n")
        stream.flush()
        os.fsync(stream.fileno())
    if os.name != "nt":
        descriptor = os.open(attempt.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    value = _request(http, token, "POST", prefix + "/bridge/" + args.stage, body)
    print(_terminal_json(_bridge_result(value, args.stage, expected)))


def _settings_input(args):
    original = _path(args.request_file)
    attempt = _path(original.with_name(original.name + ".memory-settings.attempt.json"))
    if args.action == "select-memory-settings":
        if not sys.stdin.isatty():
            raise ValueError("a future-conversation data scope requires an interactive human")
        if attempt.exists():
            raise Problem(409, "MEMORY_SETTINGS_RECONCILE_REQUIRED", "only reconcile the original memory setting")
        command = SelectMemoryBridgeSettings.model_validate_json(_canonical(_read_file(original)))
    else:
        saved = _read_file(attempt)
        command = SelectMemoryBridgeSettings.model_validate_json(_canonical(saved["command"]))
        if original.exists() and SelectMemoryBridgeSettings.model_validate_json(_canonical(_read_file(original))) != command:
            raise ValueError("the original future-conversation setting differs")
    frozen = {
        "schema_version": "fa-memory-settings-cli-attempt-v1", "api": args.api.rstrip("/"),
        "tenant": args.tenant, "audience": args.audience, "client_id": args.client_id,
        "command": command.model_dump(mode="json"),
    }
    if args.action == "memory-settings-reconcile" and saved != frozen:
        raise ValueError("the original memory setting or connection differs")
    return command, attempt, frozen


def _settings_action(args, http, token, command, attempt, frozen):
    prefix = "/operator/v1/memory-bridge-settings"
    if args.action == "memory-settings":
        value = MemoryBridgeSettingsView.model_validate_json(_canonical(_request(http, token, "GET", prefix)))
        print(_terminal_json(value.model_dump(mode="json")))
        return
    body = command.model_dump(mode="json")
    if args.action == "select-memory-settings":
        preview = _request(http, token, "POST", prefix + "/preview", body)
        if (set(preview) != {"command", "applies_to", "data_scope_change", "submitted", "note"}
                or SelectMemoryBridgeSettings.model_validate_json(_canonical(preview["command"])) != command
                or preview["applies_to"] != "new-conversations-only"
                or preview["data_scope_change"] is not True or preview["submitted"] is not False
                or (preview["note"] is None) != (command.recall is None)):
            raise ValueError("the future-conversation preview differs from the original setting")
        if command.recall is not None:
            note = preview["note"]
            if (not isinstance(note, dict) or set(note) != {"origin_binding_id", "text", "source", "usage"}
                    or not isinstance(note["text"], str)):
                raise ValueError("a complete reviewed-note preview is required")
            source = WorkMemorySource.model_validate_json(_canonical(note["source"]))
            text = note["text"].encode("utf-8")
            if (note["origin_binding_id"] != command.recall.origin_binding_id
                    or note["usage"] != "knowledge-data-not-permission"
                    or source.claim_id != command.recall.claim_id
                    or source.claim_version != command.recall.expected_claim_version
                    or source.claim_sha256 != command.recall.expected_claim_sha256
                    or source.text_utf8_bytes != len(text)
                    or source.text_sha256 != hashlib.sha256(text).hexdigest()):
                raise ValueError("the selected reviewed note differs from the human's original choice")
        print(_terminal_json(preview))
        if input("輸入 select-memory 確認只改之後新對話的記憶資料範圍；不回寫舊對話或自動建立記憶：\n") != "select-memory":
            raise ValueError("the new-conversation setting was not confirmed")
        with attempt.open("xb") as stream:
            stream.write(_canonical(frozen) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        if os.name != "nt":
            descriptor = os.open(attempt.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    route = prefix + ("/reconcile" if args.action == "memory-settings-reconcile" else "")
    response = _request(http, token, "POST", route, body)
    if set(response) != {"result", "control_request_id", "control_request_digest"}:
        raise ValueError("a complete original setting receipt is required")
    result = MemoryBridgeSettingsView.model_validate_json(_canonical(response["result"]))
    choice = None if command.recall is None else {
        "origin_binding_id": command.recall.origin_binding_id, "claim_id": command.recall.claim_id,
        "version_policy": "current-human-reviewed-version-at-new-conversation",
    }
    if (result.settings_id is None or result.enabled != command.enabled
            or (None if result.recall is None else result.recall.model_dump(mode="json")) != choice
            or response["control_request_id"] != result.settings_id):
        raise ValueError("the original memory setting receipt differs")
    TypeAdapter(Digest).validate_python(response["control_request_digest"], strict=True)
    print(_terminal_json({"state": "confirmed", "settings": result.model_dump(mode="json")}))


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
        settings_action = args.action in {"memory-settings", "select-memory-settings", "memory-settings-reconcile"}
        if not settings_action:
            TypeAdapter(Identifier).validate_python(args.binding, strict=True)
        if args.action in {"show", "proposal", "bridge"}:
            TypeAdapter(Identifier).validate_python(
                args.claim_id if args.action == "show" else args.request_id, strict=True)
        command, attempt, frozen, expected = None, None, None, None
        if args.action in {"propose", "proposal-reconcile"}:
            command, attempt, frozen = _proposal_input(args)
        elif args.action == "bridge":
            command, attempt, frozen, expected = _bridge_input(args)
        elif args.action in {"select-memory-settings", "memory-settings-reconcile"}:
            command, attempt, frozen = _settings_input(args)
        scopes = [args.audience + "/" + READ_SCOPE]
        if args.action == "select-memory-settings" or args.action == "bridge" and not args.reconcile:
            scopes.append(args.audience + "/" + APPROVAL_SCOPE)
        with private_auth_logging(), InteractiveBrowserCredential(
            tenant_id=args.tenant, client_id=args.client_id, redirect_uri="http://localhost:8400",
            disable_automatic_authentication=True,
        ) as credential, httpx.Client(
            base_url=args.api.rstrip("/"), trust_env=False, follow_redirects=False, timeout=30,
            transport=httpx.HTTPTransport(retries=0, trust_env=False,
                verify=ssl.create_default_context(cafile=os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE"))),
        ) as http:
            phase = "authentication"
            credential.authenticate(scopes=scopes)
            token = lambda: credential.get_token(*scopes).token
            if settings_action:
                phase = args.action
                _settings_action(args, http, token, command, attempt, frozen)
                return 0
            if args.action == "bridge":
                phase = "bridge-" + args.stage + ("-reconcile" if args.reconcile else "")
                _bridge_action(args, http, token, command, attempt, frozen, expected)
                return 0
            if args.action == "candidate":
                phase = "candidate-read"
                value = CandidateView.model_validate_json(_canonical(_request(
                    http, token, "GET", "/behavior/v1/bindings/" + args.binding + "/memory-candidate")))
                if value.binding_id != args.binding:
                    raise ValueError("the candidate belongs to another original binding")
                print(_terminal_json(value.model_dump(mode="json")))
                return 0
            if args.action != "show":
                phase = args.action
                _proposal_action(args, http, token, command, attempt, frozen)
                return 0
            phase = "read"
            path = "/behavior/v1/bindings/" + args.binding + "/claims/" + args.claim_id
            value = _read(http, token, path)
            phase = "response"
            result = _view(value, args.binding, args.tenant, args.claim_id)
            if args.with_sources:
                phase = "sources"
                originals = [_read_original(http, token, args.binding, reference) for reference in result["supporting"]]
                phase = "recheck"
                current = _view(_read(http, token, path), args.binding, args.tenant, args.claim_id)
                if current != result:
                    raise Problem(409, "MEMORY_NOTE_CHANGED", "the current note changed while its sources were read")
                result = {**result, "originals": originals}
            print(_terminal_json(result))
        return 0
    except Problem as error:
        print(json.dumps({**error.body, "phase": phase}, ensure_ascii=True), file=sys.stderr)
    except ClientAuthenticationError:
        print(json.dumps({"error": {"code": "MEMORY_NOTE_AUTHENTICATION_FAILED",
                                   "message": "未取得憑證；若已有提案attempt，保留原件並查回條。"},
                          "phase": phase}, ensure_ascii=True), file=sys.stderr)
    except (ValueError, OSError, KeyError, TypeError):
        print(json.dumps({"error": {"code": "MEMORY_NOTE_CLI_UNCONFIRMED",
                                   "message": "未取得確認結果；不使用舊內容代替，已有提案attempt時只查原回條。"},
                          "phase": phase}, ensure_ascii=True), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
