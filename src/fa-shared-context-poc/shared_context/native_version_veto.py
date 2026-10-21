"""ADR-0009 M8 project-level veto (phase 4b/4d): one Context Table row per (Hosted agent name, version) that
crash-looped, in its own partition ``native-version-veto``. API only, written once by Insert and never replaced or
deleted: a veto blocks that one version, so a newer selected version is a new target and is never blocked by it.
"""

from __future__ import annotations

from datetime import datetime, timezone
import logging
from typing import Annotated, Callable, Literal

from pydantic import Field

from .behavior_wire import Identifier, WireModel
from .errors import Problem
from .native_binding import AgentVersion, _timestamp, binding_store
from .storage import Repository, serialize

VETO_PARTITION = "native-version-veto"


class NativeVersionVeto(WireModel):
    schema_version: Literal["fa-native-version-veto-v1"] = "fa-native-version-veto-v1"
    agent_name: Identifier
    agent_version: AgentVersion
    reason: Literal["crash_loop"]
    vetoed_at: Annotated[str, Field(min_length=20, max_length=40)]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class NativeVersionVetoStore:
    def __init__(self, repository: Repository, *, clock: Callable[[], datetime] = _utc_now):
        self.repository, self.clock = repository, clock

    @staticmethod
    def row_key(agent_name: str, agent_version: str) -> str:
        return f"version:{agent_name}:{agent_version}"

    def read(self, agent_name: str, agent_version: str) -> NativeVersionVeto | None:
        try:
            record = self.repository.get(VETO_PARTITION, self.row_key(agent_name, agent_version))
        except Problem as error:
            if error.status == 404 and error.body["error"]["code"] == "NOT_FOUND":
                return None
            raise
        try:
            veto = NativeVersionVeto.model_validate_json(serialize(record.payload))
            if (veto.agent_name, veto.agent_version) != (agent_name, agent_version):
                raise ValueError("another version's veto row")
        except (ValueError, TypeError):
            raise Problem(409, "NATIVE_VERSION_VETO_INVALID", "the veto row differs from its fixed schema") from None
        return veto

    def rows(self, *, page: int = 100) -> tuple[NativeVersionVeto, ...]:
        """Every project veto, read page by page to the end (the deploy gate's operator report; read only). A row
        that differs from its fixed schema is logged and skipped, so it never hides the others."""
        found, after = [], None
        while True:
            items, more = self.repository.list_prefix(VETO_PARTITION, "version:", page, after=after)
            for key, record in items:
                try:
                    veto = NativeVersionVeto.model_validate_json(serialize(record.payload))
                    if self.row_key(veto.agent_name, veto.agent_version) != key:
                        raise ValueError("another version's veto row")
                except (ValueError, TypeError):
                    logging.getLogger(__name__).warning("NATIVE_VERSION_VETO_INVALID key=%s skipped", key)
                    continue
                found.append(veto)
            if not more or not items:
                return tuple(found)
            after = items[-1][0]

    def veto(self, agent_name: str, agent_version: str, *, reason: str = "crash_loop") -> NativeVersionVeto:
        """Insert once; an existing veto (409) is read back and returned unchanged, never overwritten."""
        now = _timestamp(self.clock())
        if now is None:
            raise Problem(503, "NATIVE_VERSION_VETO_UNAVAILABLE", "trusted UTC is unavailable")
        veto = NativeVersionVeto(agent_name=agent_name, agent_version=agent_version, reason=reason, vetoed_at=now)
        try:
            self.repository.create(VETO_PARTITION, self.row_key(agent_name, agent_version), veto.model_dump(mode="json"))
        except Problem as error:
            if (error.status, error.body["error"]["code"]) != (409, "ALREADY_EXISTS"):
                raise
            kept = self.read(agent_name, agent_version)
            if kept is None:
                raise Problem(503, "NATIVE_VERSION_VETO_UNAVAILABLE", "the veto row could not be read back") from None
            return kept
        return veto


def veto_store(composition) -> NativeVersionVetoStore:
    """The Context Table and clock the binding rows use (``binding_store``), in the project-level partition."""
    binding = binding_store(composition)
    if VETO_PARTITION == composition.config.table.scope.partition_key:
        raise Problem(503, "NATIVE_BINDING_UNAVAILABLE", "veto rows cannot share the legacy authority inventory")
    return NativeVersionVetoStore(binding.repository, clock=binding.clock)
