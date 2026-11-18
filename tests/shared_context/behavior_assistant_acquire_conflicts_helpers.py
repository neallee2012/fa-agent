"""從 test_behavior_assistant_acquire_conflicts.py 原封搬出的共用 helper，讓留存測試不依賴待刪的舊路測試檔。"""

from types import SimpleNamespace
import pytest
from shared_context.behavior_auth import BehaviorPrincipal
from test_behavior_assistant_server import AssistantHarness
from test_behavior_server import NOW
from test_m3_auth import jwk_client


@pytest.fixture
def installed_assistant(tmp_path, jwk_client):
    """An assistant configuration installed by the ordinary bootstrap writer, on its own offline Table."""
    h = AssistantHarness(tmp_path, jwk_client)
    h.legacy()
    h.activate()
    identity = h.config.identity
    principal = BehaviorPrincipal(identity.tenant_id, identity.human_oid, identity.team_id, "human", None,
                                  frozenset({"read", "submit", "approve", "acknowledge"}))
    try:
        yield SimpleNamespace(harness=h, table=h.table, config=h.config, principal=principal, clock=[NOW])
    finally:
        h.composition.close()
