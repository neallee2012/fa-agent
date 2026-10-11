"""Live team-data relay probe; prints only HTTP status codes, never bodies or tokens.

Run after the API is deployed, from any image that ships shared_context:
  python -m shared_context.team_data_probe --api https://<behavior-api> --audience api://<id> \
      --token-env FA_PROBE_TOKEN --mode deny
Modes and expected statuses:
  deny        a non-agent token (operator or API MI): every relay route -> 403
  write       an agent token: create-only probe record -> 201 (409 if this key was already used);
              writing published content -> 404 (no such route); delete -> 405
  read-own    an agent of the SAME team -> 200
  read-other  an agent of ANOTHER team, same key -> 404 (its own namespace has no such record)
The only write is one create-only record under result/probe-<key>; nothing is deleted.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys

from .team_data_relay import PREFIX

_VERSION = "0" * 32
EXPECTED = {
    "deny": {"record_read": 403, "record_create": 403, "object_create": 403, "object_receipt": 403},
    "write": {"record_create": 201, "published_create": 404, "record_delete": 405},
    "read-own": {"record_read": 200},
    "read-other": {"record_read": 404},
}


def probe(client, mode, key, token):
    if mode not in EXPECTED:
        raise ValueError("unknown probe mode")
    auth = {"Authorization": "Bearer " + token}
    record = PREFIX + "/records/result/probe-" + key
    obj = PREFIX + "/objects/result/probe-" + key + "/versions/" + _VERSION
    data = b"fa-team-data-probe"
    steps = {
        "record_read": lambda: client.request("GET", record, headers=auth),
        "record_create": lambda: client.request(
            "PUT", record, headers={**auth, "If-None-Match": "*", "Content-Type": "application/json"},
            content=json.dumps({"data": {"probe": True}}).encode()),
        "published_create": lambda: client.request(
            "PUT", PREFIX + "/records/published/probe-" + key,
            headers={**auth, "If-None-Match": "*", "Content-Type": "application/json"},
            content=json.dumps({"data": {"probe": True}}).encode()),
        "record_delete": lambda: client.request("DELETE", record, headers=auth),
        "object_create": lambda: client.request(
            "PUT", obj, content=data, headers={**auth, "Content-Type": "application/octet-stream",
                                               "x-fa-content-sha256": hashlib.sha256(data).hexdigest()}),
        "object_receipt": lambda: client.request("GET", obj + "/receipt", headers=auth),
    }
    observed = {name: steps[name]().status_code for name in EXPECTED[mode]}
    return observed, observed == EXPECTED[mode]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--api", required=True)
    parser.add_argument("--audience", required=True)
    parser.add_argument("--mode", choices=sorted(EXPECTED), required=True)
    parser.add_argument("--key", default="live-1")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--token-env", help="environment variable holding an already acquired bearer token")
    source.add_argument("--managed-identity", action="store_true", help="use this container's managed identity")
    args = parser.parse_args(argv)
    if args.token_env:
        token = os.environ[args.token_env]
    else:
        from azure.identity import ManagedIdentityCredential
        with ManagedIdentityCredential(client_id=os.environ.get("AZURE_CLIENT_ID") or None) as credential:
            token = credential.get_token(args.audience.rstrip("/") + "/.default").token
    import httpx
    with httpx.Client(base_url=args.api.rstrip("/"), timeout=30, follow_redirects=False, trust_env=False) as client:
        observed, ok = probe(client, args.mode, args.key, token)
    print(json.dumps({"mode": args.mode, "expected": EXPECTED[args.mode], "observed": observed, "pass": ok},
                     separators=(",", ":")))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
