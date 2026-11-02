"""`AssistantProvider.validate` is the model request's trust boundary (ADR-0006).

Each refusal keeps its status, code, text and its place in the order; several refusals share a code, so the
text is part of the assertion. Pairs that break two rules at once pin which rule answers first.
"""
import json

import pytest

from shared_context.behavior_assistant_gateway import CAPABILITY_IMAGE_BODY_BYTES, AssistantProvider
from shared_context.errors import Problem

MODEL = "gpt-5.4"
FREE = {"text_reply": True, "model": MODEL}
RESPONSES = {**FREE, "model_provider": "openai-responses"}
PROVIDER = AssistantProvider("https://synthetic.invalid/openai/v1/chat/completions", None)

CAPABILITIES = (403, "ASSISTANT_TOOLS_DENIED", "capabilities belong only to free instructions")
CONFIGURATION = (403, "ASSISTANT_PROVIDER_CONFIGURATION", "provider must belong to this original instruction")
BOUNDED = (422, "ASSISTANT_MODEL_REQUEST_INVALID", "bounded pinned-model request is required")
LOCAL_INPUT = (422, "ASSISTANT_MODEL_REQUEST_INVALID",
               "Responses must carry complete local input without provider storage")
REASONING = (422, "ASSISTANT_MODEL_REQUEST_INVALID", "unsupported Responses reasoning options")
IMAGES = (403, "ASSISTANT_IMAGES_DENIED",
          "images are allowed only for this turn's captured image capability, within its bound")
FILE_READS = (403, "ASSISTANT_TOOLS_DENIED", "original user-turn capability is required for file reads")
REPLY_READS = (403, "ASSISTANT_TOOLS_DENIED", "original user-turn capability is required for reply reads")
SHADOW = (403, "ASSISTANT_TOOLS_DENIED", "capability tools cannot shadow a base tool")
CATALOG = (403, "ASSISTANT_TOOLS_DENIED", "only the bounded assistant tool catalog is allowed")

USER = {"role": "user", "content": "hi"}
IMAGE = {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]}


def legacy(**changes):
    return {"model": "gpt-4.1-mini", "max_tokens": 1024, "stream": True, "messages": [USER], **changes}


def free(**changes):
    return {"model": MODEL, "stream": True, "messages": [USER], **changes}


def responses(**changes):
    return {"model": MODEL, "stream": True, "store": False, "input": [USER], **changes}


def without(body, key):
    return {name: value for name, value in body.items() if name != key}


def chat_tool(name):
    return {"type": "function", "function": {"name": name}}


def responses_tool(name):
    return {"type": "function", "name": name}


def refusal(body, **options):
    with pytest.raises(Problem) as denied:
        PROVIDER.validate(body, **options)
    error = denied.value.body["error"]
    return denied.value.status, error["code"], error["message"]


def sized(body, target, *, ensure_ascii):
    """`body` padded so its measured JSON is exactly `target` bytes."""
    empty = {**body, "messages": [{"role": "user", "content": ""}]}
    room = target - len(json.dumps(empty, ensure_ascii=ensure_ascii).encode())
    return {**body, "messages": [{"role": "user", "content": "x" * room}]}


@pytest.mark.parametrize("body,options", [
    (legacy(), {}),
    (legacy(n=1, temperature=0, top_p=1, stop=[], store=False, stream_options={}, parallel_tool_calls=False,
            presence_penalty=0, frequency_penalty=0, max_completion_tokens=8, reasoning_effort="low",
            tool_choice="auto"), {}),
    (free(), FREE),
    (without(free(), "model"), {"text_reply": True}),
    (responses(), RESPONSES),
    (responses(instructions="x", tool_choice="auto", reasoning=None, max_output_tokens=8, temperature=0, top_p=1,
               parallel_tool_calls=True, prompt_cache_key="key", include=[]), RESPONSES),
    (responses(include=["reasoning.encrypted_content"]), RESPONSES),
    (responses(input=[{"role": "user", "content": "x"}, {"type": "message"}, {"type": "function_call"},
                      {"type": "function_call_output"}, {"type": "reasoning"}]), RESPONSES),
    (responses(reasoning={}), RESPONSES),
    (legacy(messages=[USER] * 128), {}),
    (free(messages=[USER] * 129), FREE),
])
def test_complete_requests_pass(body, options):
    assert PROVIDER.validate(body, **options) is None


