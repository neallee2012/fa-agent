"""從 test_work_policy_lifecycle.py 原封搬出的共用 helper，讓留存測試不依賴待刪的舊路測試檔。"""

from datetime import timedelta
from dataclasses import replace
from shared_context.behavior_work_policy import WorkPolicyControl
from work_cost_policy_helpers import standing, install_offline


def enabled(assistant,*,after_global=False):
    h,policy,observed,_,human=standing(assistant)
    if after_global:h.time=h.policy.valid_until+timedelta(seconds=1)
    facts=[observed.model_copy(update={"observed_at":h.time})]
    control=WorkPolicyControl(h.runtime,policy,lambda:facts[0])
    install_offline(control,human,"offline-once-standing-policy",h.time+timedelta(minutes=10))
    initial=h.readiness()
    h.delivery.registration=replace(h.delivery.registration,verify_readiness=lambda:initial.model_copy(update={
        "observed_at":h.time,"expires_at":h.time+timedelta(minutes=5)}))
    return h,control,human,facts
