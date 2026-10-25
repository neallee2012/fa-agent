"""FA_TIMING carries content-free Table read counts: physical GETs and the sequential rounds they took."""
import asyncio
import json
import logging

import anyio
from starlette.concurrency import run_in_threadpool

from shared_context.behavior_store import BehaviorStore
from shared_context.behavior_timing import phase_timing
from test_behavior_store import FakeTable, SCOPE, changes, request, write


def _store():
    adapter = BehaviorStore(FakeTable(), SCOPE)
    adapter.initialize()
    adapter.transact(request("seed-counts"), lambda _: changes(*(write(f"secret-key-{n}", {"n": n}) for n in range(5))))
    return adapter


def _timings(caplog):
    return [json.loads(record.getMessage()[len("FA_TIMING "):]) for record in caplog.records
            if record.getMessage().startswith("FA_TIMING ")]


def test_parallel_batch_counts_one_round_and_every_get_without_keys(caplog):
    adapter = _store()
    keys = tuple(f"secret-key-{n}" for n in range(5))
    with caplog.at_level(logging.WARNING):
        with phase_timing("probe_batch", job_id="job-counts"):
            adapter.read_snapshot(keys)
        with phase_timing("probe_single"):
            adapter.read_snapshot(keys[:1])
    batch, single = _timings(caplog)
    # Guard, five rows together, closing guard.
    assert (batch["table_gets"], batch["batches"]) == (7, 3)
    assert (single["table_gets"], single["batches"]) == (3, 3)
    assert "secret-key" not in caplog.text


def test_counts_follow_the_phase_into_worker_threads_and_nested_phases(caplog):
    adapter = _store()

    async def run():
        with phase_timing("outer"):
            await asyncio.to_thread(adapter.read_snapshot, ("secret-key-0",))
            with phase_timing("inner"):
                await run_in_threadpool(adapter.read_snapshot, ("secret-key-1", "secret-key-2"))
            await anyio.to_thread.run_sync(adapter.read_snapshot, ("secret-key-3",))

    with caplog.at_level(logging.WARNING):
        asyncio.run(run())
    inner, outer = _timings(caplog)
    assert (inner["phase"], inner["table_gets"], inner["batches"]) == ("inner", 4, 3)
    assert (outer["phase"], outer["table_gets"], outer["batches"]) == ("outer", 10, 9)


def test_phase_without_table_reads_reports_zero(caplog):
    with caplog.at_level(logging.WARNING):
        with phase_timing("hosted_only"):
            pass
    timing, = _timings(caplog)
    assert (timing["table_gets"], timing["batches"], timing["outcome"]) == (0, 0, "returned")


def test_route_timing_logs_template_binding_and_counts_only(caplog):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from shared_context.behavior_timing import RouteTiming
    adapter = _store()
    app = FastAPI()
    app.add_middleware(RouteTiming)

    @app.post("/behavior/v1/bindings/{binding_id}/assistant/sessions/{session_id}/reserve-model")
    def reserve(binding_id: str, session_id: str):
        adapter.read_snapshot(("secret-key-0", "secret-key-1"))
        return {"ok": True}

    @app.get("/work-policy/v1/conversations/{conversation_id}")
    def conversation(conversation_id: str):
        adapter.read_snapshot(("secret-key-2",))
        return {"ok": True}

    with caplog.at_level(logging.WARNING), TestClient(app) as client:
        assert client.post("/behavior/v1/bindings/binding-one/assistant/sessions/secret-session/reserve-model").json()
        assert client.get("/work-policy/v1/conversations/secret-conversation").json()
    timing, = _timings(caplog)
    assert timing["phase"] == "api_reserve-model" and timing["job_id"] == "binding-one"
    assert (timing["table_gets"], timing["batches"], timing["outcome"]) == (4, 3, "returned")
    assert "secret" not in caplog.text


