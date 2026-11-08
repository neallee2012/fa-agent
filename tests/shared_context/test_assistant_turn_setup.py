"""What one Hosted turn hands the SDK: tools, options and pre-SDK refusals; no network, runtime process or model."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from shared_context import behavior_assistant_checkpoint as checkpoint
from shared_context import behavior_assistant_hosted as hosted
from shared_context import behavior_assistant_wire as aw
from shared_context.behavior_capabilities import CapabilityCapture, available_grants
from shared_context.behavior_history_prompt import HISTORY_PROFILE
from shared_context.behavior_reviewed_note_prompt import REVIEWED_NOTE_PROFILE
from shared_context.behavior_skill_resources_prompt import SKILL_RESOURCES_PROFILE
from shared_context.errors import Problem
from shared_context.m2_copilot import ToolFailure, ToolOutput
from shared_context.sdk_pin import RUNTIME_VERSION

STOP = aw.SdkStop(runtime_version=RUNTIME_VERSION, pid=42, exit_code=0, graceful=True)


class Dumped(SimpleNamespace):
    def model_dump_json(self):
        return json.dumps({key: value for key, value in vars(self).items() if not callable(value)},
                          sort_keys=True, default=vars)


def instruction(**changes):
    values = dict(
        prompt_profile=None, capabilities=None, files=(), text_attachment_profile=None,
        skill_catalog_budget_profile=None, turn_number=2, attachments=(), previous_archive_id=None,
        turn_origin_profile=None, role_prompt=None, model="offline-model", model_provider=None,
        reasoning_summary=None, model_capacity=None, checkpoint_profile=None, peer_work=None,
        content="OFFLINE request", sdk_start_profile=None)
    values.update(changes)
    return SimpleNamespace(**values)


def client(task_instruction, **methods):
    assignment = SimpleNamespace(task=SimpleNamespace(instruction=task_instruction, job_id="job-offline"),
                                 sdk_start_binding=None, sdk_session_id="sdk", resume=False)
    return SimpleNamespace(assignment=assignment, api_url="https://offline.invalid", token_provider=lambda: "OFFLINE",
                           save=lambda _: None, context=lambda **_: None, finish=lambda: None,
                           operation=lambda: None, stopped=lambda _: None, save_skill_draft=lambda _: None,
                           **methods)


def started(monkeypatch, tmp_path, turn_client):
    """The run_copilot options of a turn whose SDK then fails before any stop evidence."""
    seen = []

    async def sdk(**options):
        seen.append(options)
        raise RuntimeError("OFFLINE SDK failure")
    monkeypatch.setattr(hosted, "run_copilot", sdk)
    session = hosted.AssistantSession(turn_client, root=tmp_path, runtime_path=tmp_path / "not-started",
                                      model_transport=object())
    with pytest.raises(Problem) as error:
        asyncio.run(session._run_turn(tmp_path))
    assert error.value.body["error"]["code"] == "ASSISTANT_SDK_UNKNOWN" and len(seen) == 1
    return session, seen[0]


def refused(monkeypatch, tmp_path, turn_client):
    monkeypatch.setattr(hosted, "run_copilot", lambda **_: pytest.fail("the SDK must not start"))
    session = hosted.AssistantSession(turn_client, root=tmp_path, runtime_path=tmp_path / "not-started")
    with pytest.raises(Problem) as error:
        asyncio.run(session._run_turn(tmp_path))
    return error.value.body["error"]


def tool(options, name):
    return next(spec for spec in options["tools"] if spec.name == name)


def call(spec, arguments):
    try:
        return asyncio.run(spec.handler({"arguments": arguments}))
    except ToolFailure:
        return "refused"


def test_a_plain_instruction_turn_passes_only_the_settings_it_captured(monkeypatch, tmp_path):
    _, options = started(monkeypatch, tmp_path, client(instruction()))
    assert [spec.name for spec in options["tools"]] == ["read_context", "execute_operation"]
    assert options["prompt"] == "OFFLINE request" and options["model"] == "offline-model"
    assert options["instruction"] is True and options["timeout_seconds"] is None
    for absent in ("model_provider", "model_capacity", "checkpoint_task", "session_files", "sdk_start_gate",
                   "attached_text", "user_text_files", "capability_session", "on_tool_image",
                   "mcp_temp_directory", "mcp_display_env", "delegation_runtime", "model_call_limit"):
        assert absent not in options


def test_captured_model_capacity_provider_and_checkpoint_reach_the_sdk(monkeypatch, tmp_path):
    capacity = object()
    turn = client(instruction(model_capacity=capacity, checkpoint_profile="offline-checkpoint",
                              model_provider="openai-responses", reasoning_summary="auto"))
    _, options = started(monkeypatch, tmp_path, turn)
    assert options["model_capacity"] is capacity and options["checkpoint_task"] is turn.assignment.task
    assert (options["model_provider"], options["reasoning_summary"]) == ("openai-responses", "auto")


def test_a_turn_without_its_configured_model_is_refused_before_the_sdk(monkeypatch, tmp_path):
    assert refused(monkeypatch, tmp_path, client(instruction(model=None)))["code"] == "INSTRUCTION_MODEL_CONFIGURATION"


def test_role_text_joins_the_fixed_prompt_or_refuses_the_turn(monkeypatch, tmp_path):
    joined = []

    def with_role_text(message, role):
        joined.append(role)
        if role == "bad":
            raise ValueError("OFFLINE")
        return "ROLE " + role
    monkeypatch.setattr(hosted, "with_role_text", with_role_text)
    _, options = started(monkeypatch, tmp_path, client(instruction(role_prompt="good")))
    assert options["system_message"] == "ROLE good"
    error = refused(monkeypatch, tmp_path, client(instruction(role_prompt="bad")))
    assert error == {"code": "INSTRUCTION_PROMPT_CONFIGURATION",
                     "message": "the captured role text must follow the fixed FA prompt"}
    assert joined == ["good", "bad"]


def test_configured_tools_must_assemble_the_original_profile_record(monkeypatch, tmp_path):
    original = hosted.assemble_prompt

    def assemble(profile, *, capabilities=None):
        result = original(profile, capabilities=capabilities)
        return result if capabilities is None else SimpleNamespace(record="changed", system_message="")
    monkeypatch.setattr(hosted, "assemble_prompt", assemble)
    assert refused(monkeypatch, tmp_path, client(instruction())) == {
        "code": "INSTRUCTION_PROMPT_CONFIGURATION",
        "message": "original prompt profile and configured capabilities must match"}


def builtin(entry_id):
    return CapabilityCapture(head_selection_id=None, grants=(available_grants(None, (entry_id,))[entry_id],))


def test_a_captured_builtin_without_a_matching_handler_is_refused(monkeypatch, tmp_path):
    turn = client(instruction(prompt_profile=HISTORY_PROFILE, capabilities=builtin("fa-image-delivery-v1")))
    monkeypatch.setattr(hosted, "CAPABILITY_HANDLERS", {})
    assert refused(monkeypatch, tmp_path, turn) == {
        "code": "CAPABILITY_UNAVAILABLE", "message": "this Hosted image has no handler for a captured capability",
        "capability": "fa-image-delivery-v1"}
    monkeypatch.setattr(hosted, "CAPABILITY_HANDLERS", {"fa-image-delivery-v1": lambda session, grant: ([], {})})
    assert refused(monkeypatch, tmp_path, turn) == {
        "code": "CAPABILITY_UNAVAILABLE", "message": "a capability handler differs from its captured tools",
        "capability": "fa-image-delivery-v1"}


def test_the_image_grant_keeps_the_latest_tool_image_for_delivery(monkeypatch, tmp_path):
    turn = client(instruction(prompt_profile=HISTORY_PROFILE, capabilities=builtin("fa-image-delivery-v1")))
    session, options = started(monkeypatch, tmp_path, turn)
    assert "deliver_image" in [spec.name for spec in options["tools"]]
    options["on_tool_image"]("UE5H")
    assert session.latest_image == "UE5H"
    assert options["capability_session"] is not None and options["capability_current"] == session._capability_current


def skills(*entries):
    return Dumped(entries=entries)


def entry(name, version="v1", metadata="m" * 64):
    return Dumped(name=name, version_id=version, metadata_sha256=metadata)


def skill_client(budget, presentation=None):
    catalog = skills(entry("formal-report"), entry("weekly-summary"))
    result = lambda name: Dumped(skill=next((item for item in catalog.entries if item.name == name), entry(name)))
    resource = lambda name, alias: Dumped(skill_version_id="v2" if alias == "moved.md" else "v1",
                                          skill_metadata_sha256="m" * 64)
    return catalog, client(instruction(prompt_profile=SKILL_RESOURCES_PROFILE, skill_catalog_budget_profile=budget),
                           skill_catalog=lambda: catalog, skill_catalog_presentation=lambda _: presentation,
                           read_skill=result, read_skill_resource=resource)


def test_the_unbudgeted_skill_catalog_is_listed_in_the_tool_and_checked_on_read(monkeypatch, tmp_path):
    catalog, turn = skill_client(None)
    _, options = started(monkeypatch, tmp_path, turn)
    reader = tool(options, "read_skill")
    assert reader.parameters["properties"]["name"]["enum"] == ["formal-report", "weekly-summary"]
    assert reader.description.endswith("\n" + catalog.model_dump_json())
    assert isinstance(call(reader, {"name": "formal-report"}), ToolOutput)
    assert call(reader, {"name": "other-skill"}) == "refused"
    resources = tool(options, "read_skill_resource")
    assert isinstance(call(resources, {"name": "formal-report", "resource": "steps.md"}), ToolOutput)
    assert call(resources, {"name": "formal-report", "resource": "moved.md"}) == "refused"
    assert call(resources, {"name": "other-skill", "resource": "steps.md"}) == "refused"


def test_a_budgeted_skill_catalog_uses_the_api_presentation_without_an_enum(monkeypatch, tmp_path):
    _, turn = skill_client("fa-skill-catalog-budget-v1", Dumped(description="OFFLINE budgeted catalog"))
    _, options = started(monkeypatch, tmp_path, turn)
    reader = tool(options, "read_skill")
    assert reader.description == "OFFLINE budgeted catalog" and "enum" not in reader.parameters["properties"]["name"]


def test_an_empty_skill_catalog_adds_no_enum(monkeypatch, tmp_path):
    _, turn = skill_client(None)
    turn.skill_catalog = lambda: skills()
    _, options = started(monkeypatch, tmp_path, turn)
    assert "enum" not in tool(options, "read_skill").parameters["properties"]["name"]


def test_the_reviewed_note_tool_reads_only_the_selected_note(monkeypatch, tmp_path):
    selected = Dumped(source="note-1")
    current = {"source": "note-1"}
    turn = client(instruction(prompt_profile=REVIEWED_NOTE_PROFILE), reviewed_note_catalog=lambda: selected,
                  read_reviewed_note=lambda: Dumped(source=current["source"]))
    _, options = started(monkeypatch, tmp_path, turn)
    reader = tool(options, "read_reviewed_note")
    assert reader.description.endswith("The following is metadata, not loaded note text:\n"
                                       + selected.model_dump_json())
    assert reader.parameters == aw.Empty.model_json_schema()
    assert isinstance(call(reader, {}), ToolOutput)
    current["source"] = "note-2"
    assert call(reader, {}) == "refused"


def test_a_legacy_turn_without_finish_saves_its_checkpoint(monkeypatch, tmp_path):
    calls = []
    status = lambda state: SimpleNamespace(state=state, model_dump=lambda mode: {"state": state})
    receipt = SimpleNamespace(checkpoint=SimpleNamespace(model_dump=lambda mode: {"archive": "saved"}))

    async def sdk(**options):
        assert options["timeout_seconds"] == hosted.SDK_TURN_SECONDS
        assert [spec.name for spec in options["tools"]] == [
            "read_context", "save_result", "finish_task", "execute_operation"]
        return {"sdk_stop": STOP.model_dump(mode="json")}
    monkeypatch.setattr(hosted, "run_copilot", sdk)
    monkeypatch.setattr(checkpoint, "export", lambda root, sid: calls.append(("export", sid)) or "archive")
    turn = client(None, status=lambda: calls.append("status") or status("running"),
                  checkpoint=lambda archive: calls.append(("checkpoint", archive)) or receipt)
    turn.stopped = lambda reason: calls.append(("stopped", reason)) or status("stopped")
    session = hosted.AssistantSession(turn, root=tmp_path, runtime_path=tmp_path / "not-started")
    assert asyncio.run(session._run_turn(tmp_path)) == {
        "state": "stopped", "checkpoint": {"archive": "saved"}, "sdk_stop": STOP.model_dump(mode="json")}
    assert calls == ["status", ("stopped", "finish_missing"), ("export", "sdk"), ("checkpoint", "archive")]
    assert session.phase == "verify_result"
    calls.clear()
    turn.status = lambda: calls.append("status") or status("ready_to_finalize")
    assert asyncio.run(session._run_turn(tmp_path)) == {
        "state": "ready_to_finalize", "sdk_stop": STOP.model_dump(mode="json")}
    assert calls == ["status"]


def test_the_work_memory_tool_offers_only_the_assigned_claim(monkeypatch, tmp_path):
    from shared_context.behavior_work_memory_prompt import WORK_MEMORY_PROFILE
    assigned = Dumped(source=Dumped(claim_id="claim-1"))
    current = {"source": assigned.source}
    turn = client(instruction(prompt_profile=WORK_MEMORY_PROFILE), work_memory_catalog=lambda: assigned,
                  read_work_memory=lambda claim: Dumped(source=current["source"], claim=claim))
    _, options = started(monkeypatch, tmp_path, turn)
    reader = tool(options, "read_work_memory")
    assert reader.parameters["properties"]["claim_id"]["enum"] == ["claim-1"]
    assert reader.description.endswith("Historical results are not current authority.\n" + assigned.model_dump_json())
    assert isinstance(call(reader, {"claim_id": "claim-1"}), ToolOutput)
    current["source"] = Dumped(claim_id="claim-2")
    assert call(reader, {"claim_id": "claim-1"}) == "refused"


def test_only_history_reads_expose_the_authority_refusal_code(monkeypatch, tmp_path):
    from shared_context.behavior_user_turn_prompt import USER_TURN_PROFILE

    def refuse(*_):
        raise Problem(403, "USER_TURN_NOT_AVAILABLE", "OFFLINE refusal")

    def broken(**_):
        raise RuntimeError("OFFLINE private detail")
    turn = client(instruction(prompt_profile=USER_TURN_PROFILE), read_user_turn=refuse)
    _, options = started(monkeypatch, tmp_path, turn)

    def failure(spec, arguments):
        with pytest.raises(ToolFailure) as error:
            asyncio.run(spec.handler({"arguments": arguments}))
        return error.value.public_code
    assert failure(tool(options, "read_user_turn"), {"turn_number": 1}) == "USER_TURN_NOT_AVAILABLE"
    turn.context = lambda **_: refuse()
    assert failure(tool(options, "read_context"), {}) is None
    turn.context = broken
    assert failure(tool(options, "read_context"), {}) is None


def hosted_tool(command):
    from shared_context.behavior_capabilities import CapabilityGrant, StdioEntry
    return CapabilityCapture(head_selection_id=None, grants=(CapabilityGrant(
        entry_id="fake", entry_sha256="0" * 64, kind="mcp-hosted", tools=("fake-report",), vision=False,
        launch=StdioEntry(type="stdio", command=command, args=(), env=())),))


def test_a_hosted_tool_the_image_does_not_provide_is_refused(monkeypatch, tmp_path):
    turn = client(instruction(prompt_profile=HISTORY_PROFILE, capabilities=hosted_tool(str(tmp_path / "missing"))))
    assert refused(monkeypatch, tmp_path, turn) == {
        "code": "CAPABILITY_UNAVAILABLE", "message": "this Hosted image does not provide a captured tool",
        "capability": "fake"}


def test_builtin_tools_first_check_that_the_work_is_still_current(monkeypatch, tmp_path):
    def stale():
        raise Problem(409, "OFFLINE_STALE", "OFFLINE stale work")
    turn = client(instruction(prompt_profile=HISTORY_PROFILE, capabilities=builtin("fa-image-delivery-v1")),
                  capability_current=stale)
    _, options = started(monkeypatch, tmp_path, turn)
    with pytest.raises(ToolFailure) as error:
        asyncio.run(tool(options, "deliver_image").handler({"arguments": {}}))
    assert error.value.public_message == (
        "The work is no longer current; do not claim completion or resend unknown effects.")
    assert error.value.public_code is None


def test_compaction_keeps_reserve_and_send_on_the_api_path(monkeypatch, tmp_path):
    from shared_context import behavior_model_direct
    from shared_context.behavior_model_direct import request_fingerprint
    monkeypatch.setattr(behavior_model_direct, "direct_eligible", lambda _: False)
    calls, seen = [], []

    async def send_model(request, **options):
        calls.append(("api-send", options))
        return "api"

    class Direct:
        async def send(self, request):
            calls.append(("direct-send", {}))
            return "direct"

    async def sdk(**options):
        seen.append(options)
        raise RuntimeError("OFFLINE SDK failure")
    monkeypatch.setattr(hosted, "run_copilot", sdk)
    turn = client(instruction(), reserve_model=lambda digest: calls.append(("api-reserve", digest)) or "reserved",
                  send_model=send_model)
    session = hosted.AssistantSession(turn, root=tmp_path, runtime_path=tmp_path / "not-started")
    with pytest.raises(Problem):
        asyncio.run(session._run_turn(tmp_path))
    boundary = seen[0]["behavior_boundary"]
    session._direct_model = Direct()

    async def exchange(body, compacting):
        session._session_files = SimpleNamespace(compacting=compacting)
        reserved = await boundary.reserve(request_fingerprint(body))
        return reserved, await boundary.sender(SimpleNamespace(content=json.dumps(body)))
    assert asyncio.run(exchange({"input": "compacting"}, True)) == ("reserved", "api")
    assert asyncio.run(exchange({"input": "ordinary"}, False)) == (None, "direct")
    assert calls == [("api-reserve", request_fingerprint({"input": "compacting"})),
                     ("api-send", {"checkpoint_compaction": True}), ("direct-send", {})]


def test_shared_role_profiles_offer_no_execute_operation(monkeypatch, tmp_path):
    from shared_context.behavior_agent_role_prompt import SHARED_ROLE_PROFILES
    for profile in sorted(SHARED_ROLE_PROFILES):
        _, options = started(monkeypatch, tmp_path / profile, client(instruction(prompt_profile=profile)))
        names = [spec.name for spec in options["tools"]]
        assert names[0] == "read_context" and "execute_operation" not in names


@pytest.mark.parametrize("change", [{"custom_tools": ()}, {"custom_tools": ("deliver_image", "other_tool")},
                                    {"feature_tools": frozenset({"read_skill"})}])
def test_registered_capability_tools_must_equal_the_captured_session_needs(monkeypatch, tmp_path, change):
    import dataclasses
    real = hosted.session_needs
    monkeypatch.setattr(hosted, "session_needs", lambda captured: dataclasses.replace(real(captured), **change))
    turn = client(instruction(prompt_profile=HISTORY_PROFILE, capabilities=builtin("fa-image-delivery-v1")))
    assert refused(monkeypatch, tmp_path, turn) == {
        "code": "INSTRUCTION_PROMPT_CONFIGURATION",
        "message": "registered capability tools differ from the captured grants"}


def test_the_session_start_gate_and_files_reach_the_sdk(monkeypatch, tmp_path):
    seen = []

    async def sdk(**options):
        seen.append(options)
        raise RuntimeError("OFFLINE SDK failure")
    monkeypatch.setattr(hosted, "run_copilot", sdk)
    session = hosted.AssistantSession(client(instruction()), root=tmp_path, runtime_path=tmp_path / "not-started",
                                      model_transport=object())
    gate, files = object(), SimpleNamespace(compacting=False)
    session._sdk_start_gate, session._session_files = gate, files
    with pytest.raises(Problem):
        asyncio.run(session._run_turn(tmp_path))
    assert seen[0]["sdk_start_gate"] is gate and seen[0]["session_files"] is files


@pytest.mark.parametrize("delegated", [False, True])
def test_a_delegating_turn_keeps_the_api_model_path(monkeypatch, tmp_path, delegated):
    from shared_context import behavior_model_direct
    marker = object()
    monkeypatch.setattr(behavior_model_direct, "direct_eligible", lambda _: True)
    monkeypatch.setattr(behavior_model_direct.DirectResponses, "from_environment",
                        classmethod(lambda cls, token, model: marker))
    seen = []

    async def sdk(**options):
        seen.append(options)
        raise RuntimeError("OFFLINE SDK failure")
    monkeypatch.setattr(hosted, "run_copilot", sdk)
    captured = builtin("fa-delegate-v1") if delegated else None
    turn = client(instruction(prompt_profile=HISTORY_PROFILE, capabilities=captured), model_token=lambda: "OFFLINE")
    session = hosted.AssistantSession(turn,
                                      root=tmp_path, runtime_path=tmp_path / "not-started")
    with pytest.raises(Problem):
        asyncio.run(session._run_turn(tmp_path))
    assert ("delegation_runtime" in seen[0]) is delegated
    assert session._direct_model is (None if delegated else marker)


@pytest.mark.parametrize("peer,cancel,code,failure", [
    (True, False, "MODEL_CALL_LIMIT_REACHED", "model_call_limit_reached"),
    (False, False, "MODEL_CALL_LIMIT_REACHED", "sdk_failed"),
    (True, False, None, "sdk_failed"),
    (True, True, "MODEL_CALL_LIMIT_REACHED", "cancelled"),
])
def test_a_stopped_sdk_failure_saves_its_named_outcome(monkeypatch, tmp_path, peer, cancel, code, failure):
    saved = []

    async def sdk(**options):
        options["on_sdk_stopped"](STOP.model_dump())
        if cancel:
            session._cancel_requested.set()
        raise Problem(503, "OFFLINE_MODEL", "OFFLINE", **({} if code is None else {"upstream_error_code": code}))
    monkeypatch.setattr(hosted, "run_copilot", sdk)
    finished = SimpleNamespace(model_dump=lambda mode: {"state": "failed"})
    turn = client(instruction(peer_work=object() if peer else None),
                  finish_failed=lambda archive, stop, reason: saved.append((archive, reason)) or finished)
    session = hosted.AssistantSession(turn, root=tmp_path, runtime_path=tmp_path / "not-started",
                                      model_transport=object())
    monkeypatch.setattr(session, "_archive", lambda: "archive")
    result = asyncio.run(session._run_turn(tmp_path))
    assert saved == [("archive", failure)]
    assert result["state"] == "failed" and result["sdk_stop"] == STOP.model_dump(mode="json")
    assert result["diagnostic"].get("upstream_error_code") == code


def test_an_unresolved_sdk_failure_cancels_the_turn_clock_before_recording_it(monkeypatch, tmp_path):
    events = []
    clock = SimpleNamespace(cancel=lambda: events.append("cancel"))

    async def sdk(**options):
        raise RuntimeError("OFFLINE SDK failure")
    monkeypatch.setattr(hosted, "run_copilot", sdk)
    turn = client(instruction())
    turn.stopped = lambda reason: events.append(("stopped", reason, hosted._hosted_turn.get()))
    session = hosted.AssistantSession(turn, root=tmp_path, runtime_path=tmp_path / "not-started",
                                      model_transport=object())

    async def run():
        token = hosted._hosted_turn.set(clock)
        try:
            await session._run_turn(tmp_path)
        finally:
            hosted._hosted_turn.reset(token)
    with pytest.raises(Problem) as error:
        asyncio.run(run())
    assert error.value.body["error"]["code"] == "ASSISTANT_SDK_UNKNOWN"
    assert events == ["cancel", ("stopped", "sdk_failed", None)]


def test_every_tool_at_once_keeps_the_tool_options_and_catalog_order(monkeypatch, tmp_path):
    """All profile tools and capability kinds in one turn; no single real profile grants them all, so the
    profile checks and prompt assembly are replaced and only the order of what reaches the SDK is pinned."""
    import sys
    from shared_context.behavior_capabilities import CapabilityGrant, StdioEntry
    profile = "offline-every-tool"
    for name in ("SKILL_RESOURCES_PROFILE", "WORK_MEMORY_PROFILE", "REVIEWED_NOTE_PROFILE", "SOP_COPY_PROFILE"):
        monkeypatch.setattr(hosted, name, profile)
    for name in ("USER_INPUT_READ_PROFILES", "FILE_READ_PROFILES", "REPLY_READ_PROFILES"):
        monkeypatch.setattr(hosted, name, {profile})
    for name in ("capability_skill_access", "skill_purpose_access", "skill_draft_enabled"):
        monkeypatch.setattr(hosted, name, lambda *_: True)
    record = SimpleNamespace(declared_capabilities=())
    monkeypatch.setattr(hosted, "assemble_prompt", lambda *_, **__: SimpleNamespace(
        record=record, system_message="OFFLINE system"))
    monkeypatch.setattr(hosted, "assemble_capabilities", lambda *_: None)
    builtins = available_grants(None, ("fa-delegate-v1", "fa-image-delivery-v1"))
    captured = CapabilityCapture(head_selection_id=None, grants=(
        CapabilityGrant(entry_id="acme-crm", entry_sha256="1" * 64, kind="mcp-api", tools=("acme-crm-lookup",),
                        vision=False),
        CapabilityGrant(entry_id="acme-local", entry_sha256="2" * 64, kind="mcp-hosted",
                        tools=("acme-local-report",), vision=False,
                        launch=StdioEntry(type="stdio", command=sys.executable, args=(), env=())),
        builtins["fa-delegate-v1"], builtins["fa-image-delivery-v1"]))
    calls = []

    def catalog(name, value):
        return lambda *_: calls.append(name) or value
    listed = SimpleNamespace(entries=(SimpleNamespace(entry_id="acme-crm", entry_sha256="1" * 64, tools=(
        SimpleNamespace(name="lookup", input_schema_sha256="3" * 64, description="OFFLINE lookup",
                        input_schema={"type": "object"}),)),))
    sop = SimpleNamespace(source_artifact_id="sop", source_version_id="v1", source_sha256="4" * 64,
                          purpose=SimpleNamespace(model_dump=lambda: {"text": "OFFLINE"}))
    turn = client(
        instruction(prompt_profile=profile, capabilities=captured, files=("OFFLINE file",),
                    text_attachment_profile="offline-attachments", attachments=("attached",),
                    skill_catalog_budget_profile="offline-budget", sop_copy=sop, peer_work=object()),
        attached_text=catalog("attached_text", Dumped(entries=("attached",))),
        skill_catalog=catalog("skill_catalog", skills(entry("formal-report"))),
        skill_catalog_presentation=catalog("skill_catalog_presentation", Dumped(description="OFFLINE budget")),
        reviewed_note_catalog=catalog("reviewed_note_catalog", Dumped(source="note-1")),
        capability_catalog=catalog("capability_catalog", listed),
        work_memory_catalog=catalog("work_memory_catalog", Dumped(source=Dumped(claim_id="claim-1"))))
    session, options = started(monkeypatch, tmp_path / "root", turn)
    assert [spec.name for spec in options["tools"]] == [
        "read_context", "execute_operation", "read_skill", "read_skill_resource", "read_user_turn",
        "read_user_file", "read_assistant_reply", "read_work_memory", "read_reviewed_note",
        "copy_sop_to_skill_draft", "save_skill_draft", "deliver_image", "acme-crm-lookup"]
    assert list(options) == [
        "state_dir", "working_directory", "sdk_session_id", "resume", "prompt", "tools", "api_url",
        "token_provider", "on_event", "runtime_path", "http_transport", "behavior_boundary", "timeout_seconds",
        "mcp_temp_directory", "on_tool_image", "user_text_files", "attached_text", "capability_session",
        "capability_current", "delegation_runtime", "system_message", "model", "instruction", "on_session_ready",
        "on_sdk_stopped", "cancel_requested", "model_call_limit"]
    assert options["delegation_runtime"] is session._delegation and options["model_call_limit"] == 100
    # The read-only catalogs run together, so only their set is fixed; the work-memory catalog follows them.
    assert calls[-1] == "work_memory_catalog" and sorted(calls[:-1]) == sorted([
        "attached_text", "skill_catalog", "skill_catalog_presentation", "reviewed_note_catalog",
        "capability_catalog"])
    assert calls.index("skill_catalog") < calls.index("skill_catalog_presentation")
