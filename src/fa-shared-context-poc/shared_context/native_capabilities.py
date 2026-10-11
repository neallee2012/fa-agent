"""Captured native capabilities share the original turn, not a second executor."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Protocol
from pydantic import BaseModel

from .behavior_agent_message_wire import CAPABILITY_ID as PEER_CAPABILITY
from .behavior_capabilities import CapabilityGrant
from .behavior_capability_prompt import session_needs
from .behavior_model_direct import DirectResponses
from .behavior_native_turn_wire import NativeReceipt, NativeTurnCapture
from .behavior_sdk_state import NativeCheckpointScope
from .behavior_timing import phase_timing
from .behavior_wire import HostedIdentity, WireModel
from .errors import Problem
from .m2_copilot import ToolFailure, ToolSpec
from .native_effects import NativeEffects
from .native_turns import NativeTurnControl, NativeTurnRuntime, timing_id
from .native_assembly import NativeAssemblyCollector

if TYPE_CHECKING:
    from .native_hitl_runtime import NativeHitlRuntime

TokenProvider = Callable[[], str | Awaitable[str]]
_log = logging.getLogger(__name__)


class NativeDelegation(Protocol):
    """The existing runner's SDK delegation slot; the consumer owns its implementation."""

    sdk_session_id: str

    def observe(self, event: dict) -> None: ...
    async def pre_tool(self, data: dict, context: dict) -> dict | None: ...
    async def post_tool(self, data: dict, context: dict) -> dict | None: ...
    async def model_ref(self, context: object) -> WireModel | None: ...
    async def require_complete(self) -> None: ...
    async def request_cancel(self) -> None: ...
    async def finish(self) -> None: ...


class NativePrivateMemory(Protocol):
    async def recall(self, receipt: NativeReceipt, *, first_turn: bool = False) -> tuple[str, ...]: ...
    async def completed(self, receipt: NativeReceipt) -> None: ...


@dataclass
class NativeCapabilityContext:
    receipt: NativeReceipt
    scope: NativeCheckpointScope
    control: NativeTurnControl
    root: Path
    turn_root: Path
    runtime: NativeTurnRuntime | None
    model: DirectResponses
    api_url: str
    api_token_provider: TokenProvider | None
    model_token_provider: TokenProvider
    latest_image: str | None = None
    hitl: NativeHitlRuntime | None = None
    assembly: NativeAssemblyCollector | None = None
    bound_tools: frozenset[str] = frozenset()

    @property
    def identity(self) -> HostedIdentity:
        if self.runtime is None:
            raise Problem(503, "NATIVE_EXECUTION_UNAVAILABLE", "capabilities require the original native runtime")
        return self.runtime.identity

    @property
    def effects(self) -> NativeEffects:
        if self.runtime is None:
            raise Problem(503, "NATIVE_EXECUTION_UNAVAILABLE", "effects require the original native runtime")
        return NativeEffects(self.runtime, self.receipt, self.control)

    async def current(self) -> bool:
        # N1's watcher fences this exact turn; definition changes are checked at entry, not per tool.
        return not self.control.stop_requested()


@dataclass(frozen=True)
class NativeCapabilityBinding:
    tools: tuple[ToolSpec, ...] = ()
    result_schemas: dict[str, type[BaseModel]] = field(default_factory=dict)
    system_addendum: str = ""
    mcp_temp_directory: Path | None = None
    # The turn's own desktop X display for the Playwright MCP (m2_copilot.show_on_desktop); VM-side only.
    mcp_display_env: dict[str, str] | None = None
    delegation_runtime: NativeDelegation | None = None
    on_event: Callable[[dict], None] | None = None
    on_tool_image: Callable[[str], None] | None = None
    close: Callable[[], Awaitable[None]] | None = None


@dataclass(frozen=True)
class NativeCapabilityProvider:
    """Only matched captured grants may call bind; a declaration never enables an unbound tool."""

    accepts: Callable[[CapabilityGrant], bool]
    bind: Callable[
        [NativeCapabilityContext, tuple[CapabilityGrant, ...]], Awaitable[NativeCapabilityBinding]
    ]
    approved_skills: bool = False
    on_terminal: Callable[[NativeReceipt, NativeTurnRuntime], Awaitable[None]] | None = None
    # A fixed consumer name for its FA_TIMING phase ``native_bind_<name>``; never a grant or tool value.
    name: str = "other"
    # Prewarm (README#typing-prewarm): fill this process's kept lists ahead of a turn, outside any turn; never raises.
    warm: Callable[[TokenProvider, tuple], Awaitable[None]] | None = None


