"""從 test_agent_runtime_routing.py 原封搬出的共用 helper，讓留存測試不依賴待刪的舊路測試檔。"""

import asyncio
import pytest
from test_m3_auth import token


@pytest.fixture
def routing_peer(request):
    # Windows creates its internal loop wakeup pipe with a local socket pair.
    # Create only that loop before the startup fixture forbids every network connect.
    loop = asyncio.new_event_loop()
    try:
        yield request.getfixturevalue("peer_runtime"), loop
    finally:
        if not loop.is_closed():
            loop.close()


def agent_headers(c, key, agent):
    return {"Authorization": "Bearer " + token(key, {
        "oid": c.h.config.targets[agent].agent_oid, "idtyp": "app", "roles": ["Behavior.Agent"],
    }, remove=("scp",)), "Content-Type": "application/json"}
