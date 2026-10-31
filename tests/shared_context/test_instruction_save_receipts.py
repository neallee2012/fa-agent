"""Signed save/receipt composition; only SDK I/O and transport loss are substituted."""

import base64
import copy
from collections import Counter
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from shared_context import behavior_wire as wire, m2_copilot
from shared_context.behavior_assistant import _LocalHttp, key
from shared_context.behavior_assistant_api import create_assistant_app
from shared_context.behavior_assistant_gateway import AssistantProvider
from shared_context.behavior_assistant_wire import InstructionSubmit
from shared_context.behavior_store import ControlRequest
from shared_context.behavior_work_policy_api import install_work_policy_routes
from shared_context.errors import Problem
from sdk_checkpoint_local import sse
from test_behavior_assistant import assistant
from test_instruction_flow import (
    instruction_inventory, instruction_models, runtime_path, sdk_io,
)
from test_m3_auth import jwk_client, signing_key
from test_work_policy_lifecycle import enabled


PROMPT = "Keep this original instruction and return the original public answer."
ANSWER = "TC02 original public assistant answer."


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def saved_material(h, control, binding_id, sid):
    binding = control.records[binding_id].binding
    inventory = instruction_inventory(h)
    names = {
        "reply": key("reply", binding.job_id),
        "result": key("result", binding.work.work_id),
        "archive": key("text-archive", binding.job_id),
    }
    receipt_id = key("text-reply", sid)
    receipt = inventory.receipts.get(receipt_id)
    costs = {name: record.payload for name, record in inventory.rows.items()
             if record.payload["data"].get("kind") == "instruction_model_cost"
             and record.payload["data"]["job_id"] == binding.job_id}
    intents = {
        key("model-intent", sid, cost["data"]["request_sha256"]):
            inventory.rows[key("model-intent", sid, cost["data"]["request_sha256"])].payload
        for cost in costs.values()
    }
    return {
        "immutable": {name: inventory.entity_sha256.get("behavior:v1:row:" + row_id)
                      for name, row_id in names.items()},
        "receipt_id": receipt_id,
        "receipt_sha256": inventory.entity_sha256.get("behavior:v1:receipt:" + receipt_id),
        "receipt": None if receipt is None else receipt.payload,
        "cost_ledger": inventory.rows[control.cost_key].payload,
        "costs": costs,
        "model_intents": intents,
    }


def safe_material(value):
    if value is None:
        return None
    return {
        "immutable": value["immutable"],
        "receipt_id": value["receipt_id"],
        "receipt_sha256": value["receipt_sha256"],
        "receipt_present": value["receipt"] is not None,
        "model_cost_count": len(value["costs"]),
        "model_cost_stages": [cost["data"]["stage"] for cost in value["costs"].values()],
        "model_intents_consumed": [item["data"]["consumed"] for item in value["model_intents"].values()],
        "cost_ledger_sha256": sha(json.dumps(value["cost_ledger"], sort_keys=True).encode()),
    }


