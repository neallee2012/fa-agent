"""Exact existing source attachments through admission, signed reads and SDK I/O."""

import copy
import hashlib
import json
from dataclasses import replace
from datetime import timedelta
from types import MappingProxyType, SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from shared_context import m2_copilot
from shared_context.behavior_assistant import AssistantProfile, _LocalHttp
from shared_context.behavior_assistant_api import create_assistant_app
from shared_context.behavior_assistant_hosted import AssistantClient
from shared_context.behavior_assistant_server import _CurrentAssistantProvider
from shared_context.behavior_assistant_wire import InstructionSubmit
from shared_context.behavior_attachments import TextAttachment
from shared_context.behavior_runtime import runtime_key
from shared_context.behavior_work_policy import WorkPreflightQuery
from shared_context.behavior_work_policy_api import install_work_policy_routes
from shared_context.behavior_wire import Command, ExactSourceRef, SourcePrepare
from shared_context.errors import Problem
from shared_context.behavior_turn_context import TurnContext
from shared_context.source_materials import SourceWithdrawalInput
from test_behavior_assistant import assistant
from test_instruction_flow import check_history, instruction_models, runtime_path, sdk_io
from test_m3_auth import jwk_client, signing_key
from test_work_policy_lifecycle import enabled

BODY = "  ATTACHED_SOURCE_CANARY\r\n原始\u3000文字與數值42。\n"


def configured_attachments(h, *, active=True, successor=None, expires=False, necessary=(), visibility="team"):
    h.policy = h.policy.model_copy(update={"human_text_enabled": True, "text_policy_version": "human-text-policy-v1"})
    h.mutate(runtime_key("policy", h.binding.binding_id), human_text_enabled=True, text_policy_version="human-text-policy-v1")
    h.runtime.profiles = MappingProxyType({
        key: AssistantProfile.model_validate({**value.model_dump(), "prompt_profile": "fa-instruction-v1",
            **({"text_attachment_profile": "fa-source-text-attachments-v1"} if active else {})})
        for key, value in h.runtime.profiles.items()
    })
    h.sources._clock = lambda: h.time
    attachment = add_source(h, BODY, expires_at=h.time + timedelta(seconds=1) if expires else None, necessary=necessary)
    if successor is not None:
        add_source(h, successor, version=2)
    if visibility != "team":
        h.policy = h.policy.model_copy(update={"source_visibility": visibility})
        h.mutate(runtime_key("policy", h.binding.binding_id), source_visibility=visibility)
    h, control, human, _ = enabled(h)
    install_work_policy_routes(h.app, SimpleNamespace(
        runtime=h.runtime, verifier=h.verifier, installation=lambda: None,
        require_current_native_evidence=lambda: None,
    ))
    return h, control, human, attachment


def add_source(h, text, version=1, expires_at=None, necessary=()):
    raw = text.encode("utf-8")
    command = SourcePrepare(
        request_id=f"attachment-source-{version}", version=version, original=raw, source_schema="fa-human-text-v1",
        retention_consent_sha256=hashlib.sha256(raw).hexdigest(), expires_at=expires_at, necessary=necessary,
    )
    response = h.post("/sources/dependency/prepare", command)
    assert response.status_code == 200, response.text
    response = h.post("/sources/dependency/commit", Command(request_id=command.request_id))
    assert response.status_code == 200, response.text
    return TextAttachment(name="reference.txt", source=ExactSourceRef(
        source_id="dependency", version=version, sha256=hashlib.sha256(raw).hexdigest(), byte_length=len(raw),
    ))


def post(h, command, route="submit"):
    return h.client.post("/work-policy/v1/" + route, content=command.model_dump_json(), headers=h.headers())


def withdraw(h, control, human, attachment):
    context = h.runtime._source_context(control.policy.template, control.policy.legacy_template_policy, human)
    reference = h.runtime._reference(attachment.source, control.policy.template, control.policy.legacy_template_policy, human)
    h.sources.withdraw_source(human, context, SourceWithdrawalInput(
        withdrawal_id="withdraw-attachment", reference=reference, expected_access_revision=1,
        reason="The original human withdrew this attachment.",
    ))


