"""Signed personal `FA metrics` -> existing SDK job -> guarded Teams delivery.

Only the human-approved v2 rule can derive bindings/grants. Activities supply
neither source/owner/model parameters nor approval. Every dynamic record lives
in the same authority; WorkerCore, model/artifact budgets and delivery quotas
remain the original objects. Restarts reload the bounded immutable registry.
"""
from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import datetime
from types import MappingProxyType
from typing import Literal

from pydantic import field_validator

from .behavior_assistant import AssistantProfile
from .behavior_assistant_wire import AssistantSubmit
from .behavior_auth import BehaviorPrincipal
from .behavior_channels import ChannelRevocation, InstalledChannels
from .behavior_channels_migration import channel_key, parse_channel_approval, parse_channel_installation, check_window_interval
from .behavior_delivery import ArtifactVersion, Route, _Model, _Obligation, _Outbox, _Work, _parse, _utc
from .behavior_foundry import NativeExecutionGrant, NativeJobRoute, _TargetReservation, native_key
from .behavior_operations import (
    ExecutionContext, GrantRecord, GrantSubject, OwnerRecord, PolicyRecord, control_key,
)
from .behavior_runtime import RuntimeBinding, RuntimePolicy, material, runtime_key
from .behavior_server_config import ServerConfigurationV2, read_document
from .behavior_store import ControlChanges, ControlRequest, ControlWrite, Digest
from .behavior_workers import JobRecord, worker_key
from .errors import Problem
from .models import Identifier
from .teams_delivery import TeamsArtifactLink, TeamsRouteAccess, TeamsSendContext
from .teams_identity import digest
from .teams_native import TeamsNativeEvidence, compose_installed_personal_channel
from .teams_personal import PersonalRouteEnrollment, PersonalTeamsEntry, TeamsParentSeams, compose_personal_channel
from .teams_runtime import TeamsAdmission, _Source, retained


class TeamsWork(_Model):
    schema_version: Literal["fa-teams-metrics-work-v1"] = "fa-teams-metrics-work-v1"
    event_id: Digest
    body_digest: Digest
    route_id: Digest
    rule_sha256: Digest
    previous_event_id: Digest | None = None
    observed_at: datetime
    binding: RuntimeBinding
    profile: AssistantProfile
    _time = field_validator("observed_at")(_utc)


