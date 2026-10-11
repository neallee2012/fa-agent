from __future__ import annotations

import asyncio
import json
import logging

from azure.ai.agentserver.invocations import InvocationAgentServerHost
from starlette.requests import Request
from starlette.responses import JSONResponse

from poc_core import PocDispatcher, PocError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("fa-hosted-sandbox-poc")

MAX_REQUEST_BYTES = 3 * 1024 * 1024
dispatcher = PocDispatcher()
app = InvocationAgentServerHost()


@app.invoke_handler
async def handle_invoke(request: Request) -> JSONResponse:
    body = await request.body()
    if len(body) > MAX_REQUEST_BYTES:
        return JSONResponse(
            status_code=413,
            content=PocError(
                "INVALID_REQUEST", "request body is too large", status_code=413
            ).as_dict(),
        )
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JSONResponse(
            status_code=400,
            content=PocError(
                "INVALID_REQUEST", "request body must be valid UTF-8 JSON"
            ).as_dict(),
        )

    try:
        response = await asyncio.to_thread(dispatcher.dispatch, payload)
        response["runtime"] = {
            "session_id": getattr(request.state, "session_id", None),
            "invocation_id": getattr(request.state, "invocation_id", None),
        }
        return JSONResponse(response)
    except PocError as exc:
        logger.warning(
            "POC operation failed: reason_code=%s message=%s details=%s",
            exc.reason_code,
            exc.message,
            json.dumps(exc.details, ensure_ascii=True),
        )
        return JSONResponse(status_code=exc.status_code, content=exc.as_dict())
    except Exception as exc:
        logger.exception("Unhandled POC operation failure")
        return JSONResponse(
            status_code=500,
            content=PocError(
                "IMAGE_OR_DEPENDENCY",
                "operation failed inside the hosted container",
                status_code=500,
                details={"error_type": type(exc).__name__, "error": str(exc)},
            ).as_dict(),
        )


if __name__ == "__main__":
    app.run()
