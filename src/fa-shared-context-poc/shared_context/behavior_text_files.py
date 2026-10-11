"""Small literal UTF-8 input files, not filesystem paths, sources or permission grants."""

import hashlib
import json
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, TypeAdapter, field_validator, model_validator

from .behavior_wire import Command, Digest, Identifier, WireModel
from .behavior_user_turn import TurnNumber, UserTurnSource, USER_TURN_ERRORS
from .m3_models import Filename
from .errors import Problem

UserTextFileProfile = Literal["fa-user-text-files-v1"]
MAX_USER_INPUT_BYTES = 8192
USER_FILE_READ_ERRORS = USER_TURN_ERRORS | {"USER_FILE_NOT_FOUND", "USER_FILE_READ_DISABLED"}


class UserTextFileCapability(WireModel):
    schema_version: Literal["fa-user-text-file-capability-v1"]
    conversation_id: Identifier
    read_at: AwareDatetime
    user_text_file_profile: UserTextFileProfile | None
    authorization: Literal[False]

    @field_validator("authorization", mode="before")
    @classmethod
    def no_authority(cls, value):
        if value is not False:
            raise ValueError("Capability metadata requires the literal false boolean")
        return value


def read_user_text_file_capability(control, principal, conversation_id):
    from .behavior_attachment_catalog import conversation_input_scope
    from .behavior_assistant import _read_conflicts
    control._human(principal, "read")
    control.refresh()

    def read():
        control._human(principal, "read")
        policy = control.installed()
        if not policy.active or not policy.read_enabled:
            raise Problem(403, "WORK_POLICY_REVOKED", "current standing read scope is required")
        _, _, profile = conversation_input_scope(control, principal, policy, conversation_id)
        control._human(principal, "read")
        return UserTextFileCapability(
            schema_version="fa-user-text-file-capability-v1", conversation_id=conversation_id,
            read_at=control.runtime._now(), user_text_file_profile=profile.instruction.user_text_file_profile,
            authorization=False,
        )

    return _read_conflicts(lambda: control.store.consistent_read(read), phase="user_text_file_capability")


class UserTextFile(WireModel):
    name: Filename
    text: Annotated[str, Field(max_length=MAX_USER_INPUT_BYTES)]

    @field_validator("text")
    @classmethod
    def utf8_text(cls, value):
        if "\x00" in value or len(value.encode("utf-8")) > MAX_USER_INPUT_BYTES:
            raise ValueError("Literal input files must be bounded UTF-8 text without NUL bytes")
        return value


UserTextFiles = Annotated[tuple[UserTextFile, ...], Field(max_length=4)]


def validate_user_files(content, files, *, reference_names=()):
    names = (*reference_names, *(item.name for item in files))
    if len(names) > 4 or len(set(names)) != len(names):
        raise ValueError("Literal files and source attachments need distinct names within the four-item bound")
    if files:
        raw = json.dumps({"content": content, "files": [item.model_dump(mode="json") for item in files]},
                         ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(raw) > MAX_USER_INPUT_BYTES:
            raise ValueError("The complete instruction and literal file input must fit the original transport bound")
    return files


class UserTextFileReference(WireModel):
    name: Filename
    sha256: Digest
    utf8_bytes: Annotated[int, Field(ge=0, le=MAX_USER_INPUT_BYTES)]


def file_reference(file):
    raw = file.text.encode("utf-8")
    return UserTextFileReference(name=file.name, sha256=hashlib.sha256(raw).hexdigest(), utf8_bytes=len(raw))


class UserFileName(WireModel):
    turn_number: TurnNumber
    name: Filename


class ReadUserFile(Command):
    turn_number: TurnNumber
    name: Filename


class UserFileRead(WireModel):
    schema_version: Literal["fa-user-file-read-v1"] = "fa-user-file-read-v1"
    usage: Literal["historical-data-not-current-instructions"] = "historical-data-not-current-instructions"
    turn_source: UserTurnSource
    file: UserTextFileReference
    text: str

    @model_validator(mode="after")
    def original_bytes(self):
        original = UserTextFile(name=self.file.name, text=self.text)
        if file_reference(original) != self.file or len(self.model_dump_json().encode("utf-8")) > 16384:
            raise ValueError("The exact historical file and complete response must fit their original bounds")
        return self


def read_original_file(control, principal, conversation_id, turn_number, name):
    control._human(principal, "read")
    try:
        TypeAdapter(Filename).validate_python(name, strict=True)
    except ValueError:
        raise Problem(422, "USER_FILE_NAME_INVALID", "one exact original logical filename is required") from None
    record = control._instruction_record(principal, conversation_id, turn_number)
    task = record.profile.instruction
    for file in task.files:
        if file.name == name:
            return file
    raise Problem(404, "USER_FILE_NOT_FOUND", "the original instruction has no file with this name")