def test_route_timing_names_native_turn_routes_by_their_fixed_segment_and_hashes_the_rid(caplog):
    """v17: the owner submit, reads, skill catalog and usage report had no api_* timing at all."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from shared_context.behavior_timing import RouteTiming, timing_id
    app = FastAPI()
    app.add_middleware(RouteTiming)
    turn = "/agents/{agent_id}/work-policy/v1/native-turns/conversations/{conversation_id}/requests/{request_id}"

    @app.get(turn + "/status")
    def status(agent_id: str, conversation_id: str, request_id: str):
        return {"ok": True}

    @app.get("/work-policy/v1/native-turns/requests/{request_id}")
    def receipt(request_id: str):
        return {"ok": True}

    @app.post("/work-policy/v1/responses-turns/submit")
    def submit():
        return {"ok": True}

    @app.post("/work-policy/v1/native-capabilities/skill-catalog")
    def skill_catalog():
        return {"ok": True}

    @app.post("/agents/{agent_id}/work-policy/v1/turn-usage")
    def turn_usage(agent_id: str):
        return {"ok": True}

    @app.get("/work-policy/v1/agent-template/options")
    def options():
        return {"ok": True}

    @app.post("/agents/{agent_id}/team-data/v1/memory/search")
    def memory_search(agent_id: str):
        return {"ok": True}

    @app.get("/work-policy/v1/conversations/{conversation_id}")
    def legacy(conversation_id: str):
        return {"ok": True}

    with caplog.at_level(logging.WARNING), TestClient(app) as client:
        assert client.get("/agents/secret-agent/work-policy/v1/native-turns/conversations/secret-cid"
                          "/requests/secret-rid/status").json()
        assert client.get("/work-policy/v1/native-turns/requests/secret-rid").json()
        assert client.post("/work-policy/v1/responses-turns/submit").json()
        assert client.post("/work-policy/v1/native-capabilities/skill-catalog").json()
        assert client.post("/agents/secret-agent/work-policy/v1/turn-usage").json()
        assert client.get("/work-policy/v1/agent-template/options").json()
        assert client.post("/agents/secret-agent/team-data/v1/memory/search").json()
        assert client.get("/work-policy/v1/conversations/secret-legacy").json()
    hashed = timing_id("secret-rid")
    # These fake routes never call ``_body``, so no body RID is recorded and the path-less ones keep job_id None.
    assert [(timing["phase"], timing["job_id"]) for timing in _timings(caplog)] == [
        ("api_status", hashed), ("api_requests", hashed), ("api_submit", None), ("api_skill-catalog", None),
        ("api_turn-usage", None), ("api_options", None), ("api_search", None)]
    assert "secret" not in caplog.text


def test_route_timing_hashes_the_rid_of_a_body_only_route(caplog):
    """api_catalog had job_id=null: its RID is in the body, so it could not be joined to the Hosted turn."""
    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient
    from shared_context.behavior_api import _body
    from shared_context.behavior_native_capability_wire import NativeTurnScope
    from shared_context.behavior_timing import RouteTiming, timing_id
    from shared_context.memory_relay import WRITE_BODY_BYTES, MemoryWrite
    app = FastAPI()
    app.add_middleware(RouteTiming)

    @app.post("/work-policy/v1/native-capabilities/catalog")
    async def catalog(request: Request):
        await _body(request, NativeTurnScope)
        return {"ok": True}

    @app.post("/work-policy/v1/native-capabilities/call")
    async def unparsed():
        return {"ok": True}

    @app.post("/agents/{agent_id}/team-data/v1/memory/items")
    async def memory_items(agent_id: str, request: Request):
        await _body(request, MemoryWrite, max_body_bytes=WRITE_BODY_BYTES)
        return {"ok": True}

    write = {"scope": "fap1-" + "0" * 48, "source_request_id": "secret-turn-rid", "items": [
        {"slot": "p0", "kind": "user_profile", "content": "x", "embedding": [0.0] * 1536, "updated_at": 0}]}
    with caplog.at_level(logging.WARNING), TestClient(app) as client:
        assert client.post("/work-policy/v1/native-capabilities/catalog",
                           json={"conversation_id": "secret-cid", "request_id": "secret-rid"}).json()
        assert client.post("/work-policy/v1/native-capabilities/call").json()
        assert client.post("/agents/secret-agent/team-data/v1/memory/items", json=write).json()
    # A memory write carries its turn's RID as source_request_id.
    assert [(timing["phase"], timing["job_id"]) for timing in _timings(caplog)] == [
        ("api_catalog", timing_id("secret-rid")), ("api_call", None), ("api_items", timing_id("secret-turn-rid"))]
    assert "secret" not in caplog.text