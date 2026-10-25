"""PA-16 A: real native handlers and persistence, with named offline SDK/service doubles."""

import hashlib
import json
import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from shared_context import native_sdk
from shared_context.behavior_private_memory import PrivateMemory
from shared_context.behavior_agent_template import TemplateSkill
from shared_context.behavior_capabilities import CapabilityCapture, available_grants
from shared_context.behavior_native_turn_wire import NativeRead, NativeReceipt, NativeSubmit, material_digest
from shared_context.m2_copilot import ToolFailure
from shared_context.m3_skills import PurposeSkillCatalog, PurposeSkillEntry, PurposeSkillReadResult
from shared_context.native_capability_tools import approved_skills_provider
from shared_context.native_private_memory import NativePrivateMemory
from test_native_responses import response_submit, responses  # noqa: F401
from test_native_turn_wire import agent_receive, submit
from test_native_turns import local_env  # noqa: F401


async def offline_sdk(**options):
    observer = options.get("native_assembly")
    if observer is not None:
        observer.sdk_bound(options["system_message"], options["prompt"], options["tools"])
    files = options["session_files"]
    if not options["resume"]:
        files.files.update({
            "events.jsonl": json.dumps({
                "type": "session.start", "data": {"sessionId": options["sdk_session_id"]},
            }) + "\n",
            "workspace.yaml": "id: " + options["sdk_session_id"] + "\n",
        })
    return {
        "final_message": "OFFLINE complete answer",
        "sdk_stop": {"pid": 123, "exit_code": 0, "graceful": True, "runtime_version": "1.0.83"},
    }


def install_executor(harness, monkeypatch, tmp_path, sdk=offline_sdk, **kwargs):
    monkeypatch.setattr(native_sdk, "run_copilot", sdk)
    executor = native_sdk.NativeSdkExecutor(
        root=tmp_path / "assembly", runtime_path=tmp_path / "OFFLINE-not-executed",
        api_url="https://api.offline.invalid",
        model_url="https://offline-fixture.services.ai.azure.com/api/projects/offline/openai/v1",
        token_provider=lambda: "OFFLINE-model-token", **kwargs,
    )
    executor.reader = harness.runtime
    harness.runtime.executor, harness.runtime.preflight = executor, executor.preflight
    return executor


def run_turn(harness, request):
    accepted = harness.invoke(request)
    assert accepted.status_code == 202, accepted.text
    if request.state_profile == "foundry-responses-v1":
        response = harness.respond(harness.call(harness.runtime.read, request.command.request_id))
        assert response.status_code == 200, response.text
    return harness.wait(request.command.request_id, "completed")


@pytest.mark.parametrize("lane", ["invocations", "responses"])
def test_each_native_lane_saves_the_bound_assembly_without_prompt_content(
        responses, monkeypatch, tmp_path, lane):
    observed = []

    async def sdk(**options):
        observed.append(options)
        return await offline_sdk(**options)

    install_executor(responses, monkeypatch, tmp_path, sdk)
    request = response_submit() if lane == "responses" else submit()
    completed = run_turn(responses, request)
    read = responses.invoke(NativeRead(request_id=completed.request_id))
    assert read.status_code == 200
    assembly = read.json().get("assembly")
    assert assembly is not None, "A native receipt must retain this turn's assembly observation"
    assert assembly["schema_version"] == "fa-native-assembly-v1"
    assert assembly["capture_digest"] == request.capture.sha256
    assert assembly["selection"]["base"] == request.capture.template.role.model_dump(mode="json")
    assert assembly["selection"]["role_version"] == 1
    assert assembly["selection"]["sdk_version"] == "1.0.13"
    assert assembly["selection"]["runtime_version"] == "1.0.83"
    parts = {part["name"]: part for part in assembly["parts"]}
    system = observed[0]["system_message"].encode("utf-8")
    assert parts["system_message"]["utf8_bytes"] == len(system)
    assert parts["system_message"]["sha256"] == hashlib.sha256(system).hexdigest()
    assert parts["system_message"]["tokens"] is None
    assert parts["system_message"]["token_reason"] == "counter_unavailable"
    assert request.capture.role_prompt.text not in json.dumps(assembly)
    assert request.command.content not in json.dumps(assembly)
    assert completed.submit_digest == request.sha256 and completed.capture == request.capture
    assert responses.invoke(request).json()["assembly"] == assembly
    assert len(observed) == 1


def skill_request(request):
    bodies = {"guide-a": "SKILL_BODY_CANARY_A", "guide-b": "SKILL_BODY_CANARY_B"}
    entries = tuple(PurposeSkillEntry(
        name=name, title=name, version_id=str(index) * 32,
        sha256=hashlib.sha256(text.encode()).hexdigest(), byte_length=len(text),
        team="offline-team", source_work="offline-source", source_work_revision=1,
    ) for index, (name, text) in enumerate(bodies.items(), 1))
    grant = available_grants(None, ("fa-skills-v1",))["fa-skills-v1"]
    data = request.model_dump(mode="json")
    data["capture"]["template"]["skills"] = [
        TemplateSkill(name=item.name, version_id=item.version_id, sha256=item.sha256,
                      metadata_sha256=item.metadata_sha256).model_dump(mode="json")
        for item in entries
    ]
    data["capture"]["template"]["settings"]["capabilities"] = [grant.ref.model_dump(mode="json")]
    data["capture"]["capabilities"] = CapabilityCapture(
        head_selection_id="skill-head", grants=(grant,)).model_dump(mode="json")
    return NativeSubmit.model_validate_json(json.dumps(data)), entries, bodies


