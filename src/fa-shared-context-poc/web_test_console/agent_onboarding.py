"""CAP-06 PR5: the trusted default Team entry asks the server to onboard one Agent, then writes its fixed local link.

Only the original request ID, the exact command, its hash and the last observed server result are kept locally.
Progress is always the server's original onboarding record; resuming re-sends the same request ID and body.
"""

from datetime import datetime
from pathlib import Path
import re
import secrets
import sqlite3
from threading import Lock
import time
from typing import Annotated
from urllib.parse import quote

import httpx
from pydantic import Field

from shared_context import behavior_wire as wire
from shared_context.behavior_agent_console_wire import AgentConsoleConnection, MAX_CONSOLE_CONNECTION_BYTES
from shared_context.behavior_agent_message_wire import MAX_TEAM_AGENTS
from shared_context.behavior_agent_onboarding import NEW_AGENT_PROFILE
from shared_context.behavior_agent_role_prompt import ROLE_PROMPT_MAX_BYTES
from shared_context.behavior_agent_template import TemplateSkill
from shared_context.behavior_native_turn_wire import NativeSessionState
from shared_context.behavior_capabilities import CapabilityRefs, base_supports
from shared_context.behavior_work_policy import derive_work

from .adapter import ConsoleError, canonical, parse_config, require, sha, strict_json
from .agent_connections import AgentConnection, AgentConnections
from .instructions import InstructionConnection

# New Agents start new conversations on the native lane like A and B (fresh_export); a template that selects
# Responses still takes precedence (NativeInstructionControl.profile). Existing Agents keep their files.
NEW_AGENT_STATE_PROFILE = "foundry-native-v1"

PHASE = "agent-onboarding"
# After the local link: one native prepare per new Agent, with one fixed request ID (server prepare is idempotent).
PREPARE_PHASE = "agent-native-prepare"
# Read-backs that mean "not prepared yet" (a new Hosted version takes a few minutes to be readable); the same
# request ID may be sent again. The page retries these and unknown outcomes for this long after the first attempt.
_NOT_READY = {(409, "NATIVE_SESSION_CHANGED"), (503, "AGENT_VERSION_FOLLOWING")}
PREPARE_AUTO_RETRY_SECONDS = 300
PREPARE_RETRY_INTERVAL_SECONDS = 30
# A 403 on a freshly onboarded Agent usually means its new project roles are still propagating (managed identity
# role changes can take hours). A 403 is a refusal with no effect, so the same request ID is sent again, slowly.
PERMISSION_WAIT_SECONDS = 7200
PERMISSION_RETRY_INTERVAL_SECONDS = 600
MAX_MANIFEST_BYTES = 65536
_TERMINAL = {"CONFIRMED", "FAILED", "REFUSED"}
# Server refusals that are decided before any onboarding row exists; a new request ID is then safe.
_REFUSED = {"AGENT_QUOTA_REACHED", "AGENT_NAME_TAKEN", "AGENT_ONBOARDING_INVALID", "AGENT_ROLE_PROMPT_TOO_LARGE",
            "AGENT_ROLE_PROMPT_INVALID", "TEAM_RUNTIME_NOT_SERVED",
            # The server checks the chosen tools and skills before S0 writes anything.
            "AGENT_ONBOARDING_SKILLS_TOOL_REQUIRED", "AGENT_TEMPLATE_DISABLED", "CAPABILITY_SELECTION_INVALID",
            "CAPABILITY_BASE_UNSUPPORTED", "CAPABILITY_SOURCES_CHANGED", "PROFILE_SELECTION_SOURCES_CHANGED",
            "PROFILE_SELECTION_DENIED", "PROFILE_SELECTION_UNSUPPORTED", "SKILL_CATALOG_BUDGET_TOO_SMALL"}
_SERVER_STATES = {"admitted", "creating", "registered", "ready", "failed"}

MESSAGES = {
    "AGENT_ONBOARDING_UNAVAILABLE": "這個工作台沒有以預設 Team 入口啟動，無法新增助理。",
    "AGENT_ONBOARDING_INPUT_INVALID": "只接受名稱（最多64字）、角色說明（最多2048 bytes），以及從 Team 已批准目錄勾選的工具與 skill（兩組一起送）；不接受路徑、policy 或其他欄位。",
    "AGENT_ONBOARDING_PENDING": "已有一筆新增助理尚未完成；請先用原編號續做，不會另建第二位。",
    "AGENT_ONBOARDING_LOCAL_NOT_FOUND": "本機沒有這個新增編號的原紀錄；不會改用其他編號或重新建立。",
    "AGENT_ONBOARDING_RESPONSE_INVALID": "服務回應與原新增紀錄不符；已保留原編號，未寫入本機連線。",
    "AGENT_ONBOARDING_LOCAL_CONFLICT": "固定目錄已有不同內容的連線檔；沒有覆寫，請人工核對。",
    "AGENT_ONBOARDING_NOT_LINKED": "這位助理還沒建立完成；完成後才會準備對話環境。",
}


