#!/usr/bin/env python3
"""Bounded, stdlib-only validation of two EXISTING shared-context Hosted sessions.

No provisioning, role changes, registry operations, session creation or deletion.
Run --help for explicit project/session arguments. Evidence is synthetic and
redacted; neither a successful local test nor HOME persistence proves fixed VM.
"""
from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from uuid import uuid4

ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
CLI_UPDATE_NOTICE = re.compile(
    r"WARNING: A new version of extension '[^'\r\n]+' is available: "
    r"[A-Za-z0-9.+-]+ -> [A-Za-z0-9.+-]+\n"
    r"[ \t]*\u2022 To update: azd extension update [A-Za-z0-9_.-]+\n"
    r"[ \t]*\u2022 To update all: azd extension update --all"
)
SECRET_KEYS = r"authorization|access_token|refresh_token|client_secret|token|cookie|set-cookie"
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
GATES = (
    "runtime_binding", "shared_state", "same_etag_cas", "single_editor_handoff",
    "memory_extraction", "memory_recall", "memory_authorization", "context_boundaries",
    "idle_resume", "published_immutable", "direct_storage_a", "direct_memory_a",
    "direct_storage_b", "direct_memory_b", "gui", "cleanup_a", "cleanup_b",
)


def redact(text: str) -> str:
    text = re.sub(rf"(?im)^({SECRET_KEYS})\s*:[^\r\n]*", r"\1: [REDACTED]", text)
    text = re.sub(rf'(?i)("(?:{SECRET_KEYS})"\s*:\s*)"(?:\\.|[^"\\])*"', r'\1"[REDACTED]"', text)
    text = re.sub(r"(?i)Bearer\s+[^\s\"'<>]+", "Bearer [REDACTED]", text)
    return re.sub(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b", "[REDACTED_JWT]", text)


def reject_request_secrets(value) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if re.fullmatch(SECRET_KEYS, str(key), re.IGNORECASE):
                raise ValueError("request credential fields are forbidden")
            reject_request_secrets(item)
    elif isinstance(value, list):
        for item in value:
            reject_request_secrets(item)
    elif isinstance(value, str) and redact(value) != value:
        raise ValueError("request credential-like values are forbidden")


def parse_cli_object(text: str, *, http_status: int | None = None) -> dict:
    value, end = json.JSONDecoder().raw_decode(text.lstrip())
    suffix = text.lstrip()[end:].strip()
    error_trailer = http_status is not None and http_status >= 400 and re.fullmatch(
        rf"ERROR: POST https://[^\s]+ failed with HTTP {http_status}: {http_status} [^\r\n]+", suffix,
    )
    if suffix and not CLI_UPDATE_NOTICE.fullmatch(suffix) and not error_trailer:
        raise ValueError("unexpected CLI output after JSON object")
    if not isinstance(value, dict):
        raise ValueError("CLI response JSON must be an object")
    return value


def parse_raw_http(stdout: str) -> tuple[int, dict, dict]:
    text = ANSI.sub("", stdout).replace("\r\n", "\n").lstrip("\ufeff")
    matches = list(re.finditer(r"(?m)^HTTP/\d(?:\.\d)?\s+(\d{3})[^\n]*\n", text))
    for match in reversed(matches):
        header_end = text.find("\n\n", match.end() - 1)
        if header_end < 0:
            continue
        body_text = text[header_end + 2:].strip()
        try:
            body = parse_cli_object(body_text, http_status=int(match.group(1)))
        except ValueError:
            continue
        if not isinstance(body, dict):
            raise ValueError("Hosted raw response JSON must be an object")
        headers = {}
        for line in text[match.end():header_end].splitlines():
            name, separator, value = line.partition(":")
            if separator:
                headers[name.strip().lower()] = value.strip()
        return int(match.group(1)), headers, body
    raise ValueError("missing HTTP status/header block followed by a JSON object")


def parse_show(stdout: str) -> dict:
    text = ANSI.sub("", stdout).strip().lstrip("\ufeff")
    for match in re.finditer(r"(?m)^\s*\{", text):
        try:
            value = parse_cli_object(text[match.start():])
            if isinstance(value, dict):
                return value
        except ValueError:
            pass
    raise ValueError("sessions show did not return a JSON object")


def show_state(value: dict) -> str | None:
    for node in (value, value.get("properties"), value.get("session")):
        if isinstance(node, dict):
            state = node.get("state", node.get("status"))
            if isinstance(state, dict):
                state = state.get("state")
            if isinstance(state, str):
                return state.lower()
    return None


@dataclass
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    interrupted: bool = False


def run_process(args: list[str], cwd: str, env: dict, timeout: float) -> CommandResult:
    options = {"start_new_session": True} if os.name != "nt" else {
        "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP,
    }
    process = subprocess.Popen(args, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **options)
    timed_out = interrupted = False
    deadline = time.monotonic() + timeout
    try:
        stdout, stderr = process.communicate(timeout=max(0.1, timeout - min(10, timeout / 4)))
    except (subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
        timed_out = True
        interrupted = isinstance(exc, KeyboardInterrupt)
        cleanup_error = ""
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                               capture_output=True, timeout=max(0.1, min(5, deadline - time.monotonic())), check=False)
            else:
                os.killpg(process.pid, signal.SIGKILL)
        except (OSError, subprocess.TimeoutExpired) as kill_error:
            cleanup_error = f"\nlocal process-tree cleanup: {kill_error}"
        if process.poll() is None:
            process.kill()
        try:
            stdout, stderr = process.communicate(timeout=max(0.1, min(5, deadline - time.monotonic())))
        except subprocess.TimeoutExpired as capture_error:
            stdout, stderr = capture_error.output or b"", capture_error.stderr or b""
            cleanup_error += "\nlocal output capture did not close before deadline"
        stderr += cleanup_error.encode("utf-8", errors="replace")
    return CommandResult(process.returncode, stdout.decode("utf-8", errors="replace"),
                         stderr.decode("utf-8", errors="replace"), timed_out, interrupted)


class CheckFailed(RuntimeError):
    pass


class BudgetExceeded(RuntimeError):
    pass


@dataclass
class Config:
    azd_project: Path
    environment: str
    session_a: str
    session_b: str
    evidence_dir: Path
    endpoint_a: str
    endpoint_b: str
    agent_a: str = "fa-context-a"
    agent_b: str = "fa-context-b"
    team: str = "team-one"
    actor_a: str = "agent-a"
    actor_b: str = "agent-b"
    max_seconds: int = 2700
    gui: bool = False

    def validate(self):
        if not self.azd_project.is_dir():
            raise ValueError("--azd-project must be an existing project directory")
        if not (self.azd_project / "azure.yaml").is_file():
            # azd -C walks up to a parent azure.yaml, which would run against the wrong project.
            raise ValueError("--azd-project must contain its own azure.yaml")
        for name in ("environment", "agent_a", "agent_b", "team", "actor_a", "actor_b"):
            if not ID.fullmatch(getattr(self, name)):
                raise ValueError(f"invalid bounded identifier: {name}")
        for session in (self.session_a, self.session_b):
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,255}", session):
                raise ValueError("invalid session ID")
        if self.session_a == self.session_b or self.agent_a == self.agent_b or self.actor_a == self.actor_b:
            raise ValueError("A and B must have distinct session, agent and actor identifiers")
        project_urls = []
        for actor in ("a", "b"):
            endpoint = getattr(self, "endpoint_" + actor)
            agent = getattr(self, "agent_" + actor)
            pattern = (
                r"https://[a-z0-9][a-z0-9-]*\.services\.ai\.azure\.com"
                r"/api/projects/[A-Za-z0-9][A-Za-z0-9_-]{0,63}/agents/"
                + re.escape(agent) + r"/endpoint/protocols/invocations\?api-version=v1"
            )
            if not re.fullmatch(pattern, endpoint):
                raise ValueError(f"--endpoint-{actor} must be the matching HTTPS invocations endpoint from agent show")
            project_urls.append(endpoint.split("/agents/")[0])
        if project_urls[0] != project_urls[1]:
            raise ValueError("A and B endpoints must belong to the same Foundry project")
        if self.max_seconds < 1 or self.max_seconds > 3600:
            raise ValueError("--max-seconds must be 1..3600")


