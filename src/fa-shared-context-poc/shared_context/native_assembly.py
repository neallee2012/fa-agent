"""PA-16 metadata only. No SDK imports, prompt bodies, authority or billing state."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import logging
import re
from typing import Annotated, Literal

from pydantic import Field

from .behavior_agent_template import TemplateRole, TemplateSkill
from .behavior_capabilities import CapabilityRef, EntryId
from .behavior_sdk_state import CheckpointProfile, NativeCheckpointRef
from .behavior_wire import Digest, ExactSourceRef, Identifier, ModelDeployment, WireModel
from .native_inference import NativeInference, NativeInferenceCollector

MAX_ASSEMBLY_BYTES = 24 * 1024

Count = Annotated[int, Field(ge=0)]
PartName = Literal[
    "base", "capabilities", "role", "origin", "teammates", "groups", "providers",
    "system_message", "user_prompt", "private_memory", "team_memory", "attachments",
    "user_files", "user_images", "checkpoint", "routines", "reminders", "writing_style",
]
Reason = Literal[
    "not_recorded", "not_reached", "not_configured", "not_selected", "disabled",
    "scope_not_applicable", "unavailable", "no_results", "no_read_observed",
    "token_unavailable", "transport_error", "http_error", "response_invalid",
    "bound_count", "bound_chars", "skipped_size", "turn_interrupted",
    "observer_failed", "summary_bound", "item_bound", "source_not_reported",
    "read_refused", "read_failed",
    "configuration_invalid", "scope_unconfirmed", "scope_mismatch",
    "no_previous_checkpoint",
    "partial_search",
    "inference_partial",
]


class AssemblySelection(WireModel):
    base: TemplateRole
    profile_selection_id: Identifier | None
    profile_selection_reason: Literal["not_selected"] | None
    role_version: Count | None
    role_sha256: Digest | None
    role_reason: Literal["not_configured"] | None
    sdk_version: Literal["1.0.13"]
    runtime_version: Literal["1.0.83"]
    checkpoint_profile: CheckpointProfile | None
    model: ModelDeployment
    capability_head_selection_id: Identifier | None
    capability_reason: Literal["not_configured", "source_not_reported"] | None


class MemorySource(WireModel):
    memory_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")] | None
    id_reason: Literal["source_not_reported"] | None
    version: None = None
    version_reason: Literal["source_not_reported"] = "source_not_reported"
    updated_at: Count | None
    updated_at_reason: Literal["source_not_reported"] | None
    sha256: Digest
    hash_scope: Literal["returned-memory-content"] = "returned-memory-content"
    kind: Literal["user_profile", "chat_summary", "procedural"] | None = None
    kind_reason: Literal["source_not_reported"] | None = "source_not_reported"


class MemorySearch(WireModel):
    mode: Literal["static", "contextual"] | None = None
    state: Literal["read", "omitted", "unknown"]
    reason: Reason | None = None
    found_count: Count | None = None
    bound_count: Count | None = None
    bound_chars: Count | None = None
    truncated_items: Count | None = None


class MemoryRecall(MemorySearch):
    sources: Annotated[tuple[MemorySource, ...], Field(max_length=16)] = ()
    omitted_sources: Count = 0
    count_reason: Literal["per_search_counts"] | None = None
    searches: Annotated[tuple[MemorySearch, ...], Field(max_length=2)] = ()
    deduplicated_count: Count | None = None


class AssemblyAttachment(WireModel):
    index: Count
    sha256: Digest
    byte_length: Count
    source: ExactSourceRef | None = None
    source_reason: Literal["command_reference"] | None = None


class AssemblyPart(WireModel):
    name: PartName
    state: Literal["configured", "included", "read", "omitted", "unknown"]
    reason: Reason | None = None
    utf8_bytes: Count | None = None
    sha256: Digest | None = None
    hash_scope: Literal["utf8-text", "reference-list", "checkpoint-state"] = "utf8-text"
    source_ref: Literal[
        "capture.template.role", "capture.capabilities", "capture.role_prompt", "capture.template.settings",
        "command.content", "capture.attached_text", "command.files", "command.images", "sdk.arguments",
        "native.peer_directory", "native.group_context", "native.providers", "native.memory_search",
        "previous_checkpoint", "receipt.routine", "native.work_reminders", "native.writing_style",
    ] | None = None
    source_sha256: Digest | None = None
    data_bytes: Count | None = None
    tokens: None = None
    token_reason: Literal["counter_unavailable"] = "counter_unavailable"
    memory: MemoryRecall | None = Field(default=None, exclude_if=lambda value: value is None)
    attachments: Annotated[tuple[AssemblyAttachment, ...], Field(max_length=16)] = Field(
        default=(), exclude_if=lambda value: not value)


class AssemblyEvent(WireModel):
    type: Literal[
        "session.compaction_start", "session.compaction_complete", "session.truncation", "m2.context_images",
    ]
    source: Literal["sdk-public-event", "fa-image-filter"]
    event_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")] | None = None
    trigger: Literal["threshold", "context_limit_retry", "manual", "memory_pressure", "model_switch"] | None = None
    success: bool | None = None
    checkpoint_number: Count | None = None
    messages_removed: Count | None = None
    omitted_inline_pngs: Count | None = None
    older_inline_pngs: Count | None = None
    unsendable_inline_pngs: Count | None = None
    missing_reason: Literal["source_not_reported"] = "source_not_reported"


class AssemblySkill(WireModel):
    selected: TemplateSkill
    catalogued: bool | None = None
    # SK-04: a skill left out of the #75 budgeted view is still authorized and readable by name.
    catalog_reason: Literal["not_reached", "omitted_by_budget"] | None = "not_reached"
    state: Literal["read", "unknown"] = "unknown"
    reason: Reason | None = "no_read_observed"
    read_observations: Count | None = None
    failed_observations: Count | None = None
    last_failure: Literal["read_refused", "read_failed"] | None = None
    utf8_bytes: Count | None = None
    source: Literal["controlled-skill-reader"] = "controlled-skill-reader"
    model_adoption: Literal["unknown"] = "unknown"


class AssemblyHistoryRead(WireModel):
    """TC-06: which completed turn a history tool read; numbers and outcome only, never content or file names."""
    tool: Literal["read_user_turn", "read_user_file", "read_assistant_reply"]
    turn_number: Count | None
    outcome: Literal["read", "not_found", "unavailable"]


class AssemblyTool(WireModel):
    name: Annotated[str, Field(pattern=r"^[A-Za-z0-9_][A-Za-z0-9_.:-]{0,191}$")]
    schema_sha256: Digest
    grants: tuple[CapabilityRef, ...]
    toolbox_version: None = None
    toolbox_version_reason: Literal["source_not_reported", "not_applicable"]


ToolSourceReason = Literal[
    "upstream_auth", "source_error", "service_error", "network", "response_invalid", "identity_unavailable",
]


class AssemblyUnavailableSource(WireModel):
    """A tool source this turn ran without because its tool list could not be read (Neal 2026-10-10: 「工具列不出來時，
    這一回合少那個工具照常跑，並在畫面上講明」). Its captured tools stayed bound and only report this, so nothing was sent
    to it. ``source`` is the catalog server label or the registered connector's name; never the source's error text."""
    entry_id: EntryId
    source: Annotated[str, Field(min_length=1, max_length=64)]
    code: Literal["TOOLBOX_LIST_FAILED"] = "TOOLBOX_LIST_FAILED"
    reason: ToolSourceReason


class NativeAssembly(WireModel):
    schema_version: Literal["fa-native-assembly-v1"] = "fa-native-assembly-v1"
    state: Literal["recorded", "partial", "unknown"]
    reason: Reason | None = None
    capture_digest: Digest | None = Field(default=None, exclude_if=lambda value: value is None)
    selection: AssemblySelection | None = Field(default=None, exclude_if=lambda value: value is None)
    parts: Annotated[tuple[AssemblyPart, ...], Field(max_length=24)] = Field(
        default=(), exclude_if=lambda value: not value)
    skills: Annotated[tuple[AssemblySkill, ...], Field(max_length=4)] = Field(
        default=(), exclude_if=lambda value: not value)
    skill_catalog_stage: Literal["full", "shortened", "names", "omitted"] | None = Field(
        default=None, exclude_if=lambda value: value is None)
    history_reads: Annotated[tuple[AssemblyHistoryRead, ...], Field(max_length=32)] = Field(
        default=(), exclude_if=lambda value: not value)
    tools: Annotated[tuple[AssemblyTool, ...], Field(max_length=128)] = Field(
        default=(), exclude_if=lambda value: not value)
    tools_scope: Literal["fa-bound-tool-specs"] = "fa-bound-tool-specs"
    tools_count: Count | None = None
    tools_reason: Reason | None = "not_recorded"
    omitted_items: Count = Field(default=0, exclude_if=lambda value: not value)
    sdk_binding_observed: Literal[True] | None = None
    sdk_binding_reason: Reason | None = "not_recorded"
    unavailable_sources: Annotated[tuple[AssemblyUnavailableSource, ...], Field(max_length=16)] = Field(
        default=(), exclude_if=lambda value: not value)
    checkpoint: NativeCheckpointRef | None = Field(default=None, exclude_if=lambda value: value is None)
    events: Annotated[tuple[AssemblyEvent, ...], Field(max_length=16)] = Field(
        default=(), exclude_if=lambda value: not value)
    event_scope: Literal["observed-metadata-not-exhaustive-history"] = "observed-metadata-not-exhaustive-history"
    inference: NativeInference | None = Field(default=None, exclude_if=lambda value: value is None)

    @property
    def observed_inference(self):
        return self.inference or NativeInference(
            state="unknown", reason="not_recorded", logical_prepared=None,
            transport_started=None, counter_reason="not_recorded")


_PART_SOURCES = {
    "base": "capture.template.role", "capabilities": "capture.capabilities", "role": "capture.role_prompt",
    "origin": "capture.template.settings", "teammates": "native.peer_directory", "groups": "native.group_context",
    "providers": "native.providers", "system_message": "sdk.arguments", "user_prompt": "command.content",
    "private_memory": "native.memory_search", "team_memory": "native.memory_search",
    "attachments": "capture.attached_text", "user_files": "command.files", "user_images": "command.images",
    "checkpoint": "previous_checkpoint", "routines": "receipt.routine", "reminders": "native.work_reminders",
    "writing_style": "native.writing_style",
}


class NativeAssemblyCollector:
    """An in-memory observation of this invocation, saved only with its original turn."""

    def __init__(self, receipt):
        self.capture_digest = receipt.capture_digest
        self.selection = None
        self.parts: dict[str, AssemblyPart] = {}
        self.skills: dict[str, AssemblySkill] = {}
        self.skill_catalog_stage = None
        self.history_reads: list[AssemblyHistoryRead] = []
        self.tools: tuple[AssemblyTool, ...] = ()
        self.tools_count: int | None = None
        self.omitted_items = 0
        self.sdk_binding_observed = None
        self.unavailable_sources: dict[str, AssemblyUnavailableSource | None] = {}
        self.checkpoint = None
        self.events: list[AssemblyEvent] = []
        self.inference: NativeInferenceCollector | None = None
        self.grants = () if receipt.capture.tools is None else receipt.capture.tools.grants
        self.failure: Reason | None = None
        with self.observation():
            capture, settings = receipt.capture, receipt.capture.template.settings
            role = capture.role_prompt
            self.selection = AssemblySelection(
                base=capture.template.role, profile_selection_id=capture.profile_selection_id,
                profile_selection_reason="not_selected" if capture.profile_selection_id is None else None,
                role_version=None if role is None else role.version,
                role_sha256=None if role is None else role.sha256,
                role_reason="not_configured" if role is None else None,
                sdk_version=settings.sdk_version, runtime_version=settings.runtime_version,
                checkpoint_profile=settings.checkpoint_profile, model=capture.model.id,
                capability_head_selection_id=(None if capture.capabilities is None
                                              else capture.capabilities.head_selection_id),
                capability_reason=("not_configured" if capture.capabilities is None else
                                   "source_not_reported" if capture.capabilities.head_selection_id is None else None),
            )
            self.skills = {skill.name: AssemblySkill(selected=skill) for skill in capture.template.skills}
            for name in _PART_SOURCES:
                self.absent(name, "not_reached", state="unknown")
            self.absent("routines", "not_configured")
            self.absent("reminders", "not_selected")
            self.absent("writing_style", "not_selected")
            if role is None:
                self.absent("role", "not_configured")
            else:
                self.text("role", role.text, state="configured")
            self.input_attachments(receipt)

    @contextmanager
    def observation(self):
        try:
            yield
        except Exception as error:
            # This boundary covers metadata only, never execution, checkpoint or State store writes.
            self.failure = "observer_failed"
            logging.getLogger(__name__).warning(
                "FA_NATIVE_ASSEMBLY_PARTIAL reason=observer_failed exception_type=%s", type(error).__name__)

    def text(self, name: PartName, value: str, *, state="configured", reason=None, source_sha256=None):
        with self.observation():
            raw = value.encode("utf-8")
            self.parts[name] = AssemblyPart(
                name=name, state=state, reason=reason, utf8_bytes=len(raw),
                sha256=hashlib.sha256(raw).hexdigest(),
                source_ref=_PART_SOURCES[name], source_sha256=source_sha256,
            )

    def absent(self, name: PartName, reason: Reason, *, state="omitted"):
        with self.observation():
            self.parts[name] = AssemblyPart(name=name, state=state, reason=reason, source_ref=_PART_SOURCES[name])

    def input_attachments(self, receipt):
        with self.observation():
            attached = receipt.capture.attached_text
            groups = {
                "attachments": tuple(AssemblyAttachment(
                    index=index, sha256=item.attachment.source.sha256, byte_length=item.attachment.source.byte_length,
                    source=item.attachment.source,
                ) for index, item in enumerate(() if attached is None else attached.entries)),
                "user_files": tuple(AssemblyAttachment(
                    index=index, sha256=hashlib.sha256(item.text.encode("utf-8")).hexdigest(),
                    byte_length=len(item.text.encode("utf-8")), source_reason="command_reference",
                ) for index, item in enumerate(receipt.command.files)),
                "user_images": tuple(AssemblyAttachment(
                    index=index, sha256=item.sha256, byte_length=item.byte_length, source_reason="command_reference",
                ) for index, item in enumerate(getattr(receipt.command, "images", ()))),
            }
            for name, refs in groups.items():
                if not refs:
                    self.absent(name, "not_selected")
                    continue
                raw = json.dumps([item.model_dump(mode="json") for item in refs], sort_keys=True,
                                 ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                size = sum(item.byte_length for item in refs)
                self.parts[name] = AssemblyPart(
                    name=name, state="configured", source_ref=_PART_SOURCES[name], attachments=refs,
                    utf8_bytes=None if name == "user_images" else size, data_bytes=size,
                    sha256=hashlib.sha256(raw).hexdigest(), hash_scope="reference-list",
                )

    def restored(self, turn, previous):
        with self.observation():
            self.checkpoint = None if turn is None else turn.previous
            if self.checkpoint is None:
                self.absent("checkpoint", "no_previous_checkpoint" if previous is None else "source_not_reported",
                            state="omitted" if previous is None else "unknown")
            else:
                self.parts["checkpoint"] = AssemblyPart(
                    name="checkpoint", state="included", source_ref="previous_checkpoint",
                    sha256=self.checkpoint.sha256, data_bytes=self.checkpoint.byte_length,
                    hash_scope="checkpoint-state",
                )

    def sdk_bound(self, system_message, prompt, tools):
        with self.observation():
            self.sdk_binding_observed = True
            self.text("system_message", system_message, state="included")
            self.text("user_prompt", prompt, state="included")
            self.parts["user_prompt"] = self.parts["user_prompt"].model_copy(update={"source_ref": "sdk.arguments"})
            self.bound_tools(tools)
            for name in ("attachments", "user_files", "user_images", "role", "origin", "teammates", "groups", "providers"):
                part = self.parts[name]
                if part.state == "configured":
                    self.parts[name] = part.model_copy(update={"state": "included"})

    def skill_catalogued(self, *, stage=None, shown=None):
        with self.observation():
            self.skill_catalog_stage = stage
            self.skills = {name: skill.model_copy(update=(
                {"catalogued": True, "catalog_reason": None} if shown is None or name in shown
                else {"catalogued": False, "catalog_reason": "omitted_by_budget"}))
                for name, skill in self.skills.items()}

    def recalled(self, name: PartName, values, before: str, after: str):
        with self.observation():
            observed = getattr(values, "observation", None)
            if observed is None:
                observed = MemoryRecall(state="unknown", reason="not_recorded")
            if after != before:
                self.text(name, after[:len(after) - len(before)],
                          state="included" if values else "unknown", reason=observed.reason)
            elif values or getattr(values, "unavailable", False):
                self.absent(name, "skipped_size")
            else:
                self.absent(name, observed.reason or "not_recorded", state=observed.state)
            self.parts[name] = self.parts[name].model_copy(update={"memory": observed})
            failed = observed.reason == "observer_failed" or any(
                search.reason == "observer_failed" for search in observed.searches)
            if failed or observed.omitted_sources:
                self.failure = "observer_failed" if failed else "summary_bound"
                self.omitted_items += observed.omitted_sources

    def skill_read(self, source):
        with self.observation():
            skill = self.skills[source.name]
            self.skills[source.name] = skill.model_copy(update={
                "state": "read", "reason": None, "read_observations": (skill.read_observations or 0) + 1,
                "utf8_bytes": source.byte_length,
            })

    def skill_failed(self, arguments, code):
        with self.observation():
            name = arguments.get("name") if isinstance(arguments, dict) else None
            if not isinstance(name, str) or name not in self.skills:
                return  # Unselected/malformed caller values are never persisted as skill references.
            refused = code in {
                None, "NATIVE_CAPABILITY_REFUSED", "NATIVE_HITL_HUMAN_DENIED", "NATIVE_HITL_REVIEW_DENIED",
                "NATIVE_HITL_PERMISSION_REQUIRED", "NATIVE_HITL_SCOPE_DENIED", "NATIVE_HITL_ALREADY_AUTHORIZED",
                "NATIVE_HITL_EXPIRED", "NATIVE_HITL_TAKEOVER", "NATIVE_HITL_TURN_CLOSED",
            }
            reason = "read_refused" if refused else "read_failed"
            skill = self.skills[name]
            self.skills[name] = skill.model_copy(update={
                "reason": None if skill.state == "read" else reason, "last_failure": reason,
                "failed_observations": (skill.failed_observations or 0) + 1,
            })

    def source_unavailable(self, entry_id, source, reason) -> bool:
        """True only the first time ``source`` is named this turn, so the caller logs it once too. Keyed by the full
        name: two names that share their first 64 characters stay two records, whose stored text is cut alike."""
        if source in self.unavailable_sources:
            return False
        # None marks a source the bound left out: named once, counted once.
        self.unavailable_sources[source] = None
        with self.observation():
            if sum(item is not None for item in self.unavailable_sources.values()) == 16:
                self.failure = "summary_bound"
                self.omitted_items += 1
            else:
                self.unavailable_sources[source] = AssemblyUnavailableSource(
                    entry_id=entry_id, source=source[:64], reason=reason)
        return True

    def history_read(self, tool, turn_number, outcome):
        with self.observation():
            if len(self.history_reads) == 32:
                self.failure = "summary_bound"
                self.omitted_items += 1
                return
            self.history_reads.append(AssemblyHistoryRead(tool=tool, turn_number=turn_number, outcome=outcome))

    def bound_tools(self, specs):
        with self.observation():
            self.omitted_items += max(0, len(specs) - 128) - max(0, (self.tools_count or 0) - 128)
            self.tools_count = len(specs)
            if len(specs) > 128:
                self.failure = "summary_bound"
            tools = []
            for spec in specs[:128]:
                raw = json.dumps(spec.parameters, ensure_ascii=False, sort_keys=True,
                                 separators=(",", ":"), allow_nan=False).encode("utf-8")
                grants = tuple(grant for grant in self.grants if spec.name in grant.tools)
                tools.append(AssemblyTool(
                    name=spec.name, schema_sha256=hashlib.sha256(raw).hexdigest(),
                    grants=tuple(grant.ref for grant in grants),
                    toolbox_version_reason=("source_not_reported" if any(
                        grant.kind == "mcp-toolbox" for grant in grants) else "not_applicable"),
                ))
            self.tools = tuple(tools)

    def event(self, value):
        with self.observation():
            if self.inference is not None:
                self.inference.event(value)
            kind = value.get("type")
            if kind not in {"session.compaction_start", "session.compaction_complete",
                            "session.truncation", "m2.context_images"}:
                return
            if len(self.events) == 16:
                self.failure = "summary_bound"
                self.omitted_items += 1
                return
            data = value.get("data")
            data = data if isinstance(data, dict) else {}
            fields = {}
            for name, source in (
                ("checkpoint_number", "checkpoint_number"), ("messages_removed", "messages_removed_during_truncation"),
                ("omitted_inline_pngs", "omitted_inline_pngs"), ("older_inline_pngs", "older_inline_pngs"),
                ("unsendable_inline_pngs", "unsendable_inline_pngs"),
            ):
                number = data.get(source)
                if type(number) is int and number >= 0:
                    fields[name] = number
            trigger = data.get("trigger")
            if trigger in {"threshold", "context_limit_retry", "manual", "memory_pressure", "model_switch"}:
                fields["trigger"] = trigger
            if type(data.get("success")) is bool:
                fields["success"] = data["success"]
            identifier = value.get("id")
            if isinstance(identifier, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", identifier):
                fields["event_id"] = identifier
            # Do not retain arbitrary event fields, content, request IDs, usage, paths or exception messages.
            self.events.append(AssemblyEvent(
                type=kind, source="fa-image-filter" if kind == "m2.context_images" else "sdk-public-event", **fields))

    def snapshot(self, *, complete=False) -> NativeAssembly:
        with self.observation():
            inference = None if self.inference is None else self.inference.snapshot()
            reason = self.failure or ("turn_interrupted" if not complete else
                                      "not_reached" if self.sdk_binding_observed is None else
                                      "inference_partial" if inference is not None and inference.state == "partial" else None)
            result = NativeAssembly(
                state="recorded" if reason is None else "partial", reason=reason,
                capture_digest=self.capture_digest, selection=self.selection, parts=tuple(self.parts.values()),
                skills=tuple(self.skills.values()), skill_catalog_stage=self.skill_catalog_stage,
                history_reads=tuple(self.history_reads), tools=self.tools, tools_count=self.tools_count,
                tools_reason="not_reached" if self.tools_count is None else None, omitted_items=self.omitted_items,
                sdk_binding_observed=self.sdk_binding_observed,
                sdk_binding_reason="not_reached" if self.sdk_binding_observed is None else None,
                unavailable_sources=tuple(item for item in self.unavailable_sources.values() if item is not None),
                checkpoint=self.checkpoint, events=tuple(self.events),
                inference=inference,
            )
            while len(result.model_dump_json().encode("utf-8")) > MAX_ASSEMBLY_BYTES and result.tools:
                result = result.model_copy(update={
                    "state": "partial", "reason": "summary_bound", "tools": result.tools[:-1],
                    "omitted_items": result.omitted_items + 1,
                })
            if len(result.model_dump_json().encode("utf-8")) > MAX_ASSEMBLY_BYTES:
                logging.getLogger(__name__).warning("FA_NATIVE_ASSEMBLY_PARTIAL reason=summary_bound")
                return NativeAssembly(state="partial", reason="summary_bound", capture_digest=self.capture_digest,
                                      tools_reason="summary_bound", sdk_binding_reason="summary_bound")
            return result
        return NativeAssembly(state="partial", reason="observer_failed",
                              tools_reason="observer_failed", sdk_binding_reason="observer_failed")
