"""Constrained Hosted checksum/count executor, never a general command runner.

actual_environment=NOT_RUN until independently verified native installation.
Invocation JSON supplies only an opaque launch ID. The Hosted MI must obtain the
immutable assignment and one-use claim from the behavior API before execution.
The child has no credential, HTTP client, model, shell, or arbitrary-code input.

Local SQLite state is private recovery material, NOT shared authority/protection
proof. A process restart recovers already sealed results/checkpoints but cannot
infer termination from a missing PID, lease timeout, or an unowned process. Only
the supervisor holding the original Popen handle can attest that child exited.
All server authority mutations/launch grants are owned by behavior_runtime.
"""

from __future__ import annotations

if __package__ in (None, ""):
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = "shared_context"

import argparse
import hashlib
import json
import logging
import os
import sqlite3
import subprocess
import sys
import sysconfig
import ssl
import re
import asyncio
from contextlib import contextmanager
from pathlib import Path
from threading import RLock
from typing import Annotated, Literal
from uuid import uuid4
from urllib.parse import urlsplit, quote
from datetime import datetime, timezone

import httpx
from pydantic import Field, SecretStr, model_validator

from . import behavior_wire as wire
from .behavior_http_diagnostics import _acquire_diagnostics, _exception_type
from .behavior_wire import Digest, Identifier, WireModel as _Model
from .errors import Problem
from .config import validate_api_audience
from .private_files import guard

WORKLOAD_VERSION = "checksum-count-v1"
MAX_INPUT_BYTES = 8192
LAUNCH_PERMIT_HEADER = "X-Behavior-Launch-Permit"
REPORT_TOKEN_HEADER = "X-Behavior-Report-Token"


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False, ensure_ascii=False).encode("utf-8")


def _digest(value):
    return hashlib.sha256(_json(value)).hexdigest()


def _check(value, model):
    try:
        if not isinstance(value, model):
            raise ValueError("typed control data required")
        return model.model_validate(value.model_dump())
    except (ValueError, TypeError):
        raise Problem(422, "HOSTED_CONTRACT_INVALID", "invalid bounded executor contract") from None


def _parse(value, model):
    try:
        return model.model_validate_json(_json(value))
    except (ValueError, TypeError):
        raise Problem(422, "HOSTED_CONTRACT_INVALID", "invalid bounded executor contract") from None


def _update(value, **updates):
    return type(value).model_validate({**value.model_dump(), **updates})


ChecksumCheckpoint = wire.ChecksumCheckpoint
ChecksumResult = wire.ChecksumResult


class NativeCheckpoint(_Model):
    """Server-stamped portable WORK state, not an Agent Computer filesystem backup."""

    schema_version: Literal["fa-checksum-checkpoint-v1"] = "fa-checksum-checkpoint-v1"
    scope: Literal["owned_child_exit_and_sealed_claim"] = "owned_child_exit_and_sealed_claim"
    predecessor_launch_id: Identifier
    predecessor_evidence_id: Identifier
    predecessor_binding_digest: Digest
    predecessor_identity: wire.HostedIdentity
    predecessor_process_generation: Annotated[int, Field(ge=1)]
    predecessor_process_id: Identifier
    child_pid: Annotated[int, Field(ge=1)]
    exit_code: Literal[0] = 0
    checkpoint: wire.ChecksumCheckpoint


def decode_checkpoint(raw: bytes):
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        raise Problem(409, "HOSTED_CHECKPOINT_INVALID", "required checkpoint is invalid") from None
    if isinstance(value, dict) and "schema_version" in value:
        envelope = _parse(value, NativeCheckpoint)
        return envelope.checkpoint, envelope
    return _parse(value, ChecksumCheckpoint), None


class LocalAssignment(wire.HostedAssignment):
    """Private child DTO, constructed ONLY from a verified API assignment."""

    @model_validator(mode="after")
    def checkpoint_boundary(self):
        if self.checkpoint_after_bytes is not None and self.checkpoint_after_bytes > len(self.input):
            raise ValueError("checkpoint boundary cannot exceed the assigned input")
        return self


class HostedInvocationBody(_Model):
    binding_id: Identifier
    launch_id: Identifier


class HostedInvocation(HostedInvocationBody):
    launch_permit: SecretStr


class LocalExecution(_Model):
    launch_id: Identifier
    assignment_digest: Digest
    supervisor_id: Identifier
    child_nonce: Identifier
    phase: Literal["spawn_unknown", "running", "result_saved", "sealed", "sdk_not_started"]
    child_pid: Annotated[int, Field(ge=1)] | None = None
    control: Literal["continue", "park", "cancel"] = "continue"
    control_request_id: Identifier | None = None
    outcome: Literal["succeeded", "parked", "stopped", "failed"] | None = None
    checkpoint: bytes | None = None
    result: ChecksumResult | wire.AssistantExecutionResult | None = None
    sdk_stop: wire.AssistantSdkStop | None = Field(default=None, exclude_if=lambda value: value is None)
    sdk_no_start: wire.SdkNoStart | None = Field(default=None, exclude_if=lambda value: value is None)
    exit_code: int | None = None
    child_exited: bool = False
    sequence: Annotated[int, Field(ge=1)] = 1

    @model_validator(mode="after")
    def sealed_means_owned_exit(self):
        released = (self.sdk_stop is not None and self.sdk_stop.release is not None and not self.child_exited
                    and self.exit_code is None and self.child_pid == self.sdk_stop.pid
                    and self.outcome in {"succeeded", "stopped", "failed"})
        if self.phase == "sealed" and (
            not (self.child_exited and self.exit_code is not None or released)
            or self.child_pid is None or self.outcome is None
        ):
            raise ValueError("sealed local evidence requires an observed owned child exit or resident release")
        if self.phase == "sdk_not_started":
            if (self.sdk_no_start is None or self.child_exited or self.child_pid is not None
                    or self.exit_code is not None or self.sdk_stop is not None
                    or self.outcome != "failed" or self.result is not None or self.checkpoint is not None):
                raise ValueError("a no-SDK disposition cannot claim an owned child exit or SDK result")
        elif self.sdk_no_start is not None:
            raise ValueError("no-SDK evidence needs its explicit versioned disposition")
        return self


