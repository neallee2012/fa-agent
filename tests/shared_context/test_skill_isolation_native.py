"""Bounded task-local discovery witness and FA policy probe; scripted provider only."""

import asyncio
import hashlib
from importlib import metadata

import pytest
from copilot import CopilotRequestHandler

from shared_context.behavior_skill_prompt import SKILL_CAPABILITIES
from shared_context.sdk_pin import SDK_VERSION
from instruction_flow_helpers import instruction_models
from test_m3_auth import jwk_client, signing_key
from skill_isolation_native_helpers import child_environment, digest, CANARY_NAME, require, CANARY_MARKER


CANARY_TEXT = (
    "---\nname: " + CANARY_NAME + "\ndescription: Text-only task-local isolation canary.\n---\n"
    + CANARY_MARKER + "\nInert visibility fixture. Do not execute commands or contact services.\n"
)


FA_PROVIDER_URL = "https://provider.invalid/openai/v1/chat/completions"


class IsolationObservation:
    def __init__(self, root, *, denied_skill_probe=False):
        self.sdk_version = metadata.version("github-copilot-sdk")
        require(self.sdk_version == SDK_VERSION, "SDK_VERSION_DIFFERS")
        self.root, self.env = root, child_environment(root)
        self.phase = "witness"
        self.starts, self.stops, self.force_stops, self.sessions = [], [], [], []
        self.stop_without_runtime = []
        self.events, self.requests, self.path_metadata, self.policies = [], [], [], []
        self.catalogs, self.reads = [], []
        self.discovery_requests, self.retention = [], []
        self.violations, self.blocker = [], None
        self.diagnostic_hint = None
        self.termination = "FAILED"
        self.witness_source = "NOT_ESTABLISHED"
        self.completed = False
        self.bridges = []
        self.denied_skill_probe = denied_skill_probe
        self.denials = []
        self.canary_path = None

    def policy(self, options):
        return {
            "available": options["available_tools"].to_list(),
            "excluded": options["excluded_tools"].to_list(),
            "tools": [tool.name for tool in options["tools"]],
            "discovery": options["enable_config_discovery"],
            "managed": options["enable_managed_settings"],
            "mcp": options["mcp_servers"], "agents": options["custom_agents"],
            "skills": options["skill_directories"], "plugins": options["plugin_directories"],
            "instructions": options["instruction_directories"],
            "system_sha256": digest(options["system_message"]),
        }

    def observe_session(self, session):
        phase = self.phase
        self.sessions.append({"phase": phase, "sid": session.session_id})

        def event(value):
            kind = value.type.value
            if kind in {"assistant.turn_retry", "session.error"}:
                status = getattr(value.data, "status_code", None)
                diagnostic = {
                    "phase": phase, "type": kind,
                    "status_code": status if type(status) is int and 100 <= status <= 599 else None,
                }
                for name in ("reason", "error_type", "error_code", "message"):
                    text = getattr(value.data, name, None)
                    diagnostic[name + "_sha256"] = (hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()
                                                    if type(text) is str else None)
                self.events.append(diagnostic)
                return
            if kind in {"skill.invoked", "newskill.invoked", "tool.execution_start", "tool.execution_complete"}:
                name = getattr(value.data, "tool_name", None)
                call_id = getattr(value.data, "tool_call_id", None)
                success = getattr(value.data, "success", None)
                error = getattr(value.data, "error", None)
                error_code = getattr(error, "code", None)
                allowed = {"skill"} if phase == "witness" else set(SKILL_CAPABILITIES)
                expected_denial_attempt = (
                    self.denied_skill_probe and phase in {"fa-create", "fa-resume"}
                    and kind == "tool.execution_start" and name == "skill"
                    and call_id == "unlisted-" + phase
                    and getattr(value.data, "arguments", None) == {"skill": CANARY_NAME}
                )
                self.events.append({"phase": phase, "type": kind,
                    "tool": name if name in allowed or expected_denial_attempt else None,
                    "tool_call_sha256": hashlib.sha256(call_id.encode("utf-8", "surrogatepass")).hexdigest()
                        if type(call_id) is str else None,
                    "success": success if type(success) is bool else None,
                    "error_present": error is not None if kind == "tool.execution_complete" else None,
                    "error_code_sha256": hashlib.sha256(error_code.encode("utf-8", "surrogatepass")).hexdigest()
                        if type(error_code) is str else None,
                    "expected_denial_attempt": expected_denial_attempt,
                })
                unexpected_success = (
                    self.denied_skill_probe and phase in {"fa-create", "fa-resume"}
                    and kind == "tool.execution_complete" and call_id == "unlisted-" + phase
                    and success is True
                )
                unexpected_tool = (
                    name is not None and name not in allowed and not expected_denial_attempt
                    or phase != "witness" and kind in {"skill.invoked", "newskill.invoked"}
                )
                if unexpected_success or unexpected_tool:
                    self.violations.append("UNLISTED_NATIVE_TOOL_SUCCEEDED" if unexpected_success
                                           else "UNEXPECTED_NATIVE_TOOL")
                    asyncio.get_running_loop().create_task(session.abort())
        session.on(event)

