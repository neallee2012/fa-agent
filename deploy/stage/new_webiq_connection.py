"""Create a profile's WebIQ project connection with a key typed in a hidden prompt (ADR-0008, #415 Q9).

Replaces new-webiq-connection.ps1 so stage and azd environments (deploy/azd) share one script on any OS.
The key is read with getpass (no echo), kept only in memory, sent once in the ARM PUT body over TLS and never printed,
logged, written to a file, put on a command line or into the azd environment. Requests go through httpx with a token
from the Azure CLI login, not through `az rest`, because `az rest` would need the body in a file.
By default an existing connection is only read back: a key is never asked for or sent twice (azd relies on this).
`--rotate` is the way to replace an expired or leaked key: it first reads the connection (read-only) and stops if it is
absent (first creation goes through the normal path; a wrong --config cannot create one) or its target is not WebIQ's
(it never overwrites another connection), then asks for the key (a
terminal is required), sends
one PUT to the same connection and needs the read-back to be HTTP 200. A PUT of the same connection is idempotent, so
after an unknown outcome (timeout, no read-back) running `--rotate` again is safe.
Connection names are unique across the whole Foundry account, so each environment uses `<environment>-webiq-key`
(stage keeps `stage-webiq-key`) unless the profile names it in `webiq_connection` (dev: `webiq-key`, created by #412
before the naming rule).
Creating is only for a project whose toolbox fa-org-tools does not use another connection yet: if it already does
(a wrong or missing `webiq_connection`), the script stops before asking for a key instead of adding a second copy
(2026-10-22). This is the only WebIQ key: the API and the VM never see it (WebIQ-MCP was retired the same day).

Set the project-specific AZURE_CONFIG_DIR in the same shell first (the replaced .ps1 set it itself).

usage: python deploy/stage/new_webiq_connection.py [--config <profile.json>] [--rotate]
       (needs httpx and azure-identity; --rotate replaces the key of an existing connection and stops if it is absent)
"""
import argparse
import getpass
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "shared-context"))
from deployment_config import load_config  # noqa: E402

API_VERSION = "2025-06-01"
TARGET = "https://api.microsoft.ai/v3/mcp"
ARM_SCOPE = "https://management.azure.com/.default"
DATA_SCOPE = "https://ai.azure.com/.default"
TOOLBOX = "fa-org-tools"


def connection_name(profile):
    name = profile.get("webiq_connection") or f"{profile['environment']}-webiq-key"
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,62}", name):
        raise SystemExit(f"webiq_connection {name!r} is not a connection name")
    return name


def toolbox_connections(client, headers, profile):
    """Connections that any version of the project's toolbox fa-org-tools uses; empty when it does not exist."""
    # shortcut: reads only the first page of versions; follow the paging link once fa-org-tools has more than one page.
    response = client.get(f"{profile['project_endpoint']}/toolboxes/{TOOLBOX}/versions?api-version=v1", headers=headers)
    if response.status_code == 404:
        return set()
    if response.status_code != 200:
        raise SystemExit(f"cannot read toolbox {TOOLBOX} (HTTP {response.status_code}); nothing was sent")
    return {tool["project_connection_id"] for version in response.json().get("data") or []
            for tool in version.get("tools") or [] if tool.get("project_connection_id")}


def connection_url(profile, name):
    group = profile.get("foundry_resource_group") or profile["resource_group"]
    return (f"https://management.azure.com/subscriptions/{profile['subscription_id']}/resourceGroups/{group}"
            f"/providers/Microsoft.CognitiveServices/accounts/{profile['foundry_account']}/projects/{profile['project']}"
            f"/connections/{name}?api-version={API_VERSION}")


def body(profile, key):
    return {"properties": {
        "category": "RemoteTool", "authType": "CustomKeys", "target": TARGET, "isSharedToAll": False,
        "credentials": {"keys": {"x-apikey": key}},
        "metadata": {"fa_purpose": f"{profile['environment']} toolbox fa-org-tools WebIQ", "fa_adr": "0008"},
    }}


def readback(value):
    properties = (value or {}).get("properties") or {}
    return {"name": (value or {}).get("name"), "category": properties.get("category"),
            "authType": properties.get("authType"), "target": properties.get("target")}


