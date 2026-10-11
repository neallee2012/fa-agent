"""Create a profile's toolbox fa-org-tools (ADR-0008, #415 Q9): the dev N5 definition with that profile's connection.

Operator tool, run once with the project-specific AZURE_CONFIG_DIR login. The official prerequisite for the identity
that creates toolbox versions is Foundry User on the project (W1 gave the operator that role). The WebIQ key lives
only in the project connection `<environment>-webiq-key` (deploy/stage/new_webiq_connection.py; stage keeps
`stage-webiq-key`); nothing here reads it. --config selects the profile (default FA_DEPLOY_CONFIG, else stage.json),
so azd environments (deploy/azd) use the same script.

Safety (no blind resend): the create writes an intent file first (open mode "x"); an existing intent or an existing
toolbox means "read back only". A create with an unknown outcome is never posted again. The readback checks the
default version and lists the tools over the MCP endpoint with the runtime's headers (`tools/list` only, no tool call).

usage: python deploy/stage/new_toolbox.py --out <private dir> [--config <profile.json>]   (needs httpx and azure-identity)
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from azure.identity import AzureCliCredential

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "shared-context"))
from deployment_config import load_config  # noqa: E402

STAGE = load_config(require_runtime=False)
NAME = "fa-org-tools"


def definition(profile):
    """(connection, tools, body) of one profile; for stage exactly the original stage-webiq-key definition."""
    connection = f"{profile['environment']}-webiq-key"
    tools = [{"type": "mcp", "server_label": "WebIQ", "server_url": "https://api.microsoft.ai/v3/mcp",
              "require_approval": "never", "project_connection_id": connection, "allowed_tools": ["web", "browse"]}]
    return connection, tools, {"description": f"FA org tools (N5): WebIQ via project connection {connection}",
                               "tools": tools}


CONNECTION, TOOLS, BODY = definition(STAGE)
EXPECTED = {"WebIQ___web", "WebIQ___browse"}
SCOPE = "https://ai.azure.com/.default"
# Same preview feature header as shared_context.native_toolbox (the official BYO toolbox sample).
TOOLBOX_FEATURES = "Toolboxes=V1Preview"


def _versions(client, headers):
    response = client.get(f"{STAGE['project_endpoint']}/toolboxes/{NAME}/versions", params={"api-version": "v1"},
                          headers=headers)
    if response.status_code == 404:
        return []
    return response.raise_for_status().json()["data"]


def _same(version):
    return json.loads(json.dumps(version.get("tools"), sort_keys=True)) == json.loads(json.dumps(TOOLS, sort_keys=True))


def create(client, headers, out):
    existing = _versions(client, headers)
    intent = out / f"toolbox-{NAME}-intent.json"
    if existing or intent.exists():
        if existing and not any(_same(item) for item in existing):
            raise SystemExit(f"{NAME} exists with a different definition; read it back, do not create another version")
        return "read-back-only"
    with intent.open("x", encoding="utf-8") as stream:
        json.dump({"at": datetime.now(timezone.utc).isoformat(), "toolbox": NAME, "body": BODY,
                   "automatic_retry": False}, stream, indent=1)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        response = client.post(f"{STAGE['project_endpoint']}/toolboxes/{NAME}/versions", params={"api-version": "v1"},
                               json=BODY, headers=headers)
        return f"http_{response.status_code}"
    except httpx.HTTPError as error:
        return f"unknown:{type(error).__name__}"


def _rpc(client, url, headers, method, params=None, request_id=None):
    body = {"jsonrpc": "2.0", "method": method, **({"params": params} if params is not None else {}),
            **({"id": request_id} if request_id is not None else {})}
    response = client.post(url, json=body, headers=headers)
    response.raise_for_status()
    if request_id is None:
        return response, None
    text = response.text
    if response.headers.get("content-type", "").startswith("text/event-stream"):
        text = next(line[5:].strip() for line in text.splitlines() if line.startswith("data:"))
    return response, json.loads(text)["result"]


def _list(client, url, token):
    headers = {"Authorization": "Bearer " + token, "Foundry-Features": TOOLBOX_FEATURES,
               "Accept": "application/json, text/event-stream"}
    response, _ = _rpc(client, url, headers, "initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                                             "clientInfo": {"name": "fa-stage-toolbox", "version": "1"}}, 1)
    if response.headers.get("mcp-session-id"):
        headers["mcp-session-id"] = response.headers["mcp-session-id"]
    _rpc(client, url, headers, "notifications/initialized")
    return _rpc(client, url, headers, "tools/list", {}, 2)[1].get("tools") or []


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--config", help="environment profile; defaults to FA_DEPLOY_CONFIG or deploy/stage/stage.json")
    args = parser.parse_args()
    global STAGE, CONNECTION, TOOLS, BODY
    STAGE = load_config(args.config, require_runtime=False)
    CONNECTION, TOOLS, BODY = definition(STAGE)
    args.out.mkdir(parents=True, exist_ok=True)
    with AzureCliCredential(subscription=STAGE["subscription_id"], process_timeout=30) as credential:
        token = credential.get_token(SCOPE).token
    headers = {"Authorization": "Bearer " + token}
    with httpx.Client(timeout=60, trust_env=False, follow_redirects=False) as client:
        outcome = create(client, headers, args.out)
        versions = _versions(client, headers)
        toolbox = client.get(f"{STAGE['project_endpoint']}/toolboxes/{NAME}", params={"api-version": "v1"},
                             headers=headers).raise_for_status().json()
    default = next((item for item in versions if item.get("version") == toolbox.get("default_version")), None)
    if default is None or not _same(default):
        raise SystemExit(f"{NAME}: default version {toolbox.get('default_version')} is not the expected definition")
    url = f"{STAGE['project_endpoint']}/toolboxes/{NAME}/mcp?api-version=v1"
    listed = []
    # A new toolbox can briefly list nothing (official troubleshooting); tools/list is a read, so it may be repeated.
    with httpx.Client(timeout=60, trust_env=False, follow_redirects=False) as client:
        for _ in range(6):
            listed = _list(client, url, token)
            if {tool.get("name") for tool in listed} >= EXPECTED:
                break
            time.sleep(10)
    del token
    names = sorted(tool.get("name") for tool in listed)
    approvals = sorted({((tool.get("_meta") or {}).get("tool_configuration") or {}).get("require_approval")
                        for tool in listed}, key=str)
    result = {"create": outcome, "default_version": toolbox.get("default_version"), "versions": len(versions),
              "connection": default["tools"][0]["project_connection_id"], "tools": names, "require_approval": approvals}
    (args.out / "toolbox-result.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
    print(json.dumps(result))
    if set(names) != EXPECTED or approvals not in (["never"], [None]):
        raise SystemExit("tools/list differs from the expected WebIQ web/browse with require_approval never")


if __name__ == "__main__":
    main()
