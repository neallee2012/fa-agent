"""Pure SDK assignment preparation from an admitted work and its real worker start."""


from .errors import Problem


def capture_agent_vm(control, conversation, row, vm):
    """Capture a server-selected Agent VM before the first conversation write."""
    from .behavior_instruction import Conversation
    template=control.policy.template
    native=control.runtime.dep.native_launches
    configured=(() if native is None else
                tuple(target for target in native.targets if target.agent_oid==template.agent_oid))
    expected_agent=(control.policy.agent_id if control.policy.agent_id is not None else
                    configured[0].agent_id if len(configured)==1 else None)
    if vm.team_id!=template.team_id or vm.agent_id!=expected_agent:
        raise Problem(403,"CONVERSATION_VM_BINDING_DENIED","the original server-resolved Team and Agent are required")
    if row is not None:
        if conversation.agent_vm!=vm:
            raise Problem(409,"CONVERSATION_VM_BINDING_DENIED","an existing conversation cannot change its VM capture")
        return conversation
    if conversation.jobs or conversation.archive_id is not None or conversation.active_job_id is not None:
        raise Problem(409,"CONVERSATION_VM_BINDING_DENIED","only a new, unstarted conversation may capture its VM")
    return Conversation.model_validate({**conversation.model_dump(),
        "hosted_session_id":vm.hosted_session_id,"hosted_agent_version":vm.hosted_agent_version,"agent_vm":vm})

