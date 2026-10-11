"""Pure SDK assignment preparation from an admitted work and its real worker start."""

import hashlib

from . import behavior_assistant_wire as aw
from .behavior_assistant import key, task_for_profile
from .behavior_operations import ExecutionContext
from .behavior_wire import SdkStartBinding, sdk_start_version
from .errors import Problem
from .source_materials import digest


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


def prepare_vm_assignment(registration, job, start, *, worker_configuration_digest, installed_code_sha256,
                          agent_message_recipient=None, previous_assignment=None, previous_archive=None):
    """No Table/Blob/SDK I/O, native claim, new credential, or mutation of the supplied records."""
    binding = registration.binding
    task = task_for_profile(binding, registration.profile)
    original_input = task.model_dump_json().encode()
    if (task.instruction is None or job.binding is None or job.binding != start.binding
            or job.owner != start.binding.owner or job.configuration_digest != worker_configuration_digest
            or job.state not in {"starting", "running", "capacity_uncertain"} or job.cancel_requested
            or job.spec.job_id != binding.job_id or job.spec.work_id != binding.work.work_id
            or job.spec.input != original_input or start.input != original_input
            or start.binding.job_id != binding.job_id or start.binding.work_id != binding.work.work_id
            or start.binding.input_digest != hashlib.sha256(original_input).hexdigest()
            or any(getattr(start.binding.owner, field) != getattr(binding.owner, field)
                   for field in ExecutionContext.model_fields if field != "epoch")):
        raise Problem(403, "ASSISTANT_ASSIGNMENT_DENIED", "the original admitted job and actual ExecutorBinding are required")
    from .behavior_instruction import sdk_session_id
    sid = key("session-id", start.binding.start_request_id)
    sdk_id = sdk_session_id(task.instruction.conversation_id)
    if start.checkpoint is not None:
        checkpoint = aw.Checkpoint.model_validate_json(start.checkpoint)
        if (previous_assignment is None or previous_archive is None or job.checkpoint != start.checkpoint
                or previous_assignment.task != task or previous_assignment.session_id != checkpoint.session_id
                or previous_archive.sdk_session_id != previous_assignment.sdk_session_id
                or checkpoint.task_digest != digest(task.model_dump(mode="json"))
                or checkpoint.archive_digest != previous_archive.sha256):
            raise Problem(403, "ASSISTANT_CHECKPOINT_INVALID", "the actual original resume materials are required")
        sdk_id = previous_assignment.sdk_session_id
    assignment = aw.Assignment(
        binding_id=binding.binding_id, session_id=sid, sdk_session_id=sdk_id, task=task,
        resume=start.checkpoint is not None or task.instruction.previous_archive_id is not None,
        agent_message_recipient=agent_message_recipient,
    )
    profile = task.instruction.sdk_start_profile
    if profile is not None:
        assignment = assignment.model_copy(update={"sdk_start_binding": SdkStartBinding(
            schema_version="fa-sdk-start-binding-" + sdk_start_version(profile), profile=profile,
            session_id=sid, sdk_session_id=sdk_id, job_id=binding.job_id,
            task_digest=digest(task.model_dump(mode="json")), profile_digest=task.profile_digest,
            execution_digest=digest(start.binding.model_dump(mode="json")),
            worker_configuration_digest=worker_configuration_digest, code_sha256=installed_code_sha256,
        )})
    return assignment
