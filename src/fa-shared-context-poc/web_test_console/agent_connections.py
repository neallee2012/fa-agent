"""Trusted local Agent/configuration links, never a backend permission or directory."""

from pathlib import Path
import sqlite3
from threading import RLock
from typing import Annotated, Literal

from pydantic import Field, model_validator

from shared_context import behavior_wire as wire
from shared_context.behavior_agent_message_wire import MAX_TEAM_AGENTS, AgentMessageList, PublicAgentDirectory

from .adapter import ConsoleError, PullAdapter, parse_config, require, sha, strict_json
from .instructions import InstructionConnection
from .standing import StandingConsoleConfig

DIRECTORY_TTL_SECONDS = 60

MESSAGES = {
    "AGENT_SELECTION_REQUIRED": "請先選擇服務目錄中的助理；不會改送到預設助理。",
    "AGENT_NOT_AVAILABLE": "這位助理目前不可用；原工作與草稿保留，不會改送其他助理。",
    "AGENT_RETIRED": "這位助理已退役；訊息沒有送出，也不會改送其他助理。",
    "AGENT_DIRECTORY_UNAVAILABLE": "目前無法取得助理目錄；沒有使用舊目錄放行或改送預設助理。",
    "AGENT_DIRECTORY_INVALID": "助理目錄的身份或路由無法核對；沒有切換助理。",
    "AGENT_DEFAULT_MISMATCH": "服務的預設助理與原工作台配置不符；沒有重新歸屬舊紀錄。",
    "AGENT_CONFIGURATION_MISSING": "尚未提供這位助理的完整本機配置；不會套用其他助理的設定。",
    "AGENT_CONFIGURATION_INVALID": "這位助理的配置、固定雜湊或 journal 無法核對；原紀錄未改寫。",
    "AGENT_MESSAGES_UNAVAILABLE": "目前無法讀取這筆工作的交接紀錄；不改查其他助理，也不代表沒有交接。",
    "AGENT_MESSAGES_INVALID": "交接紀錄與原助理、工作或對話不符；未顯示為已完成或已交付。",
}


class AgentConnection(wire.WireModel):
    agent_id: wire.Identifier
    config_path: Annotated[str, Field(min_length=1, max_length=2048)]
    config_sha256: wire.Digest
    journal_path: Annotated[str, Field(min_length=1, max_length=2048)]
    instructions_path: Annotated[str, Field(min_length=1, max_length=2048)]
    instructions_sha256: wire.Digest

    @model_validator(mode="after")
    def absolute_paths(self):
        if not all(Path(value).is_absolute() for value in (
            self.config_path, self.journal_path, self.instructions_path,
        )):
            raise ValueError("Agent connections require explicit absolute local paths")
        return self


class AgentConnections(wire.WireModel):
    schema_version: Literal["fa-console-agents-v1"] = "fa-console-agents-v1"
    default_agent_id: wire.Identifier
    agents: Annotated[tuple[AgentConnection, ...], Field(min_length=1, max_length=MAX_TEAM_AGENTS)]

    @model_validator(mode="after")
    def distinct_connections(self):
        identities = {entry.agent_id for entry in self.agents}
        journals = {Path(entry.journal_path).resolve() for entry in self.agents}
        if (self.default_agent_id not in identities or len(identities) != len(self.agents)
                or len(journals) != len(self.agents)):
            raise ValueError("Agent IDs and journals must be distinct, with one explicit default")
        return self


def read_connection(entry):
    try:
        raw = Path(entry.config_path).read_bytes()
        require(len(raw) <= 65536 and sha(raw) == entry.config_sha256, "AGENT_CONFIGURATION_INVALID")
        config = parse_config(raw)
        raw = Path(entry.instructions_path).read_bytes()
        require(len(raw) <= 4096 and sha(raw) == entry.instructions_sha256, "AGENT_CONFIGURATION_INVALID")
        strict_json(raw)
        instructions = InstructionConnection.model_validate_json(raw)
    except FileNotFoundError:
        raise ConsoleError("AGENT_CONFIGURATION_MISSING") from None
    except (OSError, ValueError, TypeError):
        raise ConsoleError("AGENT_CONFIGURATION_INVALID") from None
    require(type(config) is StandingConsoleConfig and instructions.configuration_sha256 == config.sha256,
            "AGENT_CONFIGURATION_INVALID")
    return config, instructions


def validate_default(connections, config, journal_path, instructions):
    entry = next(value for value in connections.agents if value.agent_id == connections.default_agent_id)
    saved, connection = read_connection(entry)
    require(saved == config and connection == instructions
            and Path(entry.journal_path).resolve() == Path(journal_path).resolve(),
            "AGENT_DEFAULT_MISMATCH")


