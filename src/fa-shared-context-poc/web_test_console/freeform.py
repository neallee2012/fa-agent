"""Free instruction/conversation thin client over the standing HTTP contract.

Requires the matching frozen backend InstructionSubmit implementation. Old
console modes do not import this module or acquire new submission rights.
"""
import secrets
from typing import Annotated, Literal

from pydantic import Field, SecretStr, TypeAdapter, model_validator

from shared_context import behavior_wire as wire
from shared_context.behavior_assistant_wire import (
    InstructionSubmit, ModelDeployment as ModelDeploymentId, PublicProgress, TextReply,
)
from shared_context.behavior_work_policy import WorkPreflight, WorkPreflightQuery, WorkSubmitObservation, derive_work_binding
from shared_context.behavior_model_capacity import ModelCapacitySnapshot
from shared_context.behavior_skill_prompt import AssemblyRecord
from shared_context.behavior_skill_copy import SopCopyOutcome
from shared_context.behavior_skill_draft import SkillDraftOutcome
from shared_context.behavior_delegation_wire import DelegationReply, MAX_DELEGATION_BYTES
from shared_context.behavior_turn_context import MAX_TURN_CONTEXT_BYTES, TurnContext
from shared_context.behavior_attachment_catalog import AttachmentSourceCatalog
from shared_context.behavior_text_files import MAX_USER_INPUT_BYTES, UserTextFileCapability, file_reference
from shared_context.m3_models import Filename
from shared_context.behavior_workers import TERMINAL_STATES, worker_key
from .adapter import FixedTask, canonical, require, sha, strict_json, ConsoleError
from .instructions import InstructionStore, InstructionConnection, INSTRUCTION_QUOTED_UTF8_LIMIT
from .standing import (
    StandingConsoleConfig, StandingPreflightCheck, StandingPullAdapter,
)
from .work_policy import WorkPolicyQuery



class InstructionReply(wire.WireModel):
    binding_id: wire.Identifier
    status: wire.JobStatus
    conversation_id: wire.Identifier
    turn_number: Annotated[int, Field(ge=1)]
    model: ModelDeploymentId


class InstructionContext(wire.WireModel):
    conversation_id: wire.Identifier
    turn_number: Annotated[int, Field(ge=1)]
    model: ModelDeploymentId | None = None


class TextReference(wire.WireModel):
    schema_version: Literal["fa-assistant-text-reference-v1"]
    reply_id: wire.Identifier
    sha256: wire.Digest
    byte_length: Annotated[int, Field(ge=1)]


def checked_delivery_reference(content_bytes, reply_bytes):
    try:
        content = strict_json(content_bytes)
        strict_json(reply_bytes)
        expected = TextReply.model_validate_json(reply_bytes)
        if isinstance(content, dict) and content.get("schema_version") == "fa-assistant-text-reference-v1":
            reference = TextReference.model_validate_json(content_bytes)
            require(reference.sha256 == sha(reply_bytes) and reference.byte_length == len(reply_bytes),
                    "RESULT_BINDING_MISMATCH", 502)
        else:
            answer = TextReply.model_validate_json(content_bytes)
            require(answer == expected, "RESULT_BINDING_MISMATCH", 502)
    except (ValueError, TypeError):
        raise ConsoleError("RESULT_INVALID", 502) from None


class ConversationTurn(wire.WireModel):
    request_id: wire.Identifier
    binding_id: wire.Identifier
    job_id: wire.Identifier
    work_id: wire.Identifier
    turn_number: Annotated[int, Field(ge=1)]
    state: str
    model_requests: Annotated[int, Field(ge=0)]
    reply_available: bool
    delivery_id: wire.Identifier | None
    model: ModelDeploymentId
    progress: PublicProgress
    stream: "ReplyPreview | None" = Field(default=None,exclude_if=lambda value:value is None)
    model_capacity: ModelCapacitySnapshot | None = Field(default=None, exclude_if=lambda value:value is None)
    prompt_assembly: AssemblyRecord | None = Field(default=None, exclude_if=lambda value:value is None)
    sop_copy_outcome: SopCopyOutcome | None = Field(default=None, exclude_if=lambda value:value is None)
    skill_draft_outcome: SkillDraftOutcome | None = Field(default=None, exclude_if=lambda value:value is None)

    @model_validator(mode="after")
    def declared_capacity_model(self):
        if self.model_capacity is not None and self.model_capacity.model != self.model:
            raise ValueError("Declared capacity belongs to another turn model")
        return self


class ReplyPreview(wire.WireModel):
    text: Annotated[str,Field(max_length=32768)]
    sequence: Annotated[int,Field(ge=1)]
    observed_at: str
    durable: Literal[False]
    complete: Literal[False]


class ConversationProjection(wire.WireModel):
    conversation_id: wire.Identifier
    sdk_session_id: wire.Identifier | None
    turns: tuple[ConversationTurn, ...]
    can_follow_up: bool
    follow_up_blocker: str | None
    hosted_session_id: wire.Identifier | None = None
    hosted_agent_version: str | None = None
    archived: bool = False
    can_reset: bool = False


class ResetReply(wire.WireModel):
    conversation_id: wire.Identifier
    archived: Literal[True]
    reset_request_id: wire.Identifier
    hosted_session_id: wire.Identifier | None
    job_id: wire.Identifier | None
    session_stop: Literal["confirmed","unknown","not_run"]


class CancellationResult(wire.WireModel):
    actual_environment: Literal["NOT_RUN"]
    status: Literal["queued", "starting", "running", "capacity_uncertain", "waiting_dependency",
                    "succeeded", "failed", "cancelled"]
    cancel_accepted: bool
    executor_stopped: bool | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def acceptance_is_not_a_stop(self):
        if self.cancel_accepted:
            if (self.executor_stopped is None or
                    (self.status == "cancelled") != self.executor_stopped or
                    self.status in {"succeeded", "failed", "queued", "waiting_dependency"}):
                raise ValueError("Cancellation acceptance must retain its actual stop observation")
        elif self.status not in TERMINAL_STATES or self.executor_stopped is not None:
            raise ValueError("An already terminal job has no new cancellation or stop receipt")
        return self