class LocalJournal:
    def __init__(self, root: Path):
        if not root.is_absolute() or ".." in root.parts:
            raise Problem(422, "HOSTED_STATE_PATH_INVALID", "private state requires a fixed absolute root")
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        guard(root, root)
        self.root, self.path = root, root / "executions.sqlite3"
        guard(root, self.path)
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS assignments (launch_id TEXT PRIMARY KEY, body BLOB NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS executions (launch_id TEXT PRIMARY KEY, body BLOB NOT NULL)")
            db.execute(
                "CREATE TABLE IF NOT EXISTS chunks "
                "(launch_id TEXT NOT NULL, start_offset INTEGER NOT NULL, end_offset INTEGER NOT NULL, "
                "PRIMARY KEY(launch_id, start_offset))"
            )
            db.execute("CREATE TABLE IF NOT EXISTS controls (request_id TEXT PRIMARY KEY, launch_id TEXT NOT NULL, action TEXT NOT NULL)")
            db.execute(
                "CREATE TABLE IF NOT EXISTS claims (invocation_id TEXT PRIMARY KEY, launch_id TEXT UNIQUE NOT NULL, "
                "binding_id TEXT NOT NULL, process_id TEXT NOT NULL, permit_digest TEXT NOT NULL, request_id TEXT NOT NULL)"
            )

    @contextmanager
    def connect(self):
        guard(self.root, self.path)
        db = sqlite3.connect(self.path, timeout=5)
        db.execute("PRAGMA synchronous=FULL")
        try:
            with db:
                yield db
        finally:
            db.close()

    def _load(self, db, table, launch_id, model):
        if table not in {"assignments", "executions"}:
            raise Problem(500, "HOSTED_STATE_INVALID", "unknown private state type")
        row = db.execute(f"SELECT body FROM {table} WHERE launch_id=?", (launch_id,)).fetchone()
        if row is None:
            raise Problem(409, "HOSTED_MATERIAL_MISSING", "required assigned execution material is unavailable")
        try:
            value = model.model_validate_json(row[0])
            if value.launch_id != launch_id:
                raise ValueError("private execution row identity differs")
            return value
        except (ValueError, TypeError):
            raise Problem(409, "HOSTED_STATE_INVALID", "retained execution state failed validation") from None

    def assignment(self, launch_id: str) -> LocalAssignment:
        with self.connect() as db:
            return self._load(db, "assignments", launch_id, LocalAssignment)

    def reserve_claim(self, invocation_id, launch_id, binding_id, process_id, permit_digest):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT launch_id,binding_id,process_id,permit_digest,request_id FROM claims WHERE invocation_id=?",
                (invocation_id,),
            ).fetchone()
            if existing is not None:
                if existing[:4] != (launch_id, binding_id, process_id, permit_digest):
                    raise Problem(409, "HOSTED_CLAIM_FENCED", "original native claim belongs to another binding/process")
                return existing[4], False
            request_id = uuid4().hex
            db.execute("INSERT INTO claims VALUES (?, ?, ?, ?, ?, ?)", (
                invocation_id, launch_id, binding_id, process_id, permit_digest, request_id,
            ))
            return request_id, True

    def route(self, invocation_id):
        with self.connect() as db:
            result = db.execute("SELECT binding_id,launch_id FROM claims WHERE invocation_id=?", (invocation_id,)).fetchone()
            if result is None:
                raise Problem(409, "HOSTED_CLAIM_UNKNOWN", "original native claim is not locally known")
            return result

    def execution(self, launch_id: str) -> LocalExecution:
        with self.connect() as db:
            return self._load(db, "executions", launch_id, LocalExecution)

    def reserve(self, assignment: LocalAssignment, supervisor_id: str) -> bool:
        assignment = _check(assignment, LocalAssignment)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT body FROM assignments WHERE launch_id=?", (assignment.launch_id,)).fetchone()
            if existing is not None:
                if self._load(db, "assignments", assignment.launch_id, LocalAssignment) != assignment:
                    raise Problem(409, "HOSTED_LAUNCH_CONFLICT", "launch ID is already bound to different immutable material")
                return False
            if assignment.previous_launch_id is not None and assignment.workload_version == "assistant-v1.2":
                from .behavior_assistant_wire import Checkpoint, Task
                checkpoint = Checkpoint.model_validate_json(assignment.checkpoint)
                task = Task.model_validate_json(assignment.input)
                if checkpoint.task_digest != _digest(task.model_dump(mode="json")):
                    raise Problem(409, "HOSTED_RESUME_DENIED", "assistant checkpoint belongs to another task")
                local = db.execute("SELECT 1 FROM executions WHERE launch_id=?", (assignment.previous_launch_id,)).fetchone()
                if local is not None:
                    previous = self._load(db, "executions", assignment.previous_launch_id, LocalExecution)
                    prior = self._load(db, "assignments", assignment.previous_launch_id, LocalAssignment)
                    if (previous.phase != "sealed" or previous.outcome != "parked"
                            or previous.checkpoint != assignment.checkpoint or prior.input_digest != assignment.input_digest
                            or prior.identity == assignment.identity and prior.process_generation >= assignment.process_generation):
                        raise Problem(409, "HOSTED_RESUME_DENIED", "assistant predecessor is not the exact sealed checkpoint")
                # Absence is not local proof: the separate AssistantClient must
                # obtain the archived SDK bytes via the fresh server assignment.
            elif assignment.previous_launch_id is not None:
                checkpoint, envelope = decode_checkpoint(assignment.checkpoint)
                if envelope is not None and (
                    envelope.predecessor_launch_id != assignment.previous_launch_id
                    or checkpoint.input_digest != assignment.input_digest
                ):
                    raise Problem(409, "HOSTED_RESUME_DENIED", "server checkpoint lineage differs from this assigned work")
                local = db.execute("SELECT 1 FROM executions WHERE launch_id=?", (assignment.previous_launch_id,)).fetchone()
                if local is None:
                    if envelope is None:
                        raise Problem(409, "HOSTED_MATERIAL_MISSING", "a complete server-verified predecessor checkpoint is required")
                else:
                    previous = self._load(db, "executions", assignment.previous_launch_id, LocalExecution)
                    prior_assignment = self._load(db, "assignments", assignment.previous_launch_id, LocalAssignment)
                    if (
                        previous.phase != "sealed" or previous.outcome != "parked" or previous.checkpoint is None
                        or decode_checkpoint(previous.checkpoint)[0] != checkpoint
                        or prior_assignment.input_digest != assignment.input_digest
                        or prior_assignment.identity == assignment.identity
                        and prior_assignment.process_generation >= assignment.process_generation
                        or envelope is None and prior_assignment.identity != assignment.identity
                        or envelope is not None and envelope.predecessor_identity != prior_assignment.identity
                    ):
                        raise Problem(409, "HOSTED_RESUME_DENIED", "a verified parked predecessor/checkpoint is required")
            execution = LocalExecution(
                launch_id=assignment.launch_id, assignment_digest=_digest(assignment.model_dump(mode="json")),
                supervisor_id=supervisor_id, child_nonce=uuid4().hex, phase="spawn_unknown",
            )
            db.execute("INSERT INTO assignments VALUES (?, ?)", (assignment.launch_id, _json(assignment.model_dump(mode="json"))))
            db.execute("INSERT INTO executions VALUES (?, ?)", (assignment.launch_id, _json(execution.model_dump(mode="json"))))
            return True

    def _save(self, db, execution):
        db.execute("UPDATE executions SET body=? WHERE launch_id=?", (
            _json(execution.model_dump(mode="json")), execution.launch_id,
        ))

    def control(self, launch_id: str, action: Literal["park", "cancel"], request_id: str):
        if action not in {"park", "cancel"}:
            raise Problem(422, "HOSTED_CONTROL_INVALID", "only verified park/cancel controls are supported")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = self._load(db, "executions", launch_id, LocalExecution)
            previous = db.execute("SELECT launch_id,action FROM controls WHERE request_id=?", (request_id,)).fetchone()
            if previous is not None:
                if previous != (launch_id, action):
                    raise Problem(409, "HOSTED_CONTROL_CONFLICT", "control request ID is bound to another action")
                return current
            if current.phase in {"sealed", "sdk_not_started"}:
                return current
            if current.control_request_id is not None:
                if current.control != "park" or action != "cancel":
                    raise Problem(409, "HOSTED_CONTROL_CONFLICT", "only cancellation can supersede a pending park")
            db.execute("INSERT INTO controls VALUES (?, ?, ?)", (request_id, launch_id, action))
            current = _update(current, control=action, control_request_id=request_id, sequence=current.sequence + 1)
            self._save(db, current)
            return current

    def enter_child(self, launch_id: str, nonce: str) -> LocalAssignment:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            task = self._load(db, "assignments", launch_id, LocalAssignment)
            current = self._load(db, "executions", launch_id, LocalExecution)
            if current.phase != "spawn_unknown" or current.child_nonce != nonce or current.child_pid is not None:
                raise Problem(409, "HOSTED_CHILD_FENCED", "the one-use child launch is no longer available")
            if current.assignment_digest != _digest(task.model_dump(mode="json")):
                raise Problem(409, "HOSTED_INPUT_CHANGED", "retained assigned bytes have changed")
            self._save(db, _update(current, phase="running", child_pid=os.getpid(), sequence=current.sequence + 1))
            return task

    def save_chunk(self, launch_id: str, nonce: str, start: int, end: int):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = self._load(db, "executions", launch_id, LocalExecution)
            if current.phase != "running" or current.child_nonce != nonce or current.child_pid != os.getpid():
                raise Problem(409, "HOSTED_CHILD_FENCED", "child execution ownership differs")
            db.execute("INSERT INTO chunks VALUES (?, ?, ?)", (launch_id, start, end))

    def save_result(self, launch_id, nonce, outcome, *, checkpoint=None, result=None):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = self._load(db, "executions", launch_id, LocalExecution)
            if current.phase != "running" or current.child_nonce != nonce or current.child_pid != os.getpid():
                raise Problem(409, "HOSTED_CHILD_FENCED", "child result ownership differs")
            current = _update(
                current, phase="result_saved", outcome=outcome, checkpoint=checkpoint,
                result=result, sequence=current.sequence + 1,
            )
            self._save(db, current)

    def seal(self, launch_id: str, supervisor_id: str, pid: int, exit_code: int) -> LocalExecution:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = self._load(db, "executions", launch_id, LocalExecution)
            if current.phase == "sealed":
                return current
            if current.supervisor_id != supervisor_id or current.child_pid not in (None, pid):
                raise Problem(409, "HOSTED_EXIT_UNOWNED", "only the original supervisor can seal its owned child exit")
            valid = current.phase == "result_saved" and exit_code == 0
            current = _update(
                current, phase="sealed", child_pid=pid, exit_code=exit_code, child_exited=True,
                outcome=current.outcome if valid else "failed",
                checkpoint=current.checkpoint if valid else None, result=current.result if valid else None,
                sequence=current.sequence + 1,
            )
            self._save(db, current)
            return current


