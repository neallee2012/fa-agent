from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
from pathlib import Path
from typing import Callable, TypeVar
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from azure.core.exceptions import ClientAuthenticationError
from azure.identity import InteractiveBrowserCredential

from .backup_models import canonical_oid
from .config import validate_api_audience
from .errors import Problem, safe_text
from .human_cli_logging import private_auth_logging
from .m3_auth import APPROVAL_SCOPE
from .m3_client import ArtifactClient
from .m3_models import ApprovalInput, SkillDeactivationInput, validate_id

T = TypeVar("T")


def _read_with_retry(operation: Callable[[], T]) -> T:
    try:
        return operation()
    except Problem as exc:
        if exc.body["error"]["code"] != "M3_REQUEST_UNKNOWN" or not isinstance(exc.__cause__, httpx.TimeoutException):
            raise
        print(json.dumps({
            "warning": "M3_READ_RETRY", "transport_error": type(exc.__cause__).__name__,
            "message": "retrying this read once; decisions are never retried",
        }), file=sys.stderr)
    return operation()


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="M3 artifact inspection and explicit human decisions; no agent or cloud provisioning.")
    for option, env in (
        ("api", "CONTEXT_API_URL"), ("tenant", "CONTEXT_TENANT_ID"), ("audience", "CONTEXT_API_AUDIENCE"),
        ("client-id", "CONTEXT_M3_CLIENT_ID"), ("team", "CONTEXT_M3_TEAM"),
    ):
        result.add_argument("--" + option, default=os.environ.get(env))
    actions = result.add_subparsers(dest="action", required=True)
    actions.add_parser("list")
    for action in ("show", "download", "approve", "reject", "deactivate"):
        command = actions.add_parser(action)
        command.add_argument("artifact")
        if action != "show":
            command.add_argument("--version", required=True)
        if action == "download":
            command.add_argument("--output", type=Path, required=True)
        if action in ("approve", "reject", "deactivate"):
            command.add_argument("--sha256", required=True)
            command.add_argument("--metadata-sha256", default=None)
            command.add_argument("--decision-id", default=None)
    return result