class CancellationReceipt(wire.WireModel):
    result: CancellationResult
    control_request_id: wire.Digest
    control_request_digest: wire.Digest


class ModelDeployment(wire.WireModel):
    id: ModelDeploymentId
    display_name: str
    context_window_tokens: Annotated[int, Field(gt=0)] | None
    max_output_tokens: Annotated[int, Field(gt=0)] | None
    reasoning_summary_available: bool
    # B1 PA-08: the composer offers images only for a deployment that declares image input.
    supports_image_input: Literal[True] | None = Field(default=None, exclude_if=lambda value: value is None)


class AvailableModels(wire.WireModel):
    default_model: ModelDeploymentId | None
    models: tuple[ModelDeployment, ...]


class InstructionAdapter(StandingPullAdapter):
    def __init__(self, config, client, store, *, api_prefix=""):
        super().__init__(config, client, api_prefix=api_prefix)
        self.store = store
        self.preflights, self.admissions = {}, {}
        self.control = None

    def instruction(self, task):
        body = self.store.command(task.submit_request_id)
        require(body is not None, "INSTRUCTION_SCOPE")
        try:
            value = InstructionSubmit.model_validate_json(canonical(body))
        except (ValueError, TypeError):
            raise ConsoleError("INSTRUCTION_CHANGED") from None
        require(value.content == self.store.get(task.submit_request_id)["content"], "INSTRUCTION_CHANGED")
        return value

    def request(self, task, action):
        if action == "submit" and self.store.command(task.submit_request_id) is not None:
            return "POST", "/work-policy/v1/submit", self.instruction(task).model_dump(mode="json")
        return super().request(task, action)

    def checked_reply(self, task, raw):
        try:
            strict_json(raw)
            reply = InstructionReply.model_validate_json(raw)
        except (ValueError, TypeError):
            raise ConsoleError("STATUS_INVALID", 502) from None
        expected = self.preflights.get(task.task_id)
        instruction = self.instruction(task)
        if expected is None:
            prior = self.store.journal.get(task.task_id, "submit")
            require(prior is not None and prior["intent"]["body_sha256"] ==
                    sha(canonical(instruction.model_dump(mode="json"))), "INSTRUCTION_CHANGED")
            try:
                expected = InstructionContext.model_validate_json(canonical(prior["intent"]["instruction_context"]))
            except (KeyError, ValueError, TypeError):
                raise ConsoleError("WORK_POLICY_INVALID", 502) from None
        require(reply.binding_id == task.binding_id and reply.status.job_id == task.job_id
                and reply.status.work_id == task.work_id
                and (instruction.conversation_id is None or instruction.conversation_id == reply.conversation_id)
                and (instruction.model is None or reply.model == instruction.model)
                and (instruction.conversation_id is not None or reply.turn_number == 1)
                and (reply.conversation_id, reply.turn_number) ==
                     (expected.conversation_id, expected.turn_number)
                and (expected.model is None or reply.model == expected.model), "STATUS_BINDING_MISMATCH", 502)
        self.admissions[task.task_id] = reply
        return reply

    def submit(self, task, token):
        raw, _ = self.send(*self.request(task, "submit"), token)
        return self.checked_reply(task, raw).status

    def reconcile(self, task, token):
        raw, _ = self.send("POST", "/work-policy/v1/submit-reconcile",
                           self.instruction(task).model_dump(mode="json"), token)
        try:
            body=strict_json(raw)
            if type(body) is dict and body.get("schema_version")=="fa-work-submit-observation-v1":
                require(body.get("retry_same_request") is True,"SUBMISSION_OBSERVATION_INVALID",502)
                value=WorkSubmitObservation.model_validate_json(raw)
                require(value.request_id==task.task_id
                        and value.submit_sha256==sha(self.instruction(task).model_dump_json().encode("utf-8")),
                        "SUBMISSION_OBSERVATION_INVALID",502)
                return value
        except (ValueError,TypeError):
            raise ConsoleError("SUBMISSION_OBSERVATION_INVALID",502) from None
        return self.checked_reply(task, raw)

    def cancel(self, task, body, generation, token):
        raw, _ = self.send("POST", f"/work-policy/v1/bindings/{task.binding_id}/cancel", body, token)
        return self.checked_cancellation(task, generation, raw)

    def read_cancellation(self, task, generation, token):
        try:
            raw, headers = self.send("GET",
                f"/work-policy/v1/bindings/{task.binding_id}/cancel-receipt/{generation}", None, token)
        except ConsoleError as error:
            if error.upstream_status == 404:
                raise ConsoleError("CANCELLATION_RECEIPT_UNAVAILABLE", 409, 404) from None
            raise
        require(headers.get("cache-control") == "no-store" and headers.get("x-content-sha256") == sha(raw),
                "CANCELLATION_INVALID", 502)
        return self.checked_cancellation(task, generation, raw)

    def checked_cancellation(self, task, generation, raw):
        try:
            strict_json(raw)
            receipt = CancellationReceipt.model_validate_json(raw)
        except (ValueError, TypeError):
            raise ConsoleError("CANCELLATION_INVALID", 502) from None
        expected = (worker_key("cancel-bound", task.job_id, worker_key("start", task.job_id, str(generation)))
                    if generation else worker_key("cancel", task.job_id))
        require(receipt.control_request_id == expected, "CANCELLATION_INVALID", 502)
        return receipt

    def read_text_reply(self, conversation_id, turn_number, sdk_session_id, token):
        raw, headers = self.send("GET",
            f"/work-policy/v1/conversations/{conversation_id}/turns/{turn_number}/reply", None, token)
        require(len(headers.get_list("x-content-sha256")) == 1
                and headers["x-content-sha256"] == sha(raw)
                and headers.get("content-type","").split(";")[0] == "application/json"
                and headers.get("content-encoding","identity") == "identity"
                and "no-store" in {v.strip().lower() for v in headers.get("cache-control","").split(",")},
                "RESULT_INVALID", 502)
        try:
            strict_json(raw)
            reply = TextReply.model_validate_json(raw)
        except (ValueError, TypeError):
            raise ConsoleError("RESULT_INVALID", 502) from None
        require((reply.conversation_id, reply.turn_number, reply.sdk_session_id) ==
                (conversation_id, turn_number, sdk_session_id), "RESULT_BINDING_MISMATCH", 502)
        return reply, raw

    def fetch(self, task, token, delivery_id):
        result = super().fetch(task, token, delivery_id)
        if result.parsed.work_state == "succeeded" and result.parsed.message_kind == "final":
            expected = self.control.admission(task)
            if expected is None:
                expected = self.control.reader.read(self.control.query(task), SecretStr(token)).preflight
            projection = self.control.refresh_conversation(token, expected.conversation_id)
            turn = next((turn for turn in projection.turns if turn.request_id == task.task_id), None)
            require(turn is not None and turn.turn_number == expected.turn_number, "RESULT_BINDING_MISMATCH", 502)
            self.control.read_reply(token, projection, turn)
            self.check_delivery_link(task, result)
        return result

    def check_delivery_link(self, task, result):
        raw = self.control.reply_bytes.get(task.task_id)
        require(raw is not None, "RESULT_BINDING_MISMATCH", 502)
        checked_delivery_reference(result.parsed.content, raw)