class ChecksumSupervisor:
    def __init__(self, root: Path, *, python_path: Path | None = None, python_sha256: str | None = None):
        self.journal = LocalJournal(root)
        self.supervisor_id = uuid4().hex
        # Windows venv python.exe is a launcher: its PID is not the executing
        # child. Invoke the real interpreter directly, with a fixed bootstrap.
        self.python = (python_path or Path(getattr(sys, "_base_executable", sys.executable))).resolve(strict=True)
        if not self.python.is_file():
            raise Problem(403, "HOSTED_EXECUTABLE_INVALID", "a pinned trusted Python executable is required")
        self.python_digest = python_sha256 or hashlib.sha256(self.python.read_bytes()).hexdigest()
        self.module = Path(__file__).resolve(strict=True)
        self.site_packages = Path(sysconfig.get_paths()["purelib"]).resolve(strict=True)
        self.module_digest = hashlib.sha256(self.module.read_bytes()).hexdigest()
        self.children: dict[str, subprocess.Popen] = {}
        self.lock = RLock()

    def start(self, assignment: LocalAssignment) -> LocalExecution:
        if assignment.workload_version != WORKLOAD_VERSION:
            raise Problem(403, "HOSTED_PROFILE_DENIED", "checksum supervisor cannot run an assistant profile")
        with self.lock:
            if not self.journal.reserve(assignment, self.supervisor_id):
                return self.observe(assignment.launch_id)
            if (
                hashlib.sha256(self.python.read_bytes()).hexdigest() != self.python_digest
                or hashlib.sha256(self.module.read_bytes()).hexdigest() != self.module_digest
            ):
                raise Problem(403, "HOSTED_EXECUTABLE_CHANGED", "trusted executor bytes changed; launch stays unknown")
            current = self.journal.execution(assignment.launch_id)
            environment = {name: os.environ[name] for name in ("SystemRoot", "WINDIR", "LANG", "LC_ALL") if name in os.environ}
            environment.update({"PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1"})
            try:
                child = subprocess.Popen(
                    [str(self.python), "-I", "-c",
                     "import runpy,sys;sys.path.insert(0,sys.argv.pop(1));p=sys.argv.pop(1);runpy.run_path(p,run_name='__main__')",
                     str(self.site_packages), str(self.module), "_worker", "--root", str(self.journal.root),
                     "--launch-id", assignment.launch_id, "--nonce", current.child_nonce],
                    cwd=str(self.module.parent), env=environment, shell=False,
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            except OSError:
                raise Problem(503, "HOSTED_SPAWN_UNKNOWN", "original child launch remains reserved; no automatic retry") from None
            self.children[assignment.launch_id] = child
            return self.journal.execution(assignment.launch_id)

    def observe(self, launch_id: str) -> LocalExecution:
        with self.lock:
            current = self.journal.execution(launch_id)
            child = self.children.get(launch_id)
            if current.phase == "sealed":
                if child is not None and (
                    child.poll() is None or child.pid != current.child_pid or child.returncode != current.exit_code
                ):
                    raise Problem(409, "HOSTED_EXIT_UNOWNED", "saved terminal state does not match the actual owned child")
                self._validate_saved_output(launch_id, current)
                return current
            if child is None or child.poll() is None:
                return current
            code = child.wait(timeout=0)
            if current.phase == "result_saved" and code == 0:
                self._validate_saved_output(launch_id, current)
            return self.journal.seal(launch_id, self.supervisor_id, child.pid, code)

    def _validate_saved_output(self, launch_id: str, current: LocalExecution):
        task = self.journal.assignment(launch_id)
        if current.assignment_digest != _digest(task.model_dump(mode="json")):
            raise Problem(409, "HOSTED_RESULT_INVALID", "saved output assignment digest differs")
        if current.outcome == "succeeded":
            expected = ChecksumResult(
                input_digest=task.input_digest, byte_count=len(task.input), newline_count=task.input.count(b"\n"),
            )
            if current.result != expected or current.checkpoint is not None:
                raise Problem(409, "HOSTED_RESULT_INVALID", "saved checksum/count does not match the original bytes")
        elif current.outcome == "parked":
            if current.checkpoint is None or current.result is not None:
                raise Problem(409, "HOSTED_RESULT_INVALID", "required parked checkpoint is missing")
            checkpoint = _parse(json.loads(current.checkpoint), ChecksumCheckpoint)
            prefix = task.input[:checkpoint.processed_bytes]
            if (
                checkpoint.input_digest != task.input_digest or checkpoint.processed_bytes > len(task.input)
                or checkpoint.prefix_digest != hashlib.sha256(prefix).hexdigest()
                or checkpoint.newline_count != prefix.count(b"\n")
            ):
                raise Problem(409, "HOSTED_RESULT_INVALID", "parked checkpoint does not match the original byte prefix")
        elif current.result is not None or current.checkpoint is not None:
            raise Problem(409, "HOSTED_RESULT_INVALID", "failed/stopped execution cannot claim a successful result")

    def control(self, launch_id: str, action: Literal["park", "cancel"], request_id: str, *, wait_seconds: float = 0):
        if type(wait_seconds) not in (int, float) or not 0 <= wait_seconds <= 10:
            raise Problem(422, "HOSTED_WAIT_INVALID", "control wait must be bounded to ten seconds")
        self.journal.control(launch_id, action, request_id)
        with self.lock:
            child = self.children.get(launch_id)
            if child is not None and wait_seconds:
                try:
                    child.wait(timeout=wait_seconds)
                except subprocess.TimeoutExpired:
                    return self.journal.execution(launch_id)
            return self.observe(launch_id)


def run_checksum_child(root: Path, launch_id: str, nonce: str) -> None:
    journal = LocalJournal(root)
    assignment = journal.enter_child(launch_id, nonce)
    content = assignment.input
    offset, newline_count = 0, 0
    if assignment.checkpoint is not None:
        checkpoint, _ = decode_checkpoint(assignment.checkpoint)
        if (
            checkpoint.input_digest != assignment.input_digest or checkpoint.processed_bytes > len(content)
            or checkpoint.prefix_digest != hashlib.sha256(content[:checkpoint.processed_bytes]).hexdigest()
            or checkpoint.newline_count != content[:checkpoint.processed_bytes].count(b"\n")
        ):
            raise Problem(409, "HOSTED_CHECKPOINT_INVALID", "required checkpoint does not bind the assigned original bytes")
        offset, newline_count = checkpoint.processed_bytes, checkpoint.newline_count
    hasher = hashlib.sha256(content[:offset])
    while True:
        current = journal.execution(launch_id)
        park = current.control == "park" or (
            assignment.checkpoint_after_bytes is not None and offset >= assignment.checkpoint_after_bytes
        )
        if current.control == "cancel":
            journal.save_result(launch_id, nonce, "stopped")
            return
        if offset >= len(content):
            journal.save_result(launch_id, nonce, "succeeded", result=ChecksumResult(
                input_digest=hasher.hexdigest(), byte_count=len(content), newline_count=newline_count,
            ))
            return
        if park:
            checkpoint = ChecksumCheckpoint(
                input_digest=assignment.input_digest, processed_bytes=offset,
                prefix_digest=hasher.hexdigest(), newline_count=newline_count,
            )
            journal.save_result(launch_id, nonce, "parked", checkpoint=_json(checkpoint.model_dump(mode="json")))
            return
        end = min(offset + assignment.chunk_bytes, len(content))
        if assignment.checkpoint_after_bytes is not None:
            end = min(end, assignment.checkpoint_after_bytes)
        hasher.update(content[offset:end])
        newline_count += content[offset:end].count(b"\n")
        journal.save_chunk(launch_id, nonce, offset, end)
        offset = end


class BehaviorExecutorClient:
    """Fixed executor-only API routes with Hosted MI authentication.

    No submit/start/adopt/drain/approval routes exist on this client. Secret
    launch/report capabilities are serialized only into the authenticated TLS
    request; never into child input, persistent state, errors or telemetry.
    """

    def __init__(self, api_url: str, audience: str, credential=None, *, transport=None):
        parsed = urlsplit(api_url)
        try:
            validate_api_audience(audience)
        except (ValueError, TypeError):
            raise Problem(403, "HOSTED_API_CONFIGURATION", "the configured exact API audience is required") from None
        if (
            parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path not in ("", "/")
            or parsed.port not in (None, 443)
        ):
            raise Problem(403, "HOSTED_API_CONFIGURATION", "an exact HTTPS behavior API and audience are required")
        self.api_url, self.audience, self.credential = api_url.rstrip("/"), audience, credential
        self.http = httpx.Client(
            transport=transport or httpx.HTTPTransport(retries=0, verify=ssl.create_default_context()),
            timeout=httpx.Timeout(20, connect=5), follow_redirects=False, trust_env=False,
        )

    def close(self):
        self.http.close()

    def _post(self, binding_id, launch_id, suffix, command, response_model):
        for value in (binding_id, launch_id):
            if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", value) is None:
                raise Problem(422, "HOSTED_API_PATH_INVALID", "an assigned opaque binding/launch ID is required")
        if self.credential is None:
            raise Problem(403, "HOSTED_MI_DISABLED", "Hosted managed-identity credentials are not configured")
        body = command.model_dump(mode="json")
        private_headers = {}
        for field in ("launch_permit", "report_token"):
            if hasattr(command, field):
                secret = getattr(command, field)
                if not isinstance(secret, SecretStr) or re.fullmatch(r"[a-f0-9]{64}", secret.get_secret_value()) is None:
                    raise Problem(403, "HOSTED_CAPABILITY_INVALID", "required private execution capability is invalid")
                body.pop(field, None)
                private_headers[LAUNCH_PERMIT_HEADER if field == "launch_permit" else REPORT_TOKEN_HEADER] = secret.get_secret_value()
        raw = _json(body)
        if len(raw) > 24 * 1024:
            raise Problem(413, "HOSTED_REPORT_LIMIT", "complete executor request exceeds its bound")
        try:
            token = self.credential.get_token(self.audience + "/.default").token
        except Exception as error:
            raise Problem(503, "HOSTED_MI_UNAVAILABLE", "managed-identity token acquisition failed",
                          upstream_phase="token", upstream_status=None,
                          exception_type=_exception_type(error)) from None
        url = (
            f"{self.api_url}/behavior/v1/bindings/{quote(binding_id, safe='')}"
            f"/hosted/launches/{quote(launch_id, safe='')}/{suffix}"
        )
        phase, status = "request", None
        try:
            with self.http.stream("POST", url, content=raw, headers={
                "Authorization": "Bearer " + token, "Content-Type": "application/json",
                "Accept": "application/json", "Accept-Encoding": "identity",
                **private_headers,
            }) as response:
                phase, status = "response_status", response.status_code
                if response.status_code != 200 or response.headers.get("content-encoding", "identity") != "identity":
                    raise Problem(503, "HOSTED_API_OUTCOME_UNKNOWN", "executor API outcome is unavailable; retain the original intent",
                                  upstream_phase=phase, upstream_status=status,
                                  **_acquire_diagnostics(response.headers))
                phase = "response_body"
                data = bytearray()
                for chunk in response.iter_bytes():
                    if len(data) + len(chunk) > 64 * 1024:
                        raise Problem(502, "HOSTED_API_RESPONSE_INVALID", "executor API response exceeds its bound")
                    data.extend(chunk)
                try:
                    phase = "response_decode"
                    parsed = response_model.model_validate_json(data)
                    if response_model is wire.HostedClaimReceipt:
                        returned_token = response.headers.get(REPORT_TOKEN_HEADER)
                        if parsed.claim_state == "fresh":
                            if returned_token is None or re.fullmatch(r"[a-f0-9]{64}", returned_token) is None:
                                raise Problem(502, "HOSTED_API_RESPONSE_INVALID", "fresh claim is missing its private process capability")
                            parsed = parsed.model_copy(update={"report_token": SecretStr(returned_token)})
                        elif returned_token is not None:
                            raise Problem(502, "HOSTED_API_RESPONSE_INVALID", "claim replay cannot issue a new process capability")
                    phase = "response_close"
                    return parsed
                except ValueError as error:
                    raise Problem(502, "HOSTED_API_RESPONSE_INVALID", "executor API returned an invalid typed response",
                                  upstream_phase=phase, upstream_status=status,
                                  exception_type=_exception_type(error)) from None
        except httpx.HTTPError as error:
            raise Problem(503, "HOSTED_API_OUTCOME_UNKNOWN", "executor API transport is unknown; no automatic retry",
                          upstream_phase=phase, upstream_status=status,
                          exception_type=_exception_type(error)) from None

    def claim(self, binding_id, launch_id, command):
        return self._post(binding_id, launch_id, "claim", command, wire.HostedClaimReceipt)

    def resolve_probe(self, binding_id, launch_id, probe_id, command):
        if re.fullmatch(r"[a-f0-9]{64}", probe_id) is None:
            raise Problem(403, "HOSTED_PROBE_INVALID", "a server-issued fresh probe ID is required")
        return self._post(binding_id, launch_id, f"probes/{probe_id}/resolve", command, wire.HostedProbe)

    def report(self, binding_id, launch_id, probe_id, command):
        if re.fullmatch(r"[a-f0-9]{64}", probe_id) is None:
            raise Problem(403, "HOSTED_PROBE_INVALID", "a server-issued fresh probe ID is required")
        return self._post(binding_id, launch_id, f"probes/{probe_id}/report", command, wire.HostedEvidenceReceipt)


class HostedRuntime:
    def __init__(self, identity, supervisor: ChecksumSupervisor, api: BehaviorExecutorClient, *,
                 clock=lambda: datetime.now(timezone.utc)):
        self.identity = _check(identity, wire.HostedIdentity)
        self.supervisor, self.api = supervisor, api
        self.clock = clock
        self.report_tokens: dict[str, SecretStr] = {}
        self.lock = RLock()

    @classmethod
    def from_environment(cls):
        if os.environ.get("BEHAVIOR_HOSTED_ENABLED") != "true":
            raise Problem(403, "HOSTED_RUNTIME_DISABLED", "native behavior Hosted runtime is not explicitly enabled")
        from azure.identity import ManagedIdentityCredential

        try:
            identity = wire.HostedIdentity(
                agent_id=os.environ["FOUNDRY_AGENT_ID"], agent_name=os.environ["FOUNDRY_AGENT_NAME"],
                agent_version=os.environ["FOUNDRY_AGENT_VERSION"], session_id=os.environ["FOUNDRY_AGENT_SESSION_ID"],
            )
            home = Path(os.environ["HOME"])
            if not home.is_absolute() or not home.is_dir():
                raise ValueError("private HOME missing")
            root = home / ".fa-behavior-checksum-v1"
            guard(home, root)
            supervisor = ChecksumSupervisor(root)
            api = BehaviorExecutorClient(
                os.environ["BEHAVIOR_API_URL"], os.environ["BEHAVIOR_API_AUDIENCE"],
                ManagedIdentityCredential(client_id=os.environ.get("AZURE_CLIENT_ID") or None),
            )
        except (KeyError, ValueError, OSError):
            raise Problem(403, "HOSTED_RUNTIME_DISABLED", "exact native identity/private storage/API configuration is missing") from None
        return cls(identity, supervisor, api)

    def _native(self, invocation_id, session_id):
        if (
            session_id != self.identity.session_id
            or not isinstance(invocation_id, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", invocation_id) is None
        ):
            raise Problem(403, "HOSTED_NATIVE_BINDING_DENIED", "request routing differs from the trusted platform environment")

    def start(self, payload, invocation_id, session_id):
        self._native(invocation_id, session_id)
        payload = _check(payload, HostedInvocation)
        permit = payload.launch_permit
        if not isinstance(permit, SecretStr) or re.fullmatch(r"[a-f0-9]{64}", permit.get_secret_value()) is None:
            raise Problem(403, "HOSTED_CAPABILITY_INVALID", "native launch capability is unavailable")
        with self.lock:
            request_id, fresh = self.supervisor.journal.reserve_claim(
                invocation_id, payload.launch_id, payload.binding_id, self.supervisor.supervisor_id,
                hashlib.sha256(permit.get_secret_value().encode("ascii")).hexdigest(),
            )
            if not fresh:
                # Historical/unknown claim is not a new child launch permission.
                return {"launch_id": payload.launch_id, "invocation_id": invocation_id, "status": "pending"}
            receipt = self.api.claim(payload.binding_id, payload.launch_id, wire.HostedClaimRequest(
                request_id=request_id, invocation_id=invocation_id, identity=self.identity,
                process_id=self.supervisor.supervisor_id, launch_permit=permit,
            ))
            assignment = receipt.assignment
            if (
                receipt.claim_state != "fresh" or receipt.report_token is None
                or assignment.launch_id != payload.launch_id or assignment.invocation_id != invocation_id
                or assignment.identity != self.identity or assignment.process_id != self.supervisor.supervisor_id
            ):
                raise Problem(409, "HOSTED_CLAIM_UNKNOWN", "a fresh assigned one-use launch claim was not obtained")
            local = LocalAssignment.model_validate(assignment.model_dump())
            self.report_tokens[payload.launch_id] = receipt.report_token
            self.supervisor.start(local)
            return {"launch_id": payload.launch_id, "invocation_id": invocation_id, "status": "accepted"}

    def observe(self, invocation_id, session_id, probe_id, *, cancellation=False):
        self._native(invocation_id, session_id)
        binding_id, launch_id = self.supervisor.journal.route(invocation_id)
        assignment = self.supervisor.journal.assignment(launch_id)
        token = self.report_tokens.get(launch_id)
        if token is None:
            raise Problem(409, "HOSTED_PROCESS_UNOWNED", "restarted process cannot impersonate the original reporter")
        request = wire.HostedProbeRequest(
            request_id=_digest(["resolve", launch_id, probe_id]),
            invocation_id=invocation_id, identity=self.identity, process_id=self.supervisor.supervisor_id,
            claim_id=assignment.claim_id, report_token=token,
        )
        probe = self.api.resolve_probe(binding_id, launch_id, probe_id, request)
        if (
            probe.launch_id != launch_id or probe.probe_id != probe_id or probe.claim_id != assignment.claim_id
            or probe.invocation_id != invocation_id or probe.identity != self.identity
            or probe.process_id != assignment.process_id or probe.process_generation != assignment.process_generation
            or probe.binding_digest != assignment.binding_digest
            or probe.expires_at.tzinfo is None or probe.expires_at <= self.clock()
            or cancellation and probe.directive != "cancel"
        ):
            raise Problem(403, "HOSTED_PROBE_INVALID", "fresh server control differs from the original assigned execution")
        if probe.directive in {"park", "cancel"}:
            if probe.control_request_id is None:
                raise Problem(403, "HOSTED_PROBE_INVALID", "control requires its retained server request ID")
            state = self.supervisor.control(launch_id, probe.directive, probe.control_request_id, wait_seconds=2)
        else:
            state = self.supervisor.observe(launch_id)
        report = wire.HostedReport(
            request_id=_digest(["report", launch_id, probe_id]),
            invocation_id=invocation_id, identity=self.identity, process_id=self.supervisor.supervisor_id,
            claim_id=assignment.claim_id, report_token=token, probe_id=probe_id,
            process_generation=assignment.process_generation, binding_digest=assignment.binding_digest,
            sequence=state.sequence, phase=state.phase, child_pid=state.child_pid,
            supervisor_id=state.supervisor_id, outcome=state.outcome, child_exited=state.child_exited,
            exit_code=state.exit_code, checkpoint=state.checkpoint, result=state.result,
            sdk_stop=state.sdk_stop,
            sdk_no_start=state.sdk_no_start,
        )
        return self.api.report(binding_id, launch_id, probe_id, report)

    def keepalive(self, invocation_id, session_id, launch_id):
        raise Problem(403, "HOSTED_KEEPALIVE_UNAVAILABLE", "this workload has no read-only keepalive")


def create_host(runtime: HostedRuntime, *, native_runtime=None, responses_handler_factory=None):
    """Build the real SDK host; no legacy Hosted/M2/M3 registration is altered."""
    from azure.ai.agentserver.invocations import InvocationAgentServerHost
    from starlette.responses import JSONResponse
    from .behavior_foundry_probe import NativeStateProbe, PROBE_HEADER, probe_response
    from .behavior_native_turn_wire import NATIVE_TURN_HEADER

    if native_runtime is None and os.environ.get("FA_NATIVE_TURNS"):
        from .native_sdk import native_runtime_from_environment
        native_runtime = native_runtime_from_environment(runtime)
    if native_runtime is not None and native_runtime.identity != runtime.identity:
        raise Problem(403, "NATIVE_STATE_SCOPE", "native turns must use this Hosted process identity")
    responses_flag = os.environ.get("FA_RESPONSES_ENABLED", "")
    if responses_flag not in {"", "0", "1"}:
        raise Problem(422, "RESPONSES_FLAG_INVALID", "FA_RESPONSES_ENABLED must be 0 or 1")
    responses_enabled = responses_flag == "1"
    if not responses_enabled and (
        os.environ.get("FA_N6_RESPONSES_PROBE") or os.environ.get("FA_N6_RESPONSES_INTERRUPT_SESSION_ID")
    ):
        raise Problem(422, "N6_PROBE_DISABLED", "the test probe also requires explicit Responses enablement")
    probe = NativeStateProbe.from_environment(runtime.identity)
    if probe is not None or native_runtime is not None:
        from azure.ai.agentserver.core.tasks import set_resilient_tasks_enabled

        set_resilient_tasks_enabled(True)
    responses_probe = None
    if responses_enabled:
        from azure.ai.agentserver.responses import ResponsesAgentServerHost, ResponsesServerOptions
        from .responses_probe import N6ResponsesProbe, ProbeRequestFieldsMiddleware, ProbeSessionOnlyMiddleware

        class FaHost(InvocationAgentServerHost, ResponsesAgentServerHost):
            pass

        responses_probe = N6ResponsesProbe.from_environment(runtime.identity)
        if responses_probe is not None and responses_handler_factory is not None:
            raise Problem(422, "RESPONSES_HANDLER_CONFLICT", "test and production handlers cannot share this lane")
        if responses_probe is None and responses_handler_factory is None:
            raise Problem(503, "RESPONSES_HANDLER_UNAVAILABLE", "the production Responses handler is not installed")
        if responses_probe is not None and responses_probe.interrupt_session and native_runtime is not None:
            raise Problem(403, "N6_PROBE_SESSION_BUSY", "the interrupt session cannot also run native turns")
        try:
            owned_root = runtime.supervisor.journal.root
            if not owned_root.is_absolute():
                raise ValueError()
        except (AttributeError, ValueError):
            raise Problem(503, "RESPONSES_STATE_ROOT_UNAVAILABLE", "the original Hosted private root is required") from None
        state_root = owned_root / "agentserver"
        guard(owned_root, state_root)
        state_root.mkdir(mode=0o700, exist_ok=True)
        # The Responses constructor configures file-backed replay before the SDK lifespan starts.
        os.environ["AGENTSERVER_STATE_ROOT"] = str(state_root)
        app = FaHost(
            # Official SSE keep-alive comments keep a replayed preview open while the model reasons (RTT B).
            options=ResponsesServerOptions(resilient_background=True, steerable_conversations=False,
                                           sse_keep_alive_interval_seconds=15),
            configure_observability=None, access_log=None,
        )
        handler = (responses_probe.handler if responses_probe is not None
                   else responses_handler_factory(runtime, native_runtime))
        app.response_handler(handler)
        if responses_probe is not None:
            app.add_middleware(ProbeRequestFieldsMiddleware)
        if responses_probe is not None and responses_probe.interrupt_session:
            app.add_middleware(ProbeSessionOnlyMiddleware)
    else:
        app = InvocationAgentServerHost(configure_observability=None, access_log=None)
    if native_runtime is not None:
        from contextlib import asynccontextmanager

        sdk_lifespan = app.router.lifespan_context

        @asynccontextmanager
        async def native_lifespan(application):
            await native_runtime.initialize()
            try:
                async with sdk_lifespan(application):
                    await native_runtime.recover_owned()
                    yield
            finally:
                # Background open-RID removals (``NativeTurnRuntime._cas``) get a short bound; any cut stays stale.
                close_runtime = getattr(native_runtime, "aclose", None)

                async def drain_removals():
                    if close_runtime is not None:
                        try:
                            await close_runtime()
                        except Exception:
                            logging.getLogger(__name__).warning("NATIVE_RUNTIME_SHUTDOWN_UNCONFIRMED")

                await drain_removals()
                # ADR-0005: kept SDKs stop with the VM, best effort; their checkpoints are already published.
                close = getattr(getattr(native_runtime, "executor", None), "aclose", None)
                if close is not None:
                    try:
                        await close()
                    except Exception:
                        logging.getLogger(__name__).warning("RESIDENT_SDK_SHUTDOWN_UNCONFIRMED")
                    # A turn the executor stopped writes its terminal receipt and starts a removal; drain once more.
                    await drain_removals()

        app.router.lifespan_context = native_lifespan

    async def bounded_body(request):
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > 4096:
                raise Problem(413, "HOSTED_INVOCATION_LIMIT", "native launch body exceeds its bound")
            raw.extend(chunk)
        try:
            def unique(pairs):
                result = dict(pairs)
                if len(result) != len(pairs):
                    raise ValueError("duplicate fields")
                return result

            json.loads(raw, object_pairs_hook=unique)
            body = HostedInvocationBody.model_validate_json(raw)
            values = request.headers.getlist(wire.NATIVE_LAUNCH_PERMIT_HEADER)
            if len(values) != 1 or re.fullmatch(r"[a-f0-9]{64}", values[0]) is None:
                raise Problem(403, "HOSTED_CAPABILITY_INVALID", "one private native launch capability is required")
            return HostedInvocation(**body.model_dump(), launch_permit=SecretStr(values[0]))
        except (ValueError, TypeError):
            raise Problem(422, "HOSTED_INVOCATION_INVALID", "only a typed native launch selector/permit is accepted") from None

    async def respond(request, action):
        try:
            if action == "start":
                payload = await bounded_body(request)
                result = await asyncio.to_thread(
                    runtime.start, payload, request.state.invocation_id, request.state.session_id,
                )
                return JSONResponse(result, status_code=202)
            keepalive = request.headers.getlist(wire.NATIVE_KEEPALIVE_HEADER)
            if keepalive:
                if (action != "observe" or len(keepalive) != 1 or request.headers.getlist(wire.NATIVE_PROBE_HEADER)
                        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", keepalive[0]) is None):
                    raise Problem(403, "HOSTED_KEEPALIVE_INVALID", "only the original read-only invocation may be kept alive")
                async for chunk in request.stream():
                    if chunk:
                        raise Problem(403, "HOSTED_KEEPALIVE_INVALID", "keepalive accepts no body")
                result = await asyncio.to_thread(
                    runtime.keepalive, request.state.invocation_id, request.state.session_id, keepalive[0])
                return JSONResponse(result.model_dump(mode="json"))
            probes = request.headers.getlist(wire.NATIVE_PROBE_HEADER)
            if len(probes) != 1 or re.fullmatch(r"[a-f0-9]{64}", probes[0]) is None:
                raise Problem(403, "HOSTED_PROBE_INVALID", "a fresh controller probe is required")
            probe_id = probes[0]
            result = await asyncio.to_thread(
                runtime.observe, request.state.invocation_id, request.state.session_id, probe_id,
                cancellation=action == "cancel",
            )
            return JSONResponse(result.model_dump(mode="json"))
        except Problem as exc:
            return JSONResponse(exc.body, status_code=exc.status)
        except (OSError, sqlite3.Error, ValueError):
            return JSONResponse({"error": {"code": "HOSTED_EXECUTOR_UNKNOWN", "message": "executor outcome remains unknown"}}, status_code=503)

    @app.invoke_handler
    async def invoke(request):
        if request.headers.getlist("x-client-fa-n6-probe"):
            from .responses_probe import probe_response as responses_probe_response
            return await responses_probe_response(request, responses_probe)
        if responses_probe is not None and responses_probe.interrupt_session:
            return JSONResponse({"error": {"code": "N6_PROBE_SESSION_ONLY"}}, status_code=403)
        native_headers = request.headers.getlist(NATIVE_TURN_HEADER)
        if native_runtime is not None or native_headers:
            from .behavior_http_diagnostics import native_correlation_id
            logging.getLogger(__name__).warning("FA_HOSTED_DISPATCH %s", json.dumps({
                "lane": "probe" if request.headers.getlist(PROBE_HEADER) else "native" if native_headers else "legacy",
                "native_header_count": len(native_headers),
                "agent_id": runtime.identity.agent_id, "session_id": runtime.identity.session_id,
                "invocation_id": native_correlation_id(getattr(request.state, "invocation_id", None)),
            }, sort_keys=True))
        if request.headers.getlist(PROBE_HEADER):
            return await probe_response(request, probe)
        if request.headers.getlist(NATIVE_TURN_HEADER):
            from .native_turns import native_response
            return await native_response(request, native_runtime)
        return await respond(request, "start")

    @app.get_invocation_handler
    async def observe(request):
        if responses_probe is not None and responses_probe.interrupt_session:
            return JSONResponse({"error": {"code": "N6_PROBE_SESSION_ONLY"}}, status_code=403)
        if request.headers.getlist(NATIVE_TURN_HEADER):
            from .native_turns import native_response
            return await native_response(request, native_runtime, action="observe")
        return await respond(request, "observe")

    @app.cancel_invocation_handler
    async def cancel(request):
        if responses_probe is not None and responses_probe.interrupt_session:
            return JSONResponse({"error": {"code": "N6_PROBE_SESSION_ONLY"}}, status_code=403)
        if request.headers.getlist(NATIVE_TURN_HEADER):
            from .native_turns import native_response
            return await native_response(request, native_runtime, action="cancel")
        return await respond(request, "cancel")

    return app


def main():
    parser = argparse.ArgumentParser(description="Constrained behavior Hosted runtime")
    subparsers = parser.add_subparsers(dest="mode", required=True)
    worker = subparsers.add_parser("_worker")
    worker.add_argument("--root", type=Path, required=True)
    worker.add_argument("--launch-id", required=True)
    worker.add_argument("--nonce", required=True)
    subparsers.add_parser("serve")
    args = parser.parse_args()
    if args.mode == "_worker":
        try:
            run_checksum_child(args.root, args.launch_id, args.nonce)
        except (Problem, OSError, sqlite3.Error, ValueError):
            raise SystemExit(1) from None
    elif args.mode == "serve":
        try:
            create_host(HostedRuntime.from_environment()).run()
        except Problem as exc:
            raise SystemExit(exc.body["error"]["code"]) from None


if __name__ == "__main__":
    main()