def open_attached_turn(h, attachment):
    accepted = post(h, InstructionSubmit(request_id="attachment-current-turn", content="Read the original source.",
                                         attachments=(attachment,)))
    assert accepted.status_code == 200, accepted.text
    binding = h.runtime.bindings[accepted.json()["binding_id"]]
    starts = []
    h.workers.adapter = replace(h.workers.adapter, start=starts.append)
    h.workers.start(binding.job_id, binding.owner)
    assignment, ticket = h.assistant.issue(starts[0])
    client = AssistantClient("https://context.invalid", assignment, ticket,
        token_provider=lambda: h.headers("agent")["Authorization"].removeprefix("Bearer "),
        transport=_LocalHttp(h.client))
    client.acquire()
    return SimpleNamespace(client=client, assignment=assignment, binding=binding)


def test_exact_attachment_is_saved_and_original_request_reconciles_without_replacement(assistant):
    h, control, _, attachment = configured_attachments(assistant)
    command = InstructionSubmit(request_id="with-attachment", content="Read my attached source.", attachments=(attachment,))
    query = WorkPreflightQuery(read_id="attachment-preflight", request_id=command.request_id,
        submit_body_sha256=hashlib.sha256(command.model_dump_json().encode()).hexdigest(),
        content=command.content, attachments=command.attachments)
    assert post(h, query, "preflight").status_code == 200
    accepted = post(h, command)
    assert accepted.status_code == 200, accepted.text
    record = control.records[accepted.json()["binding_id"]]
    assert record.profile.instruction.attachments == (attachment,)
    before = h.store._guard()
    assert post(h, command).status_code == 200
    assert post(h, command, "submit-reconcile").status_code == 200
    assert h.store._guard() == before and not h.endpoint.requests
    changed = command.model_copy(update={"attachments": (attachment.model_copy(update={"name": "changed.txt"}),)})
    assert post(h, changed).status_code == 409
    check_history(h)


def test_attachment_profile_is_explicit_and_old_request_serialization_stays_unchanged(assistant):
    h, control, _, attachment = configured_attachments(assistant, active=False)
    command = InstructionSubmit(request_id="old-no-attachments", content="Original plain text.")
    assert "attachments" not in command.model_dump(mode="json")
    before = h.store._guard()
    refused = post(h, command.model_copy(update={"attachments": (attachment,)}))
    assert refused.status_code == 403 and refused.json()["error"]["code"] == "ATTACHMENTS_DISABLED"
    assert h.store._guard() == before and not control.records
    assert post(h, command).status_code == 200


@pytest.mark.parametrize("field,value,status", [
    ("source_id", "not-in-original-binding", 403), ("sha256", "f" * 64, 409), ("byte_length", 1, 409),
])
def test_unavailable_or_mismatched_sources_do_not_create_registration(assistant, field, value, status):
    h, control, _, attachment = configured_attachments(assistant)
    changed = attachment.model_copy(update={"source": attachment.source.model_copy(update={field: value})})
    before = h.store._guard()
    response = post(h, InstructionSubmit(request_id="invalid-attachment", content="Read source.", attachments=(changed,)))
    assert response.status_code == status, response.text
    assert h.store._guard() == before and not control.records and not h.endpoint.requests


def test_explicit_attachment_reaches_sdk_after_the_unchanged_user_text_and_cold_resume_keeps_refs(
    assistant, monkeypatch, tmp_path,
):
    h, control, _, attachment = configured_attachments(assistant)
    calls = sdk_io(monkeypatch)
    factory, sent = m2_copilot.CopilotClient.side_effect, []
    def capture(**connection):
        client = factory(**connection)
        opened = client.create_session.side_effect
        async def session(*args, **options):
            value = await opened(*args, **options)
            send = value.send.side_effect
            async def sending(prompt, **kwargs):
                sent.append((prompt, copy.deepcopy(kwargs.get("attachments"))))
                return await send(prompt, **kwargs)
            value.send.side_effect = sending
            return value
        client.create_session.side_effect = session
        client.resume_session.side_effect = session
        return client
    m2_copilot.CopilotClient.side_effect = capture
    h.endpoint.unbounded = True
    h.endpoint.replies = iter([{"content": "Original attachment acknowledged."}, {"content": "Follow-up complete."}])
    first = post(h, InstructionSubmit(request_id="attachments-first", content="  Use the attached text.\r\n",
                                     attachments=(attachment,)))
    assert first.status_code == 200, first.text
    accepted = first.json()
    assert h.runtime.controller.step(accepted["binding_id"]).state == "succeeded"
    original = control.records[accepted["binding_id"]].model_dump_json()
    control.records.clear()
    control.refresh()
    h.executor.root = tmp_path / "cold-attachment-sdk"
    second = post(h, InstructionSubmit(request_id="attachments-second", content="Continue the same source.",
                                      conversation_id=accepted["conversation_id"]))
    assert second.status_code == 200, second.text
    assert h.runtime.controller.step(second.json()["binding_id"]).state == "succeeded"
    # G-D: the original text stays first; the file follows as a fixed block, not as an SDK attachment.
    assert sent == [("  Use the attached text.\r\n\n\n" + m2_copilot._TEXT_FILES_HEADER + "\n"
                     + m2_copilot._text_file_block("reference.txt", BODY), None), ("Continue the same source.", None)]
    assert calls[0]["prompt"] == sent[0][0]
    assert calls[1]["resume"] and calls[1]["sdk_session_id"] == calls[0]["sdk_session_id"]
    assert control.records[accepted["binding_id"]].model_dump_json() == original
    context = h.client.get(f"/work-policy/v1/conversations/{accepted['conversation_id']}/turns/1/context",
                           headers=h.headers())
    assert context.status_code == 200, context.text
    metadata = TurnContext.model_validate_json(context.content)
    assert metadata.attachments == (attachment,)
    assert "ATTACHED_SOURCE_CANARY" not in context.text
    check_history(h)