class InstructionReader(StandingPreflightCheck):
    def __init__(self, config, adapter, clock, control):
        require(type(config) is StandingConsoleConfig and type(adapter) is InstructionAdapter, "WORK_POLICY_INVALID")
        self.config, self.adapter, self.clock, self.control = config, adapter, clock, control
        self.last_guard_revision = None
        self.guard_by_request = {}

    def read(self, query, bearer):
        task = self.control.task(query.submit_request_id)
        command = self.adapter.instruction(task)
        expected_query = self.control.query(task).model_copy(update={"read_id": query.read_id})
        require(query == expected_query and query.submit_body_sha256 ==
                sha(command.model_dump_json().encode("utf-8")), "WORK_POLICY_INVALID", 502)
        body = WorkPreflightQuery(read_id=query.read_id, request_id=command.request_id,
            submit_body_sha256=query.submit_body_sha256, content=command.content,
            conversation_id=command.conversation_id, model=command.model, attachments=command.attachments, files=command.files)
        raw, _ = self.adapter.send("POST", "/work-policy/v1/preflight", body.model_dump(mode="json"),
                                   bearer.get_secret_value())
        _, binding = derive_work_binding(self.config.standing_policy, command.request_id, instruction=command)
        observation = self.checked(query, task, binding, raw, instruction=True,
                                   previous_guard=self.guard_by_request.get(task.task_id))
        result = observation.preflight
        require(result.conversation_id is not None and result.turn_number >= 1
                and (command.model is None or result.model == command.model)
                and (command.conversation_id is None and result.turn_number == 1
                     or command.conversation_id == result.conversation_id), "WORK_POLICY_INVALID", 502)
        previous = self.adapter.preflights.get(task.task_id)
        require(previous is None or (previous.conversation_id, previous.turn_number) ==
                (result.conversation_id, result.turn_number), "WORK_POLICY_INVALID", 502)
        self.adapter.preflights[task.task_id] = result
        self.guard_by_request[task.task_id] = result.guard_revision
        return observation


