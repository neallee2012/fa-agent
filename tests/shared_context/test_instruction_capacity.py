"""Model-specific declared limits through real admission and Hosted SDK options."""

import json
import os
import hashlib
import asyncio
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType, SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from shared_context.behavior_assistant import AssistantProfile, _LocalHttp, key
from shared_context.behavior_assistant_api import create_assistant_app
from shared_context.behavior_assistant_gateway import AssistantProvider
from shared_context.behavior_assistant_hosted import AssistantClient
from shared_context.behavior_assistant_wire import InstructionSubmit, Task
from shared_context.behavior_instruction_models import InstructionModels
from shared_context.behavior_model_capacity import DeclaredModelLimits, ModelCapacitySnapshot
from shared_context.behavior_work_policy import work_policy_key
from shared_context.behavior_work_policy_api import install_work_policy_routes
from shared_context.errors import Problem
from test_behavior_assistant import assistant
from test_instruction_flow import instruction_models, runtime_path
from test_instruction_prompt import configured as configured_prompt, sdk_options
from test_m3_auth import signing_key, jwk_client


def declared_facts(monkeypatch):
    path = Path(os.environ["FA_INSTRUCTION_MODEL_FACTS_FILE"])
    facts = json.loads(path.read_text(encoding="utf-8"))
    facts["gpt-4.1-mini"].update(context_window_tokens=64000, max_output_tokens=2048)
    facts["configured-second-model"].update(context_window_tokens=16000, max_output_tokens=512)
    path.write_text(json.dumps(facts), encoding="utf-8")
    return facts


def configured_capacity(assistant, prompt="fa-instruction-v1"):
    assistant.runtime.profiles = MappingProxyType({
        name: AssistantProfile.model_validate({**profile.model_dump(), "model_capacity_profile": "fa-model-capacity-v1"})
        for name, profile in assistant.runtime.profiles.items()
    })
    return configured_prompt(assistant, prompt)


def submit_capacity(h, request_id, model=None, conversation=None):
    command = InstructionSubmit(request_id=request_id, content="  Exact original capacity task.\r\n",
                                model=model, conversation_id=conversation)
    response = h.client.post("/work-policy/v1/submit", content=command.model_dump_json(), headers=h.headers())
    assert response.status_code == 200, response.text
    return response.json(), command


def test_declared_capacity_reaches_official_sdk_create_options(assistant, monkeypatch):
    facts = declared_facts(monkeypatch)
    h, _, _ = configured_capacity(assistant)
    calls, options = sdk_options(monkeypatch)
    h.endpoint.unbounded = True
    h.endpoint.replies = iter([{"content": "Offline capacity reply."}])
    accepted, command = submit_capacity(h, "capacity-first", "gpt-4.1-mini")
    binding = h.runtime.bindings[accepted["binding_id"]]
    task = Task.model_validate_json(h.runtime.job_input(binding))
    assert task.instruction.content == command.content and task.instruction.model == command.model
    assert h.runtime.controller.step(binding.binding_id).state == "succeeded"
    assert calls[0]["prompt"] == command.content
    assert len(options) == 1
    limits = options[0].get("model_capabilities")
    assert limits is not None, "known server model facts did not reach official SDK capacity options"
    assert limits.limits.max_context_window_tokens == facts["gpt-4.1-mini"]["context_window_tokens"]
    assert limits.limits.max_output_tokens == facts["gpt-4.1-mini"]["max_output_tokens"]
    assert limits.limits.max_prompt_tokens is None
    capacity = task.instruction.model_capacity
    assert capacity.model == command.model and capacity.source == "server-curated-model-facts"
    assert capacity.limits.context_window_tokens == 64000
    assert capacity.limits.max_prompt_tokens is None
    assert options[0]["provider"]["max_output_tokens"] == 2048
    assert "max_prompt_tokens" not in options[0]["provider"]


