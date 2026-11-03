"""API and Hosted keep their package boundaries on a cold import (AGENTS.md rule 9).

Each side is one interpreter for its whole module list; a failure names the module and the banned import it reached.
"""

from import_boundaries import import_violations

HOSTED_SDK = ("copilot", "azure.ai.agentserver")

# The API process must never load the Hosted SDK, and its cold import opens no connection.
API_MODULES = {
    "shared_context.memory_relay": ("azure.cosmos",),  # first: nothing before it may load azure.cosmos
    "shared_context.behavior_agent_message_refs": (),
    "shared_context.behavior_agent_template": (),
    "shared_context.behavior_work_policy_api": (),
    "shared_context.behavior_fresh_operator": (),
    "shared_context.behavior_server": (),
    "shared_context.native_hitl_wire": (),
    "shared_context.native_hitl": (),
    "shared_context.native_hitl_api": (),
    "shared_context.native_routine_proposals": (),
    "shared_context.native_routines": (),
    "shared_context.behavior_native_turn_api": (),
    "shared_context.behavior_native_capture": (),
    "shared_context.native_routine_api": (),
    "shared_context.native_routine_clock": (),
    "shared_context.native_cut": (),
    "shared_context.behavior_native_turn_wire": (),
    "shared_context.behavior_routine": (),
    "shared_context.behavior_assistant_wire": (),
    "shared_context.behavior_assistant_api": (),
    "shared_context.behavior_sdk_compaction": (),
    "shared_context.behavior_sdk_state": (),
    "shared_context.behavior_skill_copy": (),
    "shared_context.behavior_skill_prompt": (),
    "shared_context.behavior_system_prompt_view": (),
    "shared_context.behavior_turn_context": (),
    "web_test_console.freeform": (),
}

API_STORAGE = ("fastapi", "azure.data.tables", "azure.storage.blob")

# Hosted-side modules must not need API-only packages; each keeps the bans its own contract names.
HOSTED_MODULES = {
    "shared_context.behavior_turn_usage": (
        "fastapi", "copilot", "shared_context.behavior_assistant", "shared_context.m2_copilot"),
    "shared_context.behavior_turn_wire": (*API_STORAGE, "copilot"),
    "shared_context.behavior_routine_wire": (*API_STORAGE, "copilot"),
    "shared_context.behavior_agent_message_wire": ("fastapi", "starlette", "mcp"),
    "shared_context.tool_catalog": ("mcp",),
    "shared_context.behavior_agent_message_tools": (
        *API_STORAGE, "azure.ai.projects", "shared_context.behavior_runtime",
        "shared_context.behavior_agent_message_refs"),
}


def test_api_modules_import_without_the_hosted_sdk_or_network():
    cases = [(module, HOSTED_SDK + extra) for module, extra in API_MODULES.items()]
    assert import_violations(cases, offline=True) == {}


def test_hosted_modules_import_without_api_only_packages():
    assert import_violations(HOSTED_MODULES.items()) == {}
