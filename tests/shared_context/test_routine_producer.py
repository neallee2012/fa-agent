"""Approved routine occurrences enter the original work/receipt path."""

import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import httpx
from fastapi.testclient import TestClient

from conftest import A
from shared_context.behavior_work_policy_api import install_work_policy_routes
from shared_context.behavior_work_policy import WorkPolicyControl
from shared_context.behavior_assistant import AssistantAuthority, AssistantRuntime, _LocalHttp
from shared_context.behavior_assistant_api import create_assistant_app
from shared_context.behavior_inbox import PullInbox
from shared_context.behavior_delivery import DeliveryCore
from shared_context.behavior_assistant_hosted import AssistantClient
from test_behavior_assistant import assistant
from test_instruction_flow import check_history, instruction_models, runtime_path, sdk_io
from test_m3_auth import jwk_client, signing_key
from test_work_cost_policy import standing
from test_work_policy_lifecycle import enabled


@pytest.fixture
def routine_owner(assistant, request):
    h, policy, observed, _, human = standing(assistant)
    document = policy.model_dump(mode="json")
    definition = {
        "routine_id": "daily-check",
        "owner_oid": human.oid,
        "approved_version": "daily-check-v1",
        "time_zone": "UTC",
        "local_time": h.time.strftime("%H:%M"),
        "content": "Summarize the approved source.",
        "trigger_data": {"topic": "daily"},
        "enabled": True,
    }
    definition.update(getattr(request, "param", {}))
    document["routines"] = [definition]
    configured = type(policy).model_validate_json(json.dumps(document))
    control = WorkPolicyControl(
        h.runtime, configured, lambda: observed.model_copy(update={"observed_at": h.time}),
    )
    control.install_offline(human, "routine-owner-approval", h.time + timedelta(minutes=10))
    install_work_policy_routes(h.app, SimpleNamespace(
        runtime=h.runtime, verifier=h.verifier, assistant=SimpleNamespace(authority=h.assistant),
        installation=lambda: None, require_current_native_evidence=lambda: None,
    ))
    return SimpleNamespace(h=h, control=control, human=human,
                          scheduled_for=h.time.replace(second=0, microsecond=0).isoformat())