def test_next_turn_switches_model_capacity_without_mutating_previous_task_or_archive(assistant, monkeypatch):
    facts = declared_facts(monkeypatch)
    h, control, _ = configured_capacity(assistant)
    calls, options = sdk_options(monkeypatch)
    h.endpoint.unbounded = True
    h.endpoint.replies = iter([{"content": "First reply."}, {"content": "Second reply."}])
    first, _ = submit_capacity(h, "capacity-large", "gpt-4.1-mini")
    original_binding = h.runtime.bindings[first["binding_id"]]
    assert h.runtime.controller.step(original_binding.binding_id).state == "succeeded"
    original_registration = control.records[original_binding.binding_id]
    keys = (work_policy_key("registration", original_registration.admission_id),
            key("text-archive", original_binding.job_id))
    original = h.store.read_snapshot(keys)
    second, _ = submit_capacity(h, "capacity-small", "configured-second-model", first["conversation_id"])
    h.endpoint.expected_model = "configured-second-model"
    assert h.runtime.controller.step(second["binding_id"]).state == "succeeded"
    assert h.store.read_snapshot(keys) == original
    assert [call["resume"] for call in calls] == [False, True]
    assert calls[0]["sdk_session_id"] == calls[1]["sdk_session_id"]
    for option, model in zip(options, facts, strict=True):
        assert option["model"] == option["provider"]["model_id"] == option["provider"]["wire_model"] == model
        assert option["model_capabilities"].limits.max_context_window_tokens == facts[model]["context_window_tokens"]
        assert option["model_capabilities"].limits.max_output_tokens == facts[model]["max_output_tokens"]
        assert option["model_capabilities"].limits.max_prompt_tokens is None
    public = h.client.get("/work-policy/v1/conversations/" + first["conversation_id"], headers=h.headers())
    assert public.status_code == 200
    assert [turn["model_capacity"]["model"] for turn in public.json()["turns"]] == list(facts)


@pytest.mark.parametrize("values", [
    {}, {"context_window_tokens": 12000}, {"max_output_tokens": 1024},
    {"context_window_tokens": 12000, "max_output_tokens": 1024},
    {"max_prompt_tokens": 10000}, {"max_prompt_tokens": 10000, "max_output_tokens": 1024},
], ids=["all-unknown", "context-only", "output-only", "context-output", "input-only", "input-output"])
def test_missing_capacity_remains_unknown_and_no_formula_or_old_constants_are_added(assistant, monkeypatch, values):
    path = Path(os.environ["FA_INSTRUCTION_MODEL_FACTS_FILE"])
    path.write_text(json.dumps({"gpt-4.1-mini": values}), encoding="utf-8")
    h, _, _ = configured_capacity(assistant)
    _, options = sdk_options(monkeypatch)
    h.endpoint.unbounded = True
    h.endpoint.replies = iter([{"content": "Offline configuration-only reply."}])
    accepted, _ = submit_capacity(h, "partial-capacity", "gpt-4.1-mini")
    assert h.runtime.controller.step(accepted["binding_id"]).state == "succeeded"
    snapshot = h.runtime.profiles[accepted["binding_id"]].instruction.model_capacity
    assert snapshot.limits.model_dump(mode="json") == {field: values.get(field) for field in (
        "context_window_tokens", "max_prompt_tokens", "max_output_tokens")}
    caps = options[0]["model_capabilities"]
    assert caps.supports is None
    assert caps.limits.max_context_window_tokens == values.get("context_window_tokens")
    assert caps.limits.max_prompt_tokens == values.get("max_prompt_tokens")
    assert caps.limits.max_output_tokens == values.get("max_output_tokens")
    assert {field: options[0]["provider"][field] for field in ("max_prompt_tokens", "max_output_tokens")
            if field in options[0]["provider"]} == {
                field: values[field] for field in ("max_prompt_tokens", "max_output_tokens") if field in values}


def test_default_profile_preserves_task_bytes_and_does_not_apply_capacity_facts(assistant, monkeypatch):
    declared_facts(monkeypatch)
    h, _, _ = configured_prompt(assistant)
    _, options = sdk_options(monkeypatch)
    h.endpoint.unbounded = True
    h.endpoint.replies = iter([{"content": "Old profile."}])
    accepted, _ = submit_capacity(h, "legacy-capacity", "gpt-4.1-mini")
    binding = h.runtime.bindings[accepted["binding_id"]]
    task = Task.model_validate_json(h.runtime.job_input(binding))
    assert task.instruction.model_capacity is None
    assert "model_capacity" not in task.model_dump_json()
    assert "model_capacity_profile" not in h.runtime.profiles[binding.binding_id].model_dump_json()
    assert h.runtime.controller.step(binding.binding_id).state == "succeeded"
    assert "model_capabilities" not in options[0]
    assert "max_output_tokens" not in options[0]["provider"] and "max_prompt_tokens" not in options[0]["provider"]
    projection = h.client.get("/work-policy/v1/conversations/" + accepted["conversation_id"], headers=h.headers())
    assert "model_capacity" not in projection.json()["turns"][0]


