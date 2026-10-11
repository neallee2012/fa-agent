"""Admitted turns verify their private ticket; old work keeps its light registry check.

The handler's CAS _gate still checks policy, cancellation and execution in the first ticket slice;
Hosted fetches its independent pre-SDK catalogs together without changing which failure it reports.
"""
import asyncio
import threading
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from shared_context import behavior_assistant_hosted as hosted
from shared_context import behavior_assistant_wire as aw
from shared_context.behavior_assistant import key
from shared_context.behavior_auth import BehaviorPrincipal
from shared_context.behavior_operations import control_key
from shared_context.behavior_workers import worker_key
from shared_context.errors import Problem
from shared_context.source_materials import digest
from test_behavior_assistant import assistant
from test_capability_calls import GRANTS
from test_instruction_flow import instruction_models
from test_m3_auth import jwk_client, signing_key
from test_session_authority_read import _fresh_turn_beside_uncertain_work, runtime_path


def _body(label):
    return {"model": "configured-second-model", "stream": True, "messages": [{"role": "user", "content": label}]}


def _progress():
    event = aw.PublicProgressEvent(seq=1, at=datetime(2026, 1, 1, tzinfo=timezone.utc), kind="stage",
                                   status="started", summary="OFFLINE stage")
    return aw.ProgressUpdate(request_id="progress-1", progress=aw.PublicProgress(events=(event,)))


def _post(h, root, headers, route, label="OFFLINE light route"):
    body = _body(label)
    identifier = digest(body)
    if route == "reserve-model":
        content = aw.Reservation(request_id=identifier, request_sha256=identifier).model_dump_json()
        return h.client.post(root + route, content=content, headers=headers)
    if route == "progress":
        return h.client.post(root + route, content=_progress().model_dump_json(), headers=headers)
    return h.client.post(root + route, json={"request_id": identifier, "body": body}, headers=headers)


def _session_id(root):
    return root.rstrip("/").rsplit("/", 1)[-1]


def _agent(h, binding):
    return BehaviorPrincipal(binding.tenant_id, binding.agent_oid, binding.team_id, "agent",
                             h.verifier.auth.actors[binding.agent_oid].actor,
                             frozenset({"read", "submit", "execute", "consume"}))


def _code(call):
    with pytest.raises(Problem) as error:
        call()
    return error.value.body["error"]["code"]


@pytest.mark.parametrize("route", ["reserve-model", "progress", "model"])
def test_a_wrong_ticket_is_still_refused_before_any_write(assistant, route):
    h, _, fresh, root, headers, _, _ = _fresh_turn_beside_uncertain_work(assistant)
    guard = h.store._guard()
    response = _post(h, root, {**headers, "x-behavior-assistant-ticket": "0" * 64}, route)
    assert response.status_code == 403, response.text
    assert h.store._guard() == guard and h.endpoint.requests == []
    assert _code(lambda: h.assistant.model_session(
        fresh.binding_id, _session_id(root), SecretStr("0" * 64), _agent(h, fresh))) == "ASSISTANT_TICKET_DENIED"


@pytest.mark.parametrize("route", ["reserve-model", "progress", "model"])
def test_registry_removal_applies_at_next_admission_not_to_the_issued_turn(assistant, route):
    h, _, fresh, root, headers, control, human = _fresh_turn_beside_uncertain_work(assistant)
    if route == "model":
        assert _post(h, root, headers, "reserve-model").status_code == 200
    admission = control.records[fresh.binding_id].admission_id
    conversation = control.records[fresh.binding_id].profile.instruction.conversation_id
    listed = h.store.read_snapshot((control.registry_key,))[control.registry_key].payload["data"]["entries"]
    h.mutate(control.registry_key, entries=[entry for entry in listed if entry != admission])
    assert fresh.binding_id in control.records, "the route check must not depend on a cache refresh"
    response = _post(h, root, headers, route)
    assert response.status_code == 200, response.text
    ticket = SecretStr(headers["x-behavior-assistant-ticket"])
    control.refresh(fresh.binding_id)
    assert h.assistant.model_session(
        fresh.binding_id, _session_id(root), ticket, _agent(h, fresh)).binding_id == fresh.binding_id
    guard = h.store._guard()
    assert _code(lambda: control.submit(human, aw.InstructionSubmit(
        request_id="next-turn-after-registry-removal", content="OFFLINE next turn",
        conversation_id=conversation))) == "BEHAVIOR_BINDING_DENIED"
    assert h.store._guard() == guard


