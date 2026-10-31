"""Send one probe input (a target id or ALL) to the hosted probe agent and print the results.

--new-session opens a fresh conversation and session; otherwise the conversation and agent_session_id saved in
--state are reused, so the call lands on the same micro-VM while it is within its idle timeout (T6: existing vs new).
"""

import argparse
import json
import os
import sys

import requests
from azure.identity import AzureCliCredential

ap = argparse.ArgumentParser(description=__doc__)
ap.add_argument("--project-endpoint", required=True)
ap.add_argument("--name", default="fa-gw-probe")
ap.add_argument("--input", default="ALL", help="target id or ALL")
group = ap.add_mutually_exclusive_group()
group.add_argument("--new-session", action="store_true")
group.add_argument("--conversation", help="conversation id to reuse (default: the one in --state)")
ap.add_argument("--state", default="invoke.state.json")
a = ap.parse_args()
token = AzureCliCredential(subscription=os.environ["FA_PROBE_SUB"]).get_token("https://ai.azure.com/.default").token
H = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
EP, V = f"{a.project_endpoint.rstrip('/')}/agents/{a.name}/endpoint/protocols/openai", "api-version=v1"

saved = {}
if not a.new_session and os.path.exists(a.state):
    with open(a.state, encoding="utf-8") as f:
        saved = json.load(f)
conversation = a.conversation or saved.get("conversation_id")
if not conversation:
    r = requests.post(f"{EP}/conversations?{V}", headers=H, json={})
    r.raise_for_status()
    conversation = r.json()["id"]

request = {"input": a.input, "store": True, "stream": False, "conversation": conversation}
session = saved.get("agent_session_id") if conversation == saved.get("conversation_id") else None
if session:
    request["agent_session_id"] = session  # Responses binds the session through the body
r = requests.post(f"{EP}/responses?{V}", headers=H, timeout=300, json=request)
body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {"text": r.text[:2000]}
with open(a.state, "w", encoding="utf-8") as f:
    json.dump({"conversation_id": conversation,
               "agent_session_id": body.get("agent_session_id") or session}, f)
texts = [c.get("text") for o in body.get("output", []) if isinstance(o, dict)
         for c in (o.get("content") or []) if isinstance(c, dict) and c.get("text")]
print(json.dumps({"status": r.status_code, "conversation_id": conversation, "response_id": body.get("id"),
                  "sent_agent_session_id": request.get("agent_session_id"),
                  "agent_session_id": body.get("agent_session_id"),
                  "results": [json.loads(t) for t in texts] or body}, indent=2, ensure_ascii=False))
sys.exit(r.status_code >= 300)
