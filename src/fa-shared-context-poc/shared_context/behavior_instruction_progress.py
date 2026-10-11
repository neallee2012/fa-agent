"""Small public SDK snapshots; never reasoning text, arguments, prompts or credentials."""
from typing import Literal

from . import behavior_assistant_wire as aw
from .behavior_operations import _Model,_row,_write
from .behavior_store import ControlChanges
from .behavior_wire import Identifier
from .errors import Problem


class ProgressRecord(_Model):
    kind:Literal["assistant_public_progress"]="assistant_public_progress"
    session_id:Identifier
    job_id:Identifier
    conversation_id:Identifier
    turn_number:int
    progress:aw.PublicProgress


def progress_record(session,progress):
    task=session.assignment.task.instruction
    if task is None or not progress.events:
        raise Problem(422,"INSTRUCTION_PROGRESS_INVALID","original instruction public events required")
    if (progress.reasoning_summary.availability=="provided" or any(event.reasoning_summary is not None for event in progress.events)):
        if task.model_provider!="openai-responses" or task.reasoning_summary is None:
            raise Problem(422,"INSTRUCTION_PROGRESS_INVALID","public reasoning was not enabled for this original task")
    summaries=[event.reasoning_summary for event in progress.events if event.reasoning_summary is not None]
    if summaries and (progress.reasoning_summary.availability!="provided" or progress.reasoning_summary.text!=summaries[-1]):
        raise Problem(422,"INSTRUCTION_PROGRESS_INVALID","public reasoning metadata differs from the visible summary")
    events=progress.events
    if [event.seq for event in events]!=sorted({event.seq for event in events}):
        raise Problem(422,"INSTRUCTION_PROGRESS_INVALID","public event sequence differs")
    if len(progress.model_dump_json().encode())>8192:
        raise Problem(413,"INSTRUCTION_PROGRESS_INVALID","public snapshot exceeds its storage envelope")
    return ProgressRecord(session_id=session.assignment.session_id,job_id=session.execution.job_id,
        conversation_id=task.conversation_id,turn_number=task.turn_number,progress=progress)


def save_progress(authority,session,command):
    from .behavior_assistant import key
    record=progress_record(session,command.progress)
    events=record.progress.events
    pkey=key("progress",session.assignment.session_id)
    def decide(rows):
        previous=rows[pkey]
        if previous is not None:
            old=_row(rows,pkey,ProgressRecord)
            if old.progress.events[-1].seq>=events[-1].seq:
                return ControlChanges(result={"state":"accepted"})
        return ControlChanges(writes=(_write(pkey,record,"create" if previous is None else "replace"),),
                              result={"state":"accepted"})
    return aw.ToolReceipt(**authority._command(session,"progress",command,decide,extra=(pkey,)).payload["result"])