def _change(h, fresh, control, human, change):
    job = worker_key("job", fresh.job_id)
    if change == "cancelled":
        h.mutate(job, cancel_requested=True)
    elif change == "owner-revoked":
        h.mutate(control_key("owner", fresh.owner.owner_record_id), active=False)
    elif change == "standing-revoked":
        control.revoke(human, "revoke-between-reserve-and-consume")
    else:
        binding = h.store.read_snapshot((job,))[job].payload["data"]["binding"]
        h.mutate(job, binding={**binding, "generation": binding["generation"] + 1})


CHANGES = ["cancelled", "owner-revoked", "standing-revoked", "execution-changed"]


@pytest.mark.parametrize("change", CHANGES)
def test_a_change_after_reserve_still_refuses_consume_and_never_sends(assistant, change):
    h, _, fresh, root, headers, control, human = _fresh_turn_beside_uncertain_work(assistant)
    label = "OFFLINE change between reserve and consume"
    reserved = _post(h, root, headers, "reserve-model", label)
    assert reserved.status_code == 200, reserved.text
    _change(h, fresh, control, human, change)
    response = _post(h, root, headers, "model", label)
    assert response.status_code == 403, response.text
    assert h.endpoint.requests == []
    intent = key("model-intent", _session_id(root), digest(_body(label)))
    assert h.store.read_snapshot((intent,))[intent].payload["data"]["consumed"] is False


@pytest.mark.parametrize("route", ["reserve-model", "progress"])
@pytest.mark.parametrize("change", CHANGES)
def test_a_change_before_a_light_route_is_refused_by_its_cas_gate(assistant, route, change):
    h, _, fresh, root, headers, control, human = _fresh_turn_beside_uncertain_work(assistant)
    _change(h, fresh, control, human, change)
    sid = _session_id(root)
    watched = (key("model-intent", sid, digest(_body("OFFLINE light route"))), key("progress", sid))
    response = _post(h, root, headers, route)
    assert response.status_code == 403, response.text
    assert all(value is None for value in h.store.read_snapshot(watched).values())
    assert h.endpoint.requests == []


def test_light_routes_do_not_refresh_the_registration_cache(assistant, monkeypatch):
    h, _, _, root, headers, control, _ = _fresh_turn_beside_uncertain_work(assistant)
    refreshed = []
    original = control.refresh
    monkeypatch.setattr(control, "refresh", lambda *args: refreshed.append(args) or original(*args))
    assert _post(h, root, headers, "reserve-model").status_code == 200
    assert _post(h, root, headers, "progress").status_code == 200
    assert refreshed == []


def test_route_classification_skips_refresh_only_for_cached_instruction_work(assistant, monkeypatch):
    from shared_context.behavior_server import ServerComposition
    h, _, fresh, _, _, control, _ = _fresh_turn_beside_uncertain_work(assistant)
    refreshed = []
    original = h.runtime.refresh_bindings
    monkeypatch.setattr(h.runtime, "refresh_bindings", lambda *args: refreshed.append(args) or original(*args))
    composition = SimpleNamespace(runtime=h.runtime)
    request = lambda binding_id: SimpleNamespace(path_params={"binding_id": binding_id})
    assert ServerComposition.instruction_request(composition, request(fresh.binding_id)) is True
    assert refreshed == []
    assert ServerComposition.instruction_request(composition, request("binding-one")) is False
    assert refreshed == [("binding-one",)]


# Hosted: independent pre-SDK catalogs are fetched together, failures reported in the original order.

