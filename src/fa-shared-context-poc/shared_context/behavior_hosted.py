"""Hosted entry runtime and SDK host: native turns, the state probe and the Responses lane.

The legacy checksum executor and its claim/probe/report lane are retired: an invocation
without the native header is refused with 410 JOB_LANE_RETIRED. ``HostedRuntime`` keeps
only the trusted identity, the private root and the behavior API address the native lane uses.
"""

from __future__ import annotations

if __package__ in (None, ""):
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = "shared_context"

import argparse
import json
import logging
import os
import sqlite3
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import Field, model_validator

from . import behavior_wire as wire
from .behavior_wire import Digest, Identifier, WireModel as _Model
from .errors import Problem
from .config import validate_api_audience
from .private_files import guard


def _check(value, model):
    try:
        if not isinstance(value, model):
            raise ValueError("typed control data required")
        return model.model_validate(value.model_dump())
    except (ValueError, TypeError):
        raise Problem(422, "HOSTED_CONTRACT_INVALID", "invalid bounded executor contract") from None


ChecksumResult = wire.ChecksumResult


class LocalAssignment(wire.HostedAssignment):
    """Private child DTO, constructed ONLY from a verified API assignment."""

    @model_validator(mode="after")
    def checkpoint_boundary(self):
        if self.checkpoint_after_bytes is not None and self.checkpoint_after_bytes > len(self.input):
            raise ValueError("checkpoint boundary cannot exceed the assigned input")
        return self


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

class ChecksumSupervisor:
    """Owns the Hosted private root's local journal; the native lane keeps its state under that root."""

    def __init__(self, root: Path):
        self.journal = LocalJournal(root)


class BehaviorExecutorClient:
    """The exact HTTPS behavior API address and MI audience the native lane calls."""

    def __init__(self, api_url: str, audience: str, credential=None):
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


class HostedRuntime:
    def __init__(self, identity, supervisor: ChecksumSupervisor, api: BehaviorExecutorClient):
        self.identity = _check(identity, wire.HostedIdentity)
        self.supervisor, self.api = supervisor, api

    @property
    def root(self):
        return self.supervisor.journal.root

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

def _native_runtime_for(runtime, native_runtime):
    if native_runtime is None and os.environ.get("FA_NATIVE_TURNS"):
        from .native_sdk import native_runtime_from_environment
        native_runtime = native_runtime_from_environment(runtime)
    if native_runtime is not None and native_runtime.identity != runtime.identity:
        raise Problem(403, "NATIVE_STATE_SCOPE", "native turns must use this Hosted process identity")
    return native_runtime


def _responses_enabled():
    responses_flag = os.environ.get("FA_RESPONSES_ENABLED", "")
    if responses_flag not in {"", "0", "1"}:
        raise Problem(422, "RESPONSES_FLAG_INVALID", "FA_RESPONSES_ENABLED must be 0 or 1")
    responses_enabled = responses_flag == "1"
    if not responses_enabled and (
        os.environ.get("FA_N6_RESPONSES_PROBE") or os.environ.get("FA_N6_RESPONSES_INTERRUPT_SESSION_ID")
    ):
        raise Problem(422, "N6_PROBE_DISABLED", "the test probe also requires explicit Responses enablement")
    return responses_enabled


def _responses_state_root(runtime):
    try:
        owned_root = runtime.root
        if not owned_root.is_absolute():
            raise ValueError()
    except (AttributeError, ValueError):
        raise Problem(503, "RESPONSES_STATE_ROOT_UNAVAILABLE", "the original Hosted private root is required") from None
    state_root = owned_root / "agentserver"
    guard(owned_root, state_root)
    state_root.mkdir(mode=0o700, exist_ok=True)
    return state_root


def _check_responses_lane(responses_probe, responses_handler_factory, native_runtime):
    if responses_probe is not None and responses_handler_factory is not None:
        raise Problem(422, "RESPONSES_HANDLER_CONFLICT", "test and production handlers cannot share this lane")
    if responses_probe is None and responses_handler_factory is None:
        raise Problem(503, "RESPONSES_HANDLER_UNAVAILABLE", "the production Responses handler is not installed")
    if responses_probe is not None and responses_probe.interrupt_session and native_runtime is not None:
        raise Problem(403, "N6_PROBE_SESSION_BUSY", "the interrupt session cannot also run native turns")


