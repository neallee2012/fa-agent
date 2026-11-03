"""The SDK's session.rpc for offline session fakes: m2_copilot sets verbosity on every session it opens."""
from types import SimpleNamespace


async def _applied(params):
    return SimpleNamespace(success=True)


OFFLINE_SESSION_RPC = SimpleNamespace(options=SimpleNamespace(update=_applied))
