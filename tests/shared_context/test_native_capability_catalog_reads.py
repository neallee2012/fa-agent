"""The native capability turn gate reads the target selection and the legacy-CID row in one guarded snapshot.

Offline counts (FA_TIMING ``table_gets``/``batches``); the same results and the same refusals in the same order.
"""

import json
import logging

import pytest

from shared_context import behavior_capability_calls, mcp_client, native_agent_api
from shared_context.behavior_instruction import conversation_key
from shared_context.behavior_timing import phase_timing
from shared_context.errors import Problem
from shared_context.native_capability_api import NativeCapabilityGateway
from test_agent_runtime import peer_runtime  # noqa: F401
from test_behavior_store import changes, request, write
from test_capability_calls import KEY, listed
from test_conversation_transfer_startup import synthetic_standing_v1  # noqa: F401
from test_instruction_flow import instruction_models  # noqa: F401
from test_m3_auth import jwk_client, signing_key  # noqa: F401
from test_native_agent_api import relay_case  # noqa: F401
from test_native_capability_api import SKILLS, WEB, code, gateway, skilled, started  # noqa: F401


@pytest.fixture
def listing(skilled, monkeypatch):
    async def fake_list(entry, lookup, **_):
        return listed()

    def run_without_loop(async_call):
        # The startup fixture forbids sockets, and anyio.run opens a socket pair; the fake never awaits.
        coroutine = async_call()
        try:
            coroutine.send(None)
        except StopIteration as done:
            return done.value
        raise AssertionError("the offline MCP fake must not await")
    monkeypatch.setattr(behavior_capability_calls, "_run", run_without_loop)
    monkeypatch.setenv("FA_CAPABILITY_SECRET_WEBIQ_API_KEY", KEY)
    monkeypatch.setattr(mcp_client, "list_tools", fake_list)
    return skilled[0]


def measured(caplog, call):
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="shared_context.behavior_timing"):
        with phase_timing("catalog_probe"):
            result = call()
    timing, = (json.loads(r.getMessage()[len("FA_TIMING "):]) for r in caplog.records
               if r.getMessage().startswith('FA_TIMING {"phase":"catalog_probe"'))
    return result, (timing["table_gets"], timing["batches"])


def test_catalog_gate_reads_both_rows_in_one_snapshot_with_the_same_result(listing, monkeypatch, caplog):
    case, native = listing, gateway(listing)
    scope = started(case, (WEB, SKILLS))
    batched, batched_counts = measured(caplog, lambda: native.capability_catalog(case.principal, scope))
    monkeypatch.setattr(NativeCapabilityGateway, "_rows", lambda self, scope, cid: None)
    separate, separate_counts = measured(caplog, lambda: native.capability_catalog(case.principal, scope))
    assert batched == separate and [item.entry_id for item in batched.entries] == ["WebIQ-MCP"]
    # Separate: two guard/installation-bracketed single-row snapshots. Batched: one bracket, both rows together.
    assert separate_counts == (10, 10)
    assert batched_counts == (6, 5)


@pytest.mark.parametrize("case_name", ["no_conversation", "other_target"])
def test_rows_reads_nothing_without_a_conversation_or_for_another_target(listing, caplog, case_name):
    case, native = listing, gateway(listing)
    scope, cid = native.relay._sender_scope(case.principal), case.sender.conversation_id
    if case_name == "no_conversation":
        cid = None
    else:
        scope = scope.model_copy(update={"target": scope.target.model_copy(update={"agent_id": "registered-peer"})})
    rows, counts = measured(caplog, lambda: native._rows(scope, cid))
    assert rows is None and counts == (0, 0)


@pytest.mark.parametrize("batched", [True, False])
def test_catalog_gate_keeps_its_refusals_and_their_order(listing, monkeypatch, batched):
    case, native = listing, gateway(listing)
    if not batched:
        monkeypatch.setattr(NativeCapabilityGateway, "_rows", lambda self, scope, cid: None)
    scope = started(case, (WEB, SKILLS))
    case.base.a.runtime.work_policy.store.transact(
        request("legacy-table-conversation"),
        lambda _: changes(write(conversation_key(scope.conversation_id), {"kind": "legacy"})))
    with pytest.raises(Problem) as legacy:
        native.capability_catalog(case.principal, scope)
    assert (legacy.value.status, code(legacy)) == (409, "NATIVE_STATE_SCOPE")
    # Another selected target still refuses first (403), before the legacy CID (409).
    real = native_agent_api.selected_targets

    def moved(*args, **kwargs):
        target, = real(*args, **kwargs)
        return (target.model_copy(update={"agent_oid": "00000000-0000-0000-0000-000000000000"}),)
    monkeypatch.setattr(native_agent_api, "selected_targets", moved)
    with pytest.raises(Problem) as target:
        native.capability_catalog(case.principal, scope)
    assert (target.value.status, code(target)) == (403, "NATIVE_STATE_SCOPE")
