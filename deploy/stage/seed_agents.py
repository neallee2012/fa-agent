"""Create the first (seed) Hosted version of each environment Agent, then add the endpoint protocols (ADR-0008, #415 W8).

Operator tool, run once from a workstation with the project-specific AZURE_CONFIG_DIR login (Foundry User on the
stage project). Later builds get their versions from `behavior_agent_versions sync`, which copies the newest
active version and replaces only the image, so the environment here is what every later build inherits.

Safety (no blind resend): every create/PATCH writes an intent file first (open mode "x"); an existing intent means
"read back only". A create with an unknown outcome is never posted again; the version is read back by fa_build.

usage: python deploy/stage/seed_agents.py --out <dir> --image <registry/repo@sha256:...> --core <code_sha256()>

The code identity is printed by the matching API/Hosted image; it is not the container manifest digest.
"""
import argparse
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from azure.ai.projects import AIProjectClient
from azure.core.exceptions import AzureError
from azure.identity import AzureCliCredential

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "shared-context"))
from deployment_config import load_config

sys.path.insert(0, str(HERE.parents[1] / "src" / "fa-shared-context-poc"))
from shared_context.behavior_agent_versions import METADATA_DEFINITION, definition_matches, definition_sha256

STAGE = load_config(require_runtime=False)
PROTOCOLS = [{"protocol": name, "version": "2.0.0"} for name in ("invocations", "invocations_ws", "responses")]
ENDPOINT_KEYS = ("invocations", "invocations_ws")


def _env(kind):
    base = {
        "PYTHONUTF8": "1",
        "BEHAVIOR_HOSTED_ENABLED": "true",
        "BEHAVIOR_API_URL": STAGE["api_url"],
        "BEHAVIOR_API_AUDIENCE": STAGE["api_audience"],
    }
    if kind == "executor":
        return base
    return {**base, **STAGE["assistant_environment"]}


def definition(agent, image):
    return {
        "kind": "hosted",
        "container_configuration": {"image": image},
        "cpu": "2",
        "memory": "4Gi",
        "environment_variables": {**_env(agent["kind"]), **agent.get("hosted_environment", {})},
        "protocol_versions": PROTOCOLS,
        "session_configuration": {"idle_timeout_seconds": STAGE["hosted_idle_timeout_seconds"]},
    }


def _now():
    return datetime.now(timezone.utc).isoformat()


def _write_x(path, value):
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, indent=1)
        stream.flush()
        os.fsync(stream.fileno())


def _of_definition(project, name, core, expected):
    try:
        items = [item.as_dict() for item in project.agents.list_versions(name, limit=20, order="desc")]
    except AzureError as error:
        if getattr(error, "status_code", None) == 404:
            return []
        raise SystemExit(f"{name}: version lookup {type(error).__name__}; no create was sent") from None
    return [item for item in items if (item.get("metadata") or {}).get("fa_build") == core
            and isinstance(item.get("definition"), dict) and definition_matches(item["definition"], expected)]


def _check_core(core, image):
    if re.fullmatch(r"[0-9a-f]{64}", core) is None or core == image.rpartition("@sha256:")[2].lower():
        raise SystemExit("--core must be a 64-hex code identity from the matching API/Hosted image's code_sha256(), "
                         "not its image digest")


