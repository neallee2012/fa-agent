"""Teams relay (docs/teams-chat-design.md, N2): one Teams message in, the FA Agent's reply text out.

Published to Teams through Foundry. It never calls a model, stores nothing and never calls a Teams API: it forwards
the message to the FA API with its own managed identity and polls the same body until the API has an answer. In a
one-to-one chat it tells the API it acknowledges replies (``X-FA-Relay-Ack: 1``); an API that then adds earlier replies the relay had stopped
waiting for lists them in ``confirm``, and the relay acknowledges them before handing the text on.
"""

import asyncio
import os
import re
import time
from urllib.parse import urlsplit

import httpx
from azure.ai.agentserver.responses import CreateResponse, ResponseContext, ResponsesAgentServerHost, TextResponse

PATH = "/teams-relay/v1/turns"
DECISIONS_PATH = "/teams-relay/v1/decisions"
ACKS_PATH = "/teams-relay/v1/acks"
# A header, not a body field: the API's bodies are strict (an older API would refuse a new field) and it ignores headers
# it does not know, so either deploy order works. Only an API that sees it records late replies for this relay to ack.
ACK_HEADER = "X-FA-Relay-Ack"
# Catch-up is per owner across conversations, so only a one-to-one chat sends the header. Teams personal conversation
# IDs start with "a:" (Microsoft's sample activities); group chats and channels use "19:...@thread...". Anything else,
# or no ID at all, gets no header: the API then answers exactly as before catch-up.
PERSONAL_PREFIX = "a:"
# One short try: a lost acknowledgement only means the API offers those replies again later (a rare repeat, never a loss).
ACK_SECONDS = 5
# The fixed approval command; anything else (even 「允許他吧」) is a normal message for the model.
COMMAND = re.compile(r"(允許|拒絕)\s+([0-9]{6})")
CHOICE = {"允許": "allow_once", "拒絕": "deny"}
POLL_SECONDS = 3
# Teams delivered a 120 s reply in the 10-10 probe; stop a little before that.
WAIT_SECONDS = 110
FINAL = frozenset({"done", "needs_approval", "busy", "refused", "failed"})
# Texts for answers the API never gave; every other reply is the API's own text. At the wait bound that is its last
# running text: only an API that records late replies for this relay says they come with the next message.
UNCONFIRMED = "暫時無法確認助理有沒有收到；請先到工作台查看，再決定要不要重傳。"
TEXT_ONLY = "v1 只收文字。"
NOT_OWNER = "這個助理只回應它的主人。"


class FaApi:
    def __init__(self, api_url, audience, credential, *, transport=None):
        address = urlsplit(api_url)
        if address.scheme != "https" or not address.hostname or address.path not in {"", "/"} or address.query:
            raise ValueError("BEHAVIOR_API_URL must be the HTTPS origin of the FA API")
        self.origin, self.scope, self.credential = api_url.rstrip("/"), audience + "/.default", credential
        self.http = httpx.AsyncClient(transport=transport, follow_redirects=False, trust_env=False)

    async def post(self, turn, timeout, path=PATH, *, ack):
        token = (await self.credential.get_token(self.scope)).token
        return await self.http.post(self.origin + path, json=turn, timeout=timeout, headers={
            "Authorization": "Bearer " + token, **({ACK_HEADER: "1"} if ack else {})})


def personal(request):
    """True only for a Teams one-to-one chat (see ``PERSONAL_PREFIX``); fails closed."""
    conversation = (request.get("metadata") or {}).get("ActivityProtocolConversationId")
    return isinstance(conversation, str) and conversation.startswith(PERSONAL_PREFIX)


async def relay(api, turn, *, path=PATH, ack=False, clock=time.monotonic, sleep=asyncio.sleep):
    """Poll one RelayTurn (or RelayDecision at its path) until the API answers with a final state or the wait bound passes.

    ``ack``: this chat takes earlier replies (a one-to-one chat), so the requests say the relay acknowledges them."""
    deadline, running = clock() + WAIT_SECONDS, None
    while True:
        try:
            response = await api.post(turn, timeout=max(1.0, deadline - clock()), path=path, ack=ack)
            if response.status_code == 200:
                reply = response.json()
                if reply["state"] in FINAL:
                    text = reply["text"]
                    if reply.get("confirm"):
                        await acknowledge(api, turn, reply["confirm"])
                    return text
                if reply.get("not_dispatched") is not True:  # a poll that took nothing confirms nothing
                    running = reply["text"]
            elif 400 <= response.status_code < 500:
                # Refused before any turn (bad identity, conflict, invalid message); sending it again cannot help.
                return UNCONFIRMED
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            pass
        if clock() + POLL_SECONDS >= deadline:
            # No 2xx ever confirmed receipt: the reply may have been lost, so the owner checks the workbench before resending.
            return UNCONFIRMED if running is None else running
        await sleep(POLL_SECONDS)


async def acknowledge(api, turn, request_ids):
    """Tell the API this relay holds the replies of ``request_ids``; every failure is ignored and they stay recorded."""
    try:
        await api.post({"chat_id": turn["chat_id"], "user_oid": turn["user_oid"], "request_ids": request_ids},
                       timeout=ACK_SECONDS, path=ACKS_PATH, ack=True)
    except Exception:  # noqa: BLE001 - token or network: the text still goes to Teams, the records stay
        pass


async def reply_text(api, request, context, **timing):
    user_oid = getattr(context.platform_context, "user_id_key", None)
    if not user_oid:
        return NOT_OWNER
    items = await context.get_input_items()
    parts = [part for item in items if item.get("type") == "message" for part in item.get("content") or ()]
    if (not parts or any(item.get("type") != "message" for item in items)
            or any(part.get("type") != "input_text" for part in parts)):
        return TEXT_ONLY
    text = await context.get_input_text()
    if not text.strip():
        return TEXT_ONLY
    turn = {"activity_id": (request.get("metadata") or {}).get("ActivityId"), "chat_id": context.conversation_id,
            "user_oid": user_oid}
    command, ack = COMMAND.fullmatch(text.strip()), personal(request)
    if command is not None:
        # An approval command never reaches the model: it goes to the decisions route, which polls like a turn.
        return await relay(api, {**turn, "choice": CHOICE[command[1]], "code": command[2]},
                           path=DECISIONS_PATH, ack=ack, **timing)
    return await relay(api, {**turn, "text": text}, ack=ack, **timing)


def build_app(api, **timing):
    app = ResponsesAgentServerHost()

    @app.response_handler
    async def handler(request: CreateResponse, context: ResponseContext, cancellation_signal: asyncio.Event):
        return TextResponse(context, request, text=await reply_text(api, request, context, **timing))

    return app


def main():
    from azure.identity.aio import ManagedIdentityCredential

    credential = ManagedIdentityCredential(client_id=os.environ.get("AZURE_CLIENT_ID") or None)
    build_app(FaApi(os.environ["BEHAVIOR_API_URL"], os.environ["BEHAVIOR_API_AUDIENCE"], credential)).run()


if __name__ == "__main__":
    main()
