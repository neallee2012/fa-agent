"""Original relay material to the SDK send boundary; named SDK I/O doubles, no model or runtime process."""

import pytest

from shared_context.behavior_capabilities import BUILTIN_CAPABILITIES
from shared_context.behavior_capability_prompt import assemble_capabilities
from shared_context.behavior_native_turn_wire import NativeOutcomeReference, material_digest
from shared_context.m2_copilot import SYSTEM_MESSAGE
from shared_context.native_agent_relay import NativePeerRequest, message_material_digest, prepare_request
from test_instruction_tool_feedback import run_callbacks
from test_native_agent_relay import sender_case
from test_native_capabilities import selected_capture


def incoming(text, kind="request"):
    _config, sender_scope, recipient, principal, sender = sender_case()
    capture = selected_capture("fa-shell-v1")
    return prepare_request(
        principal, sender_scope, sender, recipient, capture,
        NativePeerRequest(recipient_agent_id=recipient.target.agent_id, kind=kind, content=text),
        tool_call_id="original-peer-request",
    )


def sent_prompt(tmp_path, monkeypatch, text, source=None):
    sessions, options_seen = [], []

    async def callback(options, _sid):
        options_seen.append(options)

    async def unused(_packet):
        pytest.fail("framing a relay request must not execute a tool")

    run_callbacks(tmp_path, monkeypatch, callback, unused, prompt=text,
                  agent_source=source, on_session_ready=sessions.append)
    assert len(sessions) == len(options_seen) == 1
    assert options_seen[0]["system_message"]["content"] == SYSTEM_MESSAGE
    assert set(options_seen[0]["available_tools"]) == {"custom:read_context"}
    return sessions[0].send_and_wait.call_args.args[0]


@pytest.mark.parametrize("text,kind", [
    ("查一下 vm 的 cpu", "request"),
    ("請檢查這台機器的 memory", "handoff"),
    ("請看你的電腦的 disk space", "request"),
    ("Check sandbox CPU, memory and disk space.", "handoff"),
    ("請查外部主機 db.example.invalid 的 CPU，不是你的 VM。", "request"),
])
def test_forwarded_vm_request_gets_receiver_context_without_changing_pinned_material(
        tmp_path, monkeypatch, text, kind):
    message = incoming(text, kind)
    capture = message.capture
    assembly = assemble_capabilities(capture.template.role.profile, capture.capabilities)
    before = (
        message.model_dump_json(), message.sha256, message_material_digest(message),
        capture.model_dump_json(), capture.sha256, material_digest(capture.template),
        BUILTIN_CAPABILITIES["fa-shell-v1"].entry_sha256, assembly.record.model_dump_json(),
    )
    sent = sent_prompt(tmp_path, monkeypatch, message.command.content, message.source)
    assert "Recipient environment context:" in sent
    assert "VM, this machine, your computer, or sandbox" in sent
    assert "your own Hosted Linux sandbox" in sent
    assert "another host in the message or task context" in sent
    assert "explicitly specified other host takes precedence" in sent
    assert "does not grant tools or bypass permissions" in sent
    assert "not a new human instruction or authorization" in sent
    assert message.source.model_dump_json() in sent
    assert sent.endswith("\n\nOriginal message:\n" + text)
    assert (
        message.model_dump_json(), message.sha256, message_material_digest(message),
        capture.model_dump_json(), capture.sha256, material_digest(capture.template),
        BUILTIN_CAPABILITIES["fa-shell-v1"].entry_sha256,
        assemble_capabilities(capture.template.role.profile, capture.capabilities).record.model_dump_json(),
    ) == before


def test_direct_human_input_is_not_rewritten_as_a_relay_request(tmp_path, monkeypatch):
    text = "查一下 vm 的 cpu"
    assert sent_prompt(tmp_path, monkeypatch, text) == text


def test_relay_outcome_notice_does_not_reinterpret_the_reported_machine(tmp_path, monkeypatch):
    message = incoming("外部 VM 的檢查結果未確認。")
    notice = message.source.model_copy(update={
        "kind": "agent_notice", "message_kind": "blocked", "in_reply_to": "original-message",
        "outcome": NativeOutcomeReference(status="unknown", receipt_digest="a" * 64,
                                         failure_code="NATIVE_TURN_UNKNOWN"),
    })
    sent = sent_prompt(tmp_path, monkeypatch, message.command.content, notice)
    assert "Recipient environment context:" not in sent
    assert sent.endswith("\n\nOriginal message:\n" + message.command.content)