def read_messages(console, task_id):
    require(console.api_prefix.startswith("/agents/"), "AGENT_MESSAGES_UNAVAILABLE")
    identity = console.api_prefix.removeprefix("/agents/")
    task = console.task(task_id)
    token = console.gate(task, "read-agent-messages")
    admission = console.instructions.admission(task)
    require(admission is not None, "INSTRUCTION_PENDING")
    try:
        raw, headers = console.instructions.adapter.send(
            "GET", f"/work-policy/v1/bindings/{task.binding_id}/agent-messages", None, token)
    except ConsoleError as error:
        if error.upstream_status in {403, 404, 405}:
            raise ConsoleError("AGENT_MESSAGES_UNAVAILABLE", 409, error.upstream_status) from None
        raise
    try:
        strict_json(raw)
        value = AgentMessageList.model_validate_json(raw)
    except (ValueError, TypeError):
        raise ConsoleError("AGENT_MESSAGES_INVALID", 502) from None
    require(headers.get("cache-control") == "no-store"
            and (value.agent_id, value.binding_id, value.conversation_id) ==
                (identity, task.binding_id, admission.conversation_id), "AGENT_MESSAGES_INVALID", 502)
    for message in value.messages:
        own = (
            message.sender_agent_id == identity
            and (message.from_work_id, message.from_conversation_id) == (task.work_id, admission.conversation_id)
        ) or (
            message.recipient_agent_id == identity
            and (message.to_work_id, message.to_conversation_id) == (task.work_id, admission.conversation_id)
        )
        require(own, "AGENT_MESSAGES_INVALID", 502)
    return value.model_dump(mode="json")