def _responses_host(runtime, native_runtime, responses_handler_factory):
    from azure.ai.agentserver.invocations import InvocationAgentServerHost
    from azure.ai.agentserver.responses import ResponsesAgentServerHost, ResponsesServerOptions
    from .responses_probe import N6ResponsesProbe, ProbeRequestFieldsMiddleware, ProbeSessionOnlyMiddleware

    class FaHost(InvocationAgentServerHost, ResponsesAgentServerHost):
        pass

    responses_probe = N6ResponsesProbe.from_environment(runtime.identity)
    _check_responses_lane(responses_probe, responses_handler_factory, native_runtime)
    state_root = _responses_state_root(runtime)
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
    return app, responses_probe


def _native_lifespan(native_runtime, sdk_lifespan):
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

    return native_lifespan


def _retired():
    from starlette.responses import JSONResponse
    problem = Problem(410, "JOB_LANE_RETIRED", "the legacy job lane is retired")
    return JSONResponse(problem.body, status_code=problem.status)


def _log_dispatch(runtime, request, native_headers, state_probes):
    from .behavior_http_diagnostics import native_correlation_id
    logging.getLogger(__name__).warning("FA_HOSTED_DISPATCH %s", json.dumps({
        "lane": "probe" if state_probes else "native" if native_headers else "legacy",
        "native_header_count": len(native_headers),
        "agent_id": runtime.identity.agent_id, "session_id": runtime.identity.session_id,
        "invocation_id": native_correlation_id(getattr(request.state, "invocation_id", None)),
    }, sort_keys=True))


def _install_invocation_handlers(app, runtime, native_runtime, probe, responses_probe):
    from starlette.responses import JSONResponse
    from .behavior_foundry_probe import PROBE_HEADER, probe_response
    from .behavior_native_turn_wire import NATIVE_TURN_HEADER

    @app.invoke_handler
    async def invoke(request):
        if request.headers.getlist("x-client-fa-n6-probe"):
            from .responses_probe import probe_response as responses_probe_response
            return await responses_probe_response(request, responses_probe)
        if responses_probe is not None and responses_probe.interrupt_session:
            return JSONResponse({"error": {"code": "N6_PROBE_SESSION_ONLY"}}, status_code=403)
        native_headers = request.headers.getlist(NATIVE_TURN_HEADER)
        state_probes = request.headers.getlist(PROBE_HEADER)
        if native_runtime is not None or native_headers:
            _log_dispatch(runtime, request, native_headers, state_probes)
        if state_probes:
            return await probe_response(request, probe)
        if native_headers:
            from .native_turns import native_response
            return await native_response(request, native_runtime)
        return _retired()

    async def existing_invocation(request, action):
        if responses_probe is not None and responses_probe.interrupt_session:
            return JSONResponse({"error": {"code": "N6_PROBE_SESSION_ONLY"}}, status_code=403)
        if request.headers.getlist(NATIVE_TURN_HEADER):
            from .native_turns import native_response
            return await native_response(request, native_runtime, action=action)
        return _retired()

    @app.get_invocation_handler
    async def observe(request):
        return await existing_invocation(request, "observe")

    @app.cancel_invocation_handler
    async def cancel(request):
        return await existing_invocation(request, "cancel")


def create_host(runtime: HostedRuntime, *, native_runtime=None, responses_handler_factory=None):
    """Build the real SDK host; no legacy Hosted/M2/M3 registration is altered."""
    from azure.ai.agentserver.invocations import InvocationAgentServerHost
    from . import behavior_native_turn_wire  # a broken import still fails before any startup write
    from .behavior_foundry_probe import NativeStateProbe

    native_runtime = _native_runtime_for(runtime, native_runtime)
    responses_enabled = _responses_enabled()
    probe = NativeStateProbe.from_environment(runtime.identity)
    if probe is not None or native_runtime is not None:
        from azure.ai.agentserver.core.tasks import set_resilient_tasks_enabled

        set_resilient_tasks_enabled(True)
    responses_probe = None
    if responses_enabled:
        app, responses_probe = _responses_host(runtime, native_runtime, responses_handler_factory)
    else:
        app = InvocationAgentServerHost(configure_observability=None, access_log=None)
    if native_runtime is not None:
        app.router.lifespan_context = _native_lifespan(native_runtime, app.router.lifespan_context)
    _install_invocation_handlers(app, runtime, native_runtime, probe, responses_probe)
    return app


def main():
    parser = argparse.ArgumentParser(description="Constrained behavior Hosted runtime")
    subparsers = parser.add_subparsers(dest="mode", required=True)
    subparsers.add_parser("serve")
    args = parser.parse_args()
    if args.mode == "serve":
        try:
            create_host(HostedRuntime.from_environment()).run()
        except Problem as exc:
            raise SystemExit(exc.body["error"]["code"]) from None


if __name__ == "__main__":
    main()