def test_withdrawn_historical_attachment_blocks_new_turn_without_replacing_old_result(assistant, monkeypatch):
    h, control, human, attachment = configured_attachments(assistant)
    sdk_io(monkeypatch)
    h.endpoint.unbounded = True
    h.endpoint.replies = iter([{"content": "Saved first response."}])
    command = InstructionSubmit(request_id="attach-before-withdraw", content="Use the original.", attachments=(attachment,))
    accepted = post(h, command).json()
    assert h.runtime.controller.step(accepted["binding_id"]).state == "succeeded"
    withdraw(h, control, human, attachment)
    before = h.store._guard()
    refused = post(h, InstructionSubmit(request_id="attach-after-withdraw", content="Continue.",
                                       conversation_id=accepted["conversation_id"]))
    assert refused.status_code == 403 and refused.json()["error"]["code"] == "SOURCE_WITHDRAWN"
    assert post(h, command, "submit-reconcile").status_code == 200
    assert h.store._guard() == before and len(h.endpoint.requests) == 1


def test_attachment_read_keeps_exact_historical_version_when_source_advances(assistant):
    updated = "NEW_SOURCE_VERSION_MUST_NOT_REPLACE_ORIGINAL"
    h, control, _, original = configured_attachments(assistant, successor=updated)
    turn = open_attached_turn(h, original)
    try:
        before = h.store._guard()
        response = turn.client.attached_text()
        assert response.entries[0].attachment == original
        assert response.entries[0].text == BODY
        assert response.entries[0].attachment.source.sha256 != hashlib.sha256(updated.encode()).hexdigest()
        assert h.store._guard() == before and not h.endpoint.requests
    finally:
        turn.client.close()


def test_attachment_withdrawal_after_consume_is_refused_at_the_next_reserve_without_resend(assistant):
    """consume is the last check before send: a withdrawal after it lets only that one request out (seconds-level
    window); the same request is never resent and the next reserve is refused."""
    h, control, human, attachment = configured_attachments(assistant)
    sent = []
    def credential(_):
        withdraw(h, control, human, attachment)
        return SimpleNamespace(token="OFFLINE_ATTACHMENT_EGRESS")
    def provider(request):
        sent.append(True)
        return httpx.Response(200, content=b'data: {"choices":[{"delta":{"content":"in-flight"}}]}\n\n'
                              b'data: [DONE]\n\n', headers={"content-type": "text/event-stream"})
    gateway = _CurrentAssistantProvider(SimpleNamespace(config=SimpleNamespace(assistant=SimpleNamespace(
        provider_endpoint="https://provider.invalid/openai/v1/chat/completions")), runtime=h.runtime),
        SimpleNamespace(get_token=credential), transport=httpx.MockTransport(provider))
    h.app = create_assistant_app(h.assistant, h.verifier, provider=gateway)
    install_work_policy_routes(h.app, SimpleNamespace(runtime=h.runtime, verifier=h.verifier,
        installation=lambda: None, require_current_native_evidence=lambda: None))
    with TestClient(h.app) as client:
        h.client = client
        turn = open_attached_turn(h, attachment)
        try:
            assert turn.client.attached_text().entries[0].text == BODY
            def request(label):
                body = {"model": "gpt-4.1-mini", "stream": True, "messages": [{"role": "user", "content": label}]}
                return body, hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            body, request_id = request("Use source.")
            turn.client.reserve_model(request_id)
            response = turn.client.model(httpx.Request("POST", "https://context.invalid/model/v1/chat/completions", json=body))
            assert response.status_code == 200 and sent == [True]
            retained = h.store._guard()
            with pytest.raises(Problem) as denied:
                turn.client.reserve_model(request("A later model step.")[1])
            assert denied.value.body["error"]["upstream_error_code"] == "SOURCE_WITHDRAWN"
            assert h.store._guard() == retained
            with pytest.raises(Problem):
                turn.client.model(httpx.Request("POST", "https://context.invalid/model/v1/chat/completions", json=body))
            assert sent == [True]
        finally:
            turn.client.close()