@pytest.mark.parametrize("field", ["context_window_tokens", "max_prompt_tokens", "max_output_tokens"])
@pytest.mark.parametrize("value", [True, 0, -1, 1.5, "1000"])
def test_invalid_facts_are_refused_before_registering_or_dispatching(assistant, monkeypatch, field, value):
    path = Path(os.environ["FA_INSTRUCTION_MODEL_FACTS_FILE"])
    path.write_text(json.dumps({"gpt-4.1-mini": {field: value}}), encoding="utf-8")
    h, _, _ = configured_capacity(assistant)
    guard = h.store._guard()
    response = h.client.post("/work-policy/v1/submit",
        json={"request_id": "invalid-capacity-facts", "content": "Do not dispatch.", "model": "gpt-4.1-mini"},
        headers=h.headers())
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "INSTRUCTION_MODEL_CONFIGURATION"
    assert h.store._guard() == guard and h.endpoint.requests == []


@pytest.mark.parametrize("values", [
    {"context_window_tokens": 1000, "max_prompt_tokens": 1001},
    {"context_window_tokens": 1000, "max_output_tokens": 1001},
    {"context_window_tokens": 2**31},
])
def test_contradictory_or_unrepresentable_capacity_is_refused_before_registration(assistant, monkeypatch, values):
    path = Path(os.environ["FA_INSTRUCTION_MODEL_FACTS_FILE"])
    path.write_text(json.dumps({"gpt-4.1-mini": values}), encoding="utf-8")
    h, _, _ = configured_capacity(assistant)
    guard = h.store._guard()
    response = h.client.post("/work-policy/v1/submit",
        json={"request_id": "contradictory-capacity", "content": "Do not dispatch.", "model": "gpt-4.1-mini"},
        headers=h.headers())
    assert response.status_code == 503 and response.json()["error"]["code"] == "MODEL_CAPACITY_INVALID"
    assert h.store._guard() == guard and h.endpoint.requests == []


def test_independent_input_and_output_maxima_are_not_forced_into_an_invented_sum():
    limits = DeclaredModelLimits(context_window_tokens=1000, max_prompt_tokens=1000, max_output_tokens=500)
    assert limits.sdk_values() == {"max_context_window_tokens": 1000, "max_prompt_tokens": 1000, "max_output_tokens": 500}
    snapshot = ModelCapacitySnapshot.from_declared("gpt-4.1-mini", DeclaredModelLimits(
        context_window_tokens=64000, max_prompt_tokens=60000, max_output_tokens=2048))
    assert snapshot.source_sha256 == "cfc1c638425c53a970f3746eeddaac4033590a164d47e08e541739895d0c24db"


@pytest.mark.parametrize("field,value", [
    ("schema_version", "future"), ("profile", "future"), ("source", "runtime-attested"),
    ("source_sha256", "0" * 64), ("model", "different-model"),
])
def test_capacity_snapshot_rejects_forged_digest_versions_and_sources(field, value):
    snapshot = ModelCapacitySnapshot.from_declared("declared-model", DeclaredModelLimits(context_window_tokens=1000))
    data = snapshot.model_dump(mode="json")
    data[field] = value
    with pytest.raises(ValueError):
        ModelCapacitySnapshot.model_validate_json(json.dumps(data))


def test_capacity_snapshot_requires_explicit_version_and_selected_model_match():
    snapshot = ModelCapacitySnapshot.from_declared("one", DeclaredModelLimits(max_output_tokens=100))
    data = snapshot.model_dump(mode="json")
    data.pop("schema_version")
    with pytest.raises(ValueError):
        ModelCapacitySnapshot.model_validate_json(json.dumps(data))
    from shared_context.behavior_assistant_wire import InstructionTask
    with pytest.raises(ValueError):
        InstructionTask(content="Hello", conversation_id="original", turn_number=1, model="two", model_capacity=snapshot)


@pytest.mark.parametrize("field", ["model_capacity", "model_capacity_profile", "model_capabilities",
                                  "max_output_tokens", "context_window_tokens", "max_prompt_tokens"])