@pytest.mark.parametrize("effort", [None, "none", "minimal", "low", "medium", "high", "xhigh"])
@pytest.mark.parametrize("summary", [None, "auto", "concise", "detailed"])
def test_every_listed_reasoning_option_passes(effort, summary):
    assert PROVIDER.validate(responses(reasoning={"effort": effort, "summary": summary}), **RESPONSES) is None


@pytest.mark.parametrize("options", [
    {"capability_tools": frozenset({"web"})},
    {"allow_images": True},
])
def test_capabilities_need_a_free_instruction(options):
    assert refusal(legacy(), **options) == CAPABILITIES


@pytest.mark.parametrize("options", [
    {"model_provider": "openai-responses"},
    {"model_provider": "openai"},
    {"model_provider": ""},
    {"text_reply": True, "model": MODEL, "model_provider": "openai"},
])
def test_provider_must_be_the_responses_provider_on_a_free_instruction(options):
    assert refusal(legacy(), **options) == CONFIGURATION


@pytest.mark.parametrize("body,options", [
    (legacy(unknown=1), {}),
    (free(unknown=1), FREE),
    (responses(unknown=1), RESPONSES),
    *[(legacy(**{field: 1}), {}) for field in (
        "input", "instructions", "reasoning", "max_output_tokens", "prompt_cache_key", "include")],
    *[(responses(**{field: 1}), RESPONSES) for field in (
        "messages", "stream_options", "max_tokens", "n", "stop", "presence_penalty", "frequency_penalty",
        "max_completion_tokens", "reasoning_effort")],
    (legacy(model="gpt-4.1"), {}),
    (without(legacy(), "model"), {}),
    (free(), {"text_reply": True, "model": "other"}),
    (free(model="gpt-4.1-mini"), FREE),
    (responses(model="other"), RESPONSES),
    (legacy(max_tokens=1023), {}),
    (without(legacy(), "max_tokens"), {}),
    (legacy(stream=False), {}),
    (legacy(stream="true"), {}),
    (without(legacy(), "stream"), {}),
    (free(stream=1), FREE),
    (responses(stream=False), RESPONSES),
    (without(legacy(), "messages"), {}),
    (legacy(messages="hi"), {}),
    (legacy(messages=[]), {}),
    (free(messages=[]), FREE),
    (free(messages={"role": "user"}), FREE),
    (without(responses(), "input"), RESPONSES),
    (responses(input=[]), RESPONSES),
    (responses(input="hi"), RESPONSES),
    (legacy(messages=[USER] * 129), {}),
    (legacy(n=2), {}),
    (free(n=0), FREE),
])
def test_unbounded_or_unpinned_requests_are_refused(body, options):
    assert refusal(body, **options) == BOUNDED


@pytest.mark.parametrize("make,options,limit,ensure_ascii", [
    (legacy, {}, 24000, True),
    (free, FREE, 128 * 1024, False),
    (free, {**FREE, "allow_images": True}, CAPABILITY_IMAGE_BODY_BYTES, False),
])
def test_each_profile_keeps_its_exact_body_limit(make, options, limit, ensure_ascii):
    assert PROVIDER.validate(sized(make(), limit, ensure_ascii=ensure_ascii), **options) is None
    assert refusal(sized(make(), limit + 1, ensure_ascii=ensure_ascii), **options) == BOUNDED


def test_legacy_measures_escaped_ascii_and_free_measures_utf8():
    assert refusal(legacy(messages=[{"role": "user", "content": "中" * 4000}])) == BOUNDED
    assert PROVIDER.validate(free(messages=[{"role": "user", "content": "中" * 40000}]), **FREE) is None


def test_size_is_measured_only_after_the_cheaper_checks():
    assert refusal(legacy(stream=False, temperature=object())) == BOUNDED
    assert refusal(legacy(n=2, temperature=object())) == BOUNDED
    assert refusal(legacy(messages=[], temperature=object())) == BOUNDED
    assert refusal(legacy(messages=[USER] * 129, temperature=object())) == BOUNDED
    with pytest.raises(TypeError):
        PROVIDER.validate(legacy(temperature=object()))


@pytest.mark.parametrize("changes", [
    {"store": True},
    {"store": None},
    {"store": 0},
    {"include": ["other"]},
    {"include": ["reasoning.encrypted_content", "other"]},
    {"include": "reasoning.encrypted_content"},
    {"include": None},
    {"input": ["hi"]},
    {"input": [USER, 1]},
    {"input": [{"type": "item_reference"}]},
    {"input": [USER, {"type": None}]},
])
def test_responses_keep_input_local(changes):
    assert refusal(responses(**changes), **RESPONSES) == LOCAL_INPUT
    assert refusal(without(responses(**changes), "store"), **RESPONSES) == LOCAL_INPUT