class SaveResponseBoundary(httpx.BaseTransport):
    def __init__(self, h, control, mode, storage_writes, *, cancel_before_save=False):
        self.h, self.control, self.mode = h, control, mode
        self.delegate = _LocalHttp(h.client)
        self.cancel_before_save = cancel_before_save
        self.calls, self.save_packets, self.status_responses, self.replays = [], [], [], []
        self.dispatched_saves = 0
        self.before = self.committed = self.final = None
        self.sid = self.binding_id = None
        self.injected = self.complete = False
        self.original_response = self.request_headers = self.save_body = None
        self.storage_writes = storage_writes
        self.writes_before = self.writes_committed = None

    def handle_request(self, request):
        action = request.url.path.rsplit("/", 1)[-1]
        self.calls.append({"method": request.method, "action": action, "path": request.url.path,
                           "body_sha256": sha(request.content)})
        if action != "save-text-reply":
            response = self.delegate.handle_request(request)
            if action == "status":
                self.status_responses.append({"path": request.url.path, "request": json.loads(request.content),
                                              "status": response.status_code, "body": response.json()})
            return response
        self.save_packets.append({"path": request.url.path, "body_sha256": sha(request.content),
                                  "request_id": json.loads(request.content)["request_id"]})
        if not self.injected:
            self.injected = True
            self.sid = request.url.path.split("/sessions/", 1)[1].split("/", 1)[0]
            self.binding_id = request.url.path.split("/bindings/", 1)[1].split("/", 1)[0]
            self.request_headers = {
                name: request.headers[name] for name in
                ("authorization", "content-type", "accept-encoding", "x-behavior-assistant-ticket")
            }
            self.save_body = json.loads(request.content)
            if self.cancel_before_save:
                cancelled = self.h.client.post(
                    "/work-policy/v1/bindings/" + self.binding_id + "/cancel",
                    json={"request_id": "cancel-before-original-save"}, headers=self.h.headers(),
                )
                assert cancelled.status_code == 200, cancelled.text
                assert cancelled.json()["result"]["cancel_accepted"] is True
            self.before = saved_material(self.h, self.control, self.binding_id, self.sid)
            self.writes_before = dict(self.storage_writes)
            if self.mode == "before-send":
                raise httpx.ConnectError("SYNTHETIC_FINAL_SAVE_NOT_SENT", request=request)
            self.dispatched_saves += 1
            response = self.delegate.handle_request(request)
            response.read()
            assert response.status_code == 200, response.text
            self.original_response = response.json()
            self.committed = saved_material(self.h, self.control, self.binding_id, self.sid)
            self.writes_committed = dict(self.storage_writes)
            if self.mode == "after-commit":
                response.close()
                raise httpx.ReadTimeout("SYNTHETIC_COMPLETE_SAVE_RESPONSE_LOST", request=request)
            if self.mode in {"changed-text", "changed-turn"}:
                for changed in (False, True):
                    body = copy.deepcopy(self.save_body)
                    if changed:
                        if self.mode == "changed-text":
                            body["reply"]["text"] = "A regenerated replacement must not overwrite the original."
                        else:
                            body["reply"]["turn_number"] += 1
                    replay = self.h.client.post(
                        request.url.path, content=json.dumps(body, ensure_ascii=False),
                        headers=self.request_headers,
                    )
                    self.replays.append({"changed": changed, "status": replay.status_code,
                                         "body": replay.json(),
                                         "code": replay.headers.get("x-behavior-assistant-error-code")})
            return response
        self.dispatched_saves += 1
        return self.delegate.handle_request(request)

    def close(self):
        pass


def double_observations(monkeypatch):
    sends = sdk_io(monkeypatch)
    clients = []
    factory = m2_copilot.CopilotClient.side_effect

    def capture(**options):
        client = factory(**options)
        clients.append(client)
        return client

    m2_copilot.CopilotClient.side_effect = capture

    def observe():
        return {
            "kind": "sdk_io_double",
            "send_count": len(sends),
            "session_ids": [call["sdk_session_id"] for call in sends],
            "prompt_sha256": [sha(call["prompt"].encode("utf-8")) for call in sends],
            "client_count": len(clients),
            "stop_returns": sum(client.stop.await_count for client in clients),
            "force_stop_calls": sum(client.force_stop.await_count for client in clients),
        }

    return observe


