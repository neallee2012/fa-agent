"""Small public SDK snapshots; never reasoning text, arguments, prompts or credentials."""
from typing import Literal

from . import behavior_assistant_wire as aw
from .behavior_operations import _Model
from .behavior_wire import Identifier


class ProgressRecord(_Model):
    kind:Literal["assistant_public_progress"]="assistant_public_progress"
    session_id:Identifier
    job_id:Identifier
    conversation_id:Identifier
    turn_number:int
    progress:aw.PublicProgress

