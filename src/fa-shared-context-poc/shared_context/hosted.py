from __future__ import annotations

import json
import os
import re
import ssl
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Lock
from typing import Annotated, Any, Literal
from uuid import uuid4
from urllib.parse import urlparse

import httpx
from azure.identity import ManagedIdentityCredential
from pydantic import Field, ValidationError

from .config import native_memory_scope, validate_api_audience
from .environment import EnvironmentProbe, run_environment_probe
from .errors import Problem, safe_json, safe_text
from .gui import GuiProbe, run_gui_probe
from .models import Identifier, StrictModel, UpdateWork
from .m2_assistant import AssistantEngine, m4_enabled
from .backup_client import BackupClient

PROCESS_MARKER = uuid4().hex
MAX_RESPONSE_BYTES = 256 * 1024
SEGMENT = r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}"
API_PATH = re.compile(
    rf"/teams/{SEGMENT}/(?:works/{SEGMENT}(?:/handoff|/publish|/context)?"
    rf"|memory/(?:team(?:/search)?|private/{SEGMENT}(?:/search)?|operations/[a-f0-9]{{32}}))"
)
M3_API_PATH = re.compile(
    rf"/teams/{SEGMENT}/artifacts/{SEGMENT}(?:/decisions|/effective|/versions/[a-f0-9]{{32}}(?:/reconcile)?)?"
)
M4_API_PATH = re.compile(
    rf"/m4/teams/{SEGMENT}/(?:works/{SEGMENT}/context"
    rf"|memory/(?:seed|operations/[a-f0-9]{{32}}|private/{SEGMENT}/search))"
)


class M4SeedProbe(StrictModel):
    fixture_id: Literal["short_team", "long_team", "private_a", "private_b"]
    suite_sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class ApiProbe(StrictModel):
    operation: Literal["api"]
    method: Literal["GET", "POST", "PATCH"]
    path: Annotated[str, Field(max_length=300)]
    body: dict[str, Any] | None = None
    etag: Annotated[str, Field(max_length=256)] | None = None


class ApiRaceProbe(StrictModel):
    operation: Literal["api_race"]
    path: Annotated[str, Field(max_length=300)]
    etag: Annotated[str, Field(min_length=1, max_length=256)]
    updates: Annotated[list[UpdateWork], Field(min_length=2, max_length=2)]


class DirectProbe(StrictModel):
    operation: Literal["direct_storage", "direct_memory"]
    team: Identifier
    work: Identifier = "probe"


class RuntimeProbe(StrictModel):
    operation: Literal["runtime"]


class ModelBudgetProbe(StrictModel):
    operation: Literal["model_budget"]
    milestone: Literal["m2", "m3", "m4"] = "m2"


def runtime_metadata(home: Path | None = None, *, probe_home: bool = True) -> dict:
    if not probe_home:
        return {
            "pid": os.getpid(), "process_boot_marker": PROCESS_MARKER,
            "home_probe": {"status": "not_requested", "reason": "private operation checks HOME after identity binding"},
            "fixed_vm_validated": False,
        }
    home = home or Path.home()
    probe = {
        "path": str(home), "status": "unavailable",
        "effective_uid": os.geteuid() if hasattr(os, "geteuid") else None,
    }
    marker = None
    try:
        stat = home.stat()
        probe.update({"owner_uid": stat.st_uid, "owner_gid": stat.st_gid,
                      "mode": oct(stat.st_mode & 0o777)})
        marker_dir = home / ".shared-context-poc"
        marker_dir.mkdir(mode=0o700, exist_ok=True)
        marker_path = marker_dir / "home-marker"
        try:
            with marker_path.open("x", encoding="utf-8") as stream:
                stream.write(uuid4().hex)
        except FileExistsError:
            pass
        marker = marker_path.read_text(encoding="utf-8")[:64].strip()
        probe["status"] = "available"
    except OSError as exc:
        probe["error"] = {"type": type(exc).__name__, "errno": exc.errno, "message": safe_text(exc)}
    return {
        "pid": os.getpid(), "process_boot_marker": PROCESS_MARKER, "home_marker": marker,
        "home_probe": probe,
        "tls_trust": {
            "ssl_cert_file_configured": bool(os.environ.get("SSL_CERT_FILE")),
            "requests_ca_bundle_configured": bool(os.environ.get("REQUESTS_CA_BUNDLE")),
            "system_cafile": ssl.get_default_verify_paths().cafile,
        },
        "fixed_vm_validated": False,
        "note": "HOME persistence and process markers do not prove a fixed VM or shared mount.",
    }