@contextmanager
def save_case(assistant, monkeypatch, mode, observe_sdk, *, cancel_before_save=False):
    h, control, human, _ = enabled(assistant)
    # Starts and observer wakes are separate (8883481a): only a start can dispatch the original work.
    provider_requests, scheduled, woken = [], [], []
    storage_writes = Counter(create=0, upload=0, reconcile=0, blob_put=0)
    for name in ("create", "upload", "reconcile"):
        original = getattr(h.artifacts, name)

        def counted(*args, _name=name, _original=original, **kwargs):
            storage_writes[_name] += 1
            return _original(*args, **kwargs)

        monkeypatch.setattr(h.artifacts, name, counted)
    original_put = h.artifacts.store.put

    def counted_put(*args, **kwargs):
        storage_writes["blob_put"] += 1
        return original_put(*args, **kwargs)

    monkeypatch.setattr(h.artifacts.store, "put", counted_put)

    def provider(request):
        assert str(request.url) == "https://provider.invalid/openai/v1/chat/completions"
        provider_requests.append(sha(request.content))
        response = sse(request, {"content": ANSWER}, len(provider_requests))
        if mode == "before-send":
            frames = response.content.split(b"\n\n")
            retained = [frame for frame in frames if not (
                frame.startswith(b"data: {") and "usage" in json.loads(frame[6:])
            )]
            return httpx.Response(200, request=request, headers={"content-type": "text/event-stream"},
                                  content=b"\n\n".join(retained))
        return response

    gateway = AssistantProvider(
        "https://provider.invalid/openai/v1/chat/completions",
        SimpleNamespace(get_token=lambda _: SimpleNamespace(token="OFFLINE_TC02_PROVIDER")),
        transport=httpx.MockTransport(provider),
    )
    h.client.close()
    h.app = create_assistant_app(h.assistant, h.verifier, provider=gateway)
    install_work_policy_routes(h.app, SimpleNamespace(
        runtime=h.runtime, verifier=h.verifier, installation=lambda: None,
        require_current_native_evidence=lambda: None,
    ), start_work=scheduled.append, wake_work=woken.append)
    h.client = TestClient(h.app)
    boundary = SaveResponseBoundary(h, control, mode, storage_writes, cancel_before_save=cancel_before_save)
    hosted_http = httpx.Client(base_url="https://context.invalid", transport=boundary, trust_env=False)
    h.executor.http_client, h.executor.model_transport = hosted_http, None
    command = InstructionSubmit(request_id="tc02-original-save", content=PROMPT)
    case = SimpleNamespace(h=h, control=control, human=human, boundary=boundary, command=command,
                           provider_requests=provider_requests, scheduled=scheduled, woken=woken,
                           storage_writes=storage_writes, observe_sdk=observe_sdk, receipt=None)
    try:
        submitted = h.client.post("/work-policy/v1/submit", content=command.model_dump_json(), headers=h.headers())
        assert submitted.status_code == 200, submitted.text
        case.accepted = submitted.json()
        case.binding = control.records[case.accepted["binding_id"]].binding
        assert scheduled == [case.binding.binding_id]
        yield case
    finally:
        if output := os.environ.get("FA_TC02_SAVE_EVIDENCE_DIR"):
            label = mode + ("-cancelled" if cancel_before_save else "")
            proof = {
                "runtime_mode": h.runtime.mode, "sdk": observe_sdk(), "fault": label,
                "assertions_completed": boundary.complete,
                "original_instruction_request_id": command.request_id,
                "assignment_session_id": boundary.sid,
                "original_save_request_ids": [packet["request_id"] for packet in boundary.save_packets],
                "client_save_attempts": len(boundary.save_packets), "original_save_api_dispatches": boundary.dispatched_saves,
                "complete_response_observed_before_loss": boundary.original_response is not None,
                "status_reads": boundary.status_responses,
                "replays": boundary.replays,
                "before_save": safe_material(boundary.before), "committed": safe_material(boundary.committed),
                "final": safe_material(boundary.final),
                "scripted_provider_requests": len(provider_requests),
                "instruction_schedule_calls": len(scheduled),
                "checkpoint_artifact_calls": dict(storage_writes),
                "artifact_calls_before_save": boundary.writes_before,
                "artifact_calls_at_commit": boundary.writes_committed,
                "business_effect_requests": sum(call["action"] in {"operation", "copy-sop-draft", "save"}
                                               for call in boundary.calls),
                "provider_usage_reported": mode != "before-send",
                "client_receipt": case.receipt, "human_read": "NOT_ESTABLISHED",
                "new_cloud_or_paid_model_calls": 0, "full_TC02_acceptance": False,
            }
            destination = Path(output)
            destination.mkdir(parents=True, exist_ok=True)
            with (destination / (label + ".json")).open("x", encoding="utf-8") as stream:
                json.dump(proof, stream, indent=2)
                stream.write("\n")
        hosted_http.close()
        h.client.close()


def assert_one_execution(case):
    observed = case.observe_sdk()
    assert observed["send_count"] == observed["client_count"] == observed["stop_returns"] == 1
    assert observed["force_stop_calls"] == 0
    assert observed["session_ids"] == [case.boundary.save_body["reply"]["sdk_session_id"]]
    assert observed["prompt_sha256"] == [sha(PROMPT.encode("utf-8"))]
    assert case.boundary.save_body["reply"]["conversation_id"] == case.accepted["conversation_id"]
    assert case.boundary.save_body["reply"]["turn_number"] == case.accepted["turn_number"] == 1
    assert len(case.provider_requests) == 1
    assert case.h.workers.inspect(case.binding.job_id).generation == 1
    assert [record.request_id for record in case.control.records.values()] == [case.command.request_id]
    assert not any(call["action"] in {"operation", "copy-sop-draft", "save"} for call in case.boundary.calls)


