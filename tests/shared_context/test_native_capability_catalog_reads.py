"""The native capability turn gate reads only the legacy-CID row (the target selection reader was retired, R6-H).

Offline counts (FA_TIMING ``table_gets``/``batches``); the same refusals in the same order.
"""

import json
import logging

import pytest

from shared_context import behavior_capability_calls, mcp_client, native_agent_api
from shared_context.behavior_instruction import conversation_key
from shared_context.behavior_timing import phase_timing
from shared_context.errors import Problem
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


def test_catalog_gate_reads_one_guarded_legacy_row_snapshot(listing, caplog):
    case, native = listing, gateway(listing)
    scope = started(case, (WEB, SKILLS))
    result, counts = measured(caplog, lambda: native.capability_catalog(case.principal, scope))
    # Was (6, 5) with the selection key batched in, (10, 10) unbatched.
    assert counts == (5, 5)
    assert [item.entry_id for item in result.entries] == ["WebIQ-MCP"]


def test_catalog_gate_keeps_its_refusals_and_their_order(listing, monkeypatch):
    case, native = listing, gateway(listing)
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
