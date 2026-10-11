from __future__ import annotations

import asyncio
import json
from threading import Lock

from azure.ai.agentserver.invocations import InvocationAgentServerHost
from starlette.requests import Request
from starlette.responses import JSONResponse

from shared_context.errors import Problem
from shared_context.hosted import HostedClient, runtime_metadata

app = InvocationAgentServerHost(configure_observability=None, access_log=None)
client: HostedClient | None = None
client_lock = Lock()


def invoke(payload: dict) -> dict:
    global client
    private_probe = isinstance(payload, dict) and payload.get("operation") in {"environment", "assistant", "model_budget"}
    runtime = runtime_metadata(probe_home=False) if private_probe else runtime_metadata()
    with client_lock:
        if client is None:
            client = HostedClient.from_env()
    response = client.dispatch(payload)
    response["runtime"] = runtime
    return response


@app.invoke_handler
async def handle_invoke(request: Request) -> JSONResponse:
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > 64 * 1024:
            return JSONResponse(status_code=413, content={
                "error": {"code": "REQUEST_TOO_LARGE", "message": "invocation exceeds 64 KiB"},
            })
    try:
        payload = json.loads(body)
        response = await asyncio.to_thread(invoke, payload)
        response["runtime"].update({
            "session_id": getattr(request.state, "session_id", None),
            "invocation_id": getattr(request.state, "invocation_id", None),
        })
        return JSONResponse(response)
    except (ValueError, UnicodeDecodeError):
        return JSONResponse(status_code=400, content={
            "error": {"code": "INVALID_JSON_OR_CONFIG", "message": "invalid UTF-8 JSON or runtime configuration"},
        })
    except Problem as exc:
        return JSONResponse(status_code=exc.status, content=exc.body)
    except Exception as exc:
        return JSONResponse(status_code=500, content={
            "error": {"code": "HOSTED_FAILURE", "type": type(exc).__name__},
        })


if __name__ == "__main__":
    app.run()