def test_submit_cannot_supply_capacity_overrides(assistant, monkeypatch, field):
    declared_facts(monkeypatch)
    h, _, _ = configured_capacity(assistant)
    guard = h.store._guard()
    response = h.client.post("/work-policy/v1/submit", json={
        "request_id": "spoofed-capacity", "content": "Only model name is selectable.",
        "model": "gpt-4.1-mini", field: 1000,
    }, headers=h.headers())
    assert response.status_code == 422 and h.store._guard() == guard and h.endpoint.requests == []


def change_loaded_capacity(control, monkeypatch, **values):
    original = control.models.models["gpt-4.1-mini"]
    current = type(original).model_validate({**original.model_dump(), **values})
    monkeypatch.setitem(control.models.models, original.id, current)


@pytest.mark.parametrize("stage", ["issue", "acquire", "reserve", "consume", "egress"])
def test_changed_loaded_capacity_never_rewrites_or_replays_the_original_assignment(assistant, monkeypatch, stage):
    declared_facts(monkeypatch)
    h, control, _ = configured_capacity(assistant)
    sent = []

    def credential(_):
        if stage == "egress":
            change_loaded_capacity(control, monkeypatch, max_output_tokens=4096)
        return SimpleNamespace(token="OFFLINE_CAPACITY_PROVIDER_SECRET")

    gateway = AssistantProvider("https://provider.invalid/openai/v1/chat/completions",
        SimpleNamespace(get_token=credential),
        transport=httpx.MockTransport(lambda request: sent.append(True) or httpx.Response(
            200, content=b'data: {"choices":[{"delta":{"content":"in-flight"}}]}\n\ndata: [DONE]\n\n',
            headers={"content-type": "text/event-stream"})))
    h.app = create_assistant_app(h.assistant, h.verifier, provider=gateway)
    install_work_policy_routes(h.app, SimpleNamespace(runtime=h.runtime, verifier=h.verifier,
        installation=lambda: None, require_current_native_evidence=lambda: None))
    h.client = TestClient(h.app)
    accepted, _ = submit_capacity(h, "capacity-drift", "gpt-4.1-mini")
    binding = h.runtime.bindings[accepted["binding_id"]]
    record = control.records[binding.binding_id]
    rkey = work_policy_key("registration", record.admission_id)
    original = h.store.read_snapshot((rkey,))[rkey]
    starts = []
    h.workers.adapter = replace(h.workers.adapter, start=starts.append)
    h.workers.start(binding.job_id, binding.owner)
    if stage == "issue":
        change_loaded_capacity(control, monkeypatch, max_output_tokens=4096)
        guard = h.store._guard()
        with pytest.raises(Problem) as refused:
            h.assistant.issue(starts[0])
        assert refused.value.body["error"]["code"] == "MODEL_CAPACITY_CHANGED"
        assert h.store._guard() == guard and h.store.read_snapshot((rkey,))[rkey] == original
        assert sent == []
        return
    assignment, ticket = h.assistant.issue(starts[0])
    client = AssistantClient("https://context.invalid", assignment, ticket,
        token_provider=lambda: h.headers("agent")["Authorization"].removeprefix("Bearer "),
        transport=_LocalHttp(h.client))
    body = {"model": "gpt-4.1-mini", "stream": True, "messages": [{"role": "user", "content": "One original request."}]}
    request_id = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    try:
        if stage != "acquire":
            client.acquire()
        if stage in {"consume", "egress"}:
            # Capacity is checked at reserve; a change after it is inside the seconds-level window: the reserved
            # request is sent once, never resent, and the next reserve is refused.
            client.reserve_model(request_id)
            if stage == "consume":
                change_loaded_capacity(control, monkeypatch, max_output_tokens=4096)
            response = client.model(httpx.Request("POST", "https://context.invalid/model/v1/chat/completions", json=body))
            assert response.status_code == 200 and sent == [True]
            later = {**body, "messages": [{"role": "user", "content": "A later request."}]}
            request_id = hashlib.sha256(json.dumps(later, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        else:
            change_loaded_capacity(control, monkeypatch, max_output_tokens=4096)
        with pytest.raises(Problem) as refused:
            if stage == "acquire":
                client.acquire()
            else:
                client.reserve_model(request_id)
        detail = refused.value.body["error"]
        assert detail.get("upstream_error_code", detail["code"]) == "MODEL_CAPACITY_CHANGED"
        assert h.store.read_snapshot((rkey,))[rkey] == original and h.endpoint.requests == []
        assert len(sent) == (stage in {"consume", "egress"})
        if stage in {"consume", "egress"}:
            change_loaded_capacity(control, monkeypatch, max_output_tokens=2048)
            with pytest.raises(Problem) as replay:
                client.model(httpx.Request("POST", "https://context.invalid/model/v1/chat/completions", json=body))
            assert replay.value.body["error"]["upstream_status"] == 409 and len(sent) == 1
        assert h.workers.inspect(binding.job_id).binding == starts[0].binding
    finally:
        client.close()


def test_new_turn_uses_reloaded_facts_but_saved_task_remains_its_original_snapshot(assistant, monkeypatch):
    declared_facts(monkeypatch)
    h, control, _ = configured_capacity(assistant)
    _, options = sdk_options(monkeypatch)
    h.endpoint.unbounded = True
    h.endpoint.replies = iter([{"content": "Original."}, {"content": "New facts, new turn."}])
    first, _ = submit_capacity(h, "facts-first", "gpt-4.1-mini")
    assert h.runtime.controller.step(first["binding_id"]).state == "succeeded"
    record = control.records[first["binding_id"]]
    keys = (work_policy_key("registration", record.admission_id), key("text-archive", record.binding.job_id))
    original = h.store.read_snapshot(keys)
    snapshot = record.profile.instruction.model_capacity
    path = Path(os.environ["FA_INSTRUCTION_MODEL_FACTS_FILE"])
    facts = json.loads(path.read_text(encoding="utf-8"))
    facts["gpt-4.1-mini"].update(context_window_tokens=32000, max_output_tokens=1024)
    path.write_text(json.dumps(facts), encoding="utf-8")
    assert control.models.capacity_snapshot("gpt-4.1-mini") == snapshot  # Files are loaded, not hot-watched.
    control._models = InstructionModels.from_environment()
    second, _ = submit_capacity(h, "facts-second", "gpt-4.1-mini", first["conversation_id"])
    assert h.runtime.controller.step(second["binding_id"]).state == "succeeded"
    assert options[1]["model_capabilities"].limits.max_context_window_tokens == 32000
    assert options[1]["model_capabilities"].limits.max_output_tokens == 1024
    assert control.records[second["binding_id"]].profile.instruction.model_capacity.source_sha256 != snapshot.source_sha256
    assert h.store.read_snapshot(keys) == original


def test_capacity_drift_after_acquire_refuses_original_restore_without_new_model_or_archive(assistant, monkeypatch):
    from test_sdk_compaction_product import open_turn, ordinary, settle

    declared_facts(monkeypatch)
    h, control, human = configured_capacity(assistant)
    first = open_turn(h, control, human, "capacity-restore-first")
    second = None
    try:
        settle(first, ordinary(first.assignment.sdk_session_id))
        second = open_turn(h, control, human, "capacity-restore-second",
                           first.assignment.task.instruction.conversation_id)
        assert second.client.status().state == "running"
        retained_keys = (
            work_policy_key("registration", control.records[first.binding.binding_id].admission_id),
            key("text-archive", first.binding.job_id),
            work_policy_key("registration", control.records[second.binding.binding_id].admission_id),
            key("session", second.assignment.session_id),
        )
        retained = h.store.read_snapshot(retained_keys)
        change_loaded_capacity(control, monkeypatch, max_output_tokens=4096)
        guard = h.store._guard()
        with pytest.raises(Problem) as refused:
            second.client.restore()
        assert refused.value.status == 403
        assert refused.value.body["error"]["code"] == "MODEL_CAPACITY_CHANGED"
        assert h.store.read_snapshot(retained_keys) == retained and h.store._guard() == guard
        new_archive = key("text-archive", second.binding.job_id)
        assert h.store.read_snapshot((new_archive,))[new_archive] is None
        assert second.client.status().state == "running" and h.endpoint.requests == []
    finally:
        if second is not None:
            second.client.close()
        first.client.close()


def test_server_owned_capacity_profile_is_default_off_and_pinned_in_configuration():
    from shared_context.behavior_server_config import ServerConfiguration
    from test_behavior_assistant_server import assistant_configuration
    raw = assistant_configuration()
    before = ServerConfiguration.model_validate_json(json.dumps(raw))
    raw["assistant"]["profiles"][0]["model_capacity_profile"] = "fa-model-capacity-v1"
    selected = ServerConfiguration.model_validate_json(json.dumps(raw))
    assert before.sha256 != selected.sha256
    assert before.resource_bindings()["assistant_sha256"] != selected.resource_bindings()["assistant_sha256"]
    assert before.identity == selected.identity and before.table == selected.table and before.sources == selected.sources
    assert "model_capacity_profile" not in before.assistant.profiles[0].model_dump(mode="json")
    raw["assistant"]["profiles"][0]["model_capacity_profile"] = "fa-model-capacity-future"
    with pytest.raises(ValueError):
        ServerConfiguration.model_validate_json(json.dumps(raw))


@pytest.mark.parametrize("field", ["model_capacity", "model_capabilities", "capacity", "limits", "model_capacity_profile"])
def test_model_request_cannot_override_a_server_snapshot(assistant, monkeypatch, field):
    from test_sdk_compaction_product import open_turn
    declared_facts(monkeypatch)
    h, control, human = configured_capacity(assistant)
    gateway = AssistantProvider("https://provider.invalid/openai/v1/chat/completions",
        SimpleNamespace(get_token=lambda _: pytest.fail("capacity spoof must not request provider credentials")),
        transport=httpx.MockTransport(lambda _: pytest.fail("capacity spoof must not reach provider")))
    h.app = create_assistant_app(h.assistant, h.verifier, provider=gateway)
    install_work_policy_routes(h.app, SimpleNamespace(runtime=h.runtime, verifier=h.verifier,
        installation=lambda: None, require_current_native_evidence=lambda: None))
    h.client = TestClient(h.app)
    turn = open_turn(h, control, human, "model-capacity-spoof")
    try:
        guard = h.store._guard()
        path = (f"/behavior/v1/bindings/{turn.binding.binding_id}/assistant"
                f"/sessions/{turn.assignment.session_id}/model")
        body = {"model": "gpt-4.1-mini", "stream": True, "messages": [{"role": "user", "content": "Hello."}],
                field: {"max_output_tokens": 999999}}
        response = h.client.post(path, json={"request_id": "not-permission", "body": body},
            headers={**h.headers("agent"), "x-behavior-assistant-ticket": turn.client.ticket.get_secret_value()})
        assert response.status_code == 422 and h.store._guard() == guard
        assert h.endpoint.requests == []
    finally:
        turn.client.close()


def test_missing_output_facts_still_block_pricing_without_a_fabricated_capacity(assistant):
    from test_sdk_compaction_product import open_turn
    path = Path(os.environ["FA_INSTRUCTION_MODEL_FACTS_FILE"])
    path.write_text(json.dumps({"gpt-4.1-mini": {"input_microusd_per_million": 400000,
                                               "output_microusd_per_million": 1600000}}), encoding="utf-8")
    h, control, human = configured_capacity(assistant)
    gateway = AssistantProvider("https://provider.invalid/openai/v1/chat/completions",
        SimpleNamespace(get_token=lambda _: pytest.fail("unknown output bound must not be replaced")),
        transport=httpx.MockTransport(lambda _: pytest.fail("existing pricing guard must block sending")))
    h.app = create_assistant_app(h.assistant, h.verifier, provider=gateway)
    install_work_policy_routes(h.app, SimpleNamespace(runtime=h.runtime, verifier=h.verifier,
        installation=lambda: None, require_current_native_evidence=lambda: None))
    h.client = TestClient(h.app)
    turn = open_turn(h, control, human, "unknown-output-pricing")
    try:
        assert turn.assignment.task.instruction.model_capacity.limits.sdk_values() == {}
        body = {"model": "gpt-4.1-mini", "stream": True, "messages": [{"role": "user", "content": "Hello."}]}
        digest = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        turn.client.reserve_model(digest)
        guard = h.store._guard()
        with pytest.raises(Problem) as refused:
            turn.client.model(httpx.Request("POST", "https://context.invalid/model/v1/chat/completions", json=body))
        assert refused.value.status == 503
        assert h.store._guard() == guard and h.endpoint.requests == []
        intent = h.store.read_snapshot((key("model-intent", turn.assignment.session_id, digest),))[
            key("model-intent", turn.assignment.session_id, digest)]
        assert intent.payload["data"]["consumed"] is False
    finally:
        turn.client.close()


def test_snapshot_missing_or_mismatched_to_original_server_profile_is_rejected(assistant, monkeypatch):
    from test_sdk_compaction_product import open_turn
    declared_facts(monkeypatch)
    h, control, human = configured_capacity(assistant)
    turn = open_turn(h, control, human, "invalid-capacity-snapshot")
    try:
        instruction = turn.assignment.task.instruction
        original = turn.session
        for altered in (
            instruction.model_copy(update={"model_capacity": None}),
            instruction.model_copy(update={"model_capacity": ModelCapacitySnapshot.from_declared(
                instruction.model, DeclaredModelLimits(max_output_tokens=4096))}),
        ):
            task = original.assignment.task.model_copy(update={"instruction": altered})
            session = original.model_copy(update={"assignment": original.assignment.model_copy(update={"task": task})})
            with pytest.raises(Problem) as denied:
                h.assistant.current_model_capacity(session)
            assert denied.value.body["error"]["code"] in {"MODEL_CAPACITY_DISABLED", "MODEL_CAPACITY_CHANGED"}
    finally:
        turn.client.close()


def test_thin_conversation_reader_accepts_declared_snapshot_without_treating_it_as_observed(assistant, monkeypatch):
    from web_test_console.freeform import ConversationProjection
    declared_facts(monkeypatch)
    h, _, _ = configured_capacity(assistant, None)
    sdk_options(monkeypatch)
    h.endpoint.unbounded = True
    h.endpoint.replies = iter([{"content": "Declared, not guaranteed."}])
    accepted, _ = submit_capacity(h, "public-capacity", "gpt-4.1-mini")
    assert h.runtime.controller.step(accepted["binding_id"]).state == "succeeded"
    raw = h.client.get("/work-policy/v1/conversations/" + accepted["conversation_id"], headers=h.headers())
    parsed = ConversationProjection.model_validate_json(raw.content)
    capacity = parsed.turns[0].model_capacity
    assert capacity.source == "server-curated-model-facts" and capacity.model == "gpt-4.1-mini"
    corrupted = raw.json()
    corrupted["turns"][0]["model"] = "another-model"
    with pytest.raises(ValueError):
        ConversationProjection.model_validate_json(json.dumps(corrupted))


def test_unused_model_capacity_changes_do_not_change_the_selected_snapshot(assistant, monkeypatch):
    from test_sdk_compaction_product import open_turn
    declared_facts(monkeypatch)
    h, control, human = configured_capacity(assistant)
    turn = open_turn(h, control, human, "unchanged-selected-capacity")
    try:
        previous = control.models.models["configured-second-model"]
        control.models.models[previous.id] = previous.model_copy(update={"context_window_tokens": 12000})
        assert h.assistant.current_model_capacity(turn.session) == turn.assignment.task.instruction.model_capacity
        turn.client.reserve_model("d" * 64)
        assert h.endpoint.requests == []
    finally:
        turn.client.close()


def test_hosted_sdk_entry_refuses_a_capacity_snapshot_for_another_model(tmp_path, monkeypatch):
    from shared_context import m2_copilot
    from shared_context.behavior_assistant_boundary import AssistantModelBoundary
    snapshot = ModelCapacitySnapshot.from_declared("first", DeclaredModelLimits(max_output_tokens=1024))
    monkeypatch.setattr(m2_copilot, "CopilotClient", lambda **kwargs: pytest.fail("mismatched capacity must not create SDK"))
    with pytest.raises(ValueError, match="different selected deployment"):
        asyncio.run(m2_copilot.run_copilot(
            state_dir=tmp_path / "sdk", working_directory=tmp_path, sdk_session_id="original-sdk", resume=False,
            prompt="Original exact user instruction.", tools=[], api_url="https://context.invalid",
            token_provider=lambda: pytest.fail("mismatched model needs no token"), on_event=lambda _: None,
            model="second", instruction=True, behavior_boundary=AssistantModelBoundary(None, text_reply=True),
            model_capacity=snapshot, runtime_path=tmp_path / "not-started",
        ))