@pytest.mark.parametrize("read_outcome", ["one-read", "zero-read", "refused", "failed"])
@pytest.mark.parametrize("hitl", [False, True])
def test_selected_skills_are_not_read_until_the_controlled_reader_returns_the_verified_version(
        responses, monkeypatch, tmp_path, read_outcome, hitl):
    request, entries, bodies = skill_request(response_submit())
    sent, specs = [], []

    def service(incoming):
        sent.append(incoming.url.path)
        if incoming.url.path.endswith("/skill-catalog"):
            result = PurposeSkillCatalog(schema_version="fa-approved-skill-catalog-v2", entries=entries)
        else:
            if read_outcome in {"refused", "failed"}:
                return httpx.Response(403 if read_outcome == "refused" else 503, json={"error": {}})
            result = PurposeSkillReadResult(
                schema_version="fa-approved-skill-read-v2", skill=entries[0], text=bodies["guide-a"])
        return httpx.Response(200, content=result.model_dump_json().encode())

    async def sdk(**options):
        assert ("native_hitl" in options) is hitl
        specs.extend(options["tools"])
        reader = next(spec for spec in specs if spec.name == "read_skill")
        # An SDK start notification is not a successful controlled read.
        options["on_event"]({"type": "tool.execution_start", "data": {"tool_name": "read_skill"}})
        if read_outcome != "zero-read":
            packet = {"session_id": options["sdk_session_id"], "tool_call_id": "skill-call",
                      "tool_name": "read_skill", "arguments": {"name": "guide-a"}}
            if read_outcome in {"refused", "failed"}:
                with pytest.raises(ToolFailure):
                    await reader.handler(packet)
            else:
                result = await reader.handler(packet)
                assert bodies["guide-a"] in result.text
        return await offline_sdk(**options)

    async def end(route, command):
        assert route == "/end"
        return {}

    async def pending(command):
        assert command.after == 0
        return SimpleNamespace(references=(), next_after=None)

    install_executor(
        responses, monkeypatch, tmp_path, sdk, api_token_provider=lambda: "SECRET_TOKEN_CANARY",
        capability_providers=(approved_skills_provider(transport=httpx.MockTransport(service)),),
        hitl_client=SimpleNamespace(write=end, pending=pending) if hitl else None,
    )
    completed = run_turn(responses, request)
    assembly = completed.assembly.model_dump(mode="json")
    skills = {item["selected"]["name"]: item for item in assembly["skills"]}
    assert all(item["catalogued"] is True for item in skills.values())
    assert skills["guide-b"]["state"] == "unknown"
    assert skills["guide-b"]["reason"] == "no_read_observed"
    assert skills["guide-b"]["read_observations"] is None
    read = skills["guide-a"]
    if read_outcome == "one-read":
        assert read["state"] == "read" and read["read_observations"] == 1
        assert read["selected"]["sha256"] == entries[0].sha256
    else:
        assert read["state"] == "unknown" and read["read_observations"] is None
        assert read["reason"] == {
            "zero-read": "no_read_observed", "refused": "read_refused", "failed": "read_failed",
        }[read_outcome]
    tools = {item["name"]: item for item in assembly["tools"]}
    reader = next(spec for spec in specs if spec.name == "read_skill")
    expected_schema = json.dumps(reader.parameters, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    assert tools["read_skill"]["schema_sha256"] == hashlib.sha256(expected_schema).hexdigest()
    assert tools["read_skill"]["grants"] == [
        request.capture.template.settings.capabilities[0].model_dump(mode="json")]
    assert len(sent) == (1 if read_outcome == "zero-read" else 2)
    assert all(marker not in json.dumps(assembly) for marker in (*bodies.values(), "SECRET_TOKEN_CANARY"))


@pytest.mark.parametrize("case,expected", [
    ("disabled", "not_configured"), ("agent", "scope_not_applicable"),
    ("empty", "no_results"), ("http-error", "http_error"), ("token-error", "token_unavailable"),
    ("transport-error", "transport_error"), ("invalid", "response_invalid"),
    ("bound-count", None), ("bound-chars", None), ("skipped-size", "skipped_size"),
])
def test_memory_summary_distinguishes_no_result_failure_and_each_existing_size_decision(
        responses, monkeypatch, tmp_path, case, expected):
    sent, prompts = [], []
    content = "MEMORY_TEXT_CANARY " + ("偏" * 550 if case in {"bound-chars", "skipped-size"} else "x")

    def service(request):
        sent.append(request)
        if case == "http-error":
            return httpx.Response(500)
        if case == "transport-error":
            raise httpx.ReadError("SECRET_TRANSPORT_CANARY")
        if case == "invalid":
            return httpx.Response(200, json={"wrong": "SECRET_RESPONSE_CANARY"})
        entries = [] if case == "empty" else [
            {"memory_item": {"memory_id": "memory-" + str(number), "content": content,
                             "updated_at": 1_700_000_000 + number}}
            for number in range(7)
        ]
        return httpx.Response(200, json={"memories": entries})

    def token():
        if case == "token-error":
            raise RuntimeError("SECRET_TOKEN_FAILURE_CANARY")
        return "SECRET_MEMORY_TOKEN_CANARY"

    memory = None if case == "disabled" else NativePrivateMemory(PrivateMemory(
        "https://offline-fixture.services.ai.azure.com/api/projects/offline", "offline-memory", "offline-agent",
        token, transport=httpx.MockTransport(service)))
    request = agent_receive() if case == "agent" else submit()
    if case == "skipped-size":
        from shared_context.behavior_text_files import UserTextFile
        data = request.model_dump(mode="json")
        data["command"]["files"] = [UserTextFile(name="literal.txt", text="文" * 2400).model_dump(mode="json")]
        request = NativeSubmit.model_validate_json(json.dumps(data))

    async def sdk(**options):
        prompts.append(options["prompt"])
        return await offline_sdk(**options)

    install_executor(responses, monkeypatch, tmp_path, sdk, private_memory=memory)
    receipt = run_turn(responses, request)
    parts = {part.name: part for part in receipt.assembly.parts}
    part = parts["private_memory"]
    assert part.reason == expected
    assert len(sent) == (0 if case in {"disabled", "agent", "token-error"} else 2)
    if expected is None:
        assert part.state == "included" and "MEMORY_TEXT_CANARY" in prompts[0]
        recall = part.memory
        assert recall.count_reason == "per_search_counts" and recall.found_count is None
        assert [search.bound_count for search in recall.searches] == ([2, 2] if case == "bound-count" else [0, 0])
        assert [search.bound_chars for search in recall.searches] == ([4, 4] if case == "bound-chars" else [0, 0])
        assert recall.sources[0].memory_id == "memory-6"
        assert recall.sources[0].version is None
        assert recall.sources[0].version_reason == "source_not_reported"
    else:
        assert "MEMORY_TEXT_CANARY" not in prompts[0]
        if case in {"http-error", "transport-error", "invalid", "token-error"}:
            assert part.state == "unknown" and part.memory.found_count is None
        if case == "empty":
            assert [search.found_count for search in part.memory.searches] == [0, 0]
    encoded = receipt.assembly.model_dump_json()
    assert "MEMORY_TEXT_CANARY" not in encoded and "SECRET_" not in encoded


def test_source_attachments_and_checkpoint_link_to_the_original_turn_not_new_role_text(
        responses, monkeypatch, tmp_path):
    from shared_context.behavior_attachments import AttachedText, AttachedTexts, TextAttachment
    from shared_context.behavior_text_files import UserTextFile
    from shared_context.behavior_wire import ExactSourceRef

    text, literal = "ATTACHMENT_BODY_CANARY", "LITERAL_BODY_CANARY"
    source = ExactSourceRef(source_id="source-one", version=2,
                            sha256=hashlib.sha256(text.encode()).hexdigest(), byte_length=len(text))
    attachment = TextAttachment(name="source.txt", source=source)
    first = submit()
    values = first.model_dump(mode="json")
    values["command"].update(
        attachments=[attachment.model_dump(mode="json")],
        files=[UserTextFile(name="literal.txt", text=literal).model_dump(mode="json")],
    )
    values["capture"]["template"]["settings"]["text_attachment_profile"] = "fa-source-text-attachments-v1"
    values["capture"]["attached_text"] = AttachedTexts(entries=(
        AttachedText(attachment=attachment, media_type="text/plain", text=text),)).model_dump(mode="json")
    first = NativeSubmit.model_validate_json(json.dumps(values))
    install_executor(responses, monkeypatch, tmp_path)
    saved = run_turn(responses, first)
    original = saved.assembly.model_dump_json()
    parts = {part.name: part for part in saved.assembly.parts}
    assert parts["attachments"].state == "included"
    assert parts["attachments"].attachments[0].source == source
    assert parts["user_files"].attachments[0].sha256 == hashlib.sha256(literal.encode()).hexdigest()
    assert parts["checkpoint"].reason == "no_previous_checkpoint"
    assert parts["routines"].reason == "not_configured"
    assert saved.assembly.sdk_binding_observed is True

    values["command"].update(request_id="rid-2", conversation_id=saved.conversation_id)
    role = "ROLE_V2_CANARY"
    values["capture"]["role_prompt"].update(
        version=2, text=role, sha256=hashlib.sha256(role.encode()).hexdigest())
    second = NativeSubmit.model_validate_json(json.dumps(values))
    continued = run_turn(responses, second)
    assert continued.assembly.selection.role_version == 2
    assert continued.assembly.checkpoint.sha256 == saved.result.checkpoint_sha256
    assert continued.assembly.checkpoint.lineage.request_id == saved.request_id
    assert responses.call(responses.runtime.read, saved.request_id).assembly.model_dump_json() == original
    assert all(marker not in continued.assembly.model_dump_json() for marker in (text, literal, role))


def test_only_allowlisted_assembly_events_survive_and_no_event_is_not_zero_compactions(
        responses, monkeypatch, tmp_path):
    async def sdk(**options):
        observer = options["native_assembly"]
        for event in (
            {"type": "session.compaction_start", "id": "compact-start",
             "data": {"trigger": "threshold", "content": "SYSTEM_CANARY"}},
            {"type": "session.compaction_complete", "id": "compact-done",
             "data": {"trigger": "threshold", "success": True, "checkpoint_number": 3,
                      "summary": "PRIVATE_REASONING_CANARY", "encrypted_content": "ENCRYPTED_CANARY"}},
            {"type": "m2.context_images", "data": {
                "omitted_inline_pngs": 2, "older_inline_pngs": 1, "unsendable_inline_pngs": 1,
                "request_id": "SECRET_REQUEST_CANARY"}},
            {"type": "assistant.usage", "data": {"input_tokens": 999, "output_tokens": 999}},
            {"type": "system.message", "data": {"content": "SYSTEM_CANARY"}},
        ):
            observer.event(event)
        return await offline_sdk(**options)

    install_executor(responses, monkeypatch, tmp_path, sdk)
    receipt = run_turn(responses, submit())
    events = receipt.assembly.events
    assert [item.type for item in events] == [
        "session.compaction_start", "session.compaction_complete", "m2.context_images"]
    assert events[1].trigger == "threshold" and events[1].checkpoint_number == 3
    assert events[2].omitted_inline_pngs == 2 and events[2].older_inline_pngs == 1
    assert receipt.usage.usage == ()
    assert "CANARY" not in receipt.assembly.model_dump_json()


def test_published_legacy_json_roundtrips_byte_for_byte_and_is_never_backfilled():
    from shared_context.native_turns import _Turn
    fixture = json.loads((Path(__file__).parent / "fixtures" / "native_assembly_legacy.json").read_text("utf-8"))
    receipt = NativeReceipt.model_validate_json(fixture["receipt_json"])
    turn = _Turn.model_validate_json(fixture["turn_json"])
    assert receipt.assembly is None and turn.assembly is None
    assert receipt.observed_assembly.state == "unknown" and receipt.observed_assembly.reason == "not_recorded"
    assert receipt.model_dump_json().encode() == fixture["receipt_json"].encode()
    assert turn.model_dump_json().encode() == fixture["turn_json"].encode()
    assert material_digest(receipt) == fixture["receipt_canonical_sha256"]
    assert material_digest(turn) == fixture["turn_canonical_sha256"] == receipt.result.result_digest
    assert receipt.submit_digest == fixture["submit_sha256"]
    assert receipt.capture.sha256 == fixture["capture_sha256"]
    assert "assembly" not in receipt.model_dump(mode="json") and "assembly" not in turn.model_dump(mode="json")


@pytest.mark.parametrize("failure", ["summary-bound", "memory-observer", "store-item-bound"])
def test_optional_observation_loss_does_not_reject_a_complete_answer(
        responses, monkeypatch, tmp_path, failure):
    from shared_context import native_assembly, behavior_private_memory, native_turns

    kwargs = {}
    if failure == "summary-bound":
        monkeypatch.setattr(native_assembly, "MAX_ASSEMBLY_BYTES", 512)
    elif failure == "memory-observer":
        def cannot_record(**_):
            raise ValueError("SECRET_OBSERVER_CANARY")
        monkeypatch.setattr(behavior_private_memory, "MemorySource", cannot_record)
        service = httpx.MockTransport(lambda _: httpx.Response(200, json={
            "memories": [{"memory_item": {"content": "MEMORY_STILL_REQUIRED"}}]}))
        kwargs["private_memory"] = NativePrivateMemory(PrivateMemory(
            "https://offline-fixture.services.ai.azure.com/api/projects/offline", "offline-memory",
            "offline-agent", lambda: "TOKEN_CANARY", transport=service))
    else:
        # The original required request/result fit; only the optional observation runs out of room.
        monkeypatch.setattr(native_turns, "MAX_ITEM_BYTES", 4500)

    async def sdk(**options):
        if failure == "memory-observer":
            assert "MEMORY_STILL_REQUIRED" in options["prompt"]
        return await offline_sdk(**options)

    install_executor(responses, monkeypatch, tmp_path, sdk, **kwargs)
    receipt = run_turn(responses, submit())
    assert receipt.assembly.state == "partial"
    assert receipt.assembly.reason == {
        "summary-bound": "summary_bound", "memory-observer": "observer_failed",
        "store-item-bound": "item_bound",
    }[failure]
    assert receipt.result is not None and "CANARY" not in receipt.assembly.model_dump_json()


@pytest.mark.parametrize("lane", ["invocations", "responses"])
@pytest.mark.parametrize("failure", ["turn-ack", "completion-write", "sdk-failure"])
def test_original_assembly_survives_ack_loss_reentry_and_sdk_failure_without_reexecution(
        responses, monkeypatch, tmp_path, lane, failure):
    from azure.ai.agentserver.core.storage import FoundryStateStore
    from azure.core.exceptions import ServiceResponseError
    from shared_context.native_checkpoint import conversation_store_name

    calls, lost = [], []
    create, update = FoundryStateStore.create_item, FoundryStateStore.set_item

    async def create_once(store, key, value, **kwargs):
        result = await create(store, key, value, **kwargs)
        if failure == "turn-ack" and key == "turn:1" and not lost:
            lost.append(True)
            raise ServiceResponseError("OFFLINE turn saved but acknowledgment lost")
        return result

    async def update_once(store, key, value, **kwargs):
        if (failure == "completion-write" and key.startswith("rid:")
                and value["receipt"]["status"] == "completed" and not lost):
            lost.append(True)
            raise ServiceResponseError("OFFLINE completion write did not reach store")
        return await update(store, key, value, **kwargs)

    async def sdk(**options):
        calls.append(options["sdk_session_id"])
        if failure == "sdk-failure":
            options["native_assembly"].sdk_bound(options["system_message"], options["prompt"], options["tools"])
            raise RuntimeError("SECRET_SDK_FAILURE_CANARY")
        return await offline_sdk(**options)

    monkeypatch.setattr(FoundryStateStore, "create_item", create_once)
    monkeypatch.setattr(FoundryStateStore, "set_item", update_once)
    install_executor(responses, monkeypatch, tmp_path, sdk)
    request = response_submit() if lane == "responses" else submit()
    assert responses.invoke(request).status_code == 202
    if lane == "responses":
        assert responses.respond(responses.call(responses.runtime.read, request.command.request_id)).status_code == 200
    receipt = responses.wait("rid-1", "unknown" if failure == "sdk-failure" else "completed")
    original = receipt.assembly.model_dump_json()
    assert receipt.assembly.capture_digest == request.capture.sha256
    assert "CANARY" not in original
    if failure == "sdk-failure":
        assert receipt.assembly.state == "partial" and receipt.assembly.reason == "turn_interrupted"
    else:
        async def saved_assembly():
            async with responses.runtime._store(conversation_store_name(receipt.conversation_id)) as store:
                value = await store.get_item("turn:1")
                return value.value["assembly"]
        assert responses.call(saved_assembly) == receipt.assembly.model_dump(mode="json")
        assert lost == [True]
    monkeypatch.setenv("FA_PRIVATE_MEMORY_STORE", "changed-after-original")
    repeated = NativeReceipt.model_validate_json(responses.invoke(request).content)
    assert repeated.assembly.model_dump_json() == original
    assert repeated.result == receipt.result and repeated.usage == receipt.usage
    assert len(calls) == 1


def test_real_image_filter_reports_existing_omissions_without_request_or_image_content():
    from shared_context.behavior_assistant_boundary import AssistantModelBoundary
    from shared_context.m2_copilot import _ModelGateway
    from shared_context.native_assembly import NativeAssemblyCollector
    from test_native_reasoning_facts import completions_stream
    from test_user_images_native import encoded

    request = submit()
    receipt = NativeReceipt(state_profile=request.state_profile,
        agent_id="offline-agent", hosted_session_id="offline-host", request_id=request.command.request_id,
        conversation_id="offline-cid", command=request.command, capture=request.capture,
        capture_digest=request.capture.sha256, submit_digest=request.sha256, status="started",
    )
    collector = NativeAssemblyCollector(receipt)
    sent = []
    body = {"model": "OFFLINE-model", "stream": True, "messages": [
        {"role": "user", "content": [{"type": "image_url", "image_url": {
            "url": "https://UNSENDABLE_CANARY.invalid/image"}}]},
        *({"role": "user", "content": [{"type": "image_url", "image_url": {
            "url": "data:image/png;base64," + encoded(seed)}}]} for seed in (1, 2)),
    ]}

    async def run():
        async def reserve(_):
            return None
        async def token(_):
            return "TOKEN_CANARY"
        def model(incoming):
            sent.append(json.loads(incoming.content))
            return httpx.Response(200, request=incoming, headers={"content-type": "text/event-stream"},
                                  content=completions_stream("OFFLINE-model"))
        async with httpx.AsyncClient(transport=httpx.MockTransport(model)) as client:
            endpoint = httpx.URL("https://offline.invalid/chat/completions")
            gateway = _ModelGateway(
                endpoint, client, collector.event, model="OFFLINE-model", instruction=True, private_token=token,
                boundary=AssistantModelBoundary(reserve, text_reply=True, streaming=True))
            response = await gateway.send_request(
                httpx.Request("POST", endpoint, json=body),
                SimpleNamespace(request_id="REQUEST_CANARY", session_id="offline-sdk"))
            await response.aread()
            await response.aclose()
    asyncio.run(run())
    event = next(item for item in collector.snapshot().events if item.type == "m2.context_images")
    assert (event.omitted_inline_pngs, event.older_inline_pngs, event.unsendable_inline_pngs) == (2, 1, 1)
    assert len(sent) == 1 and "CANARY" not in collector.snapshot().model_dump_json()


def test_team_scope_absence_no_results_and_scope_mismatch_remain_distinct(
        responses, monkeypatch, tmp_path):
    from shared_context.behavior_native_turn_wire import TeamMemoryCapture
    from shared_context.native_team_memory import NativeTeamMemory
    sent = []

    def service(request):
        sent.append(request)
        return httpx.Response(200, json={"memories": []})

    memory = NativeTeamMemory(PrivateMemory(
        "https://offline-fixture.services.ai.azure.com/api/projects/offline", "offline-memory",
        "offline-agent", lambda: "TOKEN_CANARY", transport=httpx.MockTransport(service)), "offline-agent")
    install_executor(responses, monkeypatch, tmp_path, team_memory=memory)
    first = run_turn(responses, submit())
    assert next(part for part in first.assembly.parts if part.name == "team_memory").reason == "not_configured"
    assert sent == []
    second = submit("rid-2", first.conversation_id).model_dump(mode="json")
    second["capture"]["team_memory"] = TeamMemoryCapture(scope="fat1-" + "1" * 48).model_dump(mode="json")
    read = run_turn(responses, NativeSubmit.model_validate_json(json.dumps(second)))
    part = next(part for part in read.assembly.parts if part.name == "team_memory")
    assert part.reason == "no_results" and part.memory.found_count == 0
    second["command"]["request_id"] = "rid-3"
    second["capture"]["team_memory"]["scope"] = "fat1-" + "2" * 48
    mismatch = run_turn(responses, NativeSubmit.model_validate_json(json.dumps(second)))
    part = next(part for part in mismatch.assembly.parts if part.name == "team_memory")
    assert part.reason == "scope_mismatch" and part.memory.found_count is None
    assert len(sent) == 1


@pytest.mark.parametrize("lane", ["invocations", "responses"])
def test_uploaded_image_metadata_does_not_change_pngs_or_admission_errors(
        responses, monkeypatch, tmp_path, lane):
    from shared_context.behavior_user_images import NativeUserImageUpload
    from test_user_images_native import encoded, reference
    observed = []

    async def sdk(**options):
        observed.append(options["user_images"])
        return await offline_sdk(**options)

    install_executor(responses, monkeypatch, tmp_path, sdk)
    data = (response_submit() if lane == "responses" else submit()).model_dump(mode="json")
    data["command"]["images"] = [reference().model_dump(mode="json")]
    data["capture"]["model"]["supports_image_input"] = True
    request = NativeSubmit.model_validate_json(json.dumps(data))
    missing = responses.invoke(request)
    assert missing.status_code == 409 and missing.json()["error"]["code"] == "NATIVE_IMAGE_MISSING"
    assert responses.invoke(NativeUserImageUpload(png_base64=encoded())).status_code == 200
    receipt = run_turn(responses, request)
    assert observed == [(("chart.png", encoded()),)]
    part = next(part for part in receipt.assembly.parts if part.name == "user_images")
    assert part.state == "included" and part.utf8_bytes is None
    assert part.data_bytes == reference().byte_length and part.attachments[0].sha256 == reference().sha256
    assert encoded() not in receipt.assembly.model_dump_json()


def test_cancel_keeps_known_assembly_and_does_not_resume_the_model(responses, monkeypatch, tmp_path):
    from shared_context.behavior_native_turn_wire import NativeCancel
    ready, calls = threading.Event(), []

    async def sdk(**options):
        stopped = asyncio.Event()

        async def abort():
            stopped.set()

        calls.append(True)
        options["native_assembly"].sdk_bound(options["system_message"], options["prompt"], options["tools"])
        options["on_session_ready"](SimpleNamespace(abort=abort))
        ready.set()
        await stopped.wait()
        raise RuntimeError("CANCEL_PRIVATE_CANARY")

    install_executor(responses, monkeypatch, tmp_path, sdk)
    request = response_submit()
    assert responses.invoke(request).status_code == 202
    accepted = responses.call(responses.runtime.read, "rid-1")
    assert responses.respond(accepted).status_code == 200 and ready.wait(5)
    assert responses.invoke(NativeCancel(
        conversation_id=accepted.conversation_id, request_id="rid-1")).status_code == 200
    cancelled = responses.wait("rid-1", "unknown")
    assert cancelled.failure_code == "NATIVE_TURN_CANCELLED"
    assert cancelled.assembly.state == "partial" and cancelled.assembly.sdk_binding_observed is True
    before = cancelled.assembly.model_dump_json()
    assert NativeReceipt.model_validate_json(responses.invoke(request).content).assembly.model_dump_json() == before
    assert calls == [True] and "CANARY" not in before


def test_real_runner_projects_assembly_before_its_existing_public_event_boundary(tmp_path, monkeypatch, caplog):
    from shared_context.native_assembly import NativeAssemblyCollector
    from test_instruction_tool_feedback import run_callbacks
    request = submit()
    receipt = NativeReceipt(state_profile=request.state_profile,
        agent_id="offline-agent", hosted_session_id="offline-host", request_id=request.command.request_id,
        conversation_id="offline-cid", command=request.command, capture=request.capture,
        capture_digest=request.capture.sha256, submit_digest=request.sha256, status="started",
    )
    collector = NativeAssemblyCollector(receipt)

    async def unused(_):
        pytest.fail("This observation must not invoke a tool")

    async def callback(options, sid):
        assert options["system_message"]["content"] == "SYSTEM_CANARY"
        for event in (
            {"type": "session.compaction_start", "id": "compaction-1", "data": {"trigger": "manual"}},
            {"type": "session.compaction_complete", "id": "compaction-2", "data": {
                "trigger": "manual", "success": True, "checkpoint_number": 2,
                "summary": "REASONING_CANARY", "encrypted_content": "ENCRYPTED_CANARY"}},
            {"type": "assistant.usage", "data": {"input_tokens": 42, "output_tokens": 7,
                                                "private": "TOKEN_CANARY"}},
            {"type": "system.message", "data": {"content": "SYSTEM_CANARY"}},
        ):
            options["on_event"](event)

    public = run_callbacks(tmp_path, monkeypatch, callback, unused,
                           native_assembly=collector, system_message="SYSTEM_CANARY")
    assembly = collector.snapshot(complete=True)
    assert assembly.sdk_binding_observed is True
    assert [item.event_id for item in assembly.events] == ["compaction-1", "compaction-2"]
    assert assembly.events[-1].trigger == "manual" and assembly.events[-1].success is True
    assert all("data" not in item for item in public)
    assert "CANARY" not in assembly.model_dump_json() + json.dumps(public) + caplog.text


@pytest.mark.parametrize("static_fails", [False, True])
def test_published_tiered_recall_keeps_each_search_and_only_the_deduplicated_sources(
        responses, monkeypatch, tmp_path, static_fails):
    sent, prompts = [], []
    profile = {"memory_id": "profile-id", "kind": "user_profile", "content": "PROFILE_CANARY", "updated_at": 100}
    episode = {"memory_id": "episode-id", "kind": "chat_summary", "content": "EPISODE_CANARY", "updated_at": 101}

    def service(request):
        contextual = "items" in json.loads(request.content)
        sent.append(contextual)
        if not contextual and static_fails:
            return httpx.Response(500)
        items = (episode, profile) if contextual else (profile,)
        return httpx.Response(200, json={"memories": [{"memory_item": item} for item in items]})

    memory = NativePrivateMemory(PrivateMemory(
        "https://offline-fixture.services.ai.azure.com/api/projects/offline", "offline-memory",
        "offline-agent", lambda: "TOKEN_CANARY", transport=httpx.MockTransport(service)))

    async def sdk(**options):
        prompts.append(options["prompt"])
        return await offline_sdk(**options)

    install_executor(responses, monkeypatch, tmp_path, sdk, private_memory=memory)
    receipt = run_turn(responses, submit())
    part = next(part for part in receipt.assembly.parts if part.name == "private_memory")
    recall = part.memory
    assert [search.mode for search in recall.searches] == ["static", "contextual"]
    assert recall.deduplicated_count == (0 if static_fails else 1)
    assert {source.memory_id for source in recall.sources} == {"profile-id", "episode-id"}
    assert {source.kind for source in recall.sources} == {"user_profile", "chat_summary"}
    assert prompts[0].count("PROFILE_CANARY") == 1 and prompts[0].count("EPISODE_CANARY") == 1
    if static_fails:
        assert part.reason == "partial_search" and recall.searches[0].reason == "http_error"
        assert recall.searches[0].found_count is None and recall.searches[1].found_count == 2
        assert "could not be read" in prompts[0]
    second = run_turn(responses, submit("rid-2", receipt.conversation_id))
    follow = next(part.memory for part in second.assembly.parts if part.name == "private_memory")
    assert follow.mode == "contextual" and follow.searches == () and follow.found_count == 2
    assert sorted(sent) == [False, True, True]
    assert "CANARY" not in receipt.assembly.model_dump_json() + second.assembly.model_dump_json()


@pytest.mark.parametrize("bind_fails", [False, True])
def test_memory_recall_overlaps_capability_binding_and_stops_with_a_turn_that_ends_first(
        responses, monkeypatch, tmp_path, bind_fails):
    """v17: recall (about 0.6 s) waited for binding and operations start (about 1 s) though it uses neither."""
    import time
    from shared_context.errors import Problem
    from shared_context.m2_copilot import ToolSpec
    from shared_context.native_capabilities import NativeCapabilityBinding, NativeCapabilityProvider

    request, _, _ = skill_request(submit())
    searching, events, prompts = asyncio.Event(), [], []

    async def service(incoming):
        events.append("recall")
        searching.set()
        if bind_fails:
            try:
                await asyncio.sleep(30)  # longer than the harness wait: only a cancelled recall ends this turn in time
            except asyncio.CancelledError:
                events.append("cancelled")
                raise
        return httpx.Response(200, json={"memories": [{"memory_item": {
            "memory_id": "memory-1", "content": "RECALL_TEXT_CANARY", "updated_at": 1_700_000_000}}]})

    async def bind(context, grants):
        await asyncio.wait_for(searching.wait(), 5)
        events.append("bound")
        if bind_fails:
            raise Problem(503, "CAPABILITY_UNAVAILABLE", "OFFLINE refused binding")
        names = {name for grant in grants for name in grant.tools}
        return NativeCapabilityBinding(
            tools=tuple(ToolSpec(name, "OFFLINE probe", {"type": "object"}, lambda packet: None) for name in names),
            result_schemas={name: PurposeSkillReadResult for name in names})

    async def sdk(**options):
        prompts.append(options["prompt"])
        return await offline_sdk(**options)

    memory = NativePrivateMemory(PrivateMemory(
        "https://offline-fixture.services.ai.azure.com/api/projects/offline", "offline-memory", "offline-agent",
        lambda: "OFFLINE-memory-token", transport=httpx.MockTransport(service)))
    probe = NativeCapabilityProvider(accepts=lambda grant: True, bind=bind, approved_skills=True, name="probe")
    install_executor(responses, monkeypatch, tmp_path, sdk, private_memory=memory, capability_providers=(probe,))
    if bind_fails:
        unknown = responses.runtime._unknown

        async def recorded_unknown(*args, **kwargs):
            events.append("unknown")
            return await unknown(*args, **kwargs)

        monkeypatch.setattr(responses.runtime, "_unknown", recorded_unknown)
        started = time.monotonic()
        assert responses.invoke(request).status_code == 202
        refused = responses.wait(request.command.request_id, "unknown")
        assert refused.failure_code == "CAPABILITY_UNAVAILABLE" and time.monotonic() - started < 8 and prompts == []
        # The recall started before allocation stops with the SDK turn, before the turn records unknown.
        assert events[-3:] == ["cancelled", "cancelled", "unknown"]
    else:
        run_turn(responses, request)
        assert "RECALL_TEXT_CANARY" in prompts[0]
    assert events.index("recall") < events.index("bound")


TEAM_SCOPE = "fat1-" + "1" * 48


def early_memory(kind, service):
    """One configured memory (private, or Team with a fresh scope pin) and the submit that reaches it."""
    from shared_context.behavior_native_turn_wire import TeamMemoryCapture
    from shared_context.native_team_memory import NativeTeamMemory

    store = PrivateMemory(
        "https://offline-fixture.services.ai.azure.com/api/projects/offline", "offline-memory", "offline-agent",
        lambda: "OFFLINE-memory-token", transport=httpx.MockTransport(service))
    if kind == "private":
        memory = NativePrivateMemory(store)
        return memory, {"private_memory": memory}, submit

    def team_submit(*args):
        data = submit(*args).model_dump(mode="json")
        data["capture"]["team_memory"] = TeamMemoryCapture(scope=TEAM_SCOPE).model_dump(mode="json")
        return NativeSubmit.model_validate_json(json.dumps(data))

    memory = NativeTeamMemory(store, "offline-agent")
    return memory, {"team_memory": memory}, team_submit


def recall_timings(caplog, kind):
    """(job_id, outcome) of each FA_TIMING line for this memory's recall."""
    lines = (json.loads(record.getMessage().removeprefix("FA_TIMING ")) for record in caplog.records
             if record.getMessage().startswith("FA_TIMING "))
    return [(line["job_id"], line["outcome"]) for line in lines if line["phase"] == kind + "_memory_recall"]


@pytest.mark.parametrize("kind", ["private", "team"])
@pytest.mark.parametrize("case", ["new", "follow_up", "follow_up_without_history", "search_fails"])
def test_memory_recall_starts_before_allocation_and_keeps_its_tiers_and_failure_handling(
        responses, monkeypatch, tmp_path, caplog, case, kind):
    """Dev 10-22 (r2/r3): on warm turns recall still held the model 0.3-0.9 s after operations start, though it
    needs only the receipt. Allocation here waits for a search to have started, so a turn only completes when recall
    starts before allocation. The static profile is still read exactly when the turn has no SDK history (a follow-up
    after a failed first turn drops the guessed recall and reads it), and a failed search still degrades the prompt.
    A Team turn's first use pins the captured scope from the early recall; FA_TIMING keeps this turn's ID."""
    import logging
    from shared_context.behavior_timing import timing_id

    caplog.set_level(logging.WARNING, logger="shared_context.behavior_timing")
    events, prompts, searching, failing = [], [], asyncio.Event(), [case == "follow_up_without_history"]

    async def service(incoming):
        events.append("contextual" if "items" in json.loads(incoming.content) else "static")
        searching.set()
        if case == "search_fails":
            return httpx.Response(500)
        return httpx.Response(200, json={"memories": [{"memory_item": {
            "memory_id": "memory-1", "content": "RECALL_TEXT_CANARY", "updated_at": 1_700_000_000}}]})

    allocate = responses.runtime._allocate_timed

    async def allocate_after_recall(original):
        await asyncio.wait_for(searching.wait(), 5)
        events.append("allocated")
        return await allocate(original)

    async def sdk(**options):
        prompts.append(options["prompt"])
        if failing.pop(0):
            raise RuntimeError("OFFLINE first turn fails after allocation")
        return await offline_sdk(**options)

    memory, configured, make = early_memory(kind, service)
    install_executor(responses, monkeypatch, tmp_path, sdk, **configured)
    monkeypatch.setattr(responses.runtime, "_allocate_timed", allocate_after_recall)
    request = make()
    if case.startswith("follow_up"):
        failing.append(False)
        if case == "follow_up":
            first = run_turn(responses, request)
        else:
            assert responses.invoke(request).status_code == 202
            first = responses.wait(request.command.request_id, "unknown")
        events.clear()
        searching.clear()
        caplog.clear()
        request = make("rid-2", first.conversation_id)
    receipt = run_turn(responses, request)
    part = next(part for part in receipt.assembly.parts if part.name == kind + "_memory")
    timings = recall_timings(caplog, kind)
    assert {job for job, _ in timings} == {timing_id(request.command.request_id)}
    assert "failed" not in {outcome for _, outcome in timings}
    if kind == "team":
        assert memory._scope == TEAM_SCOPE  # pinned once, create-only, with the server-given value
    if case == "follow_up":
        assert events == ["contextual", "allocated"]
    elif case == "follow_up_without_history":
        assert events[:2] == ["contextual", "allocated"] and sorted(events[2:]) == ["contextual", "static"]
    else:
        assert events.index("allocated") > 0 and sorted(set(events) - {"allocated"}) == ["contextual", "static"]
        assert len(events) == 3
    if case == "search_fails":
        assert part.state == "unknown" and "could not be read" in prompts[-1]
        assert "RECALL_TEXT_CANARY" not in prompts[-1]
    else:
        assert part.state == "included" and prompts[-1].count("RECALL_TEXT_CANARY") == 1


@pytest.mark.parametrize("reply", ["found", "unavailable", "other_conversation"])
def test_reminders_overlap_capability_binding_and_keep_their_failure_handling(responses, monkeypatch, tmp_path, reply):
    """Stage: the reminders POST (p50 0.7 s, p90 1.3 s) waited for the bindings though it needs only the CID. A binding
    that waits for the reminders read to have started only finishes when both run together. The answer is still
    taken where it was: a 503 is reported in the prompt and the turn goes on; another CID's reminder fails the turn."""
    from shared_context.m2_copilot import ToolSpec
    from shared_context.native_capabilities import NativeCapabilityBinding, NativeCapabilityProvider
    from shared_context.native_routine_client import NativeRoutineClient

    monkeypatch.setenv("FA_NATIVE_ROUTINES_ENABLED", "1")
    request, _, _ = skill_request(submit())
    reading, events, prompts = asyncio.Event(), [], []

    async def api(incoming):
        assert incoming.url.path.endswith("/routine/reminders")
        events.append("reminders")
        reading.set()
        if reply == "unavailable":
            return httpx.Response(503, json={"error": {"code": "OFFLINE_UNAVAILABLE"}})
        cid = json.loads(incoming.content)["conversation_id"] + ("-other" if reply == "other_conversation" else "")
        return httpx.Response(200, json={
            "purpose": "attention-only-not-turn-dispatch", "reminders": [], "next_after": None, "routine_turns": [{
                "routine_id": "offline-routine", "scheduled_for": "2026-10-04T01:00:00Z",
                "request_id": "earlier-routine-rid", "conversation_id": cid, "status": "unknown",
                "failure_code": "NATIVE_TURN_UNKNOWN", "authorization": False,
                "purpose": "original-native-routine-receipt"}]})

    async def bind(context, grants):
        await asyncio.wait_for(reading.wait(), 5)
        events.append("bound")
        names = {name for grant in grants for name in grant.tools}
        return NativeCapabilityBinding(
            tools=tuple(ToolSpec(name, "OFFLINE probe", {"type": "object"}, lambda packet: None) for name in names),
            result_schemas={name: PurposeSkillReadResult for name in names})

    async def sdk(**options):
        prompts.append(options["system_message"])
        return await offline_sdk(**options)

    client = NativeRoutineClient("https://api.offline.invalid", lambda: "OFFLINE-agent",
                                 transport=httpx.MockTransport(api))
    probe = NativeCapabilityProvider(accepts=lambda grant: True, bind=bind, approved_skills=True, name="probe")
    install_executor(responses, monkeypatch, tmp_path, sdk, routine_client=client, capability_providers=(probe,))
    if reply == "other_conversation":
        assert responses.invoke(request).status_code == 202
        refused = responses.wait(request.command.request_id, "unknown")
        assert refused.failure_code == "NATIVE_ROUTINE_SCOPE" and prompts == []
    else:
        receipt = run_turn(responses, request)
        part = next(part for part in receipt.assembly.parts if part.name == "reminders")
        if reply == "found":
            assert part.state == "configured" and "earlier-routine-rid" in prompts[0]
        else:
            assert part.reason == "unavailable" and "Original work reminders could not be read" in prompts[0]
    assert events == ["reminders", "bound"]


@pytest.mark.parametrize("failing", ["bind", "reminders"])
def test_an_early_reminders_read_stops_with_a_failed_bind_and_its_own_failure_still_fails_the_turn(
        responses, monkeypatch, tmp_path, failing):
    """#942 review: a bind that fails first cancels the reminders read still in flight and waits for it to end (so
    nothing outlives the turn or leaves "Task exception was never retrieved"); a reminders failure that is not a
    ``Problem`` still fails the turn, as when the read ran after the bindings."""
    import time
    from shared_context.errors import Problem
    from shared_context.m2_copilot import ToolSpec
    from shared_context.native_capabilities import NativeCapabilityBinding, NativeCapabilityProvider
    from shared_context.native_routine_client import NativeRoutineClient

    monkeypatch.setenv("FA_NATIVE_ROUTINES_ENABLED", "1")
    request, _, _ = skill_request(submit())
    reading, events, prompts = asyncio.Event(), [], []

    async def api(incoming):
        events.append("reminders")
        reading.set()
        if failing == "reminders":
            raise RuntimeError("OFFLINE reminders transport broke")
        try:
            await asyncio.sleep(30)  # longer than the harness wait: only a cancelled read ends this turn in time
        except asyncio.CancelledError:
            events.append("cancelled")
            raise

    async def bind(context, grants):
        await asyncio.wait_for(reading.wait(), 5)
        events.append("bound")
        if failing == "bind":
            raise Problem(503, "CAPABILITY_UNAVAILABLE", "OFFLINE refused binding")
        names = {name for grant in grants for name in grant.tools}
        return NativeCapabilityBinding(
            tools=tuple(ToolSpec(name, "OFFLINE probe", {"type": "object"}, lambda packet: None) for name in names),
            result_schemas={name: PurposeSkillReadResult for name in names})

    async def sdk(**options):
        prompts.append(options["system_message"])
        return await offline_sdk(**options)

    client = NativeRoutineClient("https://api.offline.invalid", lambda: "OFFLINE-agent",
                                 transport=httpx.MockTransport(api))
    probe = NativeCapabilityProvider(accepts=lambda grant: True, bind=bind, approved_skills=True, name="probe")
    install_executor(responses, monkeypatch, tmp_path, sdk, routine_client=client, capability_providers=(probe,))
    started = time.monotonic()
    assert responses.invoke(request).status_code == 202
    refused = responses.wait(request.command.request_id, "unknown")
    assert time.monotonic() - started < 8 and prompts == []
    if failing == "bind":
        assert refused.failure_code == "CAPABILITY_UNAVAILABLE" and events == ["reminders", "bound", "cancelled"]
    else:
        assert refused.failure_code == "NATIVE_TURN_UNKNOWN" and events == ["reminders", "bound"]


@pytest.mark.parametrize("kind", ["private", "team"])
def test_a_recall_started_before_allocation_stops_with_a_refused_allocation(
        responses, monkeypatch, tmp_path, caplog, kind):
    """The recall now starts before allocation, outside the SDK turn's exit stack: a refused allocation still cancels
    it and the turn ends without waiting for the search. Its FA_TIMING line says cancelled under this turn's ID; a
    Team turn's first use has already pinned the captured scope (create-only, the same value)."""
    import logging
    import time
    from shared_context.behavior_timing import timing_id
    from shared_context.errors import Problem

    caplog.set_level(logging.WARNING, logger="shared_context.behavior_timing")
    events, searching = [], asyncio.Event()

    async def service(incoming):
        searching.set()
        try:
            await asyncio.sleep(30)  # longer than the harness wait: only a cancelled recall ends this turn in time
        except asyncio.CancelledError:
            events.append("cancelled")
            raise
        return httpx.Response(200, json={"memories": []})

    async def refuse(original):
        await asyncio.wait_for(searching.wait(), 5)
        raise Problem(409, "NATIVE_TURN_NOT_TAKEN", "OFFLINE refused allocation")

    memory, configured, make = early_memory(kind, service)
    install_executor(responses, monkeypatch, tmp_path, **configured)
    monkeypatch.setattr(responses.runtime, "_allocate_timed", refuse)
    request = make()
    started = time.monotonic()
    assert responses.invoke(request).status_code == 202
    refused = responses.wait(request.command.request_id, "unknown")
    assert refused.failure_code == "NATIVE_TURN_NOT_TAKEN" and time.monotonic() - started < 8
    while not recall_timings(caplog, kind) and time.monotonic() - started < 8:
        time.sleep(0.01)  # the runtime's finally cancels the recall after it records unknown
    assert events == ["cancelled", "cancelled"]  # a new conversation: the static and the contextual search
    assert recall_timings(caplog, kind) == [(timing_id(request.command.request_id), "cancelled")]
    if kind == "team":
        assert memory._scope == TEAM_SCOPE


@pytest.mark.parametrize("kind", ["private", "team"])
def test_a_wrong_first_turn_guess_logs_its_dropped_recall_as_cancelled_under_the_turns_id(
        responses, monkeypatch, tmp_path, caplog, kind):
    """A follow-up after a failed first turn has no SDK history, so the recall guessed from its CID (contextual only)
    is dropped while still searching and recalled again with the static profile. Dev latency reports must read that
    as one cancelled and one returned recall under this turn's ID, never as a failed one."""
    import logging
    from shared_context.behavior_timing import timing_id

    caplog.set_level(logging.WARNING, logger="shared_context.behavior_timing")
    held, failing, searching = [False], [True, False], asyncio.Event()

    async def service(incoming):
        searching.set()
        if held[0]:
            held[0] = False
            await asyncio.sleep(30)  # the guessed search: only a cancelled recall ends this turn in time
        return httpx.Response(200, json={"memories": []})

    async def sdk(**options):
        if failing.pop(0):
            raise RuntimeError("OFFLINE first turn fails after allocation")
        return await offline_sdk(**options)

    allocate = responses.runtime._allocate_timed

    async def allocate_after_recall(original):
        await asyncio.wait_for(searching.wait(), 5)
        return await allocate(original)

    memory, configured, make = early_memory(kind, service)
    install_executor(responses, monkeypatch, tmp_path, sdk, **configured)
    first = make()
    assert responses.invoke(first).status_code == 202
    first = responses.wait(first.command.request_id, "unknown")
    monkeypatch.setattr(responses.runtime, "_allocate_timed", allocate_after_recall)
    held[0] = True
    searching.clear()
    caplog.clear()
    follow = make("rid-2", first.conversation_id)
    run_turn(responses, follow)
    turn = timing_id(follow.command.request_id)
    assert sorted(recall_timings(caplog, kind)) == [(turn, "cancelled"), (turn, "returned")]
