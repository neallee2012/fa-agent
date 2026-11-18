"""WorkPolicyControl.refresh caches must only grow: a slow or stale concurrent refresh can never drop a
binding another refresh just added (live v47: KeyError before the provider request)."""

import threading

from shared_context.behavior_work_policy import work_policy_key
from assistant_api_helpers import assistant_api
from conversation_profile_selection_helpers import configured
from instruction_flow_helpers import instruction_models, runtime_path
from legacy_replay_helpers import legacy_tokens, replayed
from test_m3_auth import jwk_client, signing_key


def admit(h, request_id):
    """`request_id` as the retired WorkPolicyControl.submit admitted it on 80ec0865: its rows, then its own refresh."""
    admitted = replayed(h)
    control = h.runtime.work_policy
    control.refresh(admitted["binding_id"])
    assert control.records[admitted["binding_id"]].request_id == request_id
    return admitted["binding_id"]


def admitted(h):
    return admit(h, "refresh-first"), admit(h, "refresh-second")


def test_a_stale_scoped_refresh_never_drops_a_binding_added_meanwhile(legacy_tokens, assistant_api):
    h, control, _ = configured(assistant_api)
    old, new = admitted(h)
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


def test_a_full_refresh_from_an_older_registry_never_drops_a_newer_binding(legacy_tokens, assistant_api):
    h, control, _ = configured(assistant_api)
    old = admit(h, "refresh-older")
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
        newer = admit(h, "refresh-newer")
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

def test_a_second_refresh_inside_one_read_view_never_drops_a_binding_admitted_meanwhile(
        legacy_tokens, assistant_api):
    """8765 conversation reads refresh twice in one consistent_read; the view caches the older registry."""
    h, control, _ = configured(assistant_api)
    old = admit(h, "view-older")
    admitted_meanwhile = []

    def read():
        control.refresh()
        worker = threading.Thread(target=lambda: admitted_meanwhile.append(admit(h, "view-newer")))
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