class HostedClient:
    def __init__(self, api_url: str, api_audience: str, table_endpoint: str, table_name: str,
                 memory_endpoint: str, memory_store: str, credential, http=None):
        for url in (api_url, table_endpoint, memory_endpoint):
            parsed = urlparse(url)
            if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                    or parsed.query or parsed.fragment or parsed.port not in (None, 443)):
                raise ValueError("configured probe endpoints must use HTTPS and contain no credentials/query")
        validate_api_audience(api_audience)
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9]{2,62}", table_name):
            raise ValueError("invalid table name")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", memory_store):
            raise ValueError("invalid memory store name")
        self.api_url, self.api_audience = api_url.rstrip("/"), api_audience
        self.table_endpoint, self.table_name = table_endpoint.rstrip("/"), table_name
        self.memory_endpoint, self.memory_store = memory_endpoint.rstrip("/"), memory_store
        self.credential = credential
        self.assistant: AssistantEngine | None = None
        self.assistant_lock = Lock()
        if http is not None:
            self.http = http
        else:
            ca_bundle = os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE")
            self.http = httpx.Client(
                timeout=httpx.Timeout(70, connect=10), follow_redirects=False, trust_env=False,
                verify=ssl.create_default_context(cafile=ca_bundle),
            )

    @classmethod
    def from_env(cls):
        return cls(
            api_url=os.environ["CONTEXT_API_URL"], api_audience=os.environ["CONTEXT_API_AUDIENCE"],
            table_endpoint=os.environ["CONTEXT_TABLE_ENDPOINT"], table_name=os.environ["CONTEXT_TABLE_NAME"],
            memory_endpoint=os.environ["CONTEXT_MEMORY_ENDPOINT"], memory_store=os.environ["CONTEXT_MEMORY_STORE"],
            credential=ManagedIdentityCredential(client_id=os.environ.get("AZURE_CLIENT_ID") or None),
        )

    def request(self, method: str, url: str, scope: str, body=None, headers=None) -> dict:
        try:
            token = self.credential.get_token(scope).token
        except Exception as exc:
            return {"http_status": None, "error": {"code": "TOKEN_ACQUISITION_FAILED",
                    "type": type(exc).__name__, "message": "credential acquisition failed; the requested service was not called"}}
        request_headers = {**(headers or {}), "Authorization": f"Bearer {token}"}
        try:
            with self.http.stream(method, url, headers=request_headers, json=body) as response:
                raw = bytearray()
                truncated = False
                for chunk in response.iter_bytes():
                    remaining = MAX_RESPONSE_BYTES - len(raw)
                    raw.extend(chunk[:remaining])
                    if len(chunk) > remaining:
                        truncated = True
                        break
                text = raw.decode("utf-8", errors="replace").replace(token, "[REDACTED]")
                try:
                    content = json.loads(text) if not truncated else text
                except ValueError:
                    content = text
                return {
                    "http_status": response.status_code, "etag": response.headers.get("etag"),
                    "request_id": response.headers.get("x-ms-request-id"),
                    "content": safe_json(content), "truncated": truncated,
                }
        except httpx.HTTPError as exc:
            return {"http_status": None, "error": {"code": "HTTP_TRANSPORT_ERROR",
                    "type": type(exc).__name__, "message": "HTTP transport failed; the original outcome remains unresolved"}}

    def dispatch(self, payload: dict) -> dict:
        if not isinstance(payload, dict):
            raise Problem(422, "INVALID_REQUEST", "invocation must be an object")
        if len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) > 64 * 1024:
            raise Problem(413, "REQUEST_TOO_LARGE", "invocation exceeds 64 KiB")
        try:
            operation = payload.get("operation")
            if operation == "runtime":
                RuntimeProbe.model_validate(payload)
                return {"operation": operation}
            if operation == "gui":
                return {"operation": operation, "outcome": run_gui_probe(GuiProbe.model_validate(payload))}
            if operation == "model_budget":
                model_probe = ModelBudgetProbe.model_validate(payload)
                if model_probe.milestone == "m3" and os.environ.get("CONTEXT_M3_TOOLS_ENABLED") != "true":
                    raise Problem(403, "M3_DISABLED", "M3 has not been enabled for this Hosted deployment")
                if model_probe.milestone == "m4" and not m4_enabled():
                    raise Problem(403, "M4_DISABLED", "the approved M4 run is not enabled for this Hosted deployment")
                prefix = {"m2": "", "m3": "/m3", "m4": "/m4"}[model_probe.milestone]
                return {"operation": operation, "outcome": self.request(
                    "GET", self.api_url + prefix + "/model/v1/budget", self.api_audience + "/.default",
                )}
            if operation == "assistant":
                with self.assistant_lock:
                    if self.assistant is None:
                        self.assistant = AssistantEngine(self, process_marker=PROCESS_MARKER)
                return {"operation": operation, "outcome": self.assistant.dispatch(payload)}
            if operation == "environment":
                return {"operation": operation, "outcome": run_environment_probe(
                    EnvironmentProbe.model_validate(payload),
                    backup_client=BackupClient(self.api_url, self.api_audience, self.credential, self.http),
                )}
            if operation == "api_race":
                command = ApiRaceProbe.model_validate(payload)
                if not re.fullmatch(rf"/teams/{SEGMENT}/works/{SEGMENT}", command.path):
                    raise Problem(422, "INVALID_API_PATH", "api_race accepts only a work-item path")
                if command.etag == "*" or "," in command.etag:
                    raise Problem(422, "INVALID_ETAG", "api_race requires one exact ETag")
                start = Barrier(2, timeout=5)

                def update(body):
                    start.wait()
                    return self.request(
                        "PATCH", self.api_url + command.path, self.api_audience + "/.default",
                        body=body.model_dump(exclude_unset=True), headers={"If-Match": command.etag},
                    )

                with ThreadPoolExecutor(max_workers=2, thread_name_prefix="poc-cas") as pool:
                    attempts = list(pool.map(update, command.updates))
                statuses = [attempt["http_status"] for attempt in attempts]
                return {"operation": operation, "outcome": {
                    "concurrency": 2, "same_client_identity": True, "shared_etag": command.etag,
                    "attempts": attempts, "http_statuses": statuses,
                    "cas_expected_outcome": statuses.count(200) == 1 and statuses.count(412) == 1,
                }}
            if operation == "api":
                command = ApiProbe.model_validate(payload)
                m3_path = os.environ.get("CONTEXT_M3_TOOLS_ENABLED") == "true" and M3_API_PATH.fullmatch(command.path)
                m4_path = m4_enabled() and M4_API_PATH.fullmatch(command.path)
                if not API_PATH.fullmatch(command.path) and not m3_path and not m4_path:
                    raise Problem(422, "INVALID_API_PATH", "only documented Context API paths are accepted")
                if m4_path:
                    post = command.path.endswith(("/seed", "/search"))
                    if command.method != ("POST" if post else "GET") or command.etag is not None:
                        raise Problem(422, "INVALID_REQUEST", "M4 probes require the exact route method without an ETag")
                    if command.path.endswith("/seed"):
                        command.body = M4SeedProbe.model_validate(command.body).model_dump()
                    elif not post and command.body is not None:
                        raise Problem(422, "INVALID_REQUEST", "M4 read probes do not accept a body")
                outcome = self.request(
                    command.method, self.api_url + command.path, self.api_audience + "/.default",
                    body=command.body, headers={"If-Match": command.etag} if command.etag else {},
                )
            elif operation in {"direct_storage", "direct_memory"}:
                command = DirectProbe.model_validate(payload)
                if operation == "direct_storage":
                    url = (
                        f"{self.table_endpoint}/{self.table_name}"
                        f"(PartitionKey='{command.team}',RowKey='work:{command.work}')"
                    )
                    outcome = self.request("GET", url, "https://storage.azure.com/.default",
                                           headers={"Accept": "application/json;odata=nometadata",
                                                    "x-ms-version": "2020-12-06"})
                else:
                    outcome = self.request(
                        "POST", f"{self.memory_endpoint}/memory_stores/{self.memory_store}:search_memories?api-version=v1",
                        "https://ai.azure.com/.default",
                        body={"scope": native_memory_scope(f"team:{command.team}"), "items": [{
                            "role": "user", "type": "message",
                            "content": [{"type": "input_text", "text": "synthetic POC verification"}],
                        }]},
                        headers={"Foundry-Features": "MemoryStores=V1Preview"},
                    )
                outcome["access_denied"] = outcome["http_status"] in (401, 403)
                outcome["unexpected_success"] = outcome["http_status"] is not None and 200 <= outcome["http_status"] < 300
            else:
                raise Problem(422, "INVALID_OPERATION", "supported operations: runtime, gui, environment, assistant, model_budget, api, api_race, direct_storage, direct_memory")
        except ValidationError as exc:
            raise Problem(422, "INVALID_REQUEST", "invalid invocation fields, types, IDs, or lengths") from exc
        return {"operation": operation, "outcome": outcome}