def assert_original_receipt(case):
    b = case.boundary
    b.final = saved_material(case.h, case.control, case.binding.binding_id, b.sid)
    assert b.final == b.committed
    original = b.committed["receipt"]
    request = ControlRequest.model_validate_json(json.dumps(original["request"]))
    guard = case.h.store._guard()
    assert case.h.store.reconcile(request).payload == original
    assert case.h.store._guard() == guard
    assert all(b.final["immutable"].values()) and b.final["receipt_sha256"] is not None
    assert len(b.final["costs"]) == len(b.final["model_intents"]) == 1
    assert all(item["data"]["consumed"] for item in b.final["model_intents"].values())
    assert dict(case.storage_writes) == b.writes_committed


def read_and_ack_original(case, state):
    h, b, binding = case.h, case.boundary, case.binding
    path = "/work-policy/v1/conversations/" + case.accepted["conversation_id"]
    writes, guard = dict(case.storage_writes), h.store._guard()
    projection = h.client.get(path, headers=h.headers())
    reply = h.client.get(path + "/turns/1/reply", headers=h.headers())
    repeated = h.client.get(path + "/turns/1/reply", headers=h.headers())
    assert projection.status_code == reply.status_code == repeated.status_code == 200
    assert reply.content == repeated.content
    assert reply.headers["x-content-sha256"] == sha(reply.content)
    assert reply.headers["cache-control"] == "no-store"
    assert reply.json() == b.save_body["reply"]
    assert reply.json()["text"] == ANSWER
    assert projection.json()["turns"][0]["state"] == state
    assert projection.json()["turns"][0]["request_id"] == case.command.request_id
    assert not h.runtime.job_status(binding.binding_id, case.human).obligation_fulfilled
    assert h.store._guard() == guard and dict(case.storage_writes) == writes
    assert_original_receipt(case)
    repeated_submit = h.client.post("/work-policy/v1/submit", content=case.command.model_dump_json(), headers=h.headers())
    assert repeated_submit.status_code == 200
    assert repeated_submit.json()["binding_id"] == binding.binding_id
    assert case.scheduled == [binding.binding_id]
    # The accepted cancellation wakes its original observer once; that wake never starts the work again.
    assert case.woken == ([binding.binding_id] if state == "cancelled" else [])
    delivery = "/behavior/v1/bindings/" + binding.binding_id + "/delivery"
    fetched = h.client.post(delivery + "/pull", json={"request_id": "read-original-result"}, headers=h.headers())
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["work_state"] == state
    assert not h.runtime.job_status(binding.binding_id, case.human).obligation_fulfilled
    acknowledgment = wire.PullAcknowledgment(
        request_id="ack-original-result", exchange_id=fetched.headers["x-behavior-exchange"],
        acknowledgment_token=fetched.headers["x-behavior-ack-token"],
        binding=wire.DeliveryBinding.model_validate_json(base64.urlsafe_b64decode(fetched.headers["x-behavior-binding"])),
    )
    ack = h.client.post(delivery + "/acknowledge", content=acknowledgment.model_dump_json(), headers=h.headers())
    assert ack.status_code == 200, ack.text
    assert ack.json()["result"]["fulfillment"] == "fulfilled"
    assert ack.json()["result"]["confirmed_levels"] == ["client_received"]
    job = h.runtime.job_status(binding.binding_id, case.human)
    assert job.state == state and job.obligation_fulfilled
    assert_original_receipt(case)
    assert dict(case.storage_writes) == writes
    assert_one_execution(case)
    case.receipt = {"saved_reply_sha256": sha(reply.content), "work_state_before_and_after_ack": state,
                    "before_explicit_ack": False, "confirmed_levels": ["client_received"],
                    "control_request_id": ack.json()["control_request_id"]}