class OnboardingInput(wire.WireModel):
    display_name: Annotated[str, Field(min_length=1, max_length=64)]
    role_prompt: Annotated[str, Field(min_length=1, max_length=ROLE_PROMPT_MAX_BYTES)]
    capabilities: Annotated[CapabilityRefs | None, Field(exclude_if=lambda value: value is None)] = None
    skills: Annotated[tuple[TemplateSkill, ...] | None, Field(max_length=4, exclude_if=lambda value: value is None)] = None


_BASE_FIELDS = {"display_name", "role_prompt"}


def parse_input(raw):
    try:
        data = strict_json(raw)
        require(type(data) is dict and set(data) in (_BASE_FIELDS, _BASE_FIELDS | {"capabilities", "skills"}),
                "AGENT_ONBOARDING_INPUT_INVALID", 400)
        value = OnboardingInput.model_validate_json(raw, strict=True)
    except (ValueError, TypeError):
        raise ConsoleError("AGENT_ONBOARDING_INPUT_INVALID", 400) from None
    require(len(value.role_prompt.encode("utf-8")) <= ROLE_PROMPT_MAX_BYTES, "AGENT_ONBOARDING_INPUT_INVALID", 400)
    return value


def _error_code(value):
    error = value.get("error") if type(value) is dict else None
    code = error.get("code") if type(error) is dict else None
    return code if type(code) is str and re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", code) else None


def _create_only(path, raw):
    """Write once; identical bytes already present are the same write, anything else is never overwritten."""
    from .bridge import Journal

    path = Path(path)
    require(not any(p.is_symlink() for p in (path, *path.parents)), "AGENT_ONBOARDING_LOCAL_CONFLICT")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(path, "xb") as handle:
            handle.write(raw)
    except FileExistsError:
        require(path.is_file() and path.read_bytes() == raw, "AGENT_ONBOARDING_LOCAL_CONFLICT")
    # Remote: keep the file with the journals before the link is recorded; a failure leaves the link unconfirmed.
    if Journal.after_file is not None:
        Journal.after_file(path)