@pytest.mark.parametrize("reasoning", [
    "high",
    [],
    0,
    {"unknown": 1},
    {"effort": "low", "unknown": 1},
    {"summary": "none"},
    {"effort": "maximum"},
    {"summary": ["auto"]},
    {"effort": {"level": "low"}},
])
def test_responses_reasoning_stays_on_the_listed_options(reasoning):
    assert refusal(responses(reasoning=reasoning), **RESPONSES) == REASONING


def test_images_are_read_from_the_input_the_mode_actually_sends():
    assert refusal(free(messages=[IMAGE]), **FREE) == IMAGES
    assert refusal(responses(input=[IMAGE]), **RESPONSES) == IMAGES
    assert PROVIDER.validate(free(messages=[IMAGE]), **FREE, allow_images=True) is None
    assert PROVIDER.validate(responses(input=[IMAGE]), **RESPONSES, allow_images=True) is None


@pytest.mark.parametrize("name", ["read_context", "save_result", "finish_task", "execute_operation"])
def test_base_tools_pass_in_every_mode(name):
    assert PROVIDER.validate(legacy(tools=[chat_tool(name)])) is None
    assert PROVIDER.validate(free(tools=[chat_tool(name)]), **FREE) is None
    assert PROVIDER.validate(responses(tools=[responses_tool(name)]), **RESPONSES) is None


ACCESS_TOOLS = [
    ("read_skill", {"skill_access": True}),
    ("read_skill_resource", {"skill_access": True, "skill_resource_access": True}),
    ("read_user_turn", {"user_turn_access": True}),
    ("read_user_file", {"user_turn_access": True, "user_file_access": True}),
    ("read_assistant_reply", {"user_turn_access": True, "assistant_reply_access": True}),
    ("read_work_memory", {"work_memory_access": True}),
    ("read_reviewed_note", {"reviewed_note_access": True}),
    ("copy_sop_to_skill_draft", {"sop_copy_access": True}),
    ("save_skill_draft", {"skill_draft_access": True}),
]
ALL_ACCESS = {key: True for _, flags in ACCESS_TOOLS for key in flags}


@pytest.mark.parametrize("name,flags", ACCESS_TOOLS)
def test_each_access_flag_opens_only_its_own_tool(name, flags):
    assert PROVIDER.validate(free(tools=[chat_tool(name)]), **FREE, **flags) is None
    assert refusal(free(tools=[chat_tool(name)]), **FREE) == CATALOG
    other = {key: True for tool, needed in ACCESS_TOOLS if tool != name for key in needed}
    if not set(flags) <= set(other):
        assert refusal(free(tools=[chat_tool(name)]), **FREE, **other) == CATALOG


@pytest.mark.parametrize("name,flags", [
    (name, {key: value for key, value in flags.items() if key != "user_turn_access"})
    for name, flags in ACCESS_TOOLS if name not in {"read_user_file", "read_assistant_reply"}
])
def test_a_legacy_request_gains_no_access_tool(name, flags):
    assert refusal(legacy(tools=[chat_tool(name)]), **flags, user_turn_access=True) == CATALOG


def test_skill_resource_tool_also_needs_skill_access():
    assert refusal(free(tools=[chat_tool("read_skill_resource")]), **FREE, skill_resource_access=True) == CATALOG
    assert refusal(free(tools=[chat_tool("read_skill")]), **FREE, skill_resource_access=True) == CATALOG


@pytest.mark.parametrize("flag,expected", [
    ("user_file_access", FILE_READS),
    ("assistant_reply_access", REPLY_READS),
])
def test_past_turn_reads_need_the_user_turn_capability_on_a_free_instruction(flag, expected):
    assert refusal(free(), **FREE, **{flag: True}) == expected
    assert refusal(legacy(), **{flag: True}) == expected
    assert refusal(legacy(), **{flag: True}, user_turn_access=True) == expected


def test_capability_tools_join_the_catalog_but_cannot_shadow_it():
    web = frozenset({"web"})
    assert PROVIDER.validate(free(tools=[chat_tool("web")]), **FREE, capability_tools=web) is None
    assert PROVIDER.validate(responses(tools=[responses_tool("web")]), **RESPONSES, capability_tools=web) is None
    assert refusal(free(), **FREE, capability_tools=frozenset({"web", "read_context"})) == SHADOW
    assert refusal(free(), **FREE, capability_tools=frozenset({"read_skill"}), skill_access=True) == SHADOW
    assert PROVIDER.validate(free(), **FREE, capability_tools=frozenset({"read_skill"})) is None


