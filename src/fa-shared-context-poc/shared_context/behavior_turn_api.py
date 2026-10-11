"""Receipt endpoints for a T2-verified connection; issuer and SDK execution stay outside these routes."""

from fastapi import Depends, Request
from starlette.concurrency import run_in_threadpool

from .behavior_agent_control import CONNECTION_HEADER, MAX_FRAME_BYTES
from .behavior_agent_worker import AgentTurnAddress
from .behavior_api import _body, _principal_dependency, _response
from .behavior_turn_wire import BeginTurn, TurnEnd
from .errors import Problem

PREFIX = "/behavior/v1/turn-receipts"


def install_turn_routes(app, receipts, verifier, connection_context):
    if not callable(connection_context):
        raise ValueError("the signed control-handshake context provider is required")
    identity = _principal_dependency(verifier)

    def header(request, context):
        values = request.headers.getlist(CONNECTION_HEADER)
        if len(values) != 1 or values[0] != context.control_connection_id:
            raise Problem(403, "TURN_WORKER_DENIED", "the original signed control connection is required")

    @app.post(PREFIX + "/begin")
    async def begin(request: Request, principal=Depends(identity)):
        command = await _body(request, BeginTurn, max_body_bytes=MAX_FRAME_BYTES)

        def start():
            # Keep only context validation and the start CAS under T2's connection lease, never SDK execution.
            with connection_context(principal) as context:
                header(request, context)
                return receipts.begin(principal, context, command)

        return _response(await run_in_threadpool(start), max_bytes=MAX_FRAME_BYTES)

    for action, model in (("read", AgentTurnAddress), ("cancel", AgentTurnAddress), ("complete", TurnEnd)):
        def endpoint(action=action, model=model):
            async def call(request: Request, principal=Depends(identity)):
                command = await _body(request, model, max_body_bytes=MAX_FRAME_BYTES)

                def invoke():
                    with connection_context(principal) as context:
                        header(request, context)
                    return getattr(receipts, action)(principal, context, command)

                return _response(await run_in_threadpool(invoke), max_bytes=MAX_FRAME_BYTES)
            return call

        app.add_api_route(PREFIX + "/" + action, endpoint(), methods=["POST"])
