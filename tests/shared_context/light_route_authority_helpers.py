"""從 test_light_route_authority.py 原封搬出的共用 helper，讓留存測試不依賴待刪的舊路測試檔。"""

from shared_context.behavior_auth import BehaviorPrincipal


def _session_id(root):
    return root.rstrip("/").rsplit("/", 1)[-1]


def _agent(h, binding):
    return BehaviorPrincipal(binding.tenant_id, binding.agent_oid, binding.team_id, "agent",
                             h.verifier.auth.actors[binding.agent_oid].actor,
                             frozenset({"read", "submit", "execute", "consume"}))
