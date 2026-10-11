"""RTT B live text from SDK message deltas: parent, user-facing phase, exact-secret safe."""

import pytest

from shared_context.live_text import LiveText

SID = "sdk-session"


def live(secrets=()):
    held = set(secrets)

    def redact(value):
        for secret in sorted(held, key=len, reverse=True):
            value = value.replace(secret, "[REDACTED]")
        return value

    return LiveText(redact=redact, secrets=lambda: held, sdk_session_id=SID)


def start(message="m1", phase=None, agent=None):
    return {"type": "assistant.message_start", "agent_id": agent, "data": {"message_id": message, "phase": phase}}


def delta(text, message="m1", agent=None):
    return {"type": "assistant.message_delta", "agent_id": agent,
            "data": {"message_id": message, "delta_content": text}}


def final(content, message="m1", phase=None, agent=None):
    return {"type": "assistant.message", "agent_id": agent,
            "data": {"message_id": message, "content": content, "phase": phase}}


def shown(stream, events):
    return "".join(text for text in (stream.observe(event) for event in events) if text)


@pytest.mark.parametrize("phase", [None, "final_answer"])
def test_parent_user_facing_text_streams_as_it_arrives(phase):
    stream = live()
    assert stream.observe(start(phase=phase)) is None
    assert stream.observe(delta("你好")) == "你好"
    assert stream.observe(delta("，世界")) == "，世界"
    assert stream.observe(final("你好，世界", phase=phase)) is None


@pytest.mark.parametrize("events", [
    [start(phase="commentary"), delta("PRIVATE-commentary")],
    [start(phase="analysis"), delta("PRIVATE-analysis")],
    [delta("PRIVATE-no-start")],
    [start(agent="child"), delta("PRIVATE-child", agent="child")],
    [start(), {"type": "assistant.reasoning_delta", "data": {"message_id": "m1", "delta_content": "PRIVATE-r"}}],
    [start(), {"type": "assistant.message_delta", "data": {"message_id": "m1", "delta_content": {"x": 1}}}],
])
def test_private_unclassified_or_child_text_is_never_shown(events):
    assert shown(live(), events) == ""


def test_a_split_secret_is_held_until_it_can_be_decided_and_never_leaks():
    secret = "eyJSECRETTOKEN123"
    stream = live({secret})
    stream.observe(start())
    outputs = [stream.observe(delta(part)) for part in ("Use e", "yJSECRET", "TOKEN123 then e", "nd")]
    joined = "".join(text for text in outputs if text)
    assert secret[:3] not in joined.replace("[REDACTED]", "")
    assert joined == "Use [REDACTED] then end"
    assert outputs[1] is None


def test_the_complete_message_flushes_a_held_tail_that_was_not_a_secret():
    stream = live({"eyJSECRET"})
    stream.observe(start())
    assert stream.observe(delta("done, e")) == "done, "
    assert stream.observe(final("done, e")) == "e"


def test_a_complete_message_that_differs_from_the_stream_stops_without_rewriting():
    stream = live()
    stream.observe(start())
    assert stream.observe(delta("draft")) == "draft"
    assert stream.observe(final("different")) is None
    assert stream.observe(delta("more")) is None


def test_messages_are_separated_and_only_user_facing_phases_are_shown():
    stream = live()
    text = shown(stream, [start("m1"), delta("先查一下。", "m1"), final("先查一下。", "m1"),
                          start("m2", "commentary"), delta("PRIVATE", "m2"), final("PRIVATE", "m2", "commentary"),
                          start("m3", "final_answer"), delta("答案", "m3"), final("答案", "m3", "final_answer")])
    assert text == "先查一下。\n\n答案"
    assert live().observe(final("x", "unknown")) is None