def test_routine_trigger_is_disabled_without_server_configuration(assistant):
    h, control, _, _ = enabled(assistant)
    install_work_policy_routes(h.app, SimpleNamespace(
        runtime=h.runtime, verifier=h.verifier, assistant=SimpleNamespace(authority=h.assistant),
        installation=lambda: None, require_current_native_evidence=lambda: None,
    ))
    response = h.client.post(
        "/work-policy/v1/routines/daily-check/trigger",
        json={"scheduled_for": h.time.isoformat()},
        headers=h.headers(),
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "ROUTINE_PRODUCER_DISABLED"


def test_server_policy_retains_a_disabled_approved_daily_routine(assistant):
    _, policy, _, _, human = standing(assistant)
    document = policy.model_dump(mode="json")
    document["routines"] = [{
        "routine_id": "daily-check",
        "owner_oid": human.oid,
        "approved_version": "daily-check-v1",
        "time_zone": "Asia/Taipei",
        "local_time": "09:00",
        "content": "Summarize the approved source.",
        "trigger_data": {"topic": "daily"},
        "notification_policy": "original-conversation",
    }]
    configured = type(policy).model_validate_json(json.dumps(document))
    assert configured.routines[0].enabled is False
    assert configured.routines[0].owner_oid == human.oid
    assert configured.routines[0].approved_version == "daily-check-v1"


def test_owner_due_occurrence_enters_the_original_work_queue(routine_owner):
    c = routine_owner
    response = c.h.client.post(
        "/work-policy/v1/routines/daily-check/trigger",
        json={"scheduled_for": c.scheduled_for},
        headers=c.h.headers(),
    )
    assert response.status_code == 200
    result = response.json()
    assert result["status"]["state"] == "queued"
    original = c.h.client.get(
        f"/behavior/v1/bindings/{result['binding_id']}/jobs/{result['status']['job_id']}",
        headers=c.h.headers(),
    )
    assert original.status_code == 200
    assert original.json()["state"] == "queued"
    assert original.json()["obligation_fulfilled"] is False


def test_routine_origin_is_not_misreported_as_a_new_human_instruction(routine_owner):
    c = routine_owner
    response = c.h.client.post(
        "/work-policy/v1/routines/daily-check/trigger",
        json={"scheduled_for": c.scheduled_for},
        headers=c.h.headers(),
    )
    assert response.status_code == 200
    binding = c.h.runtime.bindings[response.json()["binding_id"]]
    starts = []
    c.h.workers.adapter = replace(c.h.workers.adapter, start=lambda value: starts.append(value))
    c.h.workers.start(binding.job_id, binding.owner)
    assignment, ticket = c.h.assistant.issue(starts[0])
    client = AssistantClient(
        "https://context.invalid", assignment, ticket,
        token_provider=lambda: c.h.headers("agent")["Authorization"].removeprefix("Bearer "),
        transport=_LocalHttp(c.h.client),
    )
    try:
        client.acquire()
        root = f"/behavior/v1/bindings/{binding.binding_id}/assistant/sessions/{assignment.session_id}/"
        observed = c.h.client.post(
            root + "context-origin", json={"request_id": "current-context"},
            headers={**c.h.headers("agent"), "x-behavior-assistant-ticket": ticket.get_secret_value()},
        )
        assert observed.status_code == 200
        origin = observed.json()["turn_origin"]
        assert origin["source_kind"] == "routine"
        assert origin["actor_role"] == "routine-owner"
        assert origin["authorization"] is False
    finally:
        client.close()


def test_equivalent_time_offsets_return_the_same_original_work(routine_owner):
    c = routine_owner
    path = "/work-policy/v1/routines/daily-check/trigger"
    first = c.h.client.post(path, json={"scheduled_for": c.scheduled_for}, headers=c.h.headers())
    assert first.status_code == 200
    local = datetime.fromisoformat(c.scheduled_for).astimezone(timezone(timedelta(hours=8)))
    replay = c.h.client.post(path, json={"scheduled_for": local.isoformat()}, headers=c.h.headers())
    assert replay.status_code == 200
    assert replay.json() == first.json()


def test_original_occurrence_is_readable_without_triggering_an_older_time(routine_owner):
    c = routine_owner
    body = {"scheduled_for": c.scheduled_for}
    first = c.h.client.post(
        "/work-policy/v1/routines/daily-check/trigger", json=body, headers=c.h.headers(),
    )
    assert first.status_code == 200
    c.h.time += timedelta(days=1)
    recovered = c.h.client.post(
        "/work-policy/v1/routines/daily-check/trigger-reconcile", json=body, headers=c.h.headers(),
    )
    assert recovered.status_code == 200
    assert recovered.json()["binding_id"] == first.json()["binding_id"]
    assert recovered.json()["status"]["job_id"] == first.json()["status"]["job_id"]
    assert recovered.json()["status"]["state"] == "queued"


def test_simultaneous_triggers_return_one_original_work(routine_owner):
    c = routine_owner
    def send():
        return c.h.client.post(
            "/work-policy/v1/routines/daily-check/trigger",
            json={"scheduled_for": c.scheduled_for}, headers=c.h.headers(),
        )
    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(lambda _: send(), range(2)))
    accepted = []
    for response in responses:
        if response.status_code == 412:
            assert response.json()["error"]["code"] == "BEHAVIOR_CONFLICT"
        else:
            assert response.status_code == 200
            accepted.append(response.json())
    assert accepted
    original = c.h.client.post(
        "/work-policy/v1/routines/daily-check/trigger-reconcile",
        json={"scheduled_for": c.scheduled_for}, headers=c.h.headers(),
    )
    assert original.status_code == 200
    assert all(value["binding_id"] == original.json()["binding_id"] for value in accepted)
    assert all(value["status"]["job_id"] == original.json()["status"]["job_id"] for value in accepted)


def test_lost_trigger_response_is_reconciled_without_a_second_trigger(routine_owner, monkeypatch):
    c = routine_owner
    path = "/work-policy/v1/routines/daily-check/trigger"
    transport = c.h.client._transport
    original = transport.handle_request
    saved = []
    trigger_calls = []
    def lose_response(request):
        response = original(request)
        if request.url.path == path:
            trigger_calls.append(request.url.path)
            response.read()
            assert response.status_code == 200
            saved.append(response.json())
            response.close()
            raise httpx.ReadTimeout("OFFLINE lost HTTP response after complete admission", request=request)
        return response
    monkeypatch.setattr(transport, "handle_request", lose_response)
    with pytest.raises(httpx.ReadTimeout):
        c.h.client.post(path, json={"scheduled_for": c.scheduled_for}, headers=c.h.headers())
    for _ in range(2):
        recovered = c.h.client.post(
            path + "-reconcile", json={"scheduled_for": c.scheduled_for}, headers=c.h.headers(),
        )
        assert recovered.status_code == 200
        assert recovered.json()["binding_id"] == saved[0]["binding_id"]
        assert recovered.json()["status"]["job_id"] == saved[0]["status"]["job_id"]
    assert trigger_calls == [path]