def completed_response_loss(case, state):
    result = case.h.runtime.controller.step(case.binding.binding_id)
    assert result.state == state and result.result_forwarded and not result.obligation_fulfilled
    b = case.boundary
    assert len(b.save_packets) == b.dispatched_saves == 1
    assert b.save_packets[0]["request_id"] == "save-text-reply"
    assert b.before["receipt"] is None and b.committed["receipt"] is not None
    assert len(b.status_responses) == 1
    observed = b.status_responses[0]
    assert observed["path"].endswith("/sessions/" + b.sid + "/status")
    assert observed["request"] == {"request_id": "status"}
    assert observed["body"]["state"] == "ready_to_finalize"
    assert observed["body"]["result_digest"] == b.original_response["result_digest"]
    assert observed["body"]["work_outcome"] == state
    read_and_ack_original(case, state)


@pytest.mark.parametrize("cancelled", [False, True], ids=["succeeded", "cancelled"])
def test_complete_save_response_loss_recovers_original_receipt_without_resend(assistant, monkeypatch, cancelled):
    observe = double_observations(monkeypatch)
    with save_case(assistant, monkeypatch, "after-commit", observe, cancel_before_save=cancelled) as case:
        completed_response_loss(case, "cancelled" if cancelled else "succeeded")
        case.boundary.complete = True


def test_save_not_sent_keeps_original_unconfirmed_work_cost_and_no_reply(assistant, monkeypatch):
    observe = double_observations(monkeypatch)
    with save_case(assistant, monkeypatch, "before-send", observe) as case:
        with pytest.raises(Problem) as unresolved:
            case.h.runtime.controller.step(case.binding.binding_id)
        assert unresolved.value.status == 503
        assert unresolved.value.body["error"]["code"] == "WORKER_START_UNKNOWN"
        b = case.boundary
        assert len(b.save_packets) == 1 and b.dispatched_saves == 0
        assert b.original_response is None and b.committed is None
        assert len(b.status_responses) == 1 and b.status_responses[0]["body"]["state"] == "running"
        b.final = saved_material(case.h, case.control, case.binding.binding_id, b.sid)
        assert b.final == b.before and b.final["receipt"] is None
        assert all(value is None for value in b.final["immutable"].values())
        assert len(b.final["costs"]) == len(b.final["model_intents"]) == 1
        assert all(item["data"]["consumed"] for item in b.final["model_intents"].values())
        ledger = b.final["cost_ledger"]["data"]
        assert ledger["model_pending_microusd"] > 0
        assert all(cost["data"]["stage"] == "pending" for cost in b.final["costs"].values())
        status = case.h.runtime.job_status(case.binding.binding_id, case.human)
        assert status.state in {"starting", "running", "capacity_uncertain"}
        assert not status.result_forwarded and not status.obligation_fulfilled
        path = "/work-policy/v1/conversations/" + case.accepted["conversation_id"]
        projection = case.h.client.get(path, headers=case.h.headers())
        reply = case.h.client.get(path + "/turns/1/reply", headers=case.h.headers())
        assert projection.status_code == 200 and not projection.json()["can_follow_up"]
        assert projection.json()["turns"][0]["reply_available"] is False
        assert reply.status_code == 404 and reply.json()["error"]["code"] == "INSTRUCTION_REPLY_NOT_READY"
        assert saved_material(case.h, case.control, case.binding.binding_id, b.sid) == b.final
        assert dict(case.storage_writes) == b.writes_before
        assert case.scheduled == [case.binding.binding_id]
        assert_one_execution(case)
        b.complete = True


@pytest.mark.parametrize("mode", ["changed-text", "changed-turn"])
def test_confirmed_save_replay_cannot_replace_original_bytes_or_turn_metadata(assistant, monkeypatch, mode):
    observe = double_observations(monkeypatch)
    with save_case(assistant, monkeypatch, mode, observe) as case:
        result = case.h.runtime.controller.step(case.binding.binding_id)
        assert result.state == "succeeded" and result.result_forwarded
        b = case.boundary
        assert len(b.save_packets) == b.dispatched_saves == 1
        original, changed = b.replays
        assert original["status"] == 200 and original["body"] == b.original_response
        assert changed["status"] in {403, 409}
        assert_original_receipt(case)
        read_and_ack_original(case, "succeeded")
        b.complete = True