class AgentConsoles:
    def __init__(self, connections, console, factory):
        self.connections, self.default, self.factory = connections, console, factory
        self.consoles = {connections.default_agent_id: console}
        self.loaded, self.failures = {}, {}
        self.directory = None
        self.directory_read_at = None
        self.error = "AGENT_DIRECTORY_UNAVAILABLE"
        self.lock = RLock()
        self.reader = PullAdapter(console.config, console.backend)

    def _load(self, identity):
        from .bridge import Journal

        entry = next((value for value in self.connections.agents if value.agent_id == identity), None)
        require(entry is not None, "AGENT_CONFIGURATION_MISSING")
        config, instructions = self._checked(entry)
        if identity == self.connections.default_agent_id:
            validate_default(self.connections, self.default.config, self.default.journal.path,
                             self.default.instructions.connection)
        prior = self.consoles.get(identity)
        require(prior is None or (prior.config == config and prior.instructions.connection == instructions),
                "AGENT_CONFIGURATION_INVALID")
        try:
            Journal.check_existing(entry.journal_path, config)
        except ConsoleError:
            raise ConsoleError("AGENT_CONFIGURATION_INVALID") from None
        return entry, config, instructions

    def _checked(self, entry):
        """Same tenant, human, client, audience and Team as the default entry, with the exact file hashes."""
        config, instructions = read_connection(entry)
        original = self.default.config
        require(all(getattr(config, field) == getattr(original, field) for field in (
            "backend_origin", "tenant_id", "human_oid", "client_id", "audience",
        )) and config.standing_policy.template.team_id == original.standing_policy.template.team_id
            and (entry.agent_id == self.connections.default_agent_id
                 or config.standing_policy.agent_id == entry.agent_id),
            "AGENT_CONFIGURATION_INVALID")
        return config, instructions

    def add_link(self, link):
        """A newly onboarded Agent joins the running manifest (no restart); the same checks as a manifest entry."""
        with self.lock:
            prior = next((entry for entry in self.connections.agents if entry.agent_id == link.agent_id), None)
            if prior is not None:
                require(prior == link, "AGENT_CONFIGURATION_INVALID")
                return
            self._checked(link)
            try:
                self.connections = AgentConnections(default_agent_id=self.connections.default_agent_id,
                                                    agents=(*self.connections.agents, link))
            except ValueError:
                raise ConsoleError("AGENT_CONFIGURATION_INVALID") from None

    def refresh_directory(self):
        # The HTTP read runs outside the lock so a slow directory never stalls resolve() or view(); only the checked
        # result is swapped in under it.
        try:
            token = self.default.vault.get()
            try:
                raw, headers = self.reader.send("GET", "/work-policy/v1/agents", None, token, limit=16384)
            except ConsoleError as error:
                raise ConsoleError("AGENT_DIRECTORY_UNAVAILABLE", 503, error.upstream_status) from None
        except ConsoleError:
            with self.lock:
                self.error = "AGENT_DIRECTORY_UNAVAILABLE"
            raise
        with self.lock:
            self.error = "AGENT_DIRECTORY_INVALID"
            try:
                strict_json(raw)
                value = PublicAgentDirectory.model_validate_json(raw)
            except (ValueError, TypeError):
                raise ConsoleError(self.error, 502) from None
            require(headers.get("cache-control") == "no-store"
                    and len({entry.agent_id for entry in value.agents}) == len(value.agents), self.error, 502)
            if value.default_agent_id != self.connections.default_agent_id:
                self.error = "AGENT_DEFAULT_MISMATCH"
                raise ConsoleError(self.error)
            self.directory, self.error = value, None
            self.directory_read_at = self.default.clock().timestamp()
            self.loaded, self.failures = {}, {}
            for entry in value.agents:
                if entry.state != "ready":
                    continue
                try:
                    self.loaded[entry.agent_id] = self._load(entry.agent_id)
                except ConsoleError as error:
                    self.failures[entry.agent_id] = error.code
            return self.view()

    def view(self):
        with self.lock:
            agents, hidden = [], []
            for entry in (() if self.directory is None else self.directory.agents):
                # A target the server marks unavailable (for example a configured executor that is never invoked) is
                # not a chat partner: it stays out of the list and only shows in the support details. The default
                # Agent always stays, so the page can still open it and show why it cannot be used; an Agent the
                # owner is still creating stays too, shown as being created.
                if (entry.state != "ready" and entry.agent_id != self.connections.default_agent_id
                        and entry.reason != "AGENT_ONBOARDING_IN_PROGRESS"):
                    hidden.append({"id": entry.agent_id, "name": entry.display_name, "reason": entry.reason})
                    continue
                reason = self.error or self.failures.get(entry.agent_id)
                if reason is None and entry.state != "ready":
                    reason = entry.reason
                agents.append({
                    "id": entry.agent_id, "name": entry.display_name, "api_prefix": entry.api_prefix,
                    "description": "" if reason is None else MESSAGES.get(reason, "目前不能使用這位助理。"),
                    "enabled": reason is None, "reason": reason, "server_state": entry.state,
                })
            return {"agent_mode": True, "default_agent_id": self.connections.default_agent_id,
                    "agents": agents, "hidden_agents": hidden, "agent_directory_error": self.error}

    def resolve(self, identity):
        with self.lock:
            require(identity is not None, "AGENT_SELECTION_REQUIRED")
            require(self.error is None and self.directory is not None, self.error or "AGENT_DIRECTORY_UNAVAILABLE")
            entry = next((entry for entry in self.directory.agents if entry.agent_id == identity), None)
            require(entry is not None and entry.state == "ready", "AGENT_NOT_AVAILABLE")
            require(identity not in self.failures, self.failures.get(identity, "AGENT_CONFIGURATION_INVALID"))
            require(identity in self.loaded, "AGENT_CONFIGURATION_MISSING")
            if identity not in self.consoles:
                link, config, instructions = self.loaded[identity]
                try:
                    self.consoles[identity] = self.factory(config, link.journal_path, instructions, entry.api_prefix)
                except (ConsoleError, OSError, sqlite3.Error):
                    self.failures[identity] = "AGENT_CONFIGURATION_INVALID"
                    raise ConsoleError("AGENT_CONFIGURATION_INVALID") from None
            console = self.consoles[identity]
            console.vault = self.default.vault
            return console

    def decorate(self, value, identity):
        return {**value, **self.view(), **self.default.authentication(), "selected_agent_id": identity}

    def bootstrap(self, value=None):
        if value is None:
            # Page load: a directory read more than a minute ago is read again, so an Agent retired or added
            # meanwhile shows without pressing 「更新助理目錄」. A failed read shows as the directory error.
            with self.lock:
                stale = (self.directory_read_at is None
                         or self.default.clock().timestamp() - self.directory_read_at > DIRECTORY_TTL_SECONDS)
                due = stale and self.default.authentication()["authentication"] == "AVAILABLE"
            if due:
                try:
                    self.refresh_directory()
                except ConsoleError:
                    pass
            value = self.default.state()
        return self.decorate({**value, "tasks": [], "composer": None, "automatic_polling": False,
            "template_tools_available": False, "template_selections": [], "template_selection_error": None,
            "routines_available": False}, None)

    def authenticate(self, automatic=False):
        with self.lock:
            prior = self.default.vault
            result = self.default.authenticate(automatic=automatic, load_models=False)
            for console in self.consoles.values():
                console.vault = self.default.vault
            refresh = not automatic or self.default.vault is not prior
        if refresh:
            try:
                self.refresh_directory()
            except ConsoleError:
                # A silent refresh renewed the sign-in; a failed directory read shows as the directory error (as
                # on page load), never as an error the page could read as an expired sign-in.
                if not automatic:
                    raise
        return {**result, **self.view()} if automatic else self.bootstrap(result)