def test_expired_attachment_is_refused_before_work_creation(assistant):
    h, control, _, attachment = configured_attachments(assistant, expires=True)
    h.time += timedelta(seconds=2)
    before = h.store._guard()
    response = post(h, InstructionSubmit(request_id="expired-attachment", content="Read source.", attachments=(attachment,)))
    assert response.status_code == 409 and response.json()["error"]["code"] == "SOURCE_EXPIRED"
    assert h.store._guard() == before and not control.records and not h.endpoint.requests


def test_withdrawn_necessary_source_denies_the_attachment_without_new_work(assistant):
    source = assistant.runtime.profiles[assistant.binding.binding_id].source
    h, control, human, attachment = configured_attachments(assistant, necessary=(source,))
    withdraw(h, control, human, TextAttachment(name="necessary.json", source=source))
    before = h.store._guard()
    response = post(h, InstructionSubmit(request_id="withdrawn-attachment-dependency",
                                        content="Use the original derived source.", attachments=(attachment,)))
    assert response.status_code == 403 and response.json()["error"]["code"] == "SOURCE_WITHDRAWN"
    assert h.store._guard() == before and not control.records and not h.endpoint.requests


def test_duplicate_aliases_are_refused_without_guessing_which_source(assistant):
    _, _, _, attachment = configured_attachments(assistant)
    with pytest.raises(ValueError):
        InstructionSubmit(request_id="duplicate-aliases", content="Read sources.", attachments=(attachment, attachment))


def test_template_exports_only_the_attachment_profile_not_source_content(assistant):
    from shared_context.behavior_agent_template import export_template, inspect_template
    h, control, human, _ = configured_attachments(assistant)
    before = h.store._guard()
    template = export_template(control, h.assistant, human)
    assert template.settings.text_attachment_profile == "fa-source-text-attachments-v1"
    assert "ATTACHED_SOURCE_CANARY" not in template.model_dump_json()
    assert not template.authorization and not inspect_template(template, template).activated
    assert h.store._guard() == before and not control.records


def test_no_attachments_does_not_consult_an_unused_private_source_scope(assistant, monkeypatch):
    h, control, _, _ = configured_attachments(assistant, visibility="private")
    calls = sdk_io(monkeypatch)
    h.endpoint.unbounded = True
    h.endpoint.replies = iter([{"content": "Plain instruction without source access."}])
    monkeypatch.setattr(h.sources, "read_graph", lambda *args, **kwargs: pytest.fail("No attachment sources to read"))
    response = post(h, InstructionSubmit(request_id="empty-private-attachments", content="Answer without using any source."))
    assert response.status_code == 200, response.text
    assert h.runtime.controller.step(response.json()["binding_id"]).state == "succeeded"
    assert calls[0]["prompt"] == "Answer without using any source."
    assert control.records[response.json()["binding_id"]].profile.instruction.attachments == ()


def test_actual_private_attachment_is_still_denied_before_any_source_read(assistant, monkeypatch):
    h, control, _, attachment = configured_attachments(assistant, visibility="private")
    monkeypatch.setattr(h.sources, "read_graph", lambda *args, **kwargs: pytest.fail("Private attachment is not supported"))
    before = h.store._guard()
    response = post(h, InstructionSubmit(request_id="actual-private-attachment", content="Read source.",
                                        attachments=(attachment,)))
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "ATTACHMENT_SOURCE_DENIED"
    assert h.store._guard() == before and not control.records and not h.endpoint.requests


@pytest.mark.parametrize("name", ["../source", "/source.txt", "C:\\source.txt", "https://example.invalid/source"])
def test_attachment_names_cannot_be_file_paths_or_urls(name):
    with pytest.raises(ValueError):
        TextAttachment(name=name, source=ExactSourceRef(source_id="allowed", version=1, sha256="a" * 64, byte_length=1))