def guard_native_tool(context, spec):
    async def handle(packet):
        try:
            if (not await context.current() or packet.get("session_id") != context.scope.sdk_session_id
                    or not packet.get("tool_call_id")):
                raise ToolFailure("The original native turn is no longer active; do not repeat an effect.")
            if context.hitl is not None:
                return await context.hitl.invoke(packet, spec.handler)
            return await spec.handler(packet)
        except ToolFailure as error:
            if context.assembly is not None and spec.name == "read_skill":
                context.assembly.skill_failed(packet.get("arguments"), error.public_code)
            raise
    return replace(spec, handler=handle)


def selected_providers(capture: NativeTurnCapture, providers: tuple[NativeCapabilityProvider, ...], *,
                       agent_messages: bool = False):
    selected: list[list[CapabilityGrant]] = [[] for _ in providers]
    for grant in () if capture.tools is None else capture.tools.grants:
        if agent_messages and grant.entry_id == PEER_CAPABILITY:
            continue
        matches = [index for index, provider in enumerate(providers) if provider.accepts(grant)]
        if len(matches) != 1:
            raise Problem(409, "NATIVE_PROFILE_UNSUPPORTED",
                          "this captured capability has no single implemented native consumer",
                          capability=grant.entry_id)
        selected[matches[0]].append(grant)
    chosen = tuple((provider, tuple(grants)) for provider, grants in zip(providers, selected) if grants)
    if capture.template.skills and not any(
            provider.approved_skills and any("read_skill" in grant.tools for grant in grants)
            for provider, grants in chosen):
        raise Problem(409, "NATIVE_PROFILE_UNSUPPORTED",
                      "the selected approved skill versions have no native controlled-read consumer")
    return chosen


async def bind_capabilities(context: NativeCapabilityContext, providers, stack: AsyncExitStack, *,
                            agent_messages=False) -> tuple[NativeCapabilityBinding, ...]:
    context.bound_tools = frozenset()
    bindings = []
    timed = timing_id(context.receipt.request_id)
    chosen = selected_providers(context.receipt.capture, providers, agent_messages=agent_messages)
    if chosen and not await context.current():
        raise Problem(409, "NATIVE_TURN_UNKNOWN", "the original turn stopped before capability setup")

    async def bind_one(provider, grants):
        try:
            with phase_timing("native_bind_" + provider.name, job_id=timed):
                return await provider.bind(context, grants)
        except Exception as error:
            # Which consumer failed, by catalog entry ID only; never arguments, tokens or responses.
            code = error.body["error"]["code"] if isinstance(error, Problem) else "UNCLASSIFIED"
            _log.warning("FA_NATIVE_CAPABILITY_BIND_FAILED entries=%s code=%s exception_type=%s",
                         ",".join(grant.entry_id for grant in grants), code, type(error).__name__)
            raise

    # Each consumer reads only its own grants and this turn, so they bind together and a turn waits for the slowest
    # one, not the sum. Results are taken in consumer order: the first failure in that order ends the bind as soon as
    # every consumer before it has finished, as when they bound one after another.
    tasks = [asyncio.ensure_future(bind_one(provider, grants)) for provider, grants in chosen]
    try:
        for task in tasks:
            await asyncio.wait((task,))
            if task.exception() is not None:
                break
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        registered = set()

        def close_made():
            # Every binding made is closed with the turn, also when another consumer failed or the turn was cancelled.
            for task in tasks:
                if task not in registered and task.done() and not task.cancelled() and task.exception() is None:
                    registered.add(task)
                    made = task.result()
                    if isinstance(made, NativeCapabilityBinding) and made.close is not None:
                        stack.push_async_callback(made.close)
        close_made()
        try:
            # Like the memory recall: nothing started here outlives the bind, and every failure is retrieved.
            await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            close_made()
    for (provider, grants), task in zip(chosen, tasks):
        if task.exception() is not None:
            raise task.exception()
        binding = task.result()
        if not isinstance(binding, NativeCapabilityBinding):
            raise Problem(503, "NATIVE_EXECUTION_UNAVAILABLE", "a native capability consumer returned no binding")
        expected = {name for grant in grants if grant.kind != "mcp-hosted"
                    and grant.entry_id not in {"fa-delegate-v1", "fa-delegate-reader-v1"} for name in grant.tools}
        names = [tool.name for tool in binding.tools]
        delegation = any(grant.entry_id == "fa-delegate-v1" for grant in grants)
        if (len(names) != len(set(names)) or set(names) != expected
                or set(binding.result_schemas) != expected
                or delegation != (binding.delegation_runtime is not None)):
            raise Problem(503, "NATIVE_EXECUTION_UNAVAILABLE",
                          "the native consumer's tools and result schemas differ from its captured grants")
        bindings.append(replace(binding, tools=tuple(guard_native_tool(context, spec) for spec in binding.tools)))
    # Validate the same SDK needs the runner will use, including duplicate names and the fixed zero-tool child.
    needs = session_needs(context.receipt.capture.tools)
    context.bound_tools = frozenset(
        tool.name for binding in bindings for tool in binding.tools
    ) | frozenset(() if needs is None else needs.builtin_tools)
    return tuple(bindings)