class AgentOnboarding:
    """Thin client of the server onboarding record; it never advances creation on its own."""

    def __init__(self, agents, journal_path, *, roles_by_server=False):
        self.agents, self.default = agents, agents.default
        self.root = Path(journal_path).resolve().parent / "agents"
        # Remote (stage) only: the API assigns a new Agent's project roles before it is ready (#468/#470), so a
        # later 403 means they are still taking effect. Locally (dev) a 403 means roles were never assigned.
        self.roles_by_server = roles_by_server
        self._journal = None
        self.lock = Lock()

    def restore_links(self):
        """Agents linked by earlier onboardings are usable again after a restart without a new manifest.

        Never blocks startup: an unreadable record only leaves those Agents shown as not configured.
        """
        if not (self.root / "onboarding.sqlite3").exists():
            return
        try:
            links = list(self._connection_entries().values())
        except (ConsoleError, OSError, ValueError, sqlite3.Error):
            return
        for link in links:
            try:
                self.agents.add_link(link)
            except ConsoleError:
                pass  # the directory shows this Agent as not configured; nothing is sent

    @property
    def journal(self):
        from .bridge import Journal

        if self._journal is None:
            self._journal = Journal(self.root / "onboarding.sqlite3", self.default.config)
        return self._journal

    # ----- server calls (default Team entry only) -----

    def _send(self, method, path, body=None, *, limit=MAX_CONSOLE_CONNECTION_BYTES):
        token = self.default.vault.get()
        url = self.default.config.backend_origin + path
        try:
            start = time.monotonic()
            with self.default.backend.stream(method, url, content=None if body is None else canonical(body),
                    headers={"Authorization": "Bearer " + token, "Content-Type": "application/json",
                             "Accept-Encoding": "identity"}, follow_redirects=False) as response:
                data = bytearray()
                for chunk in response.iter_bytes():
                    data.extend(chunk)
                    require(len(data) <= limit and time.monotonic() - start < 120, "RESPONSE_LIMIT", 502)
                require(token.encode() not in data, "RESULT_INVALID", 502)
                status, cache = response.status_code, response.headers.get("cache-control")
        except (httpx.HTTPError, OSError):
            raise ConsoleError("BACKEND_OFFLINE", 503) from None
        try:
            value = strict_json(bytes(data))
        except (ValueError, TypeError):
            value = None
        return status, value, cache

    def _observe(self, rid, status, value, cache):
        """Map one server reply to the stored last observation; unclear replies stay UNKNOWN."""
        now = self.default.clock().isoformat()
        if status in {200, 202} and type(value) is dict and cache == "no-store":
            state = value.get("state")
            require(value.get("request_id") == rid and state in _SERVER_STATES
                    and type(value.get("display_name")) is str
                    and (status == 200 or state not in {"ready", "failed"}),
                    "AGENT_ONBOARDING_RESPONSE_INVALID", 502)
            observed = {"state": "SERVER", "server_state": state, "observed_at": now,
                        "display_name": value["display_name"]}
            if type(value.get("reason")) is str:
                observed["reason"] = value["reason"]
            if state == "failed":
                observed["state"] = "FAILED"
            if type(value.get("agent_id")) is str:
                observed["agent_id"] = value["agent_id"]
            require(state != "ready" or "agent_id" in observed, "AGENT_ONBOARDING_RESPONSE_INVALID", 502)
            return observed
        error = value.get("error") if type(value) is dict else None
        code = error.get("code") if type(error) is dict else None
        observed = {"state": "UNKNOWN", "code": "BACKEND_UNAVAILABLE", "observed_at": now, "upstream_status": status}
        if type(code) is str and re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", code):
            observed["code"] = code
            for name in ("limit", "max_bytes"):
                if type(error.get(name)) is int:
                    observed[name] = error[name]
            # SELECTION_STALE names the inner refusal; the recovery differs per cause.
            if type(error.get("cause")) is str and re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", error["cause"]):
                observed["cause"] = error["cause"]
            if 400 <= status < 500 and code in _REFUSED:
                observed["state"] = "REFUSED"
        return observed

    # ----- local link -----

    def _connection_entries(self):
        """Running manifest plus every Agent this onboarding journal has already linked, in a stable order."""
        entries = {entry.agent_id: entry for entry in self.agents.connections.agents}
        for _, saved in reversed(self.journal.entries(PHASE)):
            link = saved.get("connection")
            if saved.get("state") == "CONFIRMED" and type(link) is dict:
                value = AgentConnection.model_validate(link)
                entries.setdefault(value.agent_id, value)
        return entries

    def _link(self, rid, agent_id, display_name):
        status, value, cache = self._send("GET", "/work-policy/v1/agents/" + quote(agent_id, safe="")
                                          + "/console-connection")
        if status != 200:
            return self._observe(rid, status, value, cache) | {"state": "SERVER", "server_state": "ready",
                                                                "agent_id": agent_id, "display_name": display_name,
                                                                "link_error": True}
        try:
            require(cache == "no-store" and type(value) is dict, "AGENT_ONBOARDING_RESPONSE_INVALID", 502)
            remote = AgentConsoleConnection.model_validate_json(canonical(value))
        except (ValueError, TypeError):
            raise ConsoleError("AGENT_ONBOARDING_RESPONSE_INVALID", 502) from None
        original = self.default.config
        policy = remote.standing_policy
        require(remote.agent_id == agent_id and remote.display_name == display_name
                and agent_id != self.agents.connections.default_agent_id
                and policy.template.team_id == original.standing_policy.template.team_id
                and policy.template.tenant_id == original.tenant_id
                and policy.template.human_oid == original.human_oid
                and policy.sha256 != original.standing_policy_sha256, "AGENT_ONBOARDING_RESPONSE_INVALID", 502)
        tasks = []
        for task in original.tasks:
            if task.mode != "normal_pull":
                continue
            _, binding, _ = derive_work(policy, task.submit_request_id, policy.evidence_mode)
            tasks.append({**task.model_dump(mode="json"),
                "binding_id": binding.binding_id, "job_id": binding.job_id, "work_id": binding.work.work_id,
                "event_id": binding.event_id, "root_request_id": binding.root_request_id,
                "obligation_id": binding.obligation_id, "owner_record_id": binding.owner.owner_record_id,
                "route": binding.route.model_dump(mode="json")})
        require(tasks, "AGENT_ONBOARDING_UNAVAILABLE")
        try:
            config = parse_config(canonical({
                **original.model_dump(mode="json"), "console_id": "console-" + sha(agent_id.encode())[:24],
                "backend_configuration_sha256": policy.configuration_sha256,
                "standing_policy": policy.model_dump(mode="json"), "standing_policy_sha256": policy.sha256,
                "tasks": tasks}))
            instructions = InstructionConnection(
                schema_version="fa-console-instructions-connection-v1", configuration_sha256=config.sha256,
                backend_core_sha256=self.default.instructions.connection.backend_core_sha256,
                state_profile=NEW_AGENT_STATE_PROFILE)
        except (ValueError, TypeError):
            raise ConsoleError("AGENT_ONBOARDING_RESPONSE_INVALID", 502) from None
        folder = self.root / sha(agent_id.encode())[:32]
        config_raw = canonical(config.model_dump(mode="json"))
        instructions_raw = canonical(instructions.model_dump(mode="json"))
        # A link written before new Agents defaulted to native keeps its job-bound file instead of conflicting.
        legacy_raw = canonical(instructions.model_copy(update={"state_profile": None}).model_dump(mode="json"))
        target = folder / "instructions.json"
        if target.is_file() and not target.is_symlink() and target.read_bytes() == legacy_raw:
            instructions_raw = legacy_raw
        # Same limits as read_connection: a file the console could never load is refused before it is written.
        require(len(config_raw) <= 65536 and len(instructions_raw) <= 4096, "AGENT_ONBOARDING_RESPONSE_INVALID", 502)
        _create_only(folder / "config.json", config_raw)
        _create_only(folder / "instructions.json", instructions_raw)
        link = AgentConnection(agent_id=agent_id, config_path=str(folder / "config.json"),
            config_sha256=sha(config_raw), journal_path=str(folder / "journal.sqlite3"),
            instructions_path=str(folder / "instructions.json"), instructions_sha256=sha(instructions_raw))
        entries = self._connection_entries()
        prior = entries.get(agent_id)
        require(prior is None or prior == link, "AGENT_ONBOARDING_LOCAL_CONFLICT")
        entries[agent_id] = link
        try:
            manifest = AgentConnections(default_agent_id=self.agents.connections.default_agent_id,
                                        agents=tuple(entries.values()))
        except ValueError:
            raise ConsoleError("AGENT_ONBOARDING_LOCAL_CONFLICT") from None
        raw = canonical(manifest.model_dump(mode="json"))
        require(len(raw) <= MAX_MANIFEST_BYTES, "AGENT_ONBOARDING_LOCAL_CONFLICT")
        digest = sha(raw)
        path = self.root / ("manifest-" + digest[:16] + ".json")
        _create_only(path, raw)
        return {"state": "CONFIRMED", "server_state": "ready", "agent_id": agent_id, "display_name": display_name,
                "observed_at": self.default.clock().isoformat(), "connection": link.model_dump(mode="json"),
                "manifest_path": str(path), "manifest_sha256": digest}

    # ----- browser-facing operations -----

    def _record(self, rid, observed):
        if observed.get("state") == "SERVER" and observed.get("server_state") == "ready":
            observed = self._link(rid, observed["agent_id"], observed["display_name"])
        self.journal.finish(rid, PHASE, observed)
        if observed.get("state") == "CONFIRMED":
            # The new Agent is selectable after the page's next directory read; no restart with a new manifest.
            try:
                self.agents.add_link(AgentConnection.model_validate(observed["connection"]))
            except ConsoleError:
                pass  # the link stays confirmed; the directory shows the Agent as not configured, nothing is sent
        if observed.get("state") == "CONFIRMED" and self.journal.get(rid, PREPARE_PHASE) is None:
            try:
                self._prepare(rid, post=True)
            except ConsoleError:
                pass  # the link stays confirmed; the view shows the prepare state and its retry
        return self.view(rid)

    # ----- native environment of the new Agent -----

    @staticmethod
    def _native(agent_id, tail):
        return "/agents/" + quote(agent_id, safe="") + "/work-policy/v1/native-turns/" + tail

    def _prepare(self, rid, *, post):
        """POST the one fixed prepare request when allowed, then read the preparation back; unknown stays read-only."""
        saved = self.journal.get(rid, PHASE)
        require(saved is not None and saved["state"] == "CONFIRMED" and type(saved.get("agent_id")) is str,
                "AGENT_ONBOARDING_NOT_LINKED", 409)
        agent_id = saved["agent_id"]
        current = self.journal.get(rid, PREPARE_PHASE)
        if current is None:
            command = {"request_id": "prepare-" + secrets.token_hex(12)}
            self.journal.begin(rid, PREPARE_PHASE, {"agent_id": agent_id, "command": command,
                                                   "created_at": self.default.clock().isoformat()})
            post = True
        else:
            command = current["intent"]["command"]
            require(current["intent"]["agent_id"] == agent_id, "AGENT_ONBOARDING_RESPONSE_INVALID", 500)
        refused, refused_status = None, None
        if post:
            try:
                status, value, _ = self._send("POST", self._native(agent_id, "prepare"), command)
                if status != 200:
                    refused, refused_status = _error_code(value) or "BACKEND_UNAVAILABLE", status
            except ConsoleError as error:
                refused = error.code
        observed = self._read_preparation(agent_id, command, refused, refused_status)
        self.journal.finish(rid, PREPARE_PHASE, observed)
        return observed

    def _read_preparation(self, agent_id, command, refused, refused_status=None):
        now = self.default.clock().isoformat()
        try:
            status, value, cache = self._send("POST", self._native(agent_id, "prepare/reconcile"), command)
        except ConsoleError as error:
            return {"state": "UNKNOWN", "code": error.code, "observed_at": now}
        state = None
        if status == 200 and type(value) is dict and cache == "no-store":
            try:
                state = NativeSessionState.model_validate(value)
            except ValueError:
                return {"state": "UNKNOWN", "code": "AGENT_ONBOARDING_RESPONSE_INVALID", "observed_at": now}
        if (state is not None and state.agent_id == agent_id and state.preparation is not None
                and (state.environment is None or state.environment.state == "ready")
                and (state.following is None or state.following.state == "ready")):
            prepared = state.environment or state.preparation
            return {"state": "READY", "observed_at": now, "hosted_agent_version": prepared.hosted_agent_version}
        code = "AGENT_ONBOARDING_RESPONSE_INVALID" if status == 200 else _error_code(value)
        if 401 in (status, refused_status) or 403 in (status, refused_status):
            # Missing roles or login: never retried automatically; the button still resends the same request ID.
            denied = status if status in {401, 403} else refused_status
            return {"state": "FAILED", "code": (code if denied == status else refused) or "BACKEND_UNAVAILABLE",
                    "observed_at": now, "upstream_status": denied}
        if (status, code) in _NOT_READY:
            # Read back as not prepared yet: nothing exists, so the same request ID may be sent again.
            return {"state": "FAILED", "code": refused or code, "observed_at": now}
        return {"state": "UNKNOWN", "code": code or "BACKEND_UNAVAILABLE", "observed_at": now, "upstream_status": status}

    def prepare(self, rid):
        """The retry button: a failure sends the same request ID again; an unknown outcome is only read."""
        with self.lock:
            current = self.journal.get(rid, PREPARE_PHASE)
            if current is None or current["state"] != "READY":
                self._prepare(rid, post=current is None or current["state"] == "FAILED")
            return self.view(rid)

    def _post(self, rid):
        saved = self.journal.get(rid, PHASE)
        require(saved is not None, "AGENT_ONBOARDING_LOCAL_NOT_FOUND", 404)
        command = saved["intent"]["command"]
        require(sha(canonical(command)) == saved["intent"]["command_sha256"], "AGENT_ONBOARDING_RESPONSE_INVALID", 500)
        try:
            status, value, cache = self._send("POST", "/work-policy/v1/agents", command)
        except ConsoleError as error:
            self.journal.finish(rid, PHASE, {"state": "UNKNOWN", "code": error.code,
                                             "observed_at": self.default.clock().isoformat()})
            raise
        return self._record(rid, self._observe(rid, status, value, cache))

    def create(self, chosen):
        with self.lock:
            for _, saved in self.journal.entries(PHASE):
                require(saved["state"] in _TERMINAL, "AGENT_ONBOARDING_PENDING")
            rid = "onboard-" + secrets.token_hex(12)
            command = {"request_id": rid, **chosen.model_dump(mode="json")}
            self.journal.begin(rid, PHASE, {"command": command, "command_sha256": sha(canonical(command)),
                                            "created_at": self.default.clock().isoformat()})
            return self._post(rid)

    def resume(self, rid):
        """Same request ID and same body: the server's own idempotent continuation, never a new request."""
        with self.lock:
            saved = self.journal.get(rid, PHASE)
            require(saved is not None, "AGENT_ONBOARDING_LOCAL_NOT_FOUND", 404)
            if saved["state"] in _TERMINAL:
                return self.view(rid)
            if saved["state"] == "SERVER" and saved.get("server_state") == "ready":
                return self._record(rid, {name: value for name, value in saved.items() if name != "intent"})
            return self._post(rid)

    def refresh(self, rid):
        """Read the same original server record; never POSTs."""
        with self.lock:
            saved = self.journal.get(rid, PHASE)
            require(saved is not None, "AGENT_ONBOARDING_LOCAL_NOT_FOUND", 404)
            if saved["state"] in _TERMINAL:
                return self.view(rid)
            try:
                status, value, cache = self._send("GET", "/work-policy/v1/agents/onboarding/" + quote(rid, safe=""))
            except ConsoleError as error:
                return self.view(rid) | {"read_error": error.code}
            if status == 404 and saved["state"] in {"UNKNOWN", "REFUSED"}:
                return self.view(rid) | {"read_error": "AGENT_ONBOARDING_NOT_FOUND"}
            return self._record(rid, self._observe(rid, status, value, cache))

    def view(self, rid):
        saved = self.journal.get(rid, PHASE)
        require(saved is not None, "AGENT_ONBOARDING_LOCAL_NOT_FOUND", 404)
        shown = {name: saved[name] for name in (
            "state", "server_state", "reason", "code", "cause", "limit", "max_bytes", "agent_id", "manifest_sha256",
            "observed_at", "upstream_status", "link_error") if name in saved}
        prepared = self.journal.get(rid, PREPARE_PHASE)
        if prepared is not None:
            shown["prepare"] = {"request_id": prepared["intent"]["command"]["request_id"], **{
                name: prepared[name] for name in ("state", "code", "observed_at", "hosted_agent_version", "upstream_status")
                if name in prepared}}
            started = datetime.fromisoformat(prepared["intent"]["created_at"])
            elapsed = (self.default.clock() - started).total_seconds()
            waiting = (self.roles_by_server and prepared["state"] == "FAILED"
                       and prepared.get("upstream_status") == 403)
            shown["prepare"]["auto_retry"] = (
                (waiting and elapsed < PERMISSION_WAIT_SECONDS)
                or (prepared["state"] in {"FAILED", "UNKNOWN"} and prepared.get("upstream_status") not in {401, 403}
                    and elapsed < PREPARE_AUTO_RETRY_SECONDS))
            shown["prepare"]["permission_wait"] = waiting
            shown["prepare"]["retry_seconds"] = (PERMISSION_RETRY_INTERVAL_SECONDS if waiting
                                                 else PREPARE_RETRY_INTERVAL_SECONDS)
        command = saved["intent"]["command"]
        # The chosen tool IDs only, so the page can name who restores one (Team MCP `team-*` vs. the org catalog).
        return {"request_id": rid, "display_name": command["display_name"], **shown,
                "capability_ids": [item["entry_id"] for item in command.get("capabilities") or []],
                "restart_required": False, "max_team_agents": MAX_TEAM_AGENTS,
                "ready_to_chat": saved["state"] == "CONFIRMED" and prepared is not None and prepared["state"] == "READY"}

    def list(self):
        return {"onboardings": [self.view(rid) for rid, _ in self.journal.entries(PHASE)]}

    def options(self):
        """Team-approved tools and skills from the default Team entry, with its current choice as the default.

        A new Agent always uses ``NEW_AGENT_PROFILE`` (agent-role v2). Tools that profile cannot compose (the server's own
        ``check_selectable`` rule) are listed as unsupported and left out of the default; the server still
        refuses them explicitly if they are sent."""
        offered = self.default.template_options().model_dump(mode="json")
        unsupported = sorted(item["entry_id"] for item in offered["capabilities"]
                             if not base_supports(item["entry_id"], NEW_AGENT_PROFILE))
        try:
            current = strict_json(self.default.download_template())
            chosen = {"capabilities": [item for item in current["settings"].get("capabilities") or []
                                       if item["entry_id"] not in unsupported],
                      "skills": list(current.get("skills") or [])}
        except (KeyError, TypeError, ValueError, AttributeError):
            raise ConsoleError("TEMPLATE_RESPONSE_INVALID", 502) from None
        return {"options": offered, "current": chosen, "unsupported": unsupported}