def test_current_policy_revocation_prevents_a_new_routine_occurrence(routine_owner):
    c = routine_owner
    revoked = c.h.client.post(
        "/operator/v1/standing-work-policy/revoke", json={"decision_id": "revoke-routine-owner-policy"},
        headers=c.h.headers(),
    )
    assert revoked.status_code == 200
    denied = c.h.client.post(
        "/work-policy/v1/routines/daily-check/trigger",
        json={"scheduled_for": c.scheduled_for}, headers=c.h.headers(),
    )
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "WORK_POLICY_REVOKED"


def test_duplicate_occurrence_keeps_the_saved_result_without_running_the_sdk_again(routine_owner, monkeypatch):
    c = routine_owner
    calls = sdk_io(monkeypatch)
    c.h.endpoint.unbounded = True
    c.h.endpoint.replies = iter([{"content": "The approved routine result is saved."}])
    path = "/work-policy/v1/routines/daily-check/trigger"
    body = {"scheduled_for": c.scheduled_for}
    accepted = c.h.client.post(path, json=body, headers=c.h.headers())
    assert accepted.status_code == 200
    binding_id = accepted.json()["binding_id"]
    done = c.h.runtime.controller.step(binding_id)
    assert done.state == "succeeded" and done.result_forwarded
    assert done.obligation_fulfilled is False
    check_history(c.h)
    replay = c.h.client.post(path, json=body, headers=c.h.headers())
    assert replay.status_code == 200
    assert replay.json()["binding_id"] == binding_id
    again = c.h.runtime.controller.step(binding_id)
    assert again.state == "succeeded" and again.obligation_fulfilled is False
    reply = c.h.client.get(
        f"/work-policy/v1/conversations/{accepted.json()['conversation_id']}/turns/1/reply",
        headers=c.h.headers(),
    )
    assert reply.status_code == 200
    assert reply.json()["text"] == "The approved routine result is saved."
    assert len(calls) == 1


def test_next_real_occurrence_is_a_distinct_work_not_a_content_duplicate(routine_owner):
    c = routine_owner
    path = "/work-policy/v1/routines/daily-check/trigger"
    first = c.h.client.post(path, json={"scheduled_for": c.scheduled_for}, headers=c.h.headers())
    assert first.status_code == 200
    c.h.time += timedelta(days=1)
    next_slot = datetime.fromisoformat(c.scheduled_for) + timedelta(days=1)
    following = c.h.client.post(path, json={"scheduled_for": next_slot.isoformat()}, headers=c.h.headers())
    assert following.status_code == 200
    assert following.json()["binding_id"] != first.json()["binding_id"]
    assert following.json()["status"]["job_id"] != first.json()["status"]["job_id"]


def test_rebuilt_runtime_reads_the_original_occurrence_without_registering_again(routine_owner):
    c = routine_owner
    path = "/work-policy/v1/routines/daily-check/trigger"
    body = {"scheduled_for": c.scheduled_for}
    original = c.h.client.post(path, json=body, headers=c.h.headers())
    assert original.status_code == 200
    policy = c.control.policy
    inbox = PullInbox(c.h.store, c.h.dep.inbox.registration)
    delivery = DeliveryCore(
        c.h.store, clock=lambda: c.h.time,
        registration=replace(c.h.dep.delivery.registration, channel_adapter=inbox,
                             verify_receipt=inbox.verify_receipt),
    )
    inbox.attach(delivery)
    dependencies = replace(c.h.dep, inbox=inbox, delivery=delivery)
    runtime = AssistantRuntime(
        dependencies, (policy.template,), profiles=(policy.profile,), clock=lambda: c.h.time,
        mode="offline_contract", controller_id="internal-controller",
    )
    authority = AssistantAuthority(runtime, c.h.artifacts, (A,))
    restored = WorkPolicyControl(runtime, policy, c.control.cost_reader)
    restored.attach()
    app = create_assistant_app(authority, c.h.verifier)
    install_work_policy_routes(app, SimpleNamespace(
        runtime=runtime, verifier=c.h.verifier, assistant=SimpleNamespace(authority=authority),
        installation=lambda: None, require_current_native_evidence=lambda: None,
    ))
    with TestClient(app) as restarted:
        read = restarted.post(path + "-reconcile", json=body, headers=c.h.headers())
        assert read.status_code == 200
        assert read.json()["binding_id"] == original.json()["binding_id"]
        assert read.json()["status"]["job_id"] == original.json()["status"]["job_id"]
        replay = restarted.post(path, json=body, headers=c.h.headers())
        assert replay.status_code == 200
        assert replay.json()["binding_id"] == original.json()["binding_id"]