class InstructionControl:
    def __init__(self, console, connection):
        self.console = console
        self.config, self.journal, self.clock = console.config, console.journal, console.clock
        require(type(self.config) is StandingConsoleConfig and type(connection) is InstructionConnection
                and connection.configuration_sha256 == self.config.sha256, "CONFIG_CHANGED")
        self.connection = connection
        self.store = InstructionStore(self.journal, self.clock)
        self.adapter = InstructionAdapter(self.config, console.backend, self.store, api_prefix=console.api_prefix)
        self.adapter.control = self
        self.reader = InstructionReader(self.config, self.adapter, self.clock, self)
        self.projections, self.observed, self.replies, self.reply_bytes = {}, {}, {}, {}
        self.models, self.models_observed_at = None, None

    def refresh_models(self, token):
        self.models = None
        raw, _ = self.adapter.send("GET", "/work-policy/v1/models", None, token)
        try:
            strict_json(raw)
            models = AvailableModels.model_validate_json(raw)
        except (ValueError, TypeError):
            raise ConsoleError("MODELS_INVALID", 502) from None
        ids = [model.id for model in models.models]
        require(len(ids) == len(set(ids)) and (models.default_model is None
                or models.default_model in ids), "MODELS_INVALID", 502)
        self.models, self.models_observed_at = models, self.clock().isoformat()
        return models

    def command(self, request_id):
        data = self.store.command(request_id)
        require(data is not None, "INSTRUCTION_SCOPE")
        try:
            return InstructionSubmit.model_validate_json(canonical(data))
        except (ValueError, TypeError):
            raise ConsoleError("INSTRUCTION_CHANGED") from None

    def task(self, request_id):
        require(self.store.lane(self.store.get(request_id)["thread_id"]) is None, "INSTRUCTION_ROUTE_CHANGED")
        command = self.command(request_id)
        p = self.config.standing_policy
        _, binding = derive_work_binding(p, request_id, instruction=command)
        return FixedTask(task_id=request_id, label=command.content[:60], description="你的原文交辦；不會因失聯自動重送。",
            binding_id=binding.binding_id, job_id=binding.job_id, work_id=binding.work.work_id,
            event_id=binding.event_id, root_request_id=binding.root_request_id, obligation_id=binding.obligation_id,
            owner_record_id=binding.owner.owner_record_id,
            route=wire.DeliveryRoute.model_validate_json(binding.route.model_dump_json()), channel_version="fa-pull-inbox-v1",
            submit_request_id=request_id, fetch_request_id=request_id+"-pull", ack_request_id=request_id+"-ack",
            allowed_actions=("submit", "status", "fetch", "acknowledge"), max_payload_bytes=131072)

    def tasks(self, thread_id=None):
        return [self.task(row["request_id"]) for row in self.store.all(thread_id)
                if self.store.command(row["request_id"]) is not None and self.store.lane(row["thread_id"]) is None]

    def admission(self, task):
        submitted = self.journal.get(task.task_id, "submit")
        if submitted is not None and submitted.get("admission") is not None:
            return InstructionReply.model_validate_json(canonical(submitted["admission"]))
        with self.journal.connect() as db:
            rows = db.execute("SELECT outcome FROM intents WHERE task_id=? AND "
                              "(phase LIKE 'submit-reconcile-%' OR phase LIKE 'submit-retry-%') ORDER BY rowid DESC",
                               (task.task_id,)).fetchall()
        for row in rows:
            outcome = strict_json(row[0])
            if outcome.get("state") == "CONFIRMED":
                return InstructionReply.model_validate_json(canonical(outcome["admission"]))
        return None

    def query(self, task):
        c = self.config
        return WorkPolicyQuery(read_id=secrets.token_hex(16), backend_origin=c.backend_origin,
            backend_configuration_sha256=c.backend_configuration_sha256, backend_core_sha256=c.backend_core_sha256,
            tenant_id=c.tenant_id, human_oid=c.human_oid, client_id=c.client_id,
            binding_id=task.binding_id, job_id=task.job_id, work_id=task.work_id, root_request_id=task.root_request_id,
            route=task.route, submit_request_id=task.submit_request_id,
            submit_body_sha256=sha(self.command(task.task_id).model_dump_json().encode("utf-8")))

    def current_conversation(self, thread_id):
        tasks = self.tasks(thread_id)
        if not tasks:
            return None
        admission = self.admission(tasks[-1])
        require(admission is not None, "INSTRUCTION_PENDING")
        return admission.conversation_id

    def progress_conversation(self, thread_id):
        for task in reversed(self.tasks(thread_id)):
            admitted = self.admission(task)
            if admitted is not None:
                return admitted.conversation_id
            submitted = self.journal.get(task.task_id, "submit")
            if submitted is not None:
                try:
                    raw = submitted["intent"]["instruction_context"]
                    return InstructionContext.model_validate_json(canonical(raw)).conversation_id
                except (ValueError, TypeError, KeyError):
                    raise ConsoleError("WORK_POLICY_INVALID", 502) from None
        return None

    def attachment_scope(self, request_id):
        draft = self.store.get(request_id)
        require(self.store.current(draft["thread_id"])["request_id"] == request_id, "INSTRUCTION_CHANGED")
        for item in self.store.all(draft["thread_id"])[-1:]:
            require(self.store.command(item["request_id"]) is not None, "INSTRUCTION_INCOMPLETE")
        require(self.store.command(request_id) is not None or not self.store.has_submission(request_id),
                "INSTRUCTION_INCOMPLETE")
        return draft["thread_id"], self.current_conversation(draft["thread_id"])

    def attachment_sources(self, request_id, token):
        scope = self.attachment_scope(request_id)
        thread_id, conversation_id = scope
        path = ("/work-policy/v1/attachment-sources" if conversation_id is None else
                f"/work-policy/v1/conversations/{conversation_id}/attachment-sources")
        try:
            raw, headers = self.adapter.send("GET", path, None, token, limit=16384)
        except ConsoleError as error:
            if error.upstream_status == 404:
                raise ConsoleError("ATTACHMENT_CATALOG_UNAVAILABLE", 409, 404) from None
            raise
        require(headers.get("cache-control") == "no-store"
                and headers.get("content-type", "").split(";")[0] == "application/json",
                "ATTACHMENT_CATALOG_INVALID", 502)
        try:
            strict_json(raw)
            catalog = AttachmentSourceCatalog.model_validate_json(raw)
        except (ValueError, TypeError):
            raise ConsoleError("ATTACHMENT_CATALOG_INVALID", 502) from None
        require(catalog.conversation_id == conversation_id and scope == self.attachment_scope(request_id),
                "ATTACHMENT_CATALOG_INVALID", 502)
        return {"thread_id": thread_id, "request_id": request_id, "conversation_id": conversation_id,
                "catalog": catalog.model_dump(mode="json")}

    def text_file_capability(self, request_id, token):
        scope = self.attachment_scope(request_id)
        thread_id, cid = scope
        if cid is None:
            from .agent_template import download, parse_template
            template = parse_template(download(self.adapter, token), code="USER_FILE_CAPABILITY_INVALID", status=502)
            profile, read_at = template.settings.user_text_file_profile, self.clock().isoformat()
        else:
            try:
                raw, headers = self.adapter.send(
                    "GET", f"/work-policy/v1/conversations/{cid}/user-text-file-capability", None, token, limit=2048,
                )
            except ConsoleError as error:
                if error.upstream_status == 404:
                    raise ConsoleError("USER_FILE_CAPABILITY_UNAVAILABLE", 409, 404) from None
                raise
            require(headers.get("cache-control") == "no-store"
                    and headers.get("content-type", "").split(";")[0] == "application/json",
                    "USER_FILE_CAPABILITY_INVALID", 502)
            try:
                strict_json(raw)
                value = UserTextFileCapability.model_validate_json(raw)
            except (ValueError, TypeError):
                raise ConsoleError("USER_FILE_CAPABILITY_INVALID", 502) from None
            require(value.conversation_id == cid, "USER_FILE_CAPABILITY_INVALID", 502)
            profile, read_at = value.user_text_file_profile, value.read_at.isoformat()
        require(scope == self.attachment_scope(request_id), "USER_FILE_CAPABILITY_INVALID", 502)
        require(self.console.vault.get() == token, "LOGIN_REQUIRED", 401)
        return {"thread_id": thread_id, "request_id": request_id, "conversation_id": cid,
                "read_at": read_at, "user_text_file_profile": profile, "authorization": False}

    def user_file_metadata(self, request_id):
        return [file_reference(item).model_dump(mode="json") for item in self.command(request_id).files]

    def read_original_user_file(self, task, name, token):
        try:
            TypeAdapter(Filename).validate_python(name, strict=True)
        except ValueError:
            raise ConsoleError("USER_FILE_INPUT_INVALID", 400) from None
        command = self.command(task.task_id)
        original = next((item for item in command.files if item.name == name), None)
        require(original is not None, "USER_FILE_UNAVAILABLE", 404)
        admission = self.admission(task)
        require(admission is not None, "INSTRUCTION_PENDING")
        path = f"/work-policy/v1/conversations/{admission.conversation_id}/turns/{admission.turn_number}/files/{name}"
        try:
            raw, headers = self.adapter.send("GET", path, None, token, limit=MAX_USER_INPUT_BYTES)
        except ConsoleError as error:
            if error.upstream_status == 404:
                raise ConsoleError("USER_FILE_UNAVAILABLE", 409, 404) from None
            raise
        reference = file_reference(original)
        require(headers.get("cache-control") == "no-store"
                and headers.get("content-type", "").lower() == "text/plain; charset=utf-8"
                and headers.get("content-disposition") == f'attachment; filename="{name}"'
                and headers.get("x-content-type-options") == "nosniff"
                and headers.get("x-content-sha256") == sha(raw) == reference.sha256
                and len(raw) == reference.utf8_bytes and raw == original.text.encode("utf-8"),
                "USER_FILE_RESPONSE_INVALID", 502)
        require(self.console.vault.get() == token, "LOGIN_REQUIRED", 401)
        return raw

    def submission_context(self, task):
        """Expected response identity only; backend submission still authorizes."""
        from shared_context.behavior_instruction import conversation_id
        command = self.command(task.task_id)
        thread = self.store.get(task.task_id)["thread_id"]
        previous = [item for item in self.tasks(thread) if item.task_id != task.task_id]
        number = 1
        if previous:
            admitted = self.admission(previous[-1])
            require(admitted is not None and admitted.conversation_id == command.conversation_id,
                    "INSTRUCTION_PENDING")
            number = admitted.turn_number + 1
        else:
            require(command.conversation_id is None, "INSTRUCTION_SCOPE")
        return InstructionContext(conversation_id=conversation_id(self.console.config.standing_policy, command),
                                  turn_number=number, model=command.model)

    def refresh_progress(self, token, thread_id):
        conversation_id = self.progress_conversation(thread_id)
        require(conversation_id is not None, "INSTRUCTION_PENDING")
        projection = self.refresh_conversation(token, conversation_id, thread_id=thread_id)
        for turn in projection.turns:
            if turn.reply_available and turn.request_id not in self.replies:
                self.read_reply(token, projection, turn)
        return projection

    def read_turn_context(self, task, token):
        admission = self.admission(task)
        require(admission is not None, "INSTRUCTION_PENDING")
        path = f"/work-policy/v1/conversations/{admission.conversation_id}/turns/{admission.turn_number}/context"
        raw, headers = self.adapter.send("GET", path, None, token, limit=MAX_TURN_CONTEXT_BYTES)
        require(headers.get("cache-control") == "no-store" and headers.get("x-content-sha256") == sha(raw),
                "TURN_CONTEXT_INVALID", 502)
        try:
            strict_json(raw)
            value = TurnContext.model_validate_json(raw)
        except (ValueError, TypeError):
            raise ConsoleError("TURN_CONTEXT_INVALID", 502) from None
        require((value.request_id, value.job_id, value.conversation_id, value.turn_number, value.model) ==
                (task.task_id, task.job_id, admission.conversation_id, admission.turn_number, admission.model)
                and value.read_at <= self.clock(), "TURN_CONTEXT_INVALID", 502)
        return value

    def read_delegation(self, task, token):
        from shared_context.behavior_instruction import sdk_session_id
        admission = self.admission(task)
        require(admission is not None, "INSTRUCTION_PENDING")
        path = f"/work-policy/v1/conversations/{admission.conversation_id}/turns/{admission.turn_number}/delegation"
        try:
            raw, headers = self.adapter.send("GET", path, None, token, limit=MAX_DELEGATION_BYTES)
        except ConsoleError as error:
            if error.upstream_status in {404, 405}:
                raise ConsoleError("DELEGATION_UNAVAILABLE", 409, error.upstream_status) from None
            raise
        if raw is None:
            return None
        require(headers.get("cache-control") == "no-store" and headers.get("x-content-sha256") == sha(raw),
                "DELEGATION_RESPONSE_INVALID", 502)
        try:
            body = strict_json(raw)
            require(type(body) is dict and body.get("schema_version") == "fa-delegation-reply-v1",
                    "DELEGATION_RESPONSE_INVALID", 502)
            value = DelegationReply.model_validate_json(raw)
        except (ValueError, TypeError):
            raise ConsoleError("DELEGATION_RESPONSE_INVALID", 502) from None
        require(value.parent_job_id == task.job_id
                and value.sdk_session_id == sdk_session_id(admission.conversation_id) and not value.fresh,
                "DELEGATION_RESPONSE_INVALID", 502)
        return value

    def require_cancellable(self, task):
        admission = self.admission(task)
        require(admission is not None, "INSTRUCTION_PENDING")
        require(self.journal.get(task.task_id, "cancel") is None, "CANCELLATION_ALREADY_ATTEMPTED")
        progress = self.progress(task)
        status = self.console.statuses.get(task.task_id)
        observations = [admission.status.model_dump(mode="json"),
                        None if progress is None else progress["data"],
                        None if status is None else status.get("data")]
        require(not any(value is not None and value["state"] in TERMINAL_STATES for value in observations),
                "CANCELLATION_NOT_NEEDED")

    def cancel(self, task, token):
        require(self.admission(task) is not None, "INSTRUCTION_PENDING")
        if self.journal.get(task.task_id, "cancel") is not None:
            return
        current = self.adapter.status(task, token)
        self.console.statuses[task.task_id] = {
            "data": current.model_dump(mode="json"), "observed_at": self.clock().isoformat(),
            "not_current_live_result": True,
        }
        require(current.state not in TERMINAL_STATES, "CANCELLATION_NOT_NEEDED")
        token = self.console.gate(task, "cancel")
        command = wire.Command(request_id=sha(canonical(["instruction-cancel", self.config.console_id, task.task_id])))
        body = command.model_dump(mode="json")
        self.journal.begin(task.task_id, "cancel", {
            "method": "POST", "path": f"/work-policy/v1/bindings/{task.binding_id}/cancel",
            "request_id": command.request_id, "body": body, "body_sha256": sha(canonical(body)),
            "job_id": task.job_id, "generation": current.generation, "created_at": self.clock().isoformat(),
        })
        self.console.observed_here.add((task.task_id, "cancel"))
        try:
            receipt = self.adapter.cancel(task, body, current.generation, token)
            self.journal.finish(task.task_id, "cancel", {
                "state": "CONFIRMED", "receipt": receipt.model_dump(mode="json"),
                "observed_at": self.clock().isoformat(), "automatic_retry": False,
            })
        except ConsoleError as error:
            self.journal.finish(task.task_id, "cancel", {
                "state": "UNKNOWN", "code": error.code, "observed_at": self.clock().isoformat(),
                "automatic_retry": False,
            })
            raise

    def cancellation_intent(self, task):
        require(self.admission(task) is not None, "INSTRUCTION_PENDING")
        original = self.journal.get(task.task_id, "cancel")
        require(original is not None and original["state"] == "UNKNOWN", "CANCELLATION_ORIGINAL_REQUIRED")
        try:
            intent = original["intent"]
            body = intent["body"]
            command = wire.Command.model_validate_json(canonical(body))
            require(intent["method"] == "POST"
                    and intent["path"] == f"/work-policy/v1/bindings/{task.binding_id}/cancel"
                    and intent["job_id"] == task.job_id
                    and intent["request_id"] == command.request_id
                    and intent["body_sha256"] == sha(canonical(body))
                    and type(intent["generation"]) is int and 0 <= intent["generation"] <= 8,
                    "CANCELLATION_ORIGINAL_INVALID")
        except (KeyError, TypeError, ValueError):
            raise ConsoleError("CANCELLATION_ORIGINAL_INVALID", 409) from None
        return intent

    def read_cancellation(self, task, token):
        original = self.cancellation_intent(task)
        receipt = self.adapter.read_cancellation(task, original["generation"], token)
        require(self.cancellation_intent(task) == original, "CANCELLATION_ORIGINAL_INVALID")
        return {"schema_version": "fa-cancellation-observation-v1", "task_id": task.task_id,
                "job_id": task.job_id, "generation": original["generation"],
                "receipt": receipt.model_dump(mode="json"), "observed_at": self.clock().isoformat(),
                "read_only": True, "original_outcome": "UNKNOWN"}

    def reread_reply(self, task, token):
        thread_id = self.store.get(task.task_id)["thread_id"]
        conversation_id = self.progress_conversation(thread_id)
        projection = self.refresh_conversation(token, conversation_id, thread_id=thread_id)
        turn = next((t for t in projection.turns if t.request_id == task.task_id), None)
        require(turn is not None and turn.reply_available, "NO_RESULT")
        return self.read_reply(token, projection, turn)

    def read_reply(self, token, projection, turn):
        reply, raw = self.adapter.read_text_reply(projection.conversation_id, turn.turn_number, projection.sdk_session_id, token)
        self.replies[turn.request_id] = {"data":reply.model_dump(mode="json"), "sha256":sha(raw),
            "byte_length":len(raw),"observed_at":self.clock().isoformat(),"not_a_receipt":True}
        self.reply_bytes[turn.request_id] = raw
        return reply

    def progress(self, task):
        thread_id = self.store.get(task.task_id)["thread_id"]
        conversation_id = self.progress_conversation(thread_id)
        projection = self.projections.get(conversation_id)
        if projection is None:
            return None
        turn = next((t for t in projection.turns if t.request_id == task.submit_request_id), None)
        return None if turn is None else {
            "data": turn.model_dump(mode="json"), "observed_at": self.observed[conversation_id], "not_current_live_result": True}

    def refresh_conversation(self, token, conversation_id, *, thread_id=None):
        require(conversation_id is not None, "INSTRUCTION_PENDING")
        raw, _ = self.adapter.send("GET", "/work-policy/v1/conversations/"+conversation_id, None, token)
        try:
            strict_json(raw)
            value = ConversationProjection.model_validate_json(raw)
        except (ValueError, TypeError):
            raise ConsoleError("CONVERSATION_INVALID", 502) from None
        require(value.conversation_id == conversation_id and bool(value.turns), "CONVERSATION_INVALID", 502)
        previous_number = 0
        tasks = {task.submit_request_id: task for task in self.tasks(thread_id)}
        seen = set()
        for turn in value.turns:
            task = tasks.get(turn.request_id)
            require(turn.turn_number == previous_number+1
                    and turn.request_id not in seen
                    and turn.state in {"queued","starting","running","waiting_dependency","capacity_uncertain",
                                       "succeeded","failed","cancelled"}, "CONVERSATION_INVALID", 502)
            require(task is not None and (turn.binding_id, turn.job_id, turn.work_id) ==
                    (task.binding_id, task.job_id, task.work_id), "CONVERSATION_INVALID", 502)
            requested_model = self.command(task.task_id).model
            require(requested_model is None or turn.model == requested_model, "CONVERSATION_INVALID", 502)
            require(all(event.at <= self.clock() for event in turn.progress.events)
                    and all(a.seq < b.seq for a,b in zip(turn.progress.events,turn.progress.events[1:])),
                    "CONVERSATION_INVALID", 502)
            admission = self.admission(task)
            if admission is None:
                original = self.journal.get(task.task_id, "submit")
                try:
                    expected = original["intent"]["instruction_context"]
                    require((expected["conversation_id"], expected["turn_number"]) ==
                            (value.conversation_id, turn.turn_number), "CONVERSATION_INVALID", 502)
                except (KeyError, TypeError):
                    raise ConsoleError("CONVERSATION_INVALID", 502) from None
            else:
                require((admission.conversation_id, admission.turn_number) ==
                        (value.conversation_id, turn.turn_number), "CONVERSATION_INVALID", 502)
            seen.add(turn.request_id)
            previous_number = turn.turn_number
        self.projections[conversation_id], self.observed[conversation_id] = value, self.clock().isoformat()
        return value

    def validate_input(self, request_id, content, model, attachments=(), files=(), *, conversation_id=None, images=()):
        require(type(model) is str and self.models is not None
                and model in {v.id for v in self.models.models}, "MODEL_NOT_AVAILABLE", 409)
        try:
            command = InstructionSubmit.model_validate_json(canonical({
                "request_id": request_id, "content": content, "conversation_id": conversation_id,
                "model": model, "attachments": attachments, "files": files, "images": images,
            }))
        except (ValueError, TypeError):
            raise ConsoleError("USER_IMAGE_INPUT_INVALID" if images else "USER_FILE_INPUT_INVALID" if files else
                               "ATTACHMENT_SELECTION_INVALID" if attachments else "INSTRUCTION_INVALID", 400) from None
        require(all("." in item.name and item.name.rsplit(".", 1)[1].lower() in {"txt", "md", "csv", "json"}
                    for item in command.files), "USER_FILE_INPUT_INVALID", 400)
        return command

    def prepare(self, request_id, content, model, attachments=(), files=()):
        require(type(model) is str and self.models is not None
                and model in {v.id for v in self.models.models}, "MODEL_NOT_AVAILABLE", 409)
        original = self.store.get(request_id)
        saved_command = self.store.command(request_id)
        if saved_command is None:
            require(not self.store.has_submission(request_id), "INSTRUCTION_INCOMPLETE")
            conversation_id = self.current_conversation(original["thread_id"])
        else:
            conversation_id = self.command(request_id).conversation_id
        command = self.validate_input(
            request_id, content, model, attachments, files, conversation_id=conversation_id)
        self.store.prepare(command.model_dump(mode="json"), freeze=True)
        return self.task(request_id)

    def follow_up(self, token, thread_id):
        draft = self.store.current(thread_id)
        if draft["content"] is None:
            return draft
        cid = self.current_conversation(thread_id)
        projection = self.projections.get(cid)
        if (projection is None or not projection.turns
                or projection.turns[-1].request_id != draft["request_id"] or not projection.can_follow_up):
            projection = self.refresh_conversation(token, cid, thread_id=thread_id)
        require(projection.can_follow_up, "INSTRUCTION_PENDING")
        return self.store.new(thread_id)

    def view(self, thread_id=None):
        draft = self.store.current(thread_id)
        thread_id = draft["thread_id"]
        task = next((task for task in self.tasks(thread_id) if task.task_id == draft["request_id"]), None)
        submitted = None if task is None else self.journal.get(task.task_id, "submit")
        conversation_id = self.progress_conversation(thread_id)
        projection = self.projections.get(conversation_id)
        current = (projection is not None and task is not None and bool(projection.turns)
                   and projection.turns[-1].request_id == task.submit_request_id)
        can_follow = (current and projection.can_follow_up
                      and task is not None and self.admission(task) is not None)
        authenticated = self.console.vault.state() == "AVAILABLE"
        command = None if task is None else self.command(task.task_id)
        incomplete = command is None and self.store.has_submission(draft["request_id"])
        model = command.model if command is not None else (None if self.models is None else self.models.default_model)
        return {**draft, "editable": authenticated and not incomplete and draft["content"] is None,
            "command_incomplete": incomplete,
            "can_submit": authenticated and not incomplete and submitted is None and self.models is not None and bool(self.models.models),
            "can_follow_up": authenticated and can_follow,
            "message": ("請先讀取既有登入，再輸入指令。" if not authenticated else
                        "原交辦缺少完整 command；已有送出意圖，不能補造附件或重送。" if incomplete else
                        "這段對話已封存，請開始新的 session。" if projection is not None and projection.archived else
                        "這是舊式對話；請明確 reset 或開始新的 session。" if projection is not None
                        and projection.follow_up_blocker=="CONVERSATION_SESSION_RESET_REQUIRED" else
                        "此對話的執行環境已被平台換成新版本，請開新對話。" if projection is not None
                        and projection.follow_up_blocker=="CONVERSATION_SESSION_VERSION_CHANGED" else
                        "尚未取得原專案可用的聊天模型；可更新模型清單，不會自行建立部署。" if self.models is None or not self.models.models else
                        "原交辦回應未知；請核對原接單紀錄，不會重送。" if submitted and submitted["state"] == "UNKNOWN"
                        and self.admission(task) is None else
                        "可直接交辦；每則保留原文與原請求，不会自動重送。" if submitted is None else
                        "這則已交辦，可以在同一對話追問或補充；服務仍會核對當前狀態。" if can_follow else
                        "這則已交辦。請更新進度或查看回覆，服務尚未表示可以接續對話；不需等收件確認才可接續。"),
            "conversation": None if projection is None else projection.model_dump(mode="json"),
            "conversation_id": conversation_id,
            "attachments": [] if command is None else [item.model_dump(mode="json") for item in command.attachments],
            "user_files": [] if command is None else self.user_file_metadata(command.request_id),
            "observed_at": self.observed.get(conversation_id),
            "progress_available": authenticated and conversation_id is not None,
            "threads": self.store.threads(),
            "can_new_conversation": authenticated,
            "can_reset": authenticated and current and projection.can_reset,
            "hosted_session_id": None if projection is None else projection.hosted_session_id,
            "hosted_agent_version": None if projection is None else projection.hosted_agent_version,
            "models": None if self.models is None else self.models.model_dump(mode="json"),
            "models_observed_at": self.models_observed_at,
            "model": model,
            "follow_up_blocker": None if projection is None else projection.follow_up_blocker,
            "input_capacity": {"unit":"quoted-utf8-bytes","maximum":INSTRUCTION_QUOTED_UTF8_LIMIT},
            "progress_polling": {"method":"GET","interval_ms":1000,"model_or_reserve":False},
            "connection_metadata": self.connection.model_dump(mode="json"),
            "connection_metadata_is_not_live_attestation": True}

    def reconcile(self, task):
        prior = self.journal.get(task.task_id, "submit")
        require(prior is not None and prior["state"] == "UNKNOWN", "INSTRUCTION_PENDING")
        body = self.command(task.task_id).model_dump(mode="json")
        require(prior["intent"]["body_sha256"] == sha(canonical(body)), "INSTRUCTION_CHANGED")
        with self.journal.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            count = db.execute("SELECT COUNT(*) FROM intents WHERE task_id=? AND phase LIKE 'submit-reconcile-%'",
                               (task.task_id,)).fetchone()[0]
            phase = f"submit-reconcile-{count+1:08d}"
            intent = {"method":"POST","path":"/work-policy/v1/submit-reconcile","body":body,
                "request_id":task.task_id,"body_sha256":sha(canonical(body)),
                "created_at":self.clock().isoformat(),"original_intent_sha256":sha(canonical(prior["intent"]))}
            db.execute("INSERT INTO intents VALUES (?,?,?,?)", (task.task_id, phase, canonical(intent).decode(),
                       canonical({"state":"UNKNOWN","observed_at":intent["created_at"],"automatic_retry":False}).decode()))
        try:
            reply = self.adapter.reconcile(task, self.console.gate(task, "status"))
            if isinstance(reply,WorkSubmitObservation):
                self.journal.finish(task.task_id,phase,{"state":"RETRYABLE","observed_at":self.clock().isoformat(),
                    "observation":reply.model_dump(mode="json"),"automatic_retry":False})
                return
            self.journal.finish(task.task_id, phase, {"state":"CONFIRMED","observed_at":self.clock().isoformat(),
                                                     "admission":reply.model_dump(mode="json")})
            self.console.statuses[task.task_id] = {"data":reply.status.model_dump(mode="json"),
                "observed_at":self.clock().isoformat(),"not_current_live_result":True}
        except ConsoleError as error:
            self.journal.finish(task.task_id, phase, {"state":"UNKNOWN","code":error.code,
                                                     "observed_at":self.clock().isoformat(),"automatic_retry":False})
            raise

    def retry_observation(self,task):
        if self.admission(task) is not None:
            return None
        original=self.journal.get(task.task_id,"submit")
        if original is None or original["state"]!="UNKNOWN":
            return None
        with self.journal.connect() as db:
            row=db.execute("SELECT phase,outcome FROM intents WHERE task_id=? AND phase LIKE 'submit-reconcile-%' "
                           "ORDER BY phase DESC LIMIT 1",(task.task_id,)).fetchone()
        if row is None:
            return None
        outcome=strict_json(row[1])
        if outcome.get("state")!="RETRYABLE":
            return None
        try:
            value=WorkSubmitObservation.model_validate_json(canonical(outcome["observation"]))
        except (KeyError,ValueError,TypeError):
            raise ConsoleError("SUBMISSION_OBSERVATION_INVALID",502) from None
        command=self.command(task.task_id)
        require(value.request_id==task.task_id
                and value.submit_sha256==sha(command.model_dump_json().encode("utf-8"))
                and original["intent"]["body_sha256"]==sha(canonical(command.model_dump(mode="json"))),
                "SUBMISSION_OBSERVATION_INVALID",502)
        phase=row[0].replace("submit-reconcile-","submit-retry-",1)
        return None if self.journal.get(task.task_id,phase) is not None else (phase,value)

    def retry_submission(self,task):
        token=self.console.gate(task,"submit-retry")
        observation=self.retry_observation(task)
        require(observation is not None,"SUBMISSION_RECONCILE_REQUIRED")
        phase,value=observation
        original=self.journal.get(task.task_id,"submit")
        self.journal.begin(task.task_id,phase,{
            **original["intent"],"created_at":self.clock().isoformat(),
            "reconciliation":value.model_dump(mode="json"),
        })
        try:
            status=self.adapter.submit(task,token)
            self.journal.finish(task.task_id,phase,{"state":"CONFIRMED","observed_at":self.clock().isoformat(),
                "admission":self.adapter.admissions[task.task_id].model_dump(mode="json")})
            self.console.statuses[task.task_id]={"data":status.model_dump(mode="json"),
                "observed_at":self.clock().isoformat(),"not_current_live_result":True}
        except ConsoleError as error:
            self.journal.finish(task.task_id,phase,{"state":"UNKNOWN","code":error.code,
                "observed_at":self.clock().isoformat(),"automatic_retry":False})
            raise
