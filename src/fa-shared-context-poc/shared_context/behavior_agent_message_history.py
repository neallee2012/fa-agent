"""Read the same peer root, message and queue receipts after API restart."""

from .behavior_agent_message_api import AgentCollaborationRoot, collaboration_key, _route
from .behavior_agent_message_wire import CAPABILITY_ID, PeerTeamReference
from .behavior_agent_message_runtime import (
    PeerResultForward, REFERENCE_REFUSALS, SAVED_RESULT_LIMIT_NOTICE, STOPPED_PEER_NOTICE,
    reference_refusal_notice, stopped_peer_digest,
)
from .behavior_agent_messages import (
    AgentAssignment, AgentMessage, MessageEnvelope, RootEnvelope, _MessageState, _QueueClaim, _ReceiverQueue,
    _RootState, claim_key, message_key, message_state_key, queue_key, root_key, root_state_key,
)
from .behavior_capabilities import BUILTIN_CAPABILITIES
from .behavior_delegation import _current_parent, _parent_keys
from .behavior_operations import control_key
from .behavior_store import InlineMaterial
from .errors import Problem
from .source_materials import canonical, digest


MESSAGE_MODELS = (AgentCollaborationRoot, RootEnvelope, _RootState, MessageEnvelope, _MessageState,
                  _ReceiverQueue, _QueueClaim, PeerResultForward)


