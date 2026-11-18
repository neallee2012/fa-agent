"""Real ASGI/HTTP sockets and producer receipts; never a cloud model service."""
from contextlib import contextmanager
import asyncio
import json
import socket
import threading
import time

import pytest
import uvicorn
from datetime import timedelta

from behavior_assistant_acquire_conflicts_helpers import installed_assistant
from test_m3_auth import jwk_client, signing_key


@contextmanager
def asgi_server(app):
    import logging
    # uvicorn.Config applies its logging config (uvicorn propagate=False, levels, handlers); restore it so a
    # later test's caplog still receives uvicorn.error records.
    saved = [(logger, logger.level, logger.propagate, logger.disabled, list(logger.handlers))
             for logger in map(logging.getLogger, ("uvicorn", "uvicorn.error", "uvicorn.access", "uvicorn.asgi"))]
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    config = uvicorn.Config(app, host="127.0.0.1", port=port, lifespan="off", access_log=False,
                            log_level="critical", loop="asyncio", timeout_graceful_shutdown=1)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]})
    thread.start()
    try:
        end = time.monotonic() + 5
        while not server.started and thread.is_alive() and time.monotonic() < end:
            time.sleep(0.01)
        assert server.started and thread.is_alive()
        yield port
    finally:
        server.should_exit = True
        thread.join(5)
        sock.close()
        for logger, level, propagate, disabled, handlers in saved:
            logger.setLevel(level)
            logger.propagate, logger.disabled, logger.handlers[:] = propagate, disabled, handlers
        assert not thread.is_alive(), "owned ASGI server must finish after test waiters are released"


def test_deadline_defaults_are_assistant_only_and_not_http_flags():
    import inspect
    from shared_context.behavior_assistant_deadlines import SERVER_MODEL_SECONDS, CLIENT_MODEL_SECONDS, SDK_TURN_SECONDS
    from shared_context.m2_copilot import run_copilot
    assert (SERVER_MODEL_SECONDS, CLIENT_MODEL_SECONDS, SDK_TURN_SECONDS) == (75, 90, 240)
    assert inspect.signature(run_copilot).parameters["timeout_seconds"].default == 240
    from shared_context.behavior_assistant_wire import ModelRequest
    # Later opt-in fields (checkpoint_compaction 230c7ca1, delegation) are not deadlines and stay off the default wire.
    assert set(ModelRequest.model_fields) == {"request_id", "body", "checkpoint_compaction", "delegation"}
    assert not any(word in name for name in ModelRequest.model_fields for word in ("deadline", "timeout", "seconds"))
    assert set(ModelRequest(request_id="a" * 64, body={}).model_dump(mode="json")) == {"request_id", "body"}
