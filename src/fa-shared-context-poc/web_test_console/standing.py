"""Standing console config pinned to the original deterministic policy derivation."""
from typing import Literal

from pydantic import model_validator

from shared_context import behavior_wire as wire
from shared_context.behavior_work_policy import SteadyWorkPolicy, derive_work
from .adapter import SteadyConsoleConfig, require


def task_matches(task, binding):
    return (task.binding_id == binding.binding_id and task.job_id == binding.job_id
            and task.work_id == binding.work.work_id and task.event_id == binding.event_id
            and task.root_request_id == binding.root_request_id and task.obligation_id == binding.obligation_id
            and task.owner_record_id == binding.owner.owner_record_id
            and task.route.model_dump(mode="json") == binding.route.model_dump(mode="json"))


class StandingConsoleConfig(SteadyConsoleConfig):
    schema_version: Literal["fa-web-test-console-standing-v3"] = "fa-web-test-console-standing-v3"
    standing_policy: SteadyWorkPolicy
    standing_policy_sha256: wire.Digest

    @model_validator(mode="after")
    def pinned_derivation(self):
        p = self.standing_policy
        if (p.sha256 != self.standing_policy_sha256 or p.configuration_sha256 != self.backend_configuration_sha256
                or p.template.tenant_id != self.tenant_id or p.template.human_oid != self.human_oid
                or p.template.route.recipient_id != self.human_oid):
            raise ValueError("exact trusted standing policy/configuration/identity required")
        for task in self.tasks:
            _, expected, _ = derive_work(p, task.submit_request_id, p.evidence_mode)
            if task.submission != "assistant" or not task_matches(task, expected):
                raise ValueError("standing tasks must exactly match the original deterministic policy derivation")
        return self

