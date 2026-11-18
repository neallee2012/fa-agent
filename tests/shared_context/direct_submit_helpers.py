"""Put an instruction into work state through WorkPolicyControl.submit, without the HTTP submit route."""

from shared_context.behavior_assistant_wire import InstructionSubmit


def submit_direct(h, request_id, text, conversation=None):
    """Same admission and result dict as the submit route; the principal comes from the harness's human token."""
    command = InstructionSubmit(request_id=request_id, content=text, conversation_id=conversation)
    principal = h.verifier(h.client.build_request("GET", "/", headers=h.headers()))
    control = h.runtime.work_policy
    control._human(principal, "submit")
    binding, status = control.submit(principal, command)
    turn = control.records[binding.binding_id].profile.instruction
    return {"binding_id": binding.binding_id, "status": status, "conversation_id": turn.conversation_id,
            "turn_number": turn.turn_number, "model": turn.model}, command