def ensure(client, headers, profile, ask, rotate=False, toolbox=None):
    """Read back; only when absent ask for the key (ask() is the hidden prompt) and PUT it once.

    rotate=True needs the connection to exist already (read-only GET first, so a wrong --config cannot create one in
    another project) and to be the WebIQ one (target and category; never overwrite another connection), then asks
    and PUTs (the same connection, so a rerun is safe) and returns "rotated" on a 200 read-back.
    Creating needs toolbox(), the connections fa-org-tools already uses: one other than this name means the profile
    points at the wrong connection, so nothing is asked or sent (never a second copy of the key).
    """
    name = connection_name(profile)
    url = connection_url(profile, name)
    where = profile.get("project_endpoint") or f"{profile['foundry_account']}/{profile['project']}"
    current = client.get(url, headers=headers)
    if current.status_code == 200 and not rotate:
        return "already", readback(current.json())
    if current.status_code == 404 and rotate:
        raise SystemExit(f"--rotate: connection {name} does not exist in {where}; nothing was written. Check --config "
                         "and the profile's webiq_connection; create it the first time without --rotate")
    if current.status_code not in (200, 404):
        raise SystemExit(f"cannot read the connection (HTTP {current.status_code}); nothing was sent")
    if rotate:
        found = readback(current.json())
        if (found["target"], found["category"]) != (TARGET, "RemoteTool"):
            raise SystemExit(f"--rotate: connection {name} in {where} is not the WebIQ connection (target "
                             f"{found['target']!r}, category {found['category']!r}); nothing was written")
    if current.status_code == 404:
        if toolbox is None:
            raise SystemExit("creating a connection needs the toolbox read-back; nothing was sent")
        other = sorted(toolbox() - {name})
        if other:
            raise SystemExit(f"toolbox {TOOLBOX} in {where} already uses connection {', '.join(other)}, not {name}; "
                             "nothing was written. Set the profile's webiq_connection to it and replace its key "
                             "with --rotate")
    # Shown before the hidden prompt, so someone who also has stage rights notices a missing or wrong --config.
    print(f"WebIQ connection {name} in {where}", flush=True)
    key = ask()
    if not key:
        raise SystemExit("empty key; nothing sent")
    try:
        response = client.put(url, headers={**headers, "Content-Type": "application/json"},
                              content=json.dumps(body(profile, key)))
    finally:
        key = None
    if response.status_code not in (200, 201):
        hint = "running --rotate again is safe" if rotate else "read it back before any retry"
        raise SystemExit(f"connection PUT returned HTTP {response.status_code}; {hint}")
    after = client.get(url, headers=headers)
    if after.status_code != 200:
        again = ("run --rotate again (a PUT of the same connection is idempotent)" if rotate
                 else "read it again, do not send the key again")
        raise SystemExit(f"the connection did not read back (HTTP {after.status_code}); {again}")
    return ("rotated" if rotate else "created"), readback(after.json())


def ask_key():
    """Only when a key must be sent (absent connection or --rotate): a read-back needs no terminal (non-interactive upgrades)."""
    if not sys.stdin.isatty():
        raise SystemExit("the WebIQ key is typed in a hidden prompt; run this once in an interactive terminal")
    return getpass.getpass("WebIQ API key (hidden): ")


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", help="environment profile; defaults to FA_DEPLOY_CONFIG or deploy/stage/stage.json")
    parser.add_argument("--rotate", action="store_true",
                        help="replace the key of the existing connection (always asks; needs a terminal)")
    args = parser.parse_args(argv)
    if args.rotate and not sys.stdin.isatty():
        raise SystemExit("--rotate types the new key in a hidden prompt; run it in an interactive terminal")
    import httpx
    from azure.identity import AzureCliCredential

    profile = load_config(args.config, require_runtime=False)
    for key in ("foundry_account", "project"):
        if not profile.get(key):
            raise SystemExit(f"the profile has no {key}")
    with AzureCliCredential(subscription=profile["subscription_id"], process_timeout=30) as credential, \
            httpx.Client(timeout=60, trust_env=False, follow_redirects=False) as client:
        headers = {"Authorization": "Bearer " + credential.get_token(ARM_SCOPE).token}
        # The data-plane token only when creating: a read-back or --rotate never reads the toolbox.
        outcome, value = ensure(client, headers, profile, ask_key, rotate=args.rotate, toolbox=lambda: toolbox_connections(
            client, {"Authorization": "Bearer " + credential.get_token(DATA_SCOPE).token}, profile))
    del headers
    print(json.dumps({"connection": outcome, **value}))
    if outcome == "already":
        print("The connection exists and was only read back; to replace its key run this again with --rotate.")
    print("The key was not stored anywhere on this machine.")


if __name__ == "__main__":
    main()
