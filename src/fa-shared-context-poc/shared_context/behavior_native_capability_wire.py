"""Native capability DTOs shared by the API and the Hosted tool adapter, without runtime SDK imports.

Every request names the original native turn (conversation and RID); the Agent is the verified caller.
"""

from typing import Annotated

from pydantic import Field, JsonValue

from .behavior_wire import Digest, Identifier, WireModel
from .m3_models import Filename

NATIVE_CAPABILITY_PREFIX = "/work-policy/v1/native-capabilities"


class NativeTurnScope(WireModel):
    conversation_id: Identifier
    request_id: Identifier


class NativeSkillRead(NativeTurnScope):
    name: Identifier


class NativeSkillResourceRead(NativeSkillRead):
    resource: Filename


class NativeCapabilityCall(NativeTurnScope):
    tool_call_id: Annotated[str, Field(min_length=1, max_length=256, pattern=r"^[\x21-\x7e]+$")]
    tool: Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")]
    arguments: dict[str, JsonValue]
    input_schema_sha256: Digest
