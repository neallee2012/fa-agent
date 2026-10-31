"""Create the hosted probe agent (Responses protocol) with PROBE_TARGETS from a JSON file; polls until active/failed.

Needs FA_PROBE_SUB and an az login to the probe tenant. MOCK_API_KEY is a dummy shared with the mock VM, not a secret.
"""

import argparse
import json
import os
import sys
import time

import requests
from azure.identity import AzureCliCredential


def headers():
    token = AzureCliCredential(subscription=os.environ["FA_PROBE_SUB"]).get_token("https://ai.azure.com/.default").token
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


ap = argparse.ArgumentParser(description=__doc__)
ap.add_argument("--project-endpoint", required=True)
ap.add_argument("--image", required=True)
ap.add_argument("--targets", required=True, help="JSON list of targets, passed as PROBE_TARGETS")
ap.add_argument("--name", default="fa-gw-probe")
ap.add_argument("--idle-seconds", type=int, default=600)  # service range 120..14400
a = ap.parse_args()
P, V, H = a.project_endpoint.rstrip("/"), "api-version=v1", headers()
with open(a.targets, encoding="utf-8") as f:
    targets = json.load(f)

body = {
    "name": a.name,
    "definition": {
        "kind": "hosted",
        "container_configuration": {"image": a.image},
        "cpu": "0.5",
        "memory": "1Gi",
        "protocol_versions": [{"protocol": "responses", "version": "2.0.0"}],
        "environment_variables": {"PYTHONNOUSERSITE": "1", "PROBE_TARGETS": json.dumps(targets),
                                  "MOCK_API_KEY": os.environ.get("MOCK_API_KEY", "mock-dummy-key")},
        "session_configuration": {"idle_timeout_seconds": a.idle_seconds},
    },
}
r = requests.post(f"{P}/agents?{V}", headers=H, json=body)
print(r.status_code, r.text[:2000])
if r.status_code >= 300:
    sys.exit(1)
created = r.json()
# docs: creating an agent also creates version 1; fall back to "1" if the shape differs
version = (created.get("versions", {}).get("latest", {}) or {}).get("version") or created.get("version") or "1"
for _ in range(120):  # 20 min at 10 s
    status = requests.get(f"{P}/agents/{a.name}/versions/{version}?{V}", headers=H).json().get("status")
    print("status:", status, flush=True)
    if status in ("active", "failed"):
        sys.exit(status != "active")
    time.sleep(10)
sys.exit("timed out waiting for the agent version")