@pytest.mark.parametrize("module,blocked", [
    ("shared_context.behavior_routine_wire", ("fastapi", "azure.data.tables", "azure.storage.blob", "copilot")),
    ("shared_context.behavior_routine", ("copilot",)),
])
def test_routine_imports_respect_hosted_and_api_package_boundaries(module, blocked):
    script = f"""
import importlib.abc
import sys
class ForbiddenPackages(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == name or fullname.startswith(name + '.') for name in {blocked!r}):
            raise ImportError('forbidden dependency: ' + fullname)
sys.meta_path.insert(0, ForbiddenPackages())
__import__({module!r})
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


def test_routine_history_is_unavailable_as_human_original_but_reply_is_retained(routine_owner, monkeypatch):
    from shared_context.behavior_history_prompt import HISTORY_CAPABILITIES, HISTORY_PROFILE
    from shared_context.errors import Problem
    from test_conversation_profile_selection import select, template
    from test_sdk_compaction_product import open_turn

    c = routine_owner
    selected, _ = select(c.h, value=template(HISTORY_PROFILE, user_text_file_profile="fa-user-text-files-v1"))
    assert selected.status_code == 200
    sdk_io(monkeypatch, expected_tools=HISTORY_CAPABILITIES)
    c.h.endpoint.unbounded = True
    c.h.endpoint.replies = iter([{"content": "Retained routine result."}])
    first = c.h.client.post(
        "/work-policy/v1/routines/daily-check/trigger",
        json={"scheduled_for": c.scheduled_for}, headers=c.h.headers(),
    )
    assert first.status_code == 200
    assert c.h.runtime.controller.step(first.json()["binding_id"]).state == "succeeded"
    turn = open_turn(c.h, c.control, c.human, "human-after-routine", first.json()["conversation_id"])
    try:
        for read in (
            lambda: turn.client.read_user_turn(1),
            lambda: turn.client.read_user_file(1, "input.txt"),
            lambda: turn.client.read_assistant_reply(1),
        ):
            with pytest.raises(Problem) as denied:
                read()
            assert denied.value.status == 404
            assert denied.value.body["error"]["code"] == "USER_TURN_NOT_AVAILABLE"
        original = c.h.client.get(
            f"/work-policy/v1/conversations/{first.json()['conversation_id']}/turns/1/reply",
            headers=c.h.headers(),
        )
        assert original.status_code == 200
        assert original.json()["text"] == "Retained routine result."
    finally:
        turn.client.close()


def reject_one_storage_batch(c, monkeypatch, kind):
    from azure.core.exceptions import ResourceModifiedError
    from test_behavior_store import parse_batch

    send = c.h.transport.send
    rejected = []
    def transmit(request, **kwargs):
        if request.method == "POST" and not rejected:
            operations, _ = parse_batch(request)
            rows = [json.loads(operation[1]["Payload"]) for operation in operations]
            if any(row.get("data", {}).get("kind") == kind for row in rows):
                rejected.append(kind)
                error = ResourceModifiedError("OFFLINE rejected original storage batch before apply")
                error.status_code = 412
                raise error
        return send(request, **kwargs)
    monkeypatch.setattr(c.h.transport, "send", transmit)
    return rejected


def test_original_admission_can_finish_enqueue_after_its_occurrence_window(routine_owner, monkeypatch):
    c = routine_owner
    rejected = reject_one_storage_batch(c, monkeypatch, "worker_job")
    path = "/work-policy/v1/routines/daily-check/trigger"
    body = {"scheduled_for": c.scheduled_for}
    first = c.h.client.post(path, json=body, headers=c.h.headers())
    assert first.status_code == 412
    assert rejected == ["worker_job"]
    incomplete = c.h.client.post(path + "-reconcile", json=body, headers=c.h.headers())
    assert incomplete.status_code == 200
    assert incomplete.json()["state"] == "admission_incomplete"
    assert incomplete.json()["retry_same_request"] is True
    c.h.time += timedelta(days=1)
    completed = c.h.client.post(path, json=body, headers=c.h.headers())
    assert completed.status_code == 200
    assert completed.json()["status"]["state"] == "queued"
    original = c.h.client.post(path + "-reconcile", json=body, headers=c.h.headers())
    assert original.status_code == 200
    assert original.json()["binding_id"] == completed.json()["binding_id"]
    assert original.json()["status"]["job_id"] == completed.json()["status"]["job_id"]


@pytest.mark.parametrize("still_current", [True, False])
def test_rejected_registration_can_be_retried_only_while_still_the_latest_occurrence(routine_owner, monkeypatch, still_current):
    c = routine_owner
    rejected = reject_one_storage_batch(c, monkeypatch, "steady_work_registration")
    path = "/work-policy/v1/routines/daily-check/trigger"
    body = {"scheduled_for": c.scheduled_for}
    first = c.h.client.post(path, json=body, headers=c.h.headers())
    assert first.status_code == 412
    assert rejected == ["steady_work_registration"]
    absent = c.h.client.post(path + "-reconcile", json=body, headers=c.h.headers())
    assert absent.status_code == 200
    assert absent.json()["state"] == "not_admitted"
    assert absent.json()["retry_same_request"] is True
    if still_current:
        accepted = c.h.client.post(path, json=body, headers=c.h.headers())
        assert accepted.status_code == 200
        assert accepted.json()["status"]["state"] == "queued"
        return
    c.h.time += timedelta(days=1)
    stale = c.h.client.post(path, json=body, headers=c.h.headers())
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "ROUTINE_OCCURRENCE_STALE"


@pytest.mark.parametrize("delta,code", [
    (timedelta(days=1), "ROUTINE_OCCURRENCE_FUTURE"),
    (timedelta(days=-1), "ROUTINE_OCCURRENCE_STALE"),
    (timedelta(seconds=-1), "ROUTINE_OCCURRENCE_INVALID"),
])
def test_only_the_latest_scheduled_occurrence_is_accepted(routine_owner, delta, code):
    c = routine_owner
    supplied = datetime.fromisoformat(c.scheduled_for) + delta
    response = c.h.client.post(
        "/work-policy/v1/routines/daily-check/trigger",
        json={"scheduled_for": supplied.isoformat()}, headers=c.h.headers(),
    )
    assert response.status_code in {409, 422}
    assert response.json()["error"]["code"] == code


@pytest.mark.parametrize("routine_owner,now,scheduled,code", [
    (
        {"time_zone": "America/New_York", "local_time": "02:30"},
        "2026-03-08T08:00:00+00:00", "2026-03-08T02:30:00-05:00",
        "ROUTINE_LOCAL_TIME_MISSING",
    ),
    (
        {"time_zone": "America/New_York", "local_time": "01:30"},
        "2026-11-01T06:45:00+00:00", "2026-11-01T01:30:00-04:00",
        "ROUTINE_LOCAL_TIME_AMBIGUOUS",
    ),
    (
        {"time_zone": "America/New_York", "local_time": "01:30"},
        "2026-11-01T06:00:00+00:00", "2026-10-31T01:30:00-04:00",
        "ROUTINE_LOCAL_TIME_AMBIGUOUS",
    ),
], indirect=["routine_owner"])
def test_dst_never_creates_two_occurrence_ids_or_an_older_catch_up(routine_owner, now, scheduled, code):
    c = routine_owner
    c.h.time = datetime.fromisoformat(now)
    response = c.h.client.post(
        "/work-policy/v1/routines/daily-check/trigger",
        json={"scheduled_for": scheduled}, headers=c.h.headers(),
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == code


def test_agent_cannot_trigger_the_owners_routine(routine_owner):
    c = routine_owner
    response = c.h.client.post(
        "/work-policy/v1/routines/daily-check/trigger",
        json={"scheduled_for": c.scheduled_for}, headers=c.h.headers("agent"),
    )
    assert response.status_code == 403


def test_occurrence_payload_cannot_override_server_content_or_authority(routine_owner):
    c = routine_owner
    response = c.h.client.post(
        "/work-policy/v1/routines/daily-check/trigger",
        json={"scheduled_for": c.scheduled_for, "source_kind": "human",
              "content": "Unapproved replacement", "approved": True},
        headers=c.h.headers(),
    )
    assert response.status_code == 422
