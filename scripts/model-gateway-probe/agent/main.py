"""Hosted probe agent: input is a target id from PROBE_TARGETS or ALL; replies with the probe results as JSON."""

import asyncio
import json
import logging
import os

from azure.ai.agentserver.responses import CreateResponse, ResponseContext, ResponsesAgentServerHost, TextResponse
from azure.identity import ManagedIdentityCredential

from probe import run_target

logging.basicConfig(level=logging.INFO)
TARGETS = json.loads(os.environ.get("PROBE_TARGETS") or "[]")
credential = ManagedIdentityCredential()
app = ResponsesAgentServerHost()


def get_token(audience):
    return credential.get_token(audience + "/.default").token


@app.response_handler
async def handler(request: CreateResponse, context: ResponseContext, _cancel: asyncio.Event):
    wanted = (await context.get_input_text() or "").strip()
    targets = [t for t in TARGETS if wanted in ("ALL", t["id"])]
    if targets:
        body = await asyncio.to_thread(lambda: [run_target(t, get_token) for t in targets])
    else:
        body = {"error": f"unknown target {wanted!r}", "known": [t["id"] for t in TARGETS]}
    return TextResponse(context, request, text=json.dumps(body))


app.run()