def seed(project, out, agent, image, core):
    _check_core(core, image)
    name = agent["name"]
    intent = out / f"seed-{name}-intent.json"
    desired = definition(agent, image)
    expected = definition_sha256(desired)
    body = {"definition": desired, "metadata": {
                "fa_build": core, "fa_source": "stage-seed", METADATA_DEFINITION: expected},
            "description": agent["description"]}
    body_hash = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
    if intent.exists():
        saved = json.loads(intent.read_text(encoding="utf-8"))
        legacy = {**body, "metadata": {"fa_build": core, "fa_source": "stage-seed"}}
        legacy_hash = hashlib.sha256(json.dumps(legacy, sort_keys=True).encode()).hexdigest()
        if saved.get("body_sha256") not in {body_hash, legacy_hash}:
            raise SystemExit(f"{name}: original seed intent differs; do not replace it or repeat the create")
    if not intent.exists():
        if _of_definition(project, name, core, expected):
            raise SystemExit(f"{name}: this exact definition already exists; read it back without another create")
        _write_x(intent, {"at": _now(), "agent": name, "core": core, "image": image, "automatic_retry": False,
                          "definition_sha256": expected, "body_sha256": body_hash})
        try:
            project.agents.create_version(name, body)
        except AzureError as error:
            print(json.dumps({"agent": name, "create": "unknown", "error": type(error).__name__}), flush=True)
    for _ in range(120):
        found = _of_definition(project, name, core, expected)
        if found:
            version = min(found, key=lambda item: int(item["version"]))
            # The listed status can turn active minutes before GET does; only GET decides (behavior_agent_versions._ready).
            observed = project.agents.get_version(name, version["version"]).as_dict()
            if observed.get("status") in {"active", "failed"}:
                if (not isinstance(observed.get("definition"), dict)
                        or not definition_matches(observed["definition"], expected)):
                    raise SystemExit(f"{name}: the exact seed definition did not read back")
                return observed
        time.sleep(5)
    raise SystemExit(f"{name}: version not settled; read back later, do not create again")


def patch_endpoint(client, headers, out, name):
    url = f"{STAGE['project_endpoint']}/agents/{name}"
    current = (client.get(url, params={"api-version": "v1"}, headers=headers).raise_for_status().json()
               .get("agent_endpoint") or {}).get("protocol_configuration") or {}
    if all(key in current for key in ENDPOINT_KEYS):
        return {"agent": name, "endpoint": "already", "protocol_configuration": sorted(current)}
    intent = out / f"endpoint-{name}-intent.json"
    if intent.exists():
        return {"agent": name, "endpoint": "intent-exists-read-only", "protocol_configuration": sorted(current)}
    new = {**current, **{key: current.get(key, {}) for key in ENDPOINT_KEYS}}
    body = {"agent_endpoint": {"protocol_configuration": new}}
    _write_x(intent, {"at": _now(), "agent": name, "body": body, "automatic_retry": False})
    try:
        response = client.patch(url, params={"api-version": "v1"}, content=json.dumps(body),
                                headers={**headers, "Content-Type": "application/merge-patch+json"})
        status = response.status_code
    except httpx.HTTPError as error:
        status = f"unknown:{type(error).__name__}"
    after = client.get(url, params={"api-version": "v1"}, headers=headers)
    configuration = (after.json().get("agent_endpoint") or {}).get("protocol_configuration") if after.status_code == 200 else None
    return {"agent": name, "endpoint_patch": status, "protocol_configuration": sorted(configuration or {})}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", help="environment profile; defaults to FA_DEPLOY_CONFIG or the checked-in stage profile")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--core", required=True, help="64-hex code_sha256() printed by the matching API/Hosted image")
    args = parser.parse_args()
    global STAGE
    STAGE = load_config(args.config, require_runtime=False)
    _check_core(args.core, args.image)
    if not args.image.startswith(STAGE["registry"] + "/fa-behavior-assistant-desktop@sha256:"):
        raise SystemExit("image must be the selected registry's desktop image by digest")
    args.out.mkdir(parents=True, exist_ok=True)
    results = []
    with AzureCliCredential(subscription=STAGE["subscription_id"], process_timeout=30) as credential:
        with AIProjectClient(endpoint=STAGE["project_endpoint"], credential=credential, allow_preview=True,
                             retry_total=0, logging_enable=False) as project:
            for agent in STAGE["agents"]:
                version = seed(project, args.out, agent, args.image, args.core)
                results.append({"agent": agent["name"], "version": version["version"], "status": version["status"],
                                "agent_guid": version.get("agent_guid"),
                                "principal_id": (version.get("instance_identity") or {}).get("principal_id")})
        token = credential.get_token("https://ai.azure.com/.default").token
        with httpx.Client(timeout=60, trust_env=False, follow_redirects=False) as client:
            headers = {"Authorization": "Bearer " + token}
            for agent in STAGE["agents"]:
                results.append(patch_endpoint(client, headers, args.out, agent["name"]))
        del token
    (args.out / "seed-result.json").write_text(json.dumps(results, indent=1), encoding="utf-8")
    for result in results:
        print(json.dumps(result))


if __name__ == "__main__":
    main()