class Controller:
    def __init__(self, config: Config, runner: Callable = run_process,
                 clock: Callable = time.monotonic, sleeper: Callable = time.sleep):
        config.validate()
        self.config, self.runner, self.clock, self.sleep = config, runner, clock, sleeper
        self.started = clock()
        self.deadline = self.started + config.max_seconds
        self.work_deadline = self.deadline - min(240, config.max_seconds / 4)
        self.run_id = "sc-" + uuid4().hex
        self.directory = config.evidence_dir.resolve() / self.run_id
        self.directory.mkdir(parents=True, exist_ok=False)
        self.env = os.environ.copy()
        self.env["AZURE_DEV_USER_AGENT"] = "microsoft_foundry_skill"
        self.current_gate = None
        self.runtime = {}
        self.state = None
        self.operations = {}
        self.fixtures = {
            "team": {"marker": "team-" + uuid4().hex, "value": "sepia"},
            "private": {"marker": "private-" + uuid4().hex, "value": "indigo"},
            "idle": {"marker": "idle-" + uuid4().hex, "value": "coral"},
        }
        self.work_path = f"/teams/{config.team}/works/{self.run_id}"
        self.memory_path = f"/teams/{config.team}/memory"
        self.report = {
            "run_id": self.run_id, "started_at": datetime.now(timezone.utc).isoformat(),
            "azd_project": str(config.azd_project.resolve()), "environment": config.environment,
            "sessions": {"A": config.session_a, "B": config.session_b},
            "agents": {"A": config.agent_a, "B": config.agent_b},
            "endpoints": {"A": config.endpoint_a, "B": config.endpoint_b},
            "team": config.team, "actors": {"A": config.actor_a, "B": config.actor_b},
            "max_seconds": config.max_seconds, "fixtures": self.fixtures,
            "gates": {name: {"status": "NOT_RUN", "checks": []} for name in GATES},
            "commands": [], "memory_usage": {
                "parent_updates": 1, "parent_searches": 1,
                "accepted_updates": 0, "completed_updates": 0, "accepted_searches": 0,
                "update_attempt_upper_bound": 0, "search_attempt_upper_bound": 0,
                "new_update_attempt_limit": 8, "new_search_attempt_limit": 32,
            },
            "fixed_vm_validated": False,
            "scope_note": "只驗指定 Hosted sessions；Storage 拒絕不單獨證明 RBAC，Memory scope 不是 ACL。",
        }
        self.persist(strict=True)

    def persist(self, strict=False):
        self.report["elapsed_seconds"] = round(self.clock() - self.started, 3)
        usage = self.report["memory_usage"]
        usage["total_accepted_updates_including_parent"] = usage["parent_updates"] + usage["accepted_updates"]
        usage["total_accepted_searches_including_parent"] = usage["parent_searches"] + usage["accepted_searches"]
        usage["unknown_update_attempts_upper_bound"] = usage["update_attempt_upper_bound"] - usage["accepted_updates"]
        usage["unknown_search_attempts_upper_bound"] = usage["search_attempt_upper_bound"] - usage["accepted_searches"]
        text = redact(json.dumps(self.report, ensure_ascii=False, indent=2))
        staging = self.directory / "report.next.json"
        try:
            for attempt in range(6):
                try:
                    staging.write_text(text, encoding="utf-8")
                    staging.replace(self.directory / "report.json")
                    return
                except PermissionError:
                    if attempt == 5:
                        raise
                    # Local file locks (e.g. Windows indexing), not a cloud-operation retry.
                    time.sleep(0.05 * (attempt + 1))
        except OSError as exc:
            if strict:
                raise
            self.report.setdefault("evidence_write_errors", []).append(redact(str(exc)))

    def check(self, condition: bool, name: str, details=None):
        item = {"name": name, "status": "PASS" if condition else "FAIL"}
        if details is not None:
            item["details"] = details
        if self.current_gate:
            self.report["gates"][self.current_gate]["checks"].append(item)
        self.persist()
        if not condition:
            raise CheckFailed(name)

    def budget(self, cleanup=False) -> float:
        remaining = (self.deadline if cleanup else self.work_deadline) - self.clock()
        if remaining < 1:
            raise BudgetExceeded("time budget exhausted; no additional command started")
        return remaining

    def memory_cost(self, payload: dict) -> tuple[int, int]:
        operation, path = payload.get("operation"), payload.get("path", "")
        if operation == "direct_memory":
            return 0, 1
        if operation != "api":
            return 0, 0
        if path.endswith("/context") and payload.get("method") == "GET":
            return 0, 2
        if "/memory/" in path and payload.get("method") == "POST":
            return (0, 1) if path.endswith("/search") else (1, 0)
        return 0, 0

    def count_accepted_memory(self, payload: dict, result: dict):
        outcome = result.get("outcome", {})
        content = outcome.get("content", {})
        if not isinstance(content, dict):
            return
        status = outcome.get("http_status")
        if not isinstance(status, int) or not 200 <= status < 300:
            return
        usage = self.report["memory_usage"]
        updates, searches = self.memory_cost(payload)
        native = content.get("native", {})
        if updates and isinstance(native, dict) and native.get("update_id"):
            usage["accepted_updates"] += 1
        if searches == 2:
            for key in ("team_memory", "private_memory"):
                if isinstance(content.get(key), dict) and content[key].get("native", {}).get("search_id"):
                    usage["accepted_searches"] += 1
        elif searches:
            # direct_memory returns the native object without the Context API envelope.
            data = content if payload["operation"] == "direct_memory" else native
            if isinstance(data, dict) and data.get("search_id"):
                usage["accepted_searches"] += 1

    def command(self, label: str, args: list[str], payload=None, cleanup=False, timeout=180) -> CommandResult:
        remaining = self.budget(cleanup)
        number = len(self.report["commands"]) + 1
        prefix = f"{number:03d}-{label}"
        call = {"label": label, "gate": self.current_gate, "timeout_seconds": min(timeout, 180, remaining), "started": False}
        if payload is not None:
            reject_request_secrets(payload)
            updates, searches = self.memory_cost(payload)
            usage = self.report["memory_usage"]
            if usage["update_attempt_upper_bound"] + updates > usage["new_update_attempt_limit"]:
                raise BudgetExceeded("memory update attempt ceiling reached")
            if usage["search_attempt_upper_bound"] + searches > usage["new_search_attempt_limit"]:
                raise BudgetExceeded("memory search attempt ceiling reached")
            usage["update_attempt_upper_bound"] += updates
            usage["search_attempt_upper_bound"] += searches
            request_path = self.directory / (prefix + ".request.json")
            request_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            args = [str(request_path) if item == "{input_file}" else item for item in args]
            call["request"] = request_path.name
        call.update({"args": args, "stdout": prefix + ".stdout.txt", "stderr": prefix + ".stderr.txt"})
        self.report["commands"].append(call)
        self.persist(strict=not cleanup)
        try:
            call["timeout_seconds"] = min(call["timeout_seconds"], self.budget(cleanup))
        except BudgetExceeded:
            call["not_started_reason"] = "budget expired while saving request evidence"
            self.persist()
            raise
        started = self.clock()
        call["started"] = True
        try:
            result = self.runner(args, str(self.config.azd_project.resolve()), self.env.copy(), call["timeout_seconds"])
        except Exception as exc:
            result = CommandResult(-1, stderr=f"{type(exc).__name__}: {exc}")
        (self.directory / call["stdout"]).write_text(redact(result.stdout), encoding="utf-8", newline="")
        (self.directory / call["stderr"]).write_text(redact(result.stderr), encoding="utf-8", newline="")
        call.update({"returncode": result.returncode, "timed_out": result.timed_out, "interrupted": result.interrupted,
                     "elapsed_seconds": round(self.clock() - started, 3)})
        self.persist()
        if result.interrupted:
            raise KeyboardInterrupt()
        return result

    def scope_args(self) -> list[str]:
        return ["-C", str(self.config.azd_project.resolve()), "-e", self.config.environment]

    def binding(self, actor: str) -> tuple[str, str]:
        return ((self.config.agent_a, self.config.session_a) if actor == "A"
                else (self.config.agent_b, self.config.session_b))

    def invoke(self, actor: str, payload: dict, label: str, timeout=180) -> dict:
        _, session = self.binding(actor)
        endpoint = self.config.endpoint_a if actor == "A" else self.config.endpoint_b
        # Service-name resolution can create an unused session before applying the override.
        args = ["azd", "ai", "agent", "invoke", "--agent-endpoint", endpoint,
                "--session-id", session, "--input-file", "{input_file}", "--output", "raw", *self.scope_args()]
        result = self.command(label, args, payload, timeout=timeout)
        parsed, parse_error = None, None
        try:
            parsed = parse_raw_http(result.stdout)
            self.count_accepted_memory(payload, parsed[2])
            self.report["commands"][-1]["outer_http_status"] = parsed[0]
        except ValueError as exc:
            parse_error = exc
            self.report["commands"][-1]["parse_error"] = str(exc)
        self.check(not result.timed_out and result.returncode == 0, label + ": CLI completed",
                   {"returncode": result.returncode, "timed_out": result.timed_out})
        if parse_error:
            raise parse_error
        status, headers, body = parsed
        self.check(status == 200, label + ": outer HTTP 200", status)
        self.check(body.get("operation") == payload["operation"], label + ": operation matches")
        runtime = body.get("runtime", {})
        self.check(runtime.get("session_id") == session, label + ": explicit session retained", runtime.get("session_id"))
        self.runtime[actor] = runtime
        self.persist()
        return body

    def api(self, actor: str, method: str, path: str, expected: int, label: str,
            body=None, etag=None, timeout=180):
        payload = {"operation": "api", "method": method, "path": path}
        if body is not None:
            payload["body"] = body
        if etag is not None:
            payload["etag"] = etag
        result = self.invoke(actor, payload, label, timeout)["outcome"]
        self.check(not result.get("truncated", False), label + ": complete response")
        self.check(result.get("http_status") == expected, label + ": inner HTTP status",
                   {"expected": expected, "actual": result.get("http_status")})
        return result.get("content")

    def valid_state(self, state: dict):
        self.check(isinstance(state, dict) and isinstance(state.get("etag"), str) and bool(state["etag"]), "state includes ETag")
        artifact = state.get("artifact", {})
        text = artifact.get("text")
        self.check(isinstance(text, str), "artifact text returned")
        self.check(artifact.get("sha256") == hashlib.sha256(text.encode()).hexdigest(), "artifact SHA-256 matches text")
        self.check(type(state.get("revision")) is int and type(artifact.get("version")) is int, "state versions returned")

    def poll_memory(self, actor: str, receipt: dict, label: str):
        operation = receipt["operation_id"]
        self.check(bool(re.fullmatch(r"[a-f0-9]{32}", operation)), label + ": valid operation receipt")
        end = min(self.clock() + 120, self.work_deadline)
        for index in range(12):
            remaining = end - self.clock()
            if remaining < 1:
                break
            result = self.api(actor, "GET", self.memory_path + "/operations/" + operation, 200,
                              f"{label}-poll-{index + 1}", timeout=min(30, remaining))
            native = result.get("native", {})
            status = native.get("status")
            if status == "completed":
                operations = native.get("result", {}).get("memory_operations")
                self.check(isinstance(operations, list) and bool(operations), label + ": nonempty completed extraction", operations)
                self.report["memory_usage"]["completed_updates"] += 1
                self.persist()
                return
            self.check(status in ("queued", "in_progress"), label + ": pending or completed native status", native)
            self.sleep(min(3, max(0, end - self.clock())))
        raise CheckFailed(label + ": extraction not completed within bounded poll window")

    def recall(self, response: dict, kind: str, label: str):
        expected = self.fixtures[kind]
        native = response.get("native", {})
        memories = native.get("memories")
        self.check(isinstance(memories, list), label + ": native memories array")
        text = json.dumps(memories, ensure_ascii=False)
        self.check(expected["marker"] in text and expected["value"] in text, label + ": fixture marker and value recalled")

    def gate(self, name: str, function: Callable, requires=(), enabled=True):
        entry = self.report["gates"][name]
        if not enabled or any(self.report["gates"][dependency]["status"] != "PASS" for dependency in requires):
            entry["reason"] = "optional disabled" if not enabled else "prerequisite gate did not pass"
            self.persist()
            return
        self.current_gate = name
        try:
            function()
            entry["status"] = "PASS"
        except KeyboardInterrupt:
            entry.update({"status": "FAIL", "reason": "interrupted"})
            raise
        except Exception as exc:
            entry.update({"status": "FAIL", "reason": redact(f"{type(exc).__name__}: {exc}")})
        finally:
            self.current_gate = None
            self.persist()

    def runtime_gate(self):
        for actor in ("A", "B"):
            self.invoke(actor, {"operation": "runtime"}, "runtime-" + actor.lower())
            value = self.runtime[actor]
            self.check(isinstance(value.get("process_boot_marker"), str) and bool(value["process_boot_marker"]),
                       actor + ": process boot marker available")
        self.check(self.runtime["A"]["process_boot_marker"] != self.runtime["B"]["process_boot_marker"],
                   "A/B process boot markers are distinct")

    def shared_gate(self):
        goal = "Synthetic POC recall: " + ", ".join(fixture["marker"] for fixture in self.fixtures.values())
        self.state = self.api("A", "POST", self.work_path, 201, "a-create",
                              {"goal": goal, "artifact_text": "synthetic artifact v1"})
        self.valid_state(self.state)
        self.check(self.state["owner"] == self.config.actor_a, "create owner is A")
        self.check(self.fixtures["private"]["value"] not in json.dumps(self.state), "shared work excludes private value")
        other = self.api("B", "GET", self.work_path, 200, "b-read")
        self.check(other == self.state, "B reads same authoritative state")
        self.api("A", "POST", self.work_path, 409, "duplicate-create", {"goal": goal})
        self.api("B", "PATCH", self.work_path, 403, "nonowner-edit",
                 {"progress": "forbidden"}, self.state["etag"])
        self.api("A", "PATCH", self.work_path, 428, "missing-etag", {"progress": "forbidden"})
        old = self.state
        self.state = self.api("A", "PATCH", self.work_path, 200, "owner-update",
                              {"progress": "initial checkpoint"}, old["etag"])
        self.api("A", "PATCH", self.work_path, 412, "stale-etag", {"progress": "stale"}, old["etag"])
        self.check(self.state["revision"] == old["revision"] + 1, "one valid update increments revision once")

    def cas_gate(self):
        before = copy.deepcopy(self.state)
        updates = [{"progress": self.run_id + "-contender-1"}, {"progress": self.run_id + "-contender-2"}]
        result = self.invoke("A", {"operation": "api_race", "path": self.work_path,
                                   "etag": before["etag"], "updates": updates}, "same-etag-race")["outcome"]
        attempts = result.get("attempts", [])
        statuses = [attempt.get("http_status") for attempt in attempts]
        self.check(len(attempts) == 2 and statuses.count(200) == 1 and statuses.count(412) == 1,
                   "two actual responses are exactly one 200 and one 412", statuses)
        self.check(result.get("concurrency") == 2 and result.get("same_client_identity") is True,
                   "bounded same-client race reported")
        winner = attempts[statuses.index(200)]["content"]
        self.state = self.api("B", "GET", self.work_path, 200, "cas-readback")
        self.valid_state(self.state)
        self.check(self.state == winner and self.state["revision"] == before["revision"] + 1,
                   "CAS readback equals winner and revision increases exactly once")
        self.check(self.state["progress"] == updates[statuses.index(200)]["progress"], "winning contender content persisted")

    def handoff_gate(self):
        before = self.state
        self.state = self.api("A", "POST", self.work_path + "/handoff", 200, "handoff-a-b",
                              {"to_actor": self.config.actor_b}, before["etag"])
        self.check(self.state["owner"] == self.config.actor_b, "handoff assigns B")
        self.api("A", "PATCH", self.work_path, 403, "old-owner-denied", {"progress": "forbidden"}, self.state["etag"])
        self.state = self.api("B", "PATCH", self.work_path, 200, "b-edit-after-handoff",
                              {"progress": "B checkpoint", "artifact_text": "synthetic artifact v2"}, self.state["etag"])
        self.valid_state(self.state)

    def extraction_gate(self):
        for kind, suffix in (("team", "/team"), ("private", "/private/" + self.config.actor_a)):
            receipt = self.api("A", "POST", self.memory_path + suffix, 202, "memory-write-" + kind, self.fixtures[kind])
            self.operations[kind] = receipt
            self.poll_memory("A", receipt, "memory-" + kind)

    def recall_gate(self):
        for actor, kind, suffix in (("B", "team", "/team"), ("A", "private", "/private/" + self.config.actor_a)):
            response = self.api(actor, "POST", self.memory_path + suffix + "/search", 200, "recall-" + kind,
                                {"query": self.fixtures[kind]["marker"] + " preferred dashboard color theme"})
            self.recall(response, kind, "recall-" + kind)

    def authorization_gate(self):
        private = self.memory_path + "/private/" + self.config.actor_a
        self.api("B", "POST", private + "/search", 403, "b-private-read-denied", {"query": self.fixtures["private"]["marker"]})
        self.api("B", "POST", private, 403, "b-private-write-denied", {"marker": "denied-" + self.run_id, "value": "denied"})
        self.api("B", "GET", self.memory_path + "/operations/" + self.operations["private"]["operation_id"],
                 403, "b-private-operation-denied")
        self.api("A", "GET", f"/teams/other-{self.run_id}/works/{self.run_id}", 403, "cross-team-denied")
        self.api("A", "POST", self.memory_path + "/team", 422, "injected-scope-denied",
                 {**self.fixtures["team"], "scope": "private:other:target"})
        self.api("A", "PATCH", self.work_path, 422, "injected-owner-denied",
                 {"owner": self.config.actor_a}, self.state["etag"])

    def verify_context(self, actor: str, value: dict, idle=False):
        expected_actor = self.config.actor_a if actor == "A" else self.config.actor_b
        self.check(value.get("actor") == expected_actor and value.get("team") == self.config.team,
                   actor + ": server selected actor/team")
        self.check(value.get("work") == self.state, actor + ": context has latest authoritative state")
        self.check(bool(value.get("role_instruction")), actor + ": server role instruction present")
        private = value.get("private_memory", {})
        self.check(private.get("scope") == f"private:{self.config.team}:{expected_actor}", actor + ": private scope is own")
        self.check(value.get("team_memory", {}).get("scope") == f"team:{self.config.team}", actor + ": team scope is stable")
        self.recall(value["team_memory"], "idle" if idle else "team", actor + ": context team recall")
        if actor == "A":
            self.recall(private, "private", "A: context private recall")
        else:
            self.check(self.fixtures["private"]["value"] not in json.dumps(value), "B context excludes A private value")

    def context_gate(self):
        for actor in ("A", "B"):
            result = self.api(actor, "GET", self.work_path + "/context", 200, "context-" + actor.lower())
            self.verify_context(actor, result)

    def stop_idle(self, actor: str, cleanup=False):
        agent, session = self.binding(actor)
        result = self.command("stop-" + actor.lower(),
                              ["azd", "ai", "agent", "sessions", "stop", session, "--agent-name", agent, *self.scope_args()],
                              cleanup=cleanup, timeout=30 if cleanup else 60)
        # Even if stop fails or times out, still inspect the session; never infer idle from exit code.
        stop_ok = result.returncode == 0 and not result.timed_out
        end = min(self.clock() + 60, self.deadline if cleanup else self.work_deadline)
        observed = None
        for index in range(6):
            if end - self.clock() < 1:
                break
            shown = self.command(f"show-{actor.lower()}-{index + 1}",
                                 ["azd", "ai", "agent", "sessions", "show", session, "--agent-name", agent, *self.scope_args()],
                                 cleanup=cleanup, timeout=min(15, end - self.clock()))
            if shown.returncode == 0 and not shown.timed_out:
                value = parse_show(shown.stdout)
                identity = value.get("session_id", value.get("sessionId", value.get("id")))
                if identity is not None:
                    self.check(identity == session or str(identity).endswith("/sessions/" + session),
                               actor + ": show refers to provided session", identity)
                observed = show_state(value)
                if observed == "idle":
                    self.report["gates"][self.current_gate]["stop_command_succeeded"] = stop_ok
                    self.check(True, actor + ": native session state is idle", value)
                    return
            self.sleep(min(5, max(0, end - self.clock())))
        self.check(False, actor + ": failed to verify native idle state", {"last_state": observed, "stop_ok": stop_ok})

    def resume_gate(self):
        self.invoke("A", {"operation": "runtime"}, "a-pre-stop-runtime")
        before = copy.deepcopy(self.runtime["A"])
        self.stop_idle("A")
        self.state = self.api("B", "PATCH", self.work_path, 200, "b-update-while-a-idle",
                              {"progress": "updated while A idle", "artifact_text": "synthetic artifact idle v3"},
                              self.state["etag"])
        receipt = self.api("B", "POST", self.memory_path + "/team", 202, "idle-team-memory", self.fixtures["idle"])
        self.operations["idle"] = receipt
        self.poll_memory("B", receipt, "idle-team-memory")
        self.invoke("A", {"operation": "runtime"}, "a-resume-same-session")
        after = self.runtime["A"]
        self.check(bool(after.get("process_boot_marker")) and after["process_boot_marker"] != before["process_boot_marker"],
                   "resume has changed process boot marker")
        self.check(after["session_id"] == before["session_id"] == self.config.session_a, "resume retained explicit session ID")
        homes = before.get("home_marker"), after.get("home_marker")
        self.report["gates"]["idle_resume"]["home_marker_retained"] = (
            homes[0] == homes[1] if all(isinstance(value, str) and bool(value) for value in homes) else None
        )
        self.report["gates"]["idle_resume"]["home_persistence_is_gate"] = False
        state = self.api("A", "GET", self.work_path, 200, "resumed-a-state")
        self.check(state == self.state, "resumed A sees B latest draft")
        context = self.api("A", "GET", self.work_path + "/context", 200, "resumed-a-context")
        self.verify_context("A", context, idle=True)

    def publish_gate(self):
        before = self.state
        published = self.api("B", "POST", self.work_path + "/publish", 200, "b-publish", etag=before["etag"])
        self.check(published["status"] == "published" and published["published_by"] == self.config.actor_b, "owner B published")
        self.check(published["artifact"] == before["artifact"], "publication preserves artifact hash/version/text")
        for actor in ("B", "A"):
            self.api(actor, "POST", self.work_path, 409, actor.lower() + "-published-create", {"goal": "forbidden"})
            self.api(actor, "PATCH", self.work_path, 409, actor.lower() + "-published-patch",
                     {"artifact_text": "forbidden"}, published["etag"])
            self.api(actor, "POST", self.work_path + "/handoff", 409, actor.lower() + "-published-handoff",
                     {"to_actor": self.config.actor_a}, published["etag"])
            self.api(actor, "POST", self.work_path + "/publish", 409, actor.lower() + "-published-republish",
                     etag=published["etag"])
        current = self.api("B", "GET", self.work_path, 200, "published-final-readback")
        self.check(current == published, "all supported mutation routes leave published state and ETag unchanged")
        self.state = published

    def direct_gate(self, actor: str, service: str):
        outcome = self.invoke(actor, {"operation": "direct_" + service, "team": self.config.team,
                                      "work": self.run_id}, "direct-" + service + "-" + actor.lower())["outcome"]
        self.check(outcome.get("http_status") in (401, 403), "native HTTP 401/403 required", outcome)
        self.check(outcome.get("access_denied") is True and outcome.get("unexpected_success") is False,
                   "probe confirms access denied")
        entry = self.report["gates"][self.current_gate]
        entry["separate_rbac_proof"] = False
        entry["interpretation"] = ("Storage may be blocked by private network; not separate RBAC proof"
                                   if service == "storage" else "native Memory access denied; scope itself is not ACL")

    def gui_gate(self):
        nonce = "gui-" + uuid4().hex
        outcome = self.invoke("A", {"operation": "gui", "run_id": self.run_id,
                                    "nonce": nonce, "include_screenshot": True}, "gui-a")["outcome"]
        self.check(outcome.get("probe_status") == "passed", "real Hosted GUI probe passed", outcome.get("error"))
        cleanup = outcome.get("cleanup", {})
        self.check(cleanup.get("success") is True and cleanup.get("server_stopped") is True
                   and cleanup.get("files_removed") is True and cleanup.get("errors") == [],
                   "GUI process/file/server cleanup succeeded")
        self.check(type(outcome.get("browser_uid")) is int and outcome["browser_uid"] > 0, "GUI browser is non-root")
        self.check(outcome.get("browser_uid_fields") == [outcome["browser_uid"]] * 4, "GUI browser UID fields match")
        self.check(outcome.get("browser_sandbox_disabled") is False, "GUI browser sandbox not disabled")
        browser_args = outcome.get("browser_command_line", [])
        self.check(browser_args and browser_args[0] == "chromium"
                   and not any(arg.startswith("--headless") or arg == "--no-sandbox" for arg in browser_args),
                   "headed Chromium command has no sandbox bypass")
        self.check(outcome.get("page_ready") is True and outcome.get("completed_value") == nonce,
                   "GUI page callback matches nonce")
        artifacts = outcome["artifacts"]
        for name in ("before", "after"):
            item = artifacts[name]
            data = base64.b64decode(item["png_base64"], validate=True)
            self.check(len(data) <= 256 * 1024 and data.startswith(b"\x89PNG\r\n\x1a\n"), name + ": bounded PNG decoded")
            self.check(len(data) == item["size"] and hashlib.sha256(data).hexdigest() == item["sha256"], name + ": PNG hash/size match")
            (self.directory / ("gui-" + name + ".png")).write_bytes(data)
        self.check(artifacts["before"]["sha256"] != artifacts["after"]["sha256"], "GUI screenshots show a change")
        text = f"shared-context-poc|{self.run_id}|{nonce}|GUI_COMPLETE\n"
        download = artifacts["browser_download"]
        self.check(download["text"] == text and download["sha256"] == hashlib.sha256(text.encode()).hexdigest()
                   and download["size"] == len(text.encode()), "GUI synthetic download text/hash/size match")

    def run(self) -> int:
        try:
            chain = (
                ("runtime_binding", self.runtime_gate), ("shared_state", self.shared_gate),
                ("same_etag_cas", self.cas_gate), ("single_editor_handoff", self.handoff_gate),
                ("memory_extraction", self.extraction_gate), ("memory_recall", self.recall_gate),
                ("memory_authorization", self.authorization_gate), ("context_boundaries", self.context_gate),
                ("idle_resume", self.resume_gate), ("published_immutable", self.publish_gate),
            )
            previous = ()
            for name, function in chain:
                self.gate(name, function, requires=previous)
                previous = (name,)
            for actor in ("A", "B"):
                for service in ("storage", "memory"):
                    self.gate(f"direct_{service}_{actor.lower()}", lambda a=actor, s=service: self.direct_gate(a, s))
            self.gate("gui", self.gui_gate, enabled=self.config.gui)
        except KeyboardInterrupt:
            self.report["interrupted"] = True
        finally:
            for actor in ("A", "B"):
                try:
                    self.gate("cleanup_" + actor.lower(), lambda a=actor: self.stop_idle(a, cleanup=True))
                except KeyboardInterrupt:
                    self.report["interrupted"] = True
            self.report["finished_at"] = datetime.now(timezone.utc).isoformat()
            required = [name for name in GATES if name != "gui" or self.config.gui]
            self.report["overall"] = "PASS" if (all(self.report["gates"][name]["status"] == "PASS" for name in required)
                                                and not self.report.get("evidence_write_errors")) else "FAIL"
            self.report["latest_work"] = self.state
            self.report["native_operation_receipts"] = self.operations
            self.persist()
        return 0 if self.report["overall"] == "PASS" else 1


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--azd-project", type=Path, required=True)
    parser.add_argument("--environment", required=True)
    parser.add_argument("--session-a", required=True)
    parser.add_argument("--session-b", required=True)
    parser.add_argument("--endpoint-a", required=True, help="A invocations endpoint returned by azd ai agent show")
    parser.add_argument("--endpoint-b", required=True, help="B invocations endpoint returned by azd ai agent show")
    parser.add_argument("--agent-a", default="fa-context-a")
    parser.add_argument("--agent-b", default="fa-context-b")
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--team", default="team-one")
    parser.add_argument("--actor-a", default="agent-a")
    parser.add_argument("--actor-b", default="agent-b")
    parser.add_argument("--max-seconds", type=int, default=2700)
    parser.add_argument("--gui", action="store_true", help="執行一次真實 Hosted GUI probe 並保存 PNG")
    args = parser.parse_args(argv)
    try:
        controller = Controller(Config(**vars(args)))
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    old_handler = signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        code = controller.run()
    finally:
        signal.signal(signal.SIGTERM, old_handler)
    print(f"結果：{controller.report['overall']}；證據：{controller.directory / 'report.json'}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