def main(argv: list[str] | None = None) -> int:
    options = parser()
    args = options.parse_args(argv)
    if not all((args.api, args.tenant, args.audience, args.client_id, args.team)):
        options.error("configure the API URL, tenant, audience, dedicated public client ID and team")
    phase = "validation"
    try:
        url = urlsplit(args.api)
        if (url.scheme != "https" or not url.hostname or not url.hostname.endswith(".azurecontainerapps.io")
                or url.username or url.password or url.query or url.fragment
                or url.path not in ("", "/") or url.port not in (None, 443)):
            raise ValueError("M3 API must be the configured HTTPS Azure Container Apps origin")
        canonical_oid(args.tenant)
        canonical_oid(args.client_id)
        validate_api_audience(args.audience)
        validate_id(args.team)
        if args.action != "list":
            validate_id(args.artifact)
        if args.action not in ("list", "show"):
            validate_id(args.version, version=True)
        if args.action == "download" and (args.output.exists() or args.output.is_symlink()):
            raise FileExistsError("download never replaces an existing path")
        if args.action in ("approve", "reject", "deactivate") and not sys.stdin.isatty():
            raise ValueError("human decisions require an interactive terminal; there is no automatic approval switch")
        scope = args.audience + "/" + APPROVAL_SCOPE
        with private_auth_logging(), InteractiveBrowserCredential(
            tenant_id=args.tenant, client_id=args.client_id, redirect_uri="http://localhost:8400",
            disable_automatic_authentication=True,
        ) as credential, httpx.Client(
            base_url=args.api.rstrip("/"), trust_env=False, timeout=30, follow_redirects=False,
            transport=httpx.HTTPTransport(
                retries=0, trust_env=False,
                verify=ssl.create_default_context(cafile=os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE")),
            ),
        ) as http:
            phase = "authentication"
            credential.authenticate(scopes=[scope])
            client = ArtifactClient(http, args.team, lambda: credential.get_token(scope).token)
            if args.action == "list":
                phase = "metadata"
                entries = _read_with_retry(client.all_skill_reviews)
                print(json.dumps({"team": args.team, "entries": [entry.model_dump(mode="json") for entry in entries]},
                                 ensure_ascii=True, indent=2))
            elif args.action == "show":
                phase = "metadata"
                print(json.dumps(_read_with_retry(lambda: client.get(args.artifact)).response(), ensure_ascii=True, indent=2))
            elif args.action == "download":
                phase = "download"
                version = _read_with_retry(lambda: client.download(args.artifact, args.version, args.output))
                print(json.dumps({"output": str(args.output), "version_id": version.version_id, "sha256": version.sha256}))
            else:
                phase = "metadata"
                snapshot = _read_with_retry(lambda: client.get(args.artifact))
                version = client.version(snapshot, args.version)
                if args.action == "deactivate":
                    if snapshot.payload["kind"] != "skill" or snapshot.payload["effective_version_id"] != args.version:
                        raise ValueError("select the currently effective skill version")
                    body = SkillDeactivationInput(
                        schema_version="fa-skill-deactivation-v1",
                        decision_id=args.decision_id or uuid4().hex, version_id=args.version,
                        sha256=args.sha256, metadata_sha256=args.metadata_sha256,
                    )
                else:
                    if snapshot.payload["kind"] not in ("sop", "skill") or snapshot.payload["versions"][-1]["version_id"] != args.version:
                        raise ValueError("select the latest SOP or skill proposal")
                    body = ApprovalInput(
                        decision_id=args.decision_id or uuid4().hex, version_id=args.version,
                        sha256=args.sha256, decision=args.action,
                        metadata_sha256=args.metadata_sha256,
                    )
                if version.sha256 != body.sha256:
                    raise ValueError("SHA-256 does not match the selected version")
                if version.metadata_sha256 != body.metadata_sha256:
                    raise ValueError("Metadata SHA-256 must match the complete skill version and purpose")
                if isinstance(body, SkillDeactivationInput):
                    print("停用此有效skill版本；保留原內容、批准與歷史，不刪除檔案。重新生效須另提出新版及人工批准。")
                else:
                    phase = "content"
                    text = _read_with_retry(lambda: client.content(args.artifact, version)).decode("utf-8")
                    text = text.replace("\r\n", "\n").replace("\r", "\\r")
                    print("".join(char if char.isprintable() or char in "\r\n\t" else repr(char)[1:-1] for char in text))
                if version.skill_purpose is not None:
                    print(json.dumps({"skill_purpose": version.skill_purpose.model_dump(),
                                      "metadata_sha256": version.metadata_sha256}, ensure_ascii=False, indent=2))
                if version.skill_resources is not None:
                    print(json.dumps({"skill_resources": version.skill_resources.model_dump()}, ensure_ascii=False, indent=2))
                if version.skill_tools is not None:
                    print(json.dumps({"skill_tools": version.skill_tools.model_dump(),
                                      "authorization": False}, ensure_ascii=False, indent=2))
                print(json.dumps({"artifact_id": args.artifact, **body.model_dump()}, indent=2))
                phase = "confirmation"
                prompt = (f"輸入 {args.action} 確認停用以上精確版本（保留原內容與批准）：\n"
                          if isinstance(body, SkillDeactivationInput) else
                          f"輸入 {args.action} 確認以上精確版本（這不是雲端部署授權）：\n")
                if input(prompt) != args.action:
                    raise ValueError("confirmation did not match; no decision was submitted")
                phase = "decision"
                receipt = (client.deactivate(args.artifact, snapshot, body) if isinstance(body, SkillDeactivationInput)
                           else client.decide(args.artifact, snapshot, body))
                print(json.dumps(receipt.response(), ensure_ascii=True, indent=2))
        return 0
    except Problem as exc:
        error = {**exc.body, "phase": phase}
        if isinstance(exc.__cause__, httpx.HTTPError):
            error["transport_error"] = type(exc.__cause__).__name__
        print(json.dumps(error, ensure_ascii=True), file=sys.stderr)
    except ClientAuthenticationError:
        print(json.dumps({
            "error": "ClientAuthenticationError", "code": "M3_AUTHENTICATION_UNCONFIRMED",
            "message": "Authentication is unavailable; keep original decision IDs when checking prior outcomes.",
            "phase": phase,
        }, ensure_ascii=True), file=sys.stderr)
    except (ValueError, OSError, EOFError) as exc:
        print(json.dumps({"error": type(exc).__name__, "message": safe_text(exc)}, ensure_ascii=True), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