def validate_agent_message_history(inventory, config):
    from .behavior_agent_registry import AgentScopeRegistry, registry_key
    from .behavior_instruction import hosted_session_id, sdk_session_id
    from .behavior_recovery_history import _Inventory
    from .behavior_work_policy import WorkRegistration, StandingPolicyInstallation, work_policy_key
    models = {model.model_fields["kind"].default: model for model in MESSAGE_MODELS}
    selected = {key: models[row.payload["data"]["kind"]] for key, row in inventory.rows.items()
                if row.payload["data"].get("kind") in models}
    if not selected:
        return {}
    view = _Inventory(inventory, config)
    values = {key: view.row(key, model, material=True if model is AgentCollaborationRoot else None)
              for key, model in selected.items()}

    def require(condition, message):
        if not condition:
            raise Problem(409, "AGENT_MESSAGE_HISTORY_INVALID", message)

    for key, value in values.items():
        receipt = view.receipts.get(value.request.request_id)
        require(receipt is not None and receipt.request == value.request
                and dict(receipt.writes).get(key) == view.hash(key),
                "the current message or queue state requires its exact original receipt")

    roots = {}
    for key, value in values.items():
        if not isinstance(value, AgentCollaborationRoot):
            continue
        origin, spec = value.origin, value.spec
        record = view.row(origin.registration_id, WorkRegistration, material=True)
        _current_parent({key: inventory.rows.get(key) for key in _parent_keys(origin)}, origin, executing=False)
        a, b = value.directory.agents
        policy = view.row(work_policy_key("policy", config.table.scope.authority_domain),
                          StandingPolicyInstallation).policy
        peer = view.row(registry_key(policy.authority_domain, b.agent_id), AgentScopeRegistry).policy
        task = record.profile.instruction
        capture = None if task is None else task.capabilities
        grant = None if capture is None else next((grant for grant in capture.grants
                                                  if grant.entry_id == CAPABILITY_ID), None)
        require(key == collaboration_key(record.binding.binding_id) == value.request.request_id
                and grant is not None and grant.entry_sha256 == origin.capability_sha256
                == BUILTIN_CAPABILITIES[CAPABILITY_ID].entry_sha256
                and a.agent_id == config.targets[0].agent_id and b.agent_id == config.targets[1].agent_id
                and value.recipient_policy_sha256 == peer.sha256
                and spec.root_request_id == record.admission_id
                and spec.origin_agent_id == a.agent_id
                and spec.origin_work_id == record.binding.work.work_id
                and spec.origin_attempt_id == record.binding.attempt_id
                and spec.human_obligation_id == record.binding.obligation_id
                and spec.root_budget_id == record.binding.owner.root_budget_id
                and spec.max_hops == 2 and spec.max_messages == 8,
                "peer root escaped its original human assignment or approved two-agent scope")
        cid = control_key("agent-collaboration-conversation", record.admission_id, b.agent_id)
        require(a == _route(config, policy, conversation_id=task.conversation_id,
                            hosted_session_id=record.hosted_session_id, sdk_session_id=origin.sdk_session_id,
                            profile_sha256=origin.profile_sha256)
                and b == _route(config, peer, conversation_id=cid, hosted_session_id=hosted_session_id(cid),
                                sdk_session_id=sdk_session_id(cid), profile_sha256=digest(peer.profile.model_dump(mode="json"))),
                "retained peer identities or private conversation scopes differ")
        expected = digest([origin.model_dump(mode="json"), value.directory.model_dump(mode="json"),
                           spec.model_dump(mode="json"), peer.sha256, value.recipient_hosted_version])
        require(value.request.request_digest == expected
                and set(_parent_keys(origin)) <= set(value.request.read_set),
                "peer root lost its original assignment predicates")
        view.receipt(value.request, (key,), exact={key: view.hash(key)},
                     result={"root_request_id": spec.root_request_id, "model_started": False})
        require(spec.root_request_id not in roots, "one original human assignment cannot acquire another peer root")
        roots[spec.root_request_id] = value

    messages = {key: value for key, value in values.items() if isinstance(value, MessageEnvelope)}
    for key, message in messages.items():
        root = roots.get(message.root_request_id)
        require(root is not None, "a peer message lacks its server-recorded original assignment")
        sender = root.directory.resolve(message.sender_agent_id)
        receiver = root.directory.resolve(message.recipient_agent_id)
        payload = InlineMaterial.model_validate_json(canonical(inventory.rows[key].payload["material"]))
        command = AgentMessage.model_validate_json(payload.data)
        assignment = AgentAssignment(root_request_id=message.root_request_id, agent_id=message.sender_agent_id,
                                     work_id=message.from_work_id, attempt_id=message.from_attempt_id)
        expected_key = (control_key("agent-message-contract-receipt", command.in_reply_to, sender.agent_id)
                        if command.kind == "receipt" else message_key(message.root_request_id, sender.agent_id, command.message_id))
        previous = None if command.in_reply_to is None else messages.get(command.in_reply_to)
        require(key == expected_key == message.message_key
                and message.sender_oid == sender.principal_oid and sender.agent_id != receiver.agent_id
                and command.message_id == message.message_id and command.kind == message.message_kind
                and command.recipient_agent_id == receiver.agent_id
                and payload.sha256 == message.payload_sha256 == digest(command.model_dump(mode="json"))
                and payload.data == canonical(command.model_dump(mode="json"))
                and message.root_budget_id == root.spec.root_budget_id
                and message.request.request_digest == digest([assignment.model_dump(mode="json"),
                    command.model_dump(mode="json"), root.directory.sha256]),
                "peer message identity, payload or original receipt differs")
        for ref in command.refs:
            PeerTeamReference.model_validate(ref.model_dump(mode="json"))
            require(ref in root.spec.allowed_refs, "a peer message added an unapproved reference")
        if command.in_reply_to is None:
            require(message.sender_agent_id == root.spec.origin_agent_id
                    and message.from_work_id == root.spec.origin_work_id
                    and message.from_attempt_id == root.spec.origin_attempt_id
                    and command.kind in {"request", "handoff", "fyi"}
                    and message.causation_id == message.root_request_id and message.hop == 1
                    and message.to_work_id == control_key("agent-message-contract-work", key, receiver.agent_id),
                    "the original peer handoff escaped its human root")
        else:
            require(previous is not None and previous.root_request_id == message.root_request_id
                    and previous.recipient_agent_id == sender.agent_id and previous.sender_agent_id == receiver.agent_id
                    and previous.to_work_id == message.from_work_id and previous.from_work_id == message.to_work_id
                    and message.causation_id == previous.message_key
                    and message.hop == previous.hop + int(command.kind != "receipt")
                    and command.kind in {"result", "blocked", "fyi", "receipt"}
                    and not (command.kind == previous.message_kind == "receipt"),
                    "peer response lost its original work or causation")
        require(message.hop <= root.spec.max_hops, "peer response exceeded the original hop allowance")
        state = values.get(message_state_key(key))
        require(isinstance(state, _MessageState) and state.message_key == key,
                "a peer message lost its retained queue state")
        if state.status == "silenced":
            require(command.kind in {"fyi", "receipt"} and state.decision.action == "silence",
                    "only informational messages may be silenced before processing")

    for root_id, root in roots.items():
        envelope = values.get(root_key(root_id))
        state = values.get(root_state_key(root_id))
        if envelope is None:
            require(state is None and not any(message.root_request_id == root_id for message in messages.values()),
                    "a partial original root cannot already contain messages")
            continue
        require(isinstance(envelope, RootEnvelope) and envelope.spec == root.spec
                and envelope.authority_binding == config.workers.control_binding
                and envelope.directory_sha256 == root.directory.sha256
                and envelope.cost_ledger_id == work_policy_key("cost", config.table.scope.authority_domain)
                and isinstance(state, _RootState) and state.root_request_id == root_id,
                "the ledger root lost its server-recorded scope or single cost ledger")
        original = _RootState(root_request_id=root_id, request=envelope.request)
        view.receipt(envelope.request, (root_key(root_id), root_state_key(root_id)), exact={
            root_key(root_id): view.hash(root_key(root_id)), root_state_key(root_id): view.image_hash(root_state_key(root_id), original),
        }, result={"root_request_id": root_id, "cost_ledger_id": envelope.cost_ledger_id, "new_budget": False})
        retained = [message for message in messages.values() if message.root_request_id == root_id]
        require(len(state.message_keys) == len(set(state.message_keys))
                and set(state.message_keys) == {message.message_key for message in retained}
                and state.message_count == sum(message.message_kind != "receipt" for message in retained)
                and state.receipt_count == sum(message.message_kind == "receipt" for message in retained)
                and state.message_count <= root.spec.max_messages
                and {message.message_key for message in retained if message.message_kind in {"request", "handoff"}}
                == (set() if state.handoff_message_id is None else {state.handoff_message_id})
                and {message.message_key for message in retained if message.message_kind in {"result", "blocked"}}
                == (set() if state.response_message_id is None else {state.response_message_id}),
                "the original message counter or handoff/result index differs")

    for key, value in values.items():
        if isinstance(value, (RootEnvelope, _RootState)):
            require(value.spec.root_request_id in roots if isinstance(value, RootEnvelope)
                    else value.root_request_id in roots, "orphan peer ledger root")
        elif isinstance(value, _ReceiverQueue):
            require(key == queue_key(value.agent_id) and len(value.pending) == len(set(value.pending)),
                    "receiver queue identity or ordering differs")
            for identifier in value.pending:
                message = messages.get(identifier)
                state = values.get(message_state_key(identifier))
                require(message is not None and message.recipient_agent_id == value.agent_id
                        and isinstance(state, _MessageState) and state.status in {"queued", "needs_attention", "claimed", "unknown"},
                        "the retained queue refers to another receiver or an already-consumed message")
        elif isinstance(value, _MessageState):
            require(key == message_state_key(value.message_key) and value.message_key in messages,
                    "orphan peer message state")
            if value.claim_id is not None:
                claim = values.get(value.claim_id)
                require(isinstance(claim, _QueueClaim) and claim.message_key == value.message_key,
                        "peer message lost its original claim")
        elif isinstance(value, _QueueClaim):
            message = messages.get(value.message_key)
            require(message is not None and key == value.claim_id == claim_key(value.message_key)
                    and value.receiver_agent_id == message.recipient_agent_id
                    and value.assignment.root_request_id == message.root_request_id
                    and value.assignment.agent_id == value.receiver_agent_id
                    and value.assignment.work_id == message.to_work_id,
                    "peer queue claim escaped its original receiver work")
        elif isinstance(value, PeerResultForward):
            from .behavior_assistant import key as assistant_key
            from .behavior_assistant_wire import Task
            from .behavior_delivery import VerifiedWorkResult
            from .behavior_instruction import SavedReply, unpack_reply
            from .behavior_workers import ExecutorEvidence, JobRecord, worker_key
            job_key = worker_key("job", value.job_id)
            job = view.row(job_key, JobRecord)
            task = Task.model_validate_json(job.spec.input).instruction
            require(task is not None and task.peer_work is not None
                    and task.peer_work.root_id == value.root_id and job.result_forwarded
                    and job.state in {"succeeded", "failed", "cancelled"}
                    and job.spec.obligation_id is None and job.spec.parent_job_id is None,
                    "peer forwarding requires its completed independent receiver job")
            claim = values.get(value.claim_id)
            message = messages.get(value.result_message_key)
            state = values.get(message_state_key(task.peer_work.message_key))
            result_key = assistant_key("result", job.spec.work_id)
            completion_key = worker_key("completion", value.job_id, str(job.generation))
            stopped_without_result = value.stop_evidence_sha256 is not None
            if stopped_without_result:
                completion = view.row(completion_key, ExecutorEvidence, material=True)
                require(value.result_sha256 is None and result_key not in inventory.rows
                        and assistant_key("reply", value.job_id) not in inventory.rows
                        and value.stop_evidence_sha256 == stopped_peer_digest(job, completion),
                        "the peer stop notification lost its original verified evidence")
                result = None
            elif value.result_sha256 is None:
                from .behavior_store import ControlRequest
                require(job.state == "cancelled" and job.generation == 0 and job.binding is None
                        and job.cancel_requested and job.cancel_credit == 0
                        and result_key not in inventory.rows and completion_key not in inventory.rows,
                        "pre-SDK peer cancellation cannot conceal an executed work")
                pool_key = worker_key("pool", config.workers.workspace_id, value.job_id)
                cancel = ControlRequest(request_id=worker_key("cancel", value.job_id),
                    request_digest=digest(["cancel", [value.job_id, job.owner.model_dump(mode="json")],
                        digest({"contract": config.workers.model_dump(mode="json"), "mode": "native"})]),
                    read_set=tuple(sorted({job_key, pool_key, control_key("owner", job.owner.owner_record_id),
                                           control_key("policy", job.owner.policy_id)})))
                view.receipt(cancel, (job_key, pool_key), exact={
                    job_key: view.image_hash(job_key, job.model_copy(update={"result_forwarded": False})),
                    pool_key: view.hash(pool_key),
                }, result={"actual_environment": "NOT_RUN", "status": "cancelled", "cancel_accepted": True, "executor_stopped": True})
                result, completion = None, None
            else:
                completion = view.row(completion_key, ExecutorEvidence, material=True)
                if result_key not in inventory.rows and completion.result is not None:
                    result_key = worker_key("work-result", completion.result.evidence_id)
                result = view.row(result_key, VerifiedWorkResult, material=True)
                require(completion.binding == job.binding and completion.evidence_id == job.last_evidence_id
                        and completion.result == result and result.work_id == job.spec.work_id
                        and value.result_sha256 == digest(result.model_dump(mode="json")),
                        "peer forwarding lost its original SDK result")
            require(key == control_key("agent-message-result-forward", value.job_id) == value.request.request_id
                    and isinstance(claim, _QueueClaim) and claim.assignment.work_id == job.spec.work_id
                    and claim.message_key == task.peer_work.message_key
                    and isinstance(state, _MessageState) and state.status == "completed"
                    and state.claim_id == value.claim_id
                    and message is not None and message.message_kind in {"result", "blocked"}
                    and message.from_work_id == job.spec.work_id
                    and message.causation_id == task.peer_work.message_key,
                    "peer forwarding lost its original completion, result or queue receipt")
            payload = InlineMaterial.model_validate_json(canonical(inventory.rows[value.result_message_key].payload["material"]))
            response = AgentMessage.model_validate_json(payload.data)
            if stopped_without_result:
                require(response.kind == "blocked" and response.content == STOPPED_PEER_NOTICE and not response.refs,
                        "a verified peer stop cannot be presented as an SDK result")
            elif result is None:
                require(response.kind == "blocked" and response.content == "The peer work was cancelled before SDK execution."
                        and not response.refs, "pre-SDK cancellation must remain a data-only blocked result")
            elif result.state == "succeeded":
                saved = view.row(assistant_key("reply", value.job_id), SavedReply)
                original_payload = InlineMaterial.model_validate_json(
                    canonical(inventory.rows[task.peer_work.message_key].payload["material"]))
                original_message = AgentMessage.model_validate_json(original_payload.data)
                complete = AgentMessage(message_id=response.message_id, recipient_agent_id=response.recipient_agent_id,
                    kind="result", content=unpack_reply(saved).text,
                    in_reply_to=task.peer_work.message_key, refs=original_message.refs)
                if response.kind == "blocked":
                    from .behavior_agent_message_wire import MAX_AGENT_MESSAGE_BYTES
                    size_refusal = (len(canonical(complete.model_dump(mode="json"))) > MAX_AGENT_MESSAGE_BYTES
                                    and response.content == SAVED_RESULT_LIMIT_NOTICE)
                    source_refusal = bool(original_message.refs) and response.content in {
                        reference_refusal_notice(code) for code in REFERENCE_REFUSALS}
                    require((size_refusal or source_refusal) and not response.refs,
                            "only a fixed data-free refusal may replace the intact saved result in a peer message")
                else:
                    require(response == complete, "forwarded peer text differs from its complete saved result")
            reads = (job_key, worker_key("pool", config.workers.workspace_id, value.job_id),
                     completion_key, result_key, key, value.result_message_key, message_state_key(task.peer_work.message_key))
            require(value.request.read_set == reads and value.request.request_digest == digest([
                value.root_id, value.job_id, value.claim_id, value.result_message_key, value.result_sha256,
                *((value.stop_evidence_sha256,) if stopped_without_result else ())]),
                "peer forwarding receipt differs from the original result")
            view.receipt(value.request, (job_key, key), exact={job_key: view.hash(job_key), key: view.hash(key)},
                         result={"job_id": value.job_id, "result_message_key": value.result_message_key,
                                 "human_obligation_fulfilled": False})
    return values