def test_only_legacy_requests_cap_the_tool_count():
    tool = chat_tool("read_context")
    assert PROVIDER.validate(legacy(tools=[tool] * 4)) is None
    assert refusal(legacy(tools=[tool] * 5)) == CATALOG
    assert PROVIDER.validate(free(tools=[tool] * 5), **FREE) is None
    assert PROVIDER.validate(responses(tools=[responses_tool("read_context")] * 5), **RESPONSES) is None


@pytest.mark.parametrize("tools", [
    "read_context",
    None,
    {"type": "function"},
    ["read_context"],
    [None],
    [{"function": {"name": "read_context"}}],
    [{"type": "custom", "function": {"name": "read_context"}}],
    [{"type": "function"}],
    [{"type": "function", "function": {}}],
    [{"type": "function", "name": "read_context"}],
    [chat_tool("shell")],
    [chat_tool("read_context"), chat_tool("shell")],
    [chat_tool("read_context"), None],
])
def test_chat_tools_outside_the_catalog_are_refused(tools):
    assert refusal(legacy(tools=tools)) == CATALOG
    assert refusal(free(tools=tools), **FREE) == CATALOG


@pytest.mark.parametrize("tools", [
    "read_context",
    {"type": "function"},
    [None],
    [{"name": "read_context"}],
    [{"type": "custom", "name": "read_context"}],
    [{"type": "function"}],
    [chat_tool("read_context")],
    [responses_tool(1)],
    [responses_tool(["read_context"])],
    [responses_tool({"name": "read_context"})],
    [responses_tool("shell")],
    [responses_tool("read_context"), responses_tool("shell")],
])
def test_responses_tools_outside_the_catalog_are_refused(tools):
    assert refusal(responses(tools=tools), **RESPONSES) == CATALOG


@pytest.mark.parametrize("body,options,expected", [
    # capability, then provider
    (legacy(), {"allow_images": True, "model_provider": "openai"}, CAPABILITIES),
    (legacy(), {"capability_tools": frozenset({"web"}), "model_provider": "openai"}, CAPABILITIES),
    # provider, then the bounded request
    (legacy(unknown=1), {"model_provider": "openai"}, CONFIGURATION),
    (free(unknown=1), {**FREE, "model_provider": "openai"}, CONFIGURATION),
    # bounded request, then Responses fields
    (responses(unknown=1, store=True), RESPONSES, BOUNDED),
    (responses(stream=False, reasoning="high"), RESPONSES, BOUNDED),
    # store, then each input item
    (responses(store=True, reasoning="high"), RESPONSES, LOCAL_INPUT),
    (responses(input=[1], reasoning="high"), RESPONSES, LOCAL_INPUT),
    (responses(include=["other"], reasoning="high"), RESPONSES, LOCAL_INPUT),
    # Responses fields, then images
    (responses(store=True, input=[IMAGE]), RESPONSES, LOCAL_INPUT),
    (responses(reasoning="high", input=[IMAGE]), RESPONSES, REASONING),
    # bounded request, then images and tools
    (free(n=2, messages=[IMAGE]), FREE, BOUNDED),
    (legacy(n=2, tools=[chat_tool("shell")]), {}, BOUNDED),
    # images, then every tool refusal
    (free(messages=[IMAGE]), {**FREE, "user_file_access": True}, IMAGES),
    (free(messages=[IMAGE]), {**FREE, "assistant_reply_access": True}, IMAGES),
    (free(messages=[IMAGE]), {**FREE, "capability_tools": frozenset({"read_context"})}, IMAGES),
    (free(messages=[IMAGE], tools=[chat_tool("shell")]), FREE, IMAGES),
    # file reads, reply reads, shadowing, catalog
    (free(), {**FREE, "user_file_access": True, "assistant_reply_access": True}, FILE_READS),
    (free(), {**FREE, "assistant_reply_access": True, "capability_tools": frozenset({"read_context"})}, REPLY_READS),
    (free(tools=[chat_tool("shell")]), {**FREE, "user_file_access": True}, FILE_READS),
    (free(tools=[chat_tool("shell")]), {**FREE, "capability_tools": frozenset({"read_context"})}, SHADOW),
])
def test_a_request_breaking_two_rules_gets_the_earlier_refusal(body, options, expected):
    assert refusal(body, **options) == expected