class TeamsMetrics:
    def __init__(self, parent, *, jwks_transport=None, send_transport=None, bot_token=None):
        if type(parent.config) is not ServerConfigurationV2:
            raise Problem(403, "CHANNELS_CONFIGURATION_DENIED", "固定 Teams metrics 需要明確 v2 配置")
        self.parent, self.config, self.runtime = parent, parent.config, parent.runtime
        self.store, self.clock = self.runtime.store, self.runtime.clock
        self.setup = self.config.channels
        self.rule_sha256 = digest(self.setup.model_dump(mode="json"))
        self.base_bindings, self.base_profiles = dict(self.runtime.bindings), dict(self.runtime.profiles)
        self.template = self.base_bindings[self.setup.template_binding_id]
        self.template_profile = self.base_profiles[self.setup.template_binding_id]
        self.template_policy = next(p.policy for p in self.config.policies if p.binding_id == self.setup.template_binding_id)
        self.current_key = channel_key("current", self.config.table.scope.authority_domain)
        self.revocation_key = channel_key("revocation", self.config.sha256)
        self.index_key = channel_key("requests", self.config.delivery.entry_id)
        self.enrollment = PersonalRouteEnrollment(self.store, self.setup.personal, clock=self.clock, jwks_transport=jwks_transport)
        self.send_transport, self.bot_token = send_transport, bot_token
        self.channel = None
        self.records = {}
        self.runtime.teams_metrics = self
        self.runtime.historical_binding_ids = frozenset(self.base_bindings)

    def start(self):
        if self.channel is not None:
            return self.channel
        settings = self.enrollment.settings(require_live=self.runtime.mode == "native")
        if settings.routes[0].route_id != self.setup.route_id:
            raise Problem(403, "TEAMS_NATIVE_ROUTE_DENIED", "配置必須引用原 signed personal route")
        seams = TeamsParentSeams(
            resolve_admission=self.register, resolve_context=self.context_owner,
            process_work=lambda obligation, _: self.ensure_enqueued(self.for_obligation(obligation).binding),
            resolve_send=self.send_context,
        )
        if self.runtime.mode == "native":
            if self.send_transport is not None or self.bot_token is not None or self.enrollment.jwks_transport is not None:
                raise Problem(403, "TEAMS_NATIVE_DEPENDENCY_DENIED", "native 不接受測試 transport／credential")
            self.channel = compose_installed_personal_channel(
                self.parent, self.enrollment, seams, evidence_id=self.setup.native_evidence_id,
                verify_current=self.native_evidence, artifact_links=self.artifact_links,
                artifact_origins=(self.setup.artifact_origin,),
            )
        else:
            shared = InstalledChannels(self.runtime.dep.delivery, self.config)
            self.channel = compose_personal_channel(
                self.enrollment, replace(self.runtime.dep.delivery.registration, contract=shared.contract),
                seams, transport=self.send_transport, bot_token=self.bot_token,
                artifact_links=self.artifact_links, artifact_origins=(self.setup.artifact_origin,), shared_channels=shared,
            )
        self.refresh()
        return self.channel

    def native_evidence(self, evidence_id):
        proof = read_document(self.setup.native_evidence_path, TeamsNativeEvidence)
        if proof.evidence_id != evidence_id or proof.server_configuration_sha256 != self.config.sha256:
            raise Problem(403, "TEAMS_NATIVE_INSTALLATION_DENIED", "目前 Teams observer 證據不屬於此配置")
        return proof

    def pointer(self, rows=None, *, current=True):
        rows = self.store.read_snapshot((self.current_key, self.revocation_key)) if rows is None else rows
        row = rows[self.current_key]
        if row is None:
            raise Problem(403, "CHANNELS_NOT_INSTALLED", "尚未套用原 authority 的合法 v2 遷移")
        value = parse_channel_installation(row.payload["data"])
        if current:
            check_window_interval(value, now=_utc(self.clock()))
        if (value.configuration_sha256 != self.config.sha256 or value.registry_sha256 != self.rule_sha256
                or not value.active or current and (
                    _utc(self.clock()) >= self.setup.valid_until or rows[self.revocation_key] is not None)):
            raise Problem(403, "CHANNELS_RULE_DENIED", "metrics 授權已撤銷、過期或配置不同")
        return value

    def derive(self, event_id):
        def named(kind):
            return control_key("teams-metrics-" + kind, event_id)
        owner = self.template.owner.model_copy(update={
            "owner_record_id": named("owner"), "conversation_id": named("conversation"), "policy_id": named("policy"),
            "epoch": 1,
        })
        binding = RuntimeBinding.model_validate({
            **self.template.model_dump(),
            "binding_id": named("binding"), "owner": owner, "event_id": event_id,
            "job_id": named("job"), "work": self.template.work.model_copy(update={"work_id": named("work")}),
            "route": Route(tenant_id=self.setup.personal.tenant_id, account_id=self.setup.personal.uami_client_id,
                           recipient_id=self.setup.personal.intended_user_oid, channel_id="msteams",
                           thread_id=self.setup.route_id),
            "operation_id": named("operation"), "attempt_id": named("attempt"), "grant_id": named("operation-grant"),
            "allowed_input_refs": ("teams-metrics-no-generic-operation",),
        })
        profile = self.template_profile.model_copy(update={
            "binding_id": binding.binding_id, "artifact_id": named("artifact"),
            "artifact_version": named("artifact-version")[:32], "max_model_requests": self.setup.max_model_requests,
        })
        return binding, profile

    def work_key(self, event_id):
        return channel_key("work", event_id)

    def registration_request(self, value):
        binding = value.binding
        previous = None if value.previous_event_id is None else self.derive(value.previous_event_id)[0].obligation_id
        keys = (self.current_key, self.revocation_key, self.index_key, self.work_key(value.event_id),
                control_key("budget", binding.owner.root_budget_id),
                control_key("delivery-quota", "root", binding.owner.root_budget_id),
                worker_key("pool", self.config.workers.workspace_id),
                control_key("teams-route-access", self.setup.route_id),
                *(native_key("target", target.executor_id) for target in self.config.active_targets),
                *((control_key("delivery-obligation", previous),) if previous else ()))
        return ControlRequest(request_id=channel_key("register", value.event_id),
                              request_digest=digest(value.model_dump(mode="json")), read_set=keys)

    def refresh(self):
        row = self.store.read_snapshot((self.index_key,))[self.index_key]
        events = () if row is None else tuple(row.payload["data"]["events"])
        if len(events) > self.setup.max_requests or len(set(events)) != len(events):
            raise Problem(409, "TEAMS_REGISTRY_INVALID", "有限 activity registry 已損壞")
        records = {}
        for event in events:
            value = retained(self.store, self.work_key(event), TeamsWork)
            binding, profile = self.derive(event)
            if (value.event_id != event or value.rule_sha256 != self.rule_sha256 or value.route_id != self.setup.route_id
                    or value.binding != binding or value.profile != profile):
                raise Problem(409, "TEAMS_REGISTRY_INVALID", "原 activity/source/job 固定綁定不吻合")
            self.store.reconcile(self.registration_request(value))
            records[binding.binding_id] = value
        from .behavior_work_policy import _REFRESH_LOCK
        with _REFRESH_LOCK:
            self.records = records
            # A concurrent instruction read must never see a channel-only cache.
            control = self.runtime.work_policy
            work = {} if control is None else dict(control.records)
            self.runtime.bindings = MappingProxyType({
                **self.base_bindings, **{key: value.binding for key, value in records.items()},
                **{key: value.binding for key, value in work.items()},
            })
            self.runtime.profiles = MappingProxyType({
                **self.base_profiles, **{key: value.profile for key, value in records.items()},
                **{key: value.profile for key, value in work.items()},
            })
        if self.runtime.dep.native_launches is not None:
            for value in records.values():
                b = value.binding
                self.runtime.dep.native_launches.jobs[b.job_id] = NativeJobRoute(
                    job_id=b.job_id, api_binding_id=b.binding_id, grant_id=native_key("teams-grant", b.event_id),
                )

    def gate_keys(self, binding):
        if binding.route.channel_id != "msteams":
            return ()
        return (self.current_key, self.revocation_key, self.work_key(binding.event_id),
                control_key("teams-route-access", self.setup.route_id))

    def check_state(self, binding, rows):
        if binding.route.channel_id != "msteams":
            return
        self.pointer(rows)
        row = rows[self.work_key(binding.event_id)]
        value = _parse(row.payload["data"], TeamsWork)
        access = _parse(rows[control_key("teams-route-access", self.setup.route_id)].payload["data"], TeamsRouteAccess)
        if (value.binding != binding or value.rule_sha256 != self.rule_sha256
                or row.payload["material"] != material(value.model_dump(mode="json")).model_dump(mode="json")
                or not access.active or access.recipient_id != binding.human_oid
                or binding.owner.owner_record_id not in access.owner_record_ids):
            raise Problem(403, "TEAMS_WORK_AUTHORITY_DENIED", "目前 activity、owner 或 personal ACL 不吻合")

    def check_action(self, binding, action):
        if binding.route.channel_id == "msteams" and action not in {"read", "consume", "execute"}:
            raise Problem(403, "TEAMS_FIXED_METRICS_ONLY", "此入口不授權一般來源修改、批准或其他工具")

    def context_owner(self, owner_id):
        self.refresh()
        matches = [v.binding for v in self.records.values() if v.binding.owner.owner_record_id == owner_id]
        if len(matches) != 1:
            raise Problem(403, "TEAMS_WORK_AUTHORITY_DENIED", "owner 不屬於原 activity registry")
        owner = _parse(self.store.read_snapshot((control_key("owner", owner_id),))[control_key("owner", owner_id)].payload["data"], OwnerRecord)
        context = ExecutionContext(**{field: getattr(owner, field) for field in ExecutionContext.model_fields})
        if any(getattr(context, field) != getattr(matches[0].owner, field) for field in ExecutionContext.model_fields if field != "epoch"):
            raise Problem(403, "TEAMS_WORK_AUTHORITY_DENIED", "原 owner/root 綁定已改變")
        return context

    def for_obligation(self, obligation):
        self.refresh()
        matches = [value for value in self.records.values() if value.binding.obligation_id == obligation]
        if len(matches) != 1:
            raise Problem(403, "TEAMS_WORK_AUTHORITY_DENIED", "交付責任不屬於這個 personal 入口")
        return matches[0]

    def register(self, incoming):
        if incoming.text != self.setup.command.encode() or incoming.route.route_id != self.setup.route_id:
            raise Problem(422, "TEAMS_FIXED_METRICS_ONLY", "目前只接受 FA metrics，不能由訊息選來源或工作參數")
        self.pointer()
        self.refresh()
        binding, profile = self.derive(incoming.event_id)
        prior = self.records.get(binding.binding_id)
        if prior is not None:
            if prior.body_digest != incoming.body_digest:
                raise Problem(409, "TEAMS_SOURCE_CONFLICT", "相同 provider activity 不能換內容")
            return TeamsAdmission(context=self.context_owner(binding.owner.owner_record_id),
                                  root_request_id=binding.root_request_id, work=binding.work)
        value = TeamsWork(event_id=incoming.event_id, body_digest=incoming.body_digest, route_id=self.setup.route_id,
                          rule_sha256=self.rule_sha256, observed_at=incoming.observed_at, binding=binding, profile=profile)
        work_key = self.work_key(incoming.event_id)
        root_key = control_key("delivery-quota", "root", binding.owner.root_budget_id)
        access_key = control_key("teams-route-access", self.setup.route_id)
        current = self.store.read_snapshot((self.index_key,))[self.index_key]
        active = None if current is None else current.payload["data"].get("active_event")
        value = value.model_copy(update={"previous_event_id": active})
        previous_ob = None if active is None else self.derive(active)[0].obligation_id
        request = self.registration_request(value)

        def decide(rows):
            self.pointer(rows)
            budget = rows[control_key("budget", binding.owner.root_budget_id)]
            if budget is None or not budget.payload["data"]["active"]:
                raise Problem(403, "TEAMS_WORK_AUTHORITY_DENIED", "原 root budget 已撤權，不能建立替代工作")
            index = rows[self.index_key]
            events = () if index is None else tuple(index.payload["data"]["events"])
            held = None if index is None else index.payload["data"].get("active_event")
            if held != active:
                raise Problem(409, "TEAMS_SOURCE_CONFLICT", "另一個 activity 已取得原入口")
            if active:
                ob = rows[control_key("delivery-obligation", previous_ob)]
                if ob is None or ob.payload["data"]["fulfillment"] != "fulfilled":
                    raise Problem(409, "TEAMS_PRIOR_RESPONSIBILITY_PENDING", "原 activity／交付責任尚未結案，不能另開工作")
            pool = self.runtime.dep.workers._pool(rows)
            if (rows[root_key].payload["data"]["pending"] != 0 or pool.pending or pool.completions
                    or any(slot.binding is not None or slot.lease_until is not None for slot in pool.slots)):
                raise Problem(409, "TEAMS_PRIOR_RESPONSIBILITY_PENDING", "原 root/C1 仍 occupied 或未交付")
            for target in self.config.active_targets:
                row = rows[native_key("target", target.executor_id)]
                if row is not None:
                    reserved = _parse(row.payload["data"], _TargetReservation)
                    if (reserved.target != target or reserved.active_launch_id is not None
                            or reserved.configuration_digest != self.runtime.dep.workers.configuration_digest()):
                        raise Problem(409, "TEAMS_PRIOR_RESPONSIBILITY_PENDING", "原 native target 仍未知或綁定不符")
            if len(events) >= self.setup.max_requests:
                raise Problem(429, "TEAMS_REQUEST_LIMIT", "已達本次 operator 批准的 metrics 次數")
            access = _parse(rows[access_key].payload["data"], TeamsRouteAccess)
            if (not access.active or access.route_id != self.setup.route_id or access.recipient_id != binding.human_oid
                    or self.template.owner.owner_record_id not in access.owner_record_ids):
                raise Problem(403, "TEAMS_ROUTE_ACCESS_DENIED", "原 personal ACL 未授權這個固定模板")
            policy = RuntimePolicy.model_validate({
                **self.template_policy.model_dump(), "binding_digest": self.runtime.binding_digest(binding),
                "active": True, "valid_until": self.setup.valid_until, "operation_policy_revision": 1,
                "human_text_enabled": False, "text_policy_version": None,
            })
            owner = OwnerRecord(**binding.owner.model_dump(), active=True, operation_ids=(binding.operation_id,))
            data = [
                (work_key, value.model_dump(mode="json"), material(value.model_dump(mode="json"))),
                (control_key("owner", binding.owner.owner_record_id), owner.model_dump(mode="json"), None),
                (control_key("policy", binding.owner.policy_id), PolicyRecord(policy_id=binding.owner.policy_id, revision=1, active=True).model_dump(mode="json"), None),
                (runtime_key("policy", binding.binding_id), policy.model_dump(mode="json"), None),
                (runtime_key("plan-index", binding.binding_id), {"keys": []}, None),
            ]
            if self.runtime.dep.native_launches is not None:
                task = self.task_input(binding, profile)
                grant = NativeExecutionGrant(
                    grant_id=native_key("teams-grant", binding.event_id), controller_id=self.config.controller_id,
                    owner=binding.owner, policy_revision=1, job_id=binding.job_id, definition=binding.work,
                    input_digest=hashlib.sha256(task).hexdigest(), targets=self.config.active_targets,
                    active=True, remaining_uses=self.setup.native_max_generations, not_before=_utc(self.clock()),
                    expires_at=self.setup.valid_until, workload_version="assistant-v1.2",
                )
                data.append((native_key("grant", grant.grant_id), grant.model_dump(mode="json"), None))
            writes = [ControlWrite(row_id=key, mode="create", data=body, material=fixed) for key, body, fixed in data]
            writes.extend((
                ControlWrite(row_id=self.index_key, mode="create" if index is None else "replace",
                             data={"events": [*events, incoming.event_id], "active_event": incoming.event_id}),
                ControlWrite(row_id=access_key, mode="replace", data=access.model_copy(update={
                    "revision": access.revision + 1,
                    "owner_record_ids": (self.template.owner.owner_record_id, binding.owner.owner_record_id),
                }).model_dump(mode="json")),
            ))
            return ControlChanges(writes=tuple(writes), result={"binding_id": binding.binding_id, "not_yet_admitted": True})

        self.store.transact(request, decide)
        self.refresh()
        return TeamsAdmission(context=binding.owner, root_request_id=binding.root_request_id, work=binding.work)

    @staticmethod
    def task_input(binding, profile):
        from .behavior_assistant_wire import Task
        return Task(job_id=binding.job_id, work_id=binding.work.work_id,
                    definition_digest=digest(binding.work.model_dump(mode="json")),
                    profile_digest=digest(profile.model_dump(mode="json")), source=profile.source,
                    artifact_id=profile.artifact_id, artifact_version=profile.artifact_version,
                    operation_id=binding.operation_id).model_dump_json().encode()

    def delegation(self, binding, consent):
        self.channel.runtime.verify_ingress(binding.event_id)
        return consent.model_copy(update={"operator_rule_sha256": self.rule_sha256,
                                          "source_evidence_id": binding.event_id})

    def verify_delegation(self, binding, consent, rows):
        self.check_state(binding, rows)
        if consent.operator_rule_sha256 != self.rule_sha256 or consent.source_evidence_id != binding.event_id:
            raise Problem(403, "TEAMS_WORK_AUTHORITY_DENIED", "SDK 不能借用其他活動或批准")

    def ensure_enqueued(self, binding):
        source = retained(self.store, control_key("teams-source", binding.event_id), _Source)
        self.channel.runtime.verify_ingress(binding.event_id)
        self.store.reconcile(source.request)
        if source.command.work != binding.work or source.command.route != binding.route:
            raise Problem(403, "TEAMS_SOURCE_CONFLICT", "原 signed admission 與 SDK work 不吻合")
        obligation_key = control_key("delivery-obligation", binding.obligation_id)
        obligation = _parse(self.store.read_snapshot((obligation_key,))[obligation_key].payload["data"], _Obligation)
        if obligation.fulfillment == "fulfilled":
            return
        principal = BehaviorPrincipal(binding.tenant_id, binding.human_oid, binding.team_id, "human", None,
                                      frozenset({"read", "submit"}))
        command = AssistantSubmit(request_id=channel_key("assistant-command", binding.event_id))
        context = self.runtime.resolve_owner(binding.owner.owner_record_id)
        self.runtime._save_plan(binding, "admission", principal, command, context,
                                {"admission": source.command.model_dump(mode="json")}, source.request)
        self.runtime._enqueue(binding, principal, command, context)

    def send_context(self, delivery):
        row = self.store.read_snapshot((control_key("delivery-outbox", delivery),))[control_key("delivery-outbox", delivery)]
        box = _parse(row.payload["data"], _Outbox)
        binding = self.for_obligation(box.binding.obligation_id).binding
        return TeamsSendContext(context=self.context_owner(binding.owner.owner_record_id),
                                grant_id=channel_key("send-grant", delivery))

    def artifact_links(self, versions, route):
        if route.route_id != self.setup.route_id:
            raise Problem(403, "TEAMS_ARTIFACT_SCOPE_DENIED", "成果只能交給原 personal route")
        from .behavior_dispatch import ArtifactRoute
        return tuple(TeamsArtifactLink(artifact=value, url=self.setup.artifact_origin + ArtifactRoute(
            team=self.config.identity.team_id, artifact_id=value.artifact_id, version_id=value.version,
        ).path()) for value in versions)

    def deliver(self, binding, prepared, *, cleanup_authority=None):
        if cleanup_authority is not None:
            from .behavior_teams_cleanup import CleanupDelivery
            if type(cleanup_authority) is not CleanupDelivery or cleanup_authority.service.teams is not self:
                raise Problem(403, "TEAMS_CLEANUP_DELIVERY_DENIED", "exact original cleanup authority required")
            cleanup_authority.check_owner()
        delivery = prepared.get("delivery_id") or prepared.get("binding", {}).get("delivery_id")
        if delivery is None:
            if prepared.get("status") == "fulfilled":
                return prepared
            raise Problem(409, "TEAMS_OUTBOX_NOT_READY", "固定結果尚未進入 outbox")
        adapter = self.channel.adapter if cleanup_authority is None else cleanup_authority.adapter
        if adapter._send_row(delivery) is not None:
            return adapter.reconcile(delivery)
        access_key = control_key("teams-route-access", self.setup.route_id)
        owner_key = control_key("owner", binding.owner.owner_record_id)
        outbox_key = control_key("delivery-outbox", delivery)
        job_key = worker_key("job", binding.job_id)
        work_key = control_key("delivery-work-state", binding.obligation_id)
        gate_keys = self.runtime._gate_keys(binding)
        if cleanup_authority is not None:
            gate_keys = (*gate_keys, cleanup_authority.authorization_key)
        keys = tuple(dict.fromkeys((*gate_keys, access_key, owner_key, outbox_key, job_key, work_key)))
        request = ControlRequest(request_id=channel_key("result-scope", delivery), request_digest=digest([binding.binding_id, delivery]),
                                 read_set=keys)
        def scope(rows):
            if cleanup_authority is None:
                self.runtime._state(binding, rows)
            else:
                cleanup_authority.state(binding, rows)
            box = _parse(rows[outbox_key].payload["data"], _Outbox)
            job, work = _parse(rows[job_key].payload["data"], JobRecord), _parse(rows[work_key].payload["data"], _Work)
            if (job.state not in {"succeeded", "failed", "cancelled"} or not job.result_forwarded
                    or job.spec.work_id != binding.work.work_id or work.definition != binding.work
                    or work.result is None or work.result.artifacts != box.binding.artifacts):
                raise Problem(403, "TEAMS_RESULT_DENIED", "必須使用原 WorkerCore 驗證並轉交的 SDK 結果")
            access, owner = _parse(rows[access_key].payload["data"], TeamsRouteAccess), _parse(rows[owner_key].payload["data"], OwnerRecord)
            artifacts = tuple(dict.fromkeys((*access.artifacts, *box.binding.artifacts)))
            updated_access = TeamsRouteAccess.model_validate({
                **access.model_dump(), "revision": access.revision + 1, "artifacts": artifacts,
            })
            updated_owner = OwnerRecord.model_validate({
                **owner.model_dump(), "operation_ids": tuple(dict.fromkeys((*owner.operation_ids, adapter.operation_id(delivery)))),
            })
            return ControlChanges(writes=(
                ControlWrite(row_id=access_key, mode="replace", data=updated_access.model_dump(mode="json")),
                ControlWrite(row_id=owner_key, mode="replace", data=updated_owner.model_dump(mode="json")),
            ), result={"result_scope_bound": True})
        self.store.transact(request, scope)
        plan = adapter.prepare(delivery)
        context = self.context_owner(binding.owner.owner_record_id)
        if cleanup_authority is None:
            grant_id = channel_key("send-grant", delivery)
            pointer = self.pointer()
            approval_row = self.store.read_snapshot((pointer.approval_id,))[pointer.approval_id]
            if approval_row is None:
                raise Problem(403, "CHANNELS_APPROVAL_DENIED", "installed human channel approval is missing")
            approval = parse_channel_approval(approval_row.payload["data"], pointer, operator_oid=self.config.identity.human_oid)
            expires_at = self.setup.valid_until
        else:
            grant_id = cleanup_authority.send_context(delivery).grant_id
            approval = cleanup_authority.authorization
            expires_at = cleanup_authority.expires_at
        grant_key = control_key("grant", grant_id)
        grant = GrantRecord(
            grant_id=grant_id, approval_source="human", approval_id=approval.decision_id,
            subjects=(GrantSubject(principal_id=context.principal_id, conversation_id=context.conversation_id),),
            root_budget_id=context.root_budget_id, description_digest=plan.description_digest,
            adapter_contract_digest=adapter.contract_digest, policy_id=context.policy_id, policy_revision=1,
            revocation_revision=0, revoked=False, not_before=_utc(self.clock()), expires_at=expires_at,
            remaining_uses=1, remaining_units=adapter.contract.reservation_units,
        )
        grant_request = ControlRequest(
            request_id=channel_key("authorize-send" if cleanup_authority is None else "cleanup-authorize-send", delivery),
            request_digest=digest([delivery, plan.description_digest, self.rule_sha256] if cleanup_authority is None else
                                  [delivery, plan.description_digest, digest(approval.model_dump(mode="json"))]),
            read_set=(*gate_keys, grant_key))
        def authorize(rows):
            if cleanup_authority is None:
                self.runtime._state(binding, rows)
            else:
                cleanup_authority.state(binding, rows)
            if rows[grant_key] is not None:
                raise Problem(409, "TEAMS_SEND_UNKNOWN", "原 send grant 已存在，只能核對原請求")
            return ControlChanges(writes=(ControlWrite(row_id=grant_key, mode="create", data=grant.model_dump(mode="json")),),
                                  result={"approval_id": approval.decision_id, "rule_sha256": self.rule_sha256,
                                          "description_digest": plan.description_digest})
        self.store.transact(grant_request, authorize)
        return adapter.send(delivery)

    def drain(self, controller, *, limit):
        self.refresh()
        row = self.store.read_snapshot((self.index_key,))[self.index_key]
        active = None if row is None else row.payload["data"].get("active_event")
        if active is None:
            return ()
        binding = self.derive(active)[0]
        ob_key = control_key("delivery-obligation", binding.obligation_id)
        ob = self.store.read_snapshot((ob_key,))[ob_key]
        if ob is not None and ob.payload["data"]["fulfillment"] == "fulfilled":
            return ()
        try:
            delivery_id = None if ob is None else ob.payload["data"]["final_delivery_id"]
            if delivery_id is not None and self.channel.adapter._send_row(delivery_id) is not None:
                receipt = self.channel.adapter.reconcile(delivery_id)
                return ({**receipt.payload["result"],
                         "problem": self.channel.adapter.inspect(delivery_id)["problem_code"]},)
            self.ensure_enqueued(binding)
            status = self.runtime._controller_step(binding.binding_id, controller)
            return (self.runtime.dep.workers._result(job_id=binding.job_id, status=status.state,
                                                     obligation_fulfilled=status.obligation_fulfilled),)
        except Problem as error:
            return (self.runtime.dep.workers._result(job_id=binding.job_id, problem=error.body["error"]["code"]),)

    def deliver_page(self, page):
        results = []
        for item in page:
            binding = item.get("binding", {})
            if item.get("problem") or binding.get("route", {}).get("channel_id") != "msteams":
                results.append(item)
                continue
            try:
                work = self.for_obligation(binding["obligation_id"])
                receipt = self.deliver(work.binding, item)
                results.append(receipt.payload["result"] if hasattr(receipt, "payload") else receipt)
            except Problem as error:
                results.append({**item, "problem": error.body["error"]["code"]})
        return tuple(results)

    def entry(self):
        self.start()
        service = self
        class Entry(PersonalTeamsEntry):
            def receive(self, authorization, body):
                service.pointer()
                service.parent.require_current_native_evidence()
                service.channel.runtime.enable_admission()
                result = super().receive(authorization, body)
                if "obligation_id" in result:
                    service.ensure_enqueued(service.for_obligation(result["obligation_id"]).binding)
                return result
        entry = Entry(self.enrollment)
        entry.attach_runtime(self.channel.runtime)
        return entry

    def install_routes(self, app, verifier):
        from fastapi import Depends
        from fastapi.responses import Response
        from .behavior_api import _principal_dependency, _response
        from .behavior_dispatch import ArtifactRoute

        app.include_router(self.entry().router())
        authenticate = _principal_dependency(verifier)

        def human(principal):
            principal.require(self.config.identity.team_id, "read")
            if (principal.kind != "human" or principal.oid != self.config.identity.human_oid
                    or principal.tenant_id != self.config.identity.tenant_id):
                raise Problem(403, "TEAMS_HUMAN_REQUIRED", "只有原指定使用者可讀取／管理這個 personal 入口")

        @app.post("/behavior/v2/channels/teams/native")
        def publish(principal=Depends(authenticate)):
            human(principal)
            principal.require(self.config.identity.team_id, "approve")
            self.pointer()
            self.parent.require_current_native_evidence()
            return _response(self.channel.adapter.publish_installation())

        @app.post("/behavior/v2/channels/teams/revoke")
        def revoke(principal=Depends(authenticate)):
            human(principal)
            principal.require(self.config.identity.team_id, "approve")
            self.parent.installation()
            request = ControlRequest(
                request_id=self.revocation_key, request_digest=digest([self.config.sha256, principal.oid]),
                read_set=(self.current_key, self.revocation_key),
            )
            def decide(rows):
                self.pointer(rows, current=False)
                value = ChannelRevocation(
                    configuration_sha256=self.config.sha256, registry_sha256=self.rule_sha256,
                    operator_oid=principal.oid, revoked_at=_utc(self.clock()),
                )
                return ControlChanges(writes=(ControlWrite(
                    row_id=self.revocation_key, mode="create", data=value.model_dump(mode="json"),
                    material=material(value.model_dump(mode="json")),
                ),), result={"revoked": True, "inflight_effects_recalled": False})
            return _response(self.store.transact(request, decide))

        @app.get("/behavior/v2/channels/{channel_id}/receipts/{evidence_id}")
        def receipt(channel_id: str, evidence_id: str, principal=Depends(authenticate)):
            human(principal)
            self.parent.installation()
            if channel_id == "msteams":
                proof = self.channel.adapter.verify_receipt(evidence_id)
            elif channel_id == "authenticated-pull":
                proof = self.runtime.dep.inbox.verify_receipt(evidence_id)
            else:
                raise Problem(404, "CHANNELS_RECEIPT_NOT_FOUND", "未註冊的 channel")
            if (proof.binding.route.channel_id != channel_id
                    or proof.binding.route.recipient_id != principal.oid
                    or proof.binding.route.tenant_id != principal.tenant_id):
                raise Problem(403, "CHANNELS_RECEIPT_DENIED", "receipt 不屬於此使用者或通道")
            return _response(proof)

        @app.get("/teams/{team}/artifacts/{artifact_id}/versions/{version_id}/content")
        def artifact(team: str, artifact_id: str, version_id: str, principal=Depends(authenticate)):
            human(principal)
            self.parent.require_current_native_evidence()
            ArtifactRoute(team=team, artifact_id=artifact_id, version_id=version_id)
            self.refresh()
            matches = [item for item in self.records.values()
                       if item.binding.team_id == team and item.profile.artifact_id == artifact_id
                       and item.profile.artifact_version == version_id]
            if len(matches) != 1:
                raise Problem(404, "TEAMS_ARTIFACT_NOT_FOUND", "成果版本不屬於這個 personal 入口")
            item = matches[0]
            artifacts = self.parent.assistant.artifacts
            actor = self.parent.assistant.authority.actors[item.binding.agent_oid]
            version = artifacts.version(actor, team, artifact_id, version_id)
            access_key = control_key("teams-route-access", self.setup.route_id)
            access = _parse(self.store.read_snapshot((access_key,))[access_key].payload["data"], TeamsRouteAccess)
            if (not access.active or access.recipient_id != principal.oid
                    or ArtifactVersion(artifact_id=artifact_id, version=version_id, sha256=version.sha256) not in access.artifacts):
                raise Problem(403, "TEAMS_ARTIFACT_SCOPE_DENIED", "目前收件人讀取權限不允許這個成果")
            _, content = artifacts.download(actor, team, artifact_id, version_id, version.blob.etag)
            return Response(content, media_type="application/json", headers={"ETag": version.blob.etag, "Cache-Control": "no-store"})
