from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import secrets
import time
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote

import requests
from azure.identity import AzureCliCredential, ManagedIdentityCredential

API_VERSION = "v1"
TOKEN_SCOPE = "https://ai.azure.com/.default"
SESSION_DOC = "https://learn.microsoft.com/azure/foundry/agents/how-to/manage-hosted-sessions"
LATENCY_DOC = "https://learn.microsoft.com/azure/foundry/agents/how-to/debug-hosted-agent#diagnose-invocation-latency"
RESUME_HEADER = "x-ms-debug-latency-session-start-type"


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def save_manifest(run_dir: Path, manifest: dict) -> None:
    temporary = run_dir / "run-manifest.tmp"
    temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    for attempt in range(4):
        try:
            temporary.replace(run_dir / "run-manifest.json")
            return
        except PermissionError:
            # Windows scanners can briefly hold the destination; never retry a Hosted invocation.
            if attempt == 3:
                raise
            time.sleep(0.05 * (attempt + 1))


class HostedPocClient:
    def __init__(
        self,
        project_endpoint: str,
        agent_name: str,
        output_dir: Path,
        credential: AzureCliCredential | ManagedIdentityCredential | None = None,
    ) -> None:
        self.project_endpoint = project_endpoint.rstrip("/")
        self.agent_name = agent_name
        self.output_dir = output_dir
        self.credential = credential if credential is not None else AzureCliCredential()
        self.http = requests.Session()

    def _headers(self) -> dict[str, str]:
        token = self.credential.get_token(TOKEN_SCOPE).token
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    def create_session(self, version: str | None) -> dict[str, Any]:
        url = (
            f"{self.project_endpoint}/agents/{quote(self.agent_name)}/endpoint/sessions"
            f"?api-version={API_VERSION}"
        )
        body: dict[str, Any] = {}
        if version:
            body["version_indicator"] = {
                "type": "version_ref",
                "agent_version": version,
            }
        response = self.http.post(
            url, headers=self._headers(), json=body, timeout=60, allow_redirects=False
        )
        response.raise_for_status()
        if not 200 <= response.status_code < 300:
            raise RuntimeError(f"Session creation returned HTTP {response.status_code}")
        return response.json()

    def invoke(self, session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = (
            f"{self.project_endpoint}/agents/{quote(self.agent_name)}"
            f"/endpoint/protocols/invocations?api-version={API_VERSION}"
            f"&agent_session_id={quote(session_id)}"
        )
        started = utc_now()
        response = self.http.post(
            url,
            headers={**self._headers(), "x-ms-debug-latency-enabled": "true"},
            json=payload,
            timeout=180,
            allow_redirects=False,
        )
        completed = utc_now()
        record = {
            "started_at_utc": started,
            "completed_at_utc": completed,
            "status_code": response.status_code,
            "headers": {
                key: value
                for key, value in response.headers.items()
                if key.lower().startswith(("x-ms-", "x-agent-", "request-id"))
            },
        }
        try:
            record["body"] = response.json()
        except ValueError:
            record["body"] = {"raw": response.text}
        return record

    def stop_session(self, session_id: str) -> dict[str, Any]:
        url = (
            f"{self.project_endpoint}/agents/{quote(self.agent_name)}"
            f"/endpoint/sessions/{quote(session_id)}:stop?api-version={API_VERSION}"
        )
        response = self.http.post(url, headers=self._headers(), timeout=60, allow_redirects=False)
        response.raise_for_status()
        if not 200 <= response.status_code < 300:
            raise RuntimeError(f"Session stop returned HTTP {response.status_code}")
        return response.json() if response.content else {"stopped": True}

    def download_session_file(self, session_id: str, relative_path: str, target: Path) -> dict:
        """Official session Files REST: path is relative to the Hosted session's HOME."""
        path = PurePosixPath(relative_path)
        if (
            not relative_path or path.is_absolute() or "\\" in relative_path
            or ":" in relative_path or any(part in {"", ".", ".."} for part in relative_path.split("/"))
        ):
            raise ValueError("Files API path must be HOME-relative without traversal")
        url = (
            f"{self.project_endpoint}/agents/{quote(self.agent_name, safe='')}"
            f"/endpoint/sessions/{quote(session_id, safe='')}/files/content"
        )
        record = {
            "started_at_utc": utc_now(), "api_source": SESSION_DOC + "#download-a-file",
            "bytes": 0,
        }
        deadline = time.monotonic() + 90
        with self.http.get(
            url, headers={**self._headers(), "Accept": "application/octet-stream"},
            params={"api-version": API_VERSION, "path": relative_path},
            timeout=(10, 60), stream=True, allow_redirects=False,
        ) as response:
            response.raise_for_status()
            if response.status_code != 200:
                raise RuntimeError(f"Files download returned HTTP {response.status_code}")
            record["status_code"] = response.status_code
            with target.open("wb") as output:
                for chunk in response.iter_content(chunk_size=65536):
                    record["bytes"] += len(chunk)
                    if record["bytes"] > 8 * 1024 * 1024 or time.monotonic() > deadline:
                        raise RuntimeError("Files evidence exceeded the byte/time limit")
                    output.write(chunk)
        record["completed_at_utc"] = utc_now()
        return record

    def get_session(self, session_id: str) -> dict[str, Any]:
        url = (
            f"{self.project_endpoint}/agents/{quote(self.agent_name)}"
            f"/endpoint/sessions/{quote(session_id)}?api-version={API_VERSION}"
        )
        response = self.http.get(url, headers=self._headers(), timeout=60)
        response.raise_for_status()
        return response.json()

    def capture_session_logs(self, session_id: str, version: str, path: Path) -> dict:
        url = (
            f"{self.project_endpoint}/agents/{quote(self.agent_name, safe='')}"
            f"/versions/{quote(version, safe='')}/sessions/{quote(session_id, safe='')}"
            f":logstream?api-version={API_VERSION}"
        )
        record: dict[str, Any] = {"path": path.name, "bytes": 0}
        deadline = time.monotonic() + 20
        try:
            with path.open("wb") as output, self.http.get(
                url, headers={**self._headers(), "Accept": "text/event-stream"},
                stream=True, timeout=(10, 10),
            ) as response:
                response.raise_for_status()
                for chunk in response.iter_content(chunk_size=1024):
                    output.write(chunk)
                    record["bytes"] += len(chunk)
                    if time.monotonic() >= deadline or record["bytes"] >= 262144:
                        record["truncated"] = True
                        break
        except requests.RequestException as exc:
            record["error"] = str(exc)
        return record


def payload(
    poc_id: str,
    run_id: str,
    operation: str,
    *,
    step_id: str | None = None,
    **values: Any,
) -> dict[str, Any]:
    result = {
        "schema_version": "1.0",
        "poc_id": poc_id,
        "run_id": run_id,
        "operation": operation,
        **values,
    }
    if step_id:
        result["step_id"] = step_id
    return result


def require_mount(mount: dict, expected_source: str) -> None:
    if mount.get("mounted") is not True or mount.get("filesystem_type") != "cifs":
        raise RuntimeError("Mount response does not prove a CIFS mount")
    if mount.get("source") != expected_source:
        raise RuntimeError("CIFS mount source differs from the approved share")


def _run_shared(args: argparse.Namespace, client: HostedPocClient, manifest: dict, invoke, save) -> None:
    nonce = manifest["nonce"]
    manifest["active_gate"] = "G0"
    manifest["environments"] = {}
    for alias in ("A", "B"):
        environment = invoke(
            alias,
            payload(args.poc_id, args.run_id, "probe_environment"),
        )
        if environment["agent"]["version"] != args.agent_version:
            raise RuntimeError("Session version mismatch")
        manifest["environments"][alias] = environment
        mount = invoke(
            alias,
            payload(
                args.poc_id,
                args.run_id,
                "mount_shared",
                step_id=f"g0-mount-{alias.lower()}",
            ),
        )
        require_mount(mount, args.expected_source)
        if getattr(args, "smb_auth_mode", "account_key") == "managed_identity":
            if mount.get("auth_mode") != "managed_identity":
                raise RuntimeError("Mount response does not prove the requested managed identity path")
    manifest["gates"]["G0"] = {"status": "PASS"}
    if getattr(args, "g0_only", False):
        return
    from g3_only import download_and_verify, verify_result

    manifest["active_gate"] = "G1"

    v1 = f"{args.run_id}|{nonce}|v1\n"
    invoke(
        "A",
        payload(
            args.poc_id,
            args.run_id,
            "shared_io_step",
            step_id="g1-a-write-v1",
            action="write_text",
            relative_path="handoff.txt",
            content=v1,
        ),
    )
    invoke(
        "B",
        payload(
            args.poc_id,
            args.run_id,
            "shared_io_step",
            step_id="g1-b-read-v1",
            action="read_text",
            relative_path="handoff.txt",
        ),
    )
    invoke(
        "B",
        payload(
            args.poc_id, args.run_id, "shared_io_step",
            step_id="g1-b-write-v2", action="write_text", relative_path="handoff.txt",
            content=f"{args.run_id}|{nonce}|v2\n",
        ),
    )
    invoke(
        "A",
        payload(
            args.poc_id, args.run_id, "shared_io_step",
            step_id="g1-a-read-v2", action="read_text", relative_path="handoff.txt",
        ),
    )

    binary = secrets.token_bytes(1024 * 1024)
    invoke(
        "B",
        payload(
            args.poc_id,
            args.run_id,
            "shared_io_step",
            step_id="g1-b-write-binary",
            action="write_bytes",
            relative_path="binary-v2.bin",
            content_base64=base64.b64encode(binary).decode("ascii"),
        ),
    )
    invoke(
        "A",
        payload(
            args.poc_id,
            args.run_id,
            "shared_io_step",
            step_id="g1-a-hash-binary",
            action="hash",
            relative_path="binary-v2.bin",
        ),
    )
    manifest["controller_binary_sha256"] = hashlib.sha256(binary).hexdigest()

    invoke(
        "B",
        payload(
            args.poc_id,
            args.run_id,
            "shared_io_step",
            step_id="g1-b-rename",
            action="rename",
            relative_path="handoff.txt",
            destination_relative_path="handoff-renamed.txt",
        ),
    )
    invoke(
        "A",
        payload(
            args.poc_id, args.run_id, "shared_io_step",
            step_id="g1-a-inspect-old-name", action="inspect",
            relative_path="handoff.txt", expected_exists=False,
        ),
    )
    invoke(
        "A",
        payload(
            args.poc_id, args.run_id, "shared_io_step",
            step_id="g1-a-inspect-new-name", action="inspect",
            relative_path="handoff-renamed.txt", expected_exists=True,
        ),
    )
    invoke(
        "A",
        payload(
            args.poc_id,
            args.run_id,
            "shared_io_step",
            step_id="g1-a-read-renamed",
            action="read_text",
            relative_path="handoff-renamed.txt",
        ),
    )
    invoke(
        "A",
        payload(
            args.poc_id,
            args.run_id,
            "shared_io_step",
            step_id="g1-a-private-marker",
            action="write_private_marker",
            content=f"{nonce}|private-a",
        ),
    )
    invoke(
        "B",
        payload(
            args.poc_id,
            args.run_id,
            "shared_io_step",
            step_id="g1-b-private-absent",
            action="check_private_marker_absent",
        ),
    )
    manifest["gates"]["G1"] = {
        "status": "PASS", "reason_code": None,
        "detail": "Both sessions directly verified bytes/hash; A inspected both rename paths on the approved CIFS mount; B's private marker was absent.",
    }
    save()
    # G2 precedes GUI startup: no desktop processes, streams, or Files requests keep A alive.
    if getattr(args, "include_g2", False):
        manifest["active_gate"] = "G2"
        try:
            _run_g2(args, manifest, invoke, save)
        except (requests.RequestException, RuntimeError, KeyError, OSError, ValueError) as exc:
            manifest["gates"]["G2"] = {
                "status": "PARTIAL", "reason_code": "SHARED_IO_CONSISTENCY",
                "remount_mode": manifest.get("g2", {}).get("remount_mode", "unknown"),
                "error": {"type": type(exc).__name__, "message": str(exc)},
            }
        save()

    manifest["active_gate"] = "G3"
    desktop = invoke(
        "A",
        payload(
            args.poc_id, args.run_id, "desktop_smoke",
            step_id="g3-desktop", nonce=nonce, publish_relative_path="browser-artifact.txt",
        ),
    )
    manifest["desktop_result"] = desktop
    verify_result(desktop, nonce)
    download_and_verify(
        client, manifest["sessions"]["A"], desktop,
        home=manifest["environments"]["A"]["home"], poc_id=args.poc_id,
        run_id=args.run_id, nonce=nonce, run_dir=args.output / args.run_id,
        downloads=manifest["downloads"], save=save,
    )
    invoke(
        "B",
        payload(
            args.poc_id, args.run_id, "shared_io_step",
            step_id="g3-b-read-browser-artifact", action="read_text",
            relative_path="browser-artifact.txt",
        ),
    )
    manifest["gates"]["G3"] = {"status": "PASS", "reason_code": None, "shared_publish": "PASS"}


def _run_g2(args: argparse.Namespace, manifest: dict, invoke, save) -> None:
    timeout = getattr(args, "idle_timeout_seconds", 300)
    if not 120 <= timeout <= 3600 or args.idle_seconds <= timeout:
        manifest["gates"]["G2"] = {
            "status": "BLOCKED", "reason_code": "RESUME_NOT_PROVEN", "remount_mode": "unknown",
            "detail": "Declare the deployed idle timeout (120..3600s) and wait longer than it.",
        }
        return
    evidence = manifest["g2"] = {
        "idle_started_at_utc": utc_now(), "idle_seconds": args.idle_seconds,
        "declared_idle_timeout_seconds": timeout, "remount_mode": "unknown",
        "observation": "No session GET, invocation, Files API, or log stream during A's idle wait.",
        "api_source": LATENCY_DOC,
    }
    save()
    time.sleep(args.idle_seconds)
    evidence["idle_wait_completed_at_utc"] = utc_now()
    b_mount = invoke("B", payload(args.poc_id, args.run_id, "status"))["team_mount"]
    if b_mount.get("mounted") is False:
        b_mount = invoke(
            "B", payload(args.poc_id, args.run_id, "mount_shared", step_id="g2-b-remount")
        )
    require_mount(b_mount, args.expected_source)
    path = "handoff-renamed.txt"
    invoke(
        "B", payload(
            args.poc_id, args.run_id, "shared_io_step", step_id="g2-b-write-v3",
            action="write_text", relative_path=path,
            content=f"{args.run_id}|{manifest['nonce']}|v3\n",
        ),
    )
    evidence["b_write_v3_event_index"] = len(manifest["events"]) - 1
    prior_mount = invoke("A", payload(args.poc_id, args.run_id, "status"))["team_mount"]
    event = manifest["events"][-1]
    headers = {key.lower(): value for key, value in event["response"].get("headers", {}).items()}
    evidence.update(
        resume_event_index=len(manifest["events"]) - 1,
        resume_response_headers=event["response"].get("headers", {}),
        session_start_type=headers.get(RESUME_HEADER),
        platform_resume_proven=headers.get(RESUME_HEADER) == "resume",
        prior_mount=prior_mount,
    )
    save()
    if prior_mount.get("mounted") is True:
        require_mount(prior_mount, args.expected_source)
        evidence["remount_mode"] = "platform_auto"
    elif prior_mount.get("mounted") is False:
        mounted = invoke(
            "A", payload(args.poc_id, args.run_id, "mount_shared", step_id="g2-a-remount")
        )
        evidence["application_mount_result"] = mounted
        require_mount(mounted, args.expected_source)
        if mounted.get("mount_mode") != "application_mount" or mounted.get("idempotent_replay"):
            raise RuntimeError("A's mount appeared without a fresh application mount; remount mode is unproven")
        evidence["remount_mode"] = "application_remount"
    else:
        raise RuntimeError("A's pre-remount inspection did not report mount presence")
    invoke(
        "A", payload(
            args.poc_id, args.run_id, "shared_io_step", step_id="g2-a-read-v3",
            action="read_text", relative_path=path,
        ),
    )
    invoke(
        "A", payload(
            args.poc_id, args.run_id, "shared_io_step", step_id="g2-a-write-v4",
            action="write_text", relative_path=path,
            content=f"{args.run_id}|{manifest['nonce']}|v4\n",
        ),
    )
    invoke(
        "B", payload(
            args.poc_id, args.run_id, "shared_io_step", step_id="g2-b-read-v4",
            action="read_text", relative_path=path,
        ),
    )
    evidence["handoff_verified"] = True
    manifest["gates"]["G2"] = {
        "status": (
            "PASS_PLATFORM_AUTO" if evidence["remount_mode"] == "platform_auto"
            else "PASS_WITH_APPLICATION_REMOUNT"
        ) if evidence["platform_resume_proven"] else "BLOCKED",
        "reason_code": None if evidence["platform_resume_proven"] else "RESUME_NOT_PROVEN",
        "remount_mode": evidence["remount_mode"],
        "detail": (
            "The first A status invocation reported platform resume; direct v3/v4 handoff verified."
            if evidence["platform_resume_proven"]
            else f"Missing exact {RESUME_HEADER}: resume on A's first post-idle invocation; raw headers/logs retained, elapsed time and HOME state are not proof."
        ),
    }


def verify_io(request: dict, result: dict, artifacts: dict[str, dict]) -> None:
    action = request.get("action")
    path = request.get("relative_path")
    if action in {"write_text", "write_bytes"}:
        content = (
            request["content"].encode("utf-8") if action == "write_text"
            else base64.b64decode(request["content_base64"], validate=True)
        )
        expected = {"sha256": hashlib.sha256(content).hexdigest(), "size": len(content)}
        if any(result.get(key) != value for key, value in expected.items()):
            raise RuntimeError("Write result differs from controller bytes/hash")
        artifacts[path] = expected
    elif action == "inspect":
        expected_exists = request["expected_exists"]
        if result.get("exists") is not expected_exists:
            raise RuntimeError("A's direct inspection disagrees with expected filename presence")
        if expected_exists and (
            result.get("is_file") is not True
            or any(result.get(key) != value for key, value in artifacts[path].items())
        ):
            raise RuntimeError("A's direct inspection differs from the renamed artifact")
    elif action in {"read_text", "read_bytes", "hash"}:
        expected = artifacts[path]
        if any(result.get(key) != value for key, value in expected.items()):
            raise RuntimeError("Shared read differs from last completed owner write")
        if action != "hash":
            content = (
                result["content"].encode("utf-8") if action == "read_text"
                else base64.b64decode(result["content_base64"], validate=True)
            )
            if len(content) != expected["size"] or hashlib.sha256(content).hexdigest() != expected["sha256"]:
                raise RuntimeError("Shared response bytes differ from reported hash")
    elif action == "rename":
        if (
            result.get("source_exists") is not False
            or result.get("destination_sha256") != artifacts[path]["sha256"]
            or result.get("destination_size") != artifacts[path]["size"]
        ):
            raise RuntimeError("Rename result is inconsistent")
        artifacts[request["destination_relative_path"]] = artifacts.pop(path)
    elif action == "write_private_marker" and result.get("sha256") != hashlib.sha256(request["content"].encode("utf-8")).hexdigest():
        raise RuntimeError("Private marker write hash differs")
    elif action == "check_private_marker_absent" and result.get("absent") is not True:
        raise RuntimeError("Private marker appeared in session B")


def run(args: argparse.Namespace, client: HostedPocClient | None = None) -> dict:
    run_dir = args.output / args.run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    if client is None:
        client = HostedPocClient(args.project_endpoint, args.agent_name, run_dir)
    manifest = {
        "schema_version": "1.0", "poc_id": args.poc_id, "run_id": args.run_id,
        "started_at_utc": utc_now(), "project_endpoint": args.project_endpoint,
        "agent_name": args.agent_name, "agent_version": args.agent_version,
        "image_digest": args.image_digest, "expected_source": args.expected_source,
        "smb_auth_mode": getattr(args, "smb_auth_mode", "account_key"),
        "sessions": {}, "nonce": secrets.token_hex(12), "events": [], "cleanup": [],
        "gates": {gate: {"status": "NOT_RUN_DEPENDENCY"} for gate in ("G0", "G1", "G2", "G3")},
        "active_gate": "G0", "artifacts": {}, "downloads": [], "owner_history": [], "handoffs": [],
        "api_sources": {"session_and_files": SESSION_DOC, "resume_diagnostics": LATENCY_DOC},
        "product_contract": {"fixed_vm": "NOT_VALIDATED", "g4": "NOT_RUN_OPTIONAL"},
    }
    if not getattr(args, "include_g2", False) and not getattr(args, "g0_only", False):
        manifest["gates"]["G2"] = {"status": "NOT_RUN_OPTIONAL"}
    writes: dict[str, dict] = {}

    def save() -> None:
        save_manifest(run_dir, manifest)

    def invoke(alias: str, request: dict) -> dict:
        event = {"session_alias": alias, "request": dict(request), "started_at_utc": utc_now()}
        # Keep hashes and length instead of duplicating the 1 MiB request body.
        if "content_base64" in event["request"]:
            event["request"]["content_base64"] = "<synthetic bytes omitted; hash recorded>"
        manifest["events"].append(event)
        save()
        started = time.monotonic()
        try:
            response = client.invoke(manifest["sessions"][alias], request)
        except Exception as exc:
            event["transport_error"] = {"type": type(exc).__name__, "message": str(exc)}
            event["completed_at_utc"] = utc_now()
            save()
            raise
        completed = time.monotonic()
        event["response"] = response
        event["completed_at_utc"] = utc_now()
        save()
        body = response["body"]
        if response["status_code"] != 200 or body.get("status") != "success":
            raise RuntimeError(f"Invocation failed: HTTP {response['status_code']}; {body}")
        if body.get("runtime", {}).get("session_id") != manifest["sessions"][alias]:
            raise RuntimeError("Invocation routed to another session")
        result = body["result"]
        if request["operation"] == "shared_io_step":
            action = request["action"]
            path = request.get("relative_path")
            if action in {"read_text", "read_bytes", "hash", "inspect", "check_private_marker_absent"} and result.get("idempotent_replay"):
                raise RuntimeError("A cached step result is not a direct filesystem observation")
            if action == "inspect":
                require_mount(result.get("team_mount", {}), args.expected_source)
            verify_io(request, result, manifest["artifacts"])
            if action in {"write_text", "write_bytes", "rename"}:
                previous = writes.get(path, {})
                destination = request.get("destination_relative_path", path)
                writes[destination] = {"owner": alias, "completed": completed, "step_id": request["step_id"]}
                if action == "rename":
                    writes.pop(path, None)
                manifest["owner_history"].append({
                    "owner": alias, "previous_owner": previous.get("owner"),
                    "relative_path": destination, "step_id": request["step_id"],
                    **manifest["artifacts"][destination],
                })
            elif action in {"read_text", "read_bytes", "hash", "inspect"} and path in writes:
                previous = writes[path]
                if previous["owner"] != alias and not previous.get("observed"):
                    latency = completed - previous["completed"]
                    manifest["handoffs"].append({
                        "writer": previous["owner"], "reader": alias, "relative_path": path,
                        "write_step_id": previous["step_id"], "read_step_id": request["step_id"],
                        "close_to_read_seconds_upper_bound": latency,
                        "read_request_seconds": completed - started, "window_seconds": 10,
                    })
                    # Resume includes provisioning/remount time; report it, not a false SMB timeout.
                    if manifest["active_gate"] == "G1" and latency > 10:
                        raise RuntimeError("Shared close-to-read observation exceeded 10 seconds")
                    previous["observed"] = True
        if request["operation"] == "desktop_smoke":
            published = result["artifacts"].get("published_shared")
            content = f"{args.poc_id}|{args.run_id}|{manifest['nonce']}|GUI_COMPLETE\n".encode("utf-8")
            expected = {"sha256": hashlib.sha256(content).hexdigest(), "size": len(content)}
            if (
                not published or published.get("relative_path") != request["publish_relative_path"]
                or any(published.get(key) != value for key, value in expected.items())
            ):
                raise RuntimeError("Published artifact does not match this run's browser bytes")
            manifest["artifacts"][published["relative_path"]] = expected
            writes[published["relative_path"]] = {
                "owner": alias, "completed": completed, "step_id": request["step_id"],
            }
            manifest["owner_history"].append({
                "owner": alias, "relative_path": published["relative_path"],
                "step_id": request["step_id"], **expected,
            })
        save()
        return result

    save()
    try:
        for alias in ("A", "B"):
            session = client.create_session(args.agent_version)
            manifest["sessions"][alias] = session["agent_session_id"]
            manifest.setdefault("session_created", {})[alias] = session
            save()
        if len(set(manifest["sessions"].values())) != 2:
            raise RuntimeError("Sessions A and B must differ")
        _run_shared(args, client, manifest, invoke, save)
    except (requests.RequestException, RuntimeError, KeyError, OSError, ValueError) as exc:
        gate = manifest["active_gate"]
        manifest["gates"][gate] = {
            "status": "BLOCKED" if gate == "G0" else "PARTIAL",
            "reason_code": {
                "G0": "ENVIRONMENT_OR_APPROVAL", "G1": "SHARED_IO_CONSISTENCY",
                "G2": "RESUME_NOT_PROVEN", "G3": "GUI_OR_BROWSER_RUNTIME",
            }[gate],
            "error": {"type": type(exc).__name__, "message": str(exc)},
        }
    finally:
        for alias, session_id in manifest["sessions"].items():
            if getattr(args, "capture_logs", False) or not getattr(args, "g0_only", False):
                try:
                    logs = client.capture_session_logs(
                        session_id, args.agent_version, run_dir / f"{alias}-console.sse"
                    )
                    manifest.setdefault("session_logs", {})[alias] = logs
                    save()
                except Exception as exc:
                    manifest.setdefault("session_logs", {})[alias] = {"error": str(exc)}
            try:
                result = client.stop_session(session_id)
                manifest["cleanup"].append({"alias": alias, "session_id": session_id, "response": result})
            except Exception as exc:
                manifest["cleanup"].append({"alias": alias, "session_id": session_id, "error": str(exc)})
            try:
                save()
            except Exception as exc:
                manifest["persistence_error"] = str(exc)
        manifest["completed_at_utc"] = utc_now()
        try:
            save()
        finally:
            client.http.close()
            client.credential.close()
    return manifest


def run_succeeded(args: argparse.Namespace, manifest: dict) -> bool:
    gates = ["G0"] if getattr(args, "g0_only", False) else ["G0", "G1", "G3"]
    if getattr(args, "include_g2", False) and not getattr(args, "g0_only", False):
        gates.append("G2")
    return (
        all(
            manifest["gates"][gate]["status"] in (
                {"PASS_PLATFORM_AUTO", "PASS_WITH_APPLICATION_REMOUNT"} if gate == "G2" else {"PASS"}
            )
            for gate in gates
        )
        and not any("error" in item for item in manifest["cleanup"])
        and not manifest.get("persistence_error")
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Shared-mount probe; fails closed on missing evidence.")
    parser.add_argument("--project-endpoint", required=True)
    parser.add_argument("--agent-name", default="fa-hosted-sandbox-poc")
    parser.add_argument("--agent-version", required=True)
    parser.add_argument("--image-digest", required=True)
    parser.add_argument("--expected-source", required=True)
    parser.add_argument("--smb-auth-mode", choices=("account_key", "managed_identity"), default="account_key")
    parser.add_argument("--poc-id", default="fa-hosted-sandbox")
    parser.add_argument("--run-id", default=datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"))
    parser.add_argument("--output", type=Path, default=Path("evidence"))
    parser.add_argument("--include-g2", action="store_true")
    parser.add_argument("--g0-only", action="store_true")
    parser.add_argument("--capture-logs", action="store_true")
    parser.add_argument("--idle-seconds", type=int, default=330)
    parser.add_argument(
        "--idle-timeout-seconds", type=int, default=300,
        help="Already-deployed version's idle timeout; does not change Azure configuration.",
    )
    args = parser.parse_args()
    if not os.environ.get("AZURE_CONFIG_DIR"):
        parser.error("Set the isolated AZURE_CONFIG_DIR first.")
    if args.include_g2 and not args.g0_only and (
        not 120 <= args.idle_timeout_seconds <= 3600 or args.idle_seconds <= args.idle_timeout_seconds
    ):
        parser.error("Use the deployed idle timeout (120..3600s) and wait longer than it.")
    result = run(args)
    print(json.dumps({"gates": result["gates"], "cleanup": result["cleanup"]}, indent=2))
    if not run_succeeded(args, result):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
