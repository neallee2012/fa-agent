"""One small CAS item per native relay root. Message contents and receipts remain in the Agents."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, TypeAdapter, model_validator

from .behavior_native_turn_wire import material_digest
from .behavior_wire import Digest, WireModel
from .errors import Problem
from .records import Record
from .storage import Repository, serialize

_DIGEST = TypeAdapter(Digest)
MAX_QUOTA_BYTES = 4096
MAX_NATIVE_RELAY_MESSAGES = 10


class _Message(WireModel):
    message_id: Digest
    payload_sha256: Digest


class RootAllowance(WireModel):
    kind: Literal["fa-native-relay-quota-v1"] = "fa-native-relay-quota-v1"
    root_id: Digest
    messages: Annotated[tuple[_Message, ...], Field(max_length=MAX_NATIVE_RELAY_MESSAGES)] = ()

    @model_validator(mode="after")
    def distinct_ids(self):
        ids = [message.message_id for message in self.messages]
        if len(set(ids)) != len(ids):
            raise ValueError("Original message IDs must be distinct")
        return self


class NativeRelayQuota:
    def __init__(self, repository: Repository, *, tenant_id: str, team_id: str):
        self.repository = repository
        # The legacy startup inventory reads only its authority partition, never this Team's quota partition.
        self.partition_key = "native-relay:" + material_digest({"tenant": tenant_id, "team": team_id})

    def _read(self, root_id: str) -> tuple[RootAllowance, Record | None]:
        try:
            root_id = _DIGEST.validate_python(root_id)
        except ValueError:
            raise Problem(403, "AGENT_MESSAGE_ROOT_DENIED", "a server-derived original root is required") from None
        try:
            record = self.repository.get(self.partition_key, "root:" + root_id)
        except Problem as error:
            if error.status == 404 and error.body["error"]["code"] == "NOT_FOUND":
                return RootAllowance(root_id=root_id), None
            raise
        try:
            value = RootAllowance.model_validate_json(serialize(record.payload))
            if value.root_id != root_id or not record.etag or record.etag == "*":
                raise ValueError()
        except (ValueError, TypeError):
            raise Problem(409, "NATIVE_RELAY_QUOTA_INVALID", "the original relay allowance differs") from None
        return value, record

    def read(self, root_id: str) -> RootAllowance:
        return self._read(root_id)[0]

    @staticmethod
    def _matching(item, message_id, payload_sha256):
        existing = next((message for message in item.messages if message.message_id == message_id), None)
        if existing is not None and existing.payload_sha256 != payload_sha256:
            raise Problem(409, "AGENT_MESSAGE_CONFLICT", "the original message ID names different material")
        return existing

    def _save(self, value, previous, message_id, payload_sha256):
        payload = value.model_dump(mode="json")
        if len(serialize(payload)) > MAX_QUOTA_BYTES:
            raise Problem(413, "NATIVE_RELAY_QUOTA_INVALID", "relay quota metadata exceeds its fixed bound")
        row = "root:" + value.root_id
        try:
            if previous is None:
                self.repository.create(self.partition_key, row, payload)
            else:
                self.repository.replace(self.partition_key, row, payload, previous.etag)
        except Problem as error:
            if error.status not in {409, 412} and error.status < 500:
                raise
            original, _ = self._read(value.root_id)
            matched = self._matching(original, message_id, payload_sha256)
            if matched is not None:
                return matched
            raise Problem(503 if error.status >= 500 else 409, "NATIVE_RELAY_QUOTA_UNKNOWN",
                          "the original allowance is unconfirmed; reconcile the same message ID") from None
        return self._matching(value, message_id, payload_sha256)

    def reserve_message(self, root_id: str, message_id: str, payload_sha256: str) -> _Message:
        candidate = _Message(message_id=message_id, payload_sha256=payload_sha256)
        original, record = self._read(root_id)
        existing = self._matching(original, message_id, payload_sha256)
        if existing is not None:
            return existing
        if len(original.messages) >= MAX_NATIVE_RELAY_MESSAGES:
            raise Problem(409, "AGENT_MESSAGE_ALLOWANCE", "the original root's new-message allowance is exhausted")
        updated = RootAllowance(root_id=root_id, messages=(*original.messages, candidate))
        return self._save(updated, record, message_id, payload_sha256)