class _Catalogs:
    def __init__(self, slow, fast):
        self.slow, self.fast = slow, fast
        self.started = {name: threading.Event() for name in ("attached", "skills", "capability")}
        self.calls, self.concurrent = [], None

    def _enter(self, name):
        self.calls.append(name)
        self.started[name].set()
        if name == self.slow:
            # Wait until the later-listed call is also in flight; a sequential caller never gets there.
            self.concurrent = self.started[self.fast].wait(2)
            raise Problem(409, "ATTACHMENT_SOURCE_CHANGED", "OFFLINE slow first failure")
        raise Problem(503, "CAPABILITY_UNAVAILABLE" if name == "capability" else "SKILL_SOURCE_CHANGED",
                      "OFFLINE fast later failure")


def _client(profile, catalogs, *, capture=None, turn_number=2, attachments=(), previous_archive_id="archive"):
    instruction = SimpleNamespace(
        prompt_profile=profile, capabilities=capture, files=(), text_attachment_profile="fa-text-attachments-v1",
        skill_catalog_budget_profile="fa-skill-catalog-budget-v1", sdk_start_profile=None,
        turn_number=turn_number, attachments=attachments, previous_archive_id=previous_archive_id)
    assignment = SimpleNamespace(task=SimpleNamespace(instruction=instruction, job_id="job-offline"),
                                 sdk_start_binding=None)
    return SimpleNamespace(
        assignment=assignment, save=lambda *_: None,
        attached_text=lambda: catalogs._enter("attached"),
        skill_catalog=lambda: catalogs._enter("skills"),
        skill_catalog_presentation=lambda _: pytest.fail("presentation needs its catalog first"),
        capability_catalog=lambda: catalogs._enter("capability"),
    )


@pytest.mark.parametrize("profile,capture,fast", [
    ("fa-instruction-skills-v1", None, "skills"),
    ("fa-instruction-history-v1", "webiq", "capability"),
])
def test_pre_sdk_catalogs_run_together_but_report_the_first_failure_in_original_order(
        monkeypatch, tmp_path, profile, capture, fast):
    from shared_context.behavior_capabilities import CapabilityCapture
    monkeypatch.setattr(hosted, "CAPABILITY_HANDLERS", {})
    monkeypatch.setattr(hosted, "run_copilot", lambda **_: pytest.fail("the SDK must not start"))
    catalogs = _Catalogs("attached", fast)
    captured = None if capture is None else CapabilityCapture(head_selection_id=None, grants=(GRANTS["WebIQ-MCP"],))
    session = hosted.AssistantSession(_client(profile, catalogs, capture=captured),
                                      root=tmp_path, runtime_path=tmp_path / "not-started")
    with pytest.raises(Problem) as error:
        asyncio.run(session._run_turn(tmp_path))
    assert error.value.body["error"]["code"] == "ATTACHMENT_SOURCE_CHANGED"
    assert catalogs.concurrent is True and sorted(catalogs.calls) == sorted(("attached", fast))


@pytest.mark.parametrize("turn_number,attachments,previous,read", [
    (1, (), None, False),
    (1, ("attachment",), None, True),
    (2, (), "archive", True),
    (1, (), "archive", True),
])
def test_only_a_first_turn_without_attachments_skips_the_empty_attachment_read(
        monkeypatch, tmp_path, turn_number, attachments, previous, read):
    monkeypatch.setattr(hosted, "CAPABILITY_HANDLERS", {})
    monkeypatch.setattr(hosted, "run_copilot", lambda **_: pytest.fail("the SDK must not start"))
    calls = []

    def refuse(name, code):
        def call(*_):
            calls.append(name)
            raise Problem(403, code, "OFFLINE refusal")
        return call

    client = _client("fa-instruction-skills-v1", None, turn_number=turn_number, attachments=attachments,
                     previous_archive_id=previous)
    client.attached_text = refuse("attached", "ATTACHMENT_SOURCE_CHANGED")
    client.skill_catalog = refuse("skills", "SKILL_SOURCE_CHANGED")
    session = hosted.AssistantSession(client, root=tmp_path, runtime_path=tmp_path / "not-started")
    with pytest.raises(Problem) as error:
        asyncio.run(session._run_turn(tmp_path))
    assert error.value.body["error"]["code"] == ("ATTACHMENT_SOURCE_CHANGED" if read else "SKILL_SOURCE_CHANGED")
    assert ("attached" in calls) is read
