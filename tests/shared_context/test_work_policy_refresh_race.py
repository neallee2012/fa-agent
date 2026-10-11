"""WorkPolicyControl.refresh caches must only grow: a slow or stale concurrent refresh can never drop a
binding another refresh just added (live v47: KeyError before the provider request)."""

import threading

from shared_context.behavior_work_policy import work_policy_key
from test_behavior_assistant import assistant
from test_conversation_profile_selection import configured
from test_instruction_flow import instruction_models, runtime_path
from test_instruction_prompt import submit
from test_m3_auth import jwk_client, signing_key


def admitted(h, control):
    sdk_io(lambda *args, **kwargs: None) if False else None
    first, _ = submit(h, "refresh-first", "First turn.")
    second, _ = submit(h, "refresh-second", "Second turn.")
    return first["binding_id"], second["binding_id"]


def test_a_stale_scoped_refresh_never_drops_a_binding_added_meanwhile(assistant):
    h, control, _ = configured(assistant)
    old, new = admitted(h, control)
    # The API process had cached only the older turn when the newer one arrived.
    control.records = {old: control.records[old]}
    old_key = work_policy_key("registration", control.records[old].admission_id)
    read = control.store.read_snapshot
    stale_started, fresh_done = threading.Event(), threading.Event()

    def interleaved(keys):
        if threading.current_thread().name == "stale-refresh" and tuple(keys) == (old_key,):
            stale_started.set()
            assert fresh_done.wait(10)
        return read(keys)

    control.store.read_snapshot = interleaved
    try:
        stale = threading.Thread(target=control.refresh, args=(old,), name="stale-refresh")
        stale.start()
        assert stale_started.wait(10)
        control.refresh(new)
        assert new in control.records and new in h.runtime.bindings
        fresh_done.set()
        stale.join(10)
    finally:
        control.store.read_snapshot = read
    assert not stale.is_alive()
    assert {old, new} <= set(control.records)
    assert h.runtime.bindings[new] == control.records[new].binding
    assert h.runtime.profiles[new] == control.records[new].profile


def test_a_full_refresh_from_an_older_registry_never_drops_a_newer_binding(assistant):
    h, control, _ = configured(assistant)
    old, _ = submit(h, "refresh-older", "Older turn.")
    old = old["binding_id"]
    control.records = {}
    registry = control.registry_key
    read = control.store.read_snapshot
    stale_started, fresh_done = threading.Event(), threading.Event()

    def interleaved(keys):
        rows = read(keys)
        if threading.current_thread().name == "stale-full" and registry in keys:
            stale_started.set()
            assert fresh_done.wait(10)
        return rows

    control.store.read_snapshot = interleaved
    try:
        stale = threading.Thread(target=control.refresh, name="stale-full")
        stale.start()
        assert stale_started.wait(10)
        control.store.read_snapshot = read
        newer, _ = submit(h, "refresh-newer", "Newer turn.")
        newer = newer["binding_id"]
        assert newer in control.records
        control.store.read_snapshot = interleaved
        fresh_done.set()
        stale.join(10)
    finally:
        control.store.read_snapshot = read
    assert not stale.is_alive()
    assert {old, newer} <= set(control.records) and newer in h.runtime.bindings


def test_a_failure_inside_before_send_is_labelled_and_never_reaches_the_provider():
    import asyncio
    from types import SimpleNamespace
    import httpx
    import pytest
    from shared_context.behavior_assistant_gateway import AssistantProvider
    from shared_context.errors import Problem

    sent = []
    provider = AssistantProvider("https://provider.invalid/openai/v1/chat/completions",
        SimpleNamespace(get_token=lambda _: SimpleNamespace(token="OFFLINE_BEFORE_SEND")),
        transport=httpx.MockTransport(lambda request: sent.append(request) or httpx.Response(500)))

    async def lost_binding():
        raise KeyError("binding")

    body = {"model": "configured-model", "stream": True, "messages": [{"role": "user", "content": "x"}]}
    with pytest.raises(Problem) as error:
        asyncio.run(provider.send(body, before_send=lost_binding, text_reply=True, model="configured-model"))
    detail = error.value.body["error"]
    assert error.value.status == 503 and detail["code"] == "ASSISTANT_MODEL_OUTCOME_UNKNOWN"
    assert detail["upstream_error_phase"] == "before_send" and detail["exception_type"] == "KeyError"
    assert sent == []

def test_a_second_refresh_inside_one_read_view_never_drops_a_binding_admitted_meanwhile(assistant):
    """8765 conversation reads refresh twice in one consistent_read; the view caches the older registry."""
    h, control, _ = configured(assistant)
    old, _ = submit(h, "view-older", "Older turn.")
    old = old["binding_id"]
    admitted_meanwhile = []

    def read():
        control.refresh()
        worker = threading.Thread(target=lambda: admitted_meanwhile.append(
            submit(h, "view-newer", "Newer turn.")[0]["binding_id"]))
        worker.start()
        worker.join(10)
        control.refresh()
        return set(control.records)

    try:
        h.store.consistent_read(read, max_keys=None)
    except Exception:
        pass  # The view itself may refuse; its in-memory caches must still keep committed work.
    new, = admitted_meanwhile
    assert {old, new} <= set(control.records)
    assert h.runtime.bindings[new] == control.records[new].binding
    assert h.runtime.profiles[new] == control.records[new].profile