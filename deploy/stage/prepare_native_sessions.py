"""First native-session preparation for a profile's seeded assistant Agents (new-environment lesson 2026-10-07).

A seeded Agent (fresh installation + sync job + route + environment follow) has a published Hosted session but no
confirmed preparation, so its first native turn fails with 409 NATIVE_SESSION_CHANGED ("the current reader has no
confirmed preparation"). A submit never prepares implicitly. The Portal "+" onboarding prepares the Agents it creates;
the seeded A/B need this once, after routes/follow and before the template selection and the Portal smoke.

Per Agent: GET .../native-turns/environment (read only), then POST .../prepare/reconcile, which observes and writes
nothing. An Agent that already has a preparation is reported and not prepared again. Otherwise one intent file per
Agent and request ID (open mode "x") is written before the single POST .../prepare. A rerun with an existing intent
only reconciles; a POST whose outcome is unknown is never repeated. No model call is made.

Success is the preparation read back through reconcile: the Agent's own, state=ready, model_started=false,
original_work_replayed=false and the hosted version the environment read named. HTTP 200 alone is not success.

usage: PYTHONPATH=src/fa-shared-context-poc python deploy/stage/prepare_native_sessions.py \
           --out <private dir> --request-id <new id> [--config <profile.json>] [--agent <name> ...] [--dry-run]
The token is the Team owner's (Behavior.Read and Behavior.Approve on the profile's api_audience), so run it with the
owner's login in the project-specific AZURE_CONFIG_DIR.
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "shared-context"))
from deployment_config import config_path, load_config  # noqa: E402

EXPECTED = {"state": "ready", "model_started": False, "original_work_replayed": False}


def _write_x(path, value):
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, indent=1)
        stream.flush()
        os.fsync(stream.fileno())


def _post(client, path, body, headers):
    try:
        response = client.post(path, json=body, headers=headers)
    except httpx.HTTPError as error:
        return None, {"error": {"code": "OPERATOR_RESPONSE_UNKNOWN", "exception_type": type(error).__name__}}
    try:
        value = response.json()
    except ValueError:
        value = {}
    return response.status_code, value if isinstance(value, dict) else {}


def _code(value):
    return (value.get("error") or {}).get("code")


def problems(preparation, agent, environment):
    """What keeps a read-back preparation from counting as this Agent's ready, model-free first preparation."""
    if not isinstance(preparation, dict):
        return ["preparation"]
    found = [key for key, value in {"agent_id": agent["agent_id"], **EXPECTED}.items() if preparation.get(key) != value]
    if preparation.get("hosted_agent_version") != environment.get("hosted_agent_version"):
        found.append("hosted_agent_version")
    return found


def prepare(client, headers, out, agent, request_id, dry_run):
    """(result, preparation); preparation is the read-back value, None when nothing is confirmed."""
    base = f"/agents/{agent['agent_id']}/work-policy/v1/native-turns"
    environment = client.get(base + "/environment", headers=headers).raise_for_status().json()
    command = {"request_id": f"{request_id}-{agent['name']}"}
    result = {"agent": agent["name"], "hosted_session_id": environment.get("hosted_session_id"),
              "hosted_agent_version": environment.get("hosted_agent_version"),
              "environment_state": environment.get("state"), "model_calls": 0}
    intent = out / f"prepare-{agent['name']}-{request_id}-intent.json"
    if intent.exists():
        saved = json.loads(intent.read_text(encoding="utf-8"))["body"]
        if saved != command:
            raise SystemExit(f"{agent['name']}: intent {intent.name} holds another command; reconcile it unchanged")
        result["outcome"] = "intent-exists-reconciled"
    else:
        status, observed = _post(client, base + "/prepare/reconcile", command, headers)
        if status == 200 and isinstance(observed.get("preparation"), dict):
            result["outcome"] = "already-prepared"
            return result, observed["preparation"]
        if status != 409 or _code(observed) != "NATIVE_SESSION_CHANGED":
            raise SystemExit(f"{agent['name']}: cannot tell whether a preparation exists (HTTP {status}, "
                             f"{_code(observed)}); nothing was prepared")
        if dry_run:
            result["outcome"] = "dry-run"
            result["would_prepare"] = command["request_id"]
            return result, None
        _write_x(intent, {"at": datetime.now(timezone.utc).isoformat(), "body": command,
                          "expected_session_id": environment.get("hosted_session_id"),
                          "expected_version": environment.get("hosted_agent_version"),
                          "automatic_retry": False, "model_calls": 0})
        status, value = _post(client, base + "/prepare", command, headers)
        result["outcome"] = f"http_{status}" if status is not None else "unknown:" + value["error"]["exception_type"]
        if status is not None and status != 200:
            result["error_code"] = _code(value)
    status, read = _post(client, base + "/prepare/reconcile", command, headers)
    preparation = read.get("preparation") if status == 200 else None
    if preparation is None:
        result["reconcile"] = {"http": status, "error_code": _code(read)}
    return result, preparation


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--config", help="deployment profile (default: FA_DEPLOY_CONFIG, else deploy/stage/stage.json)")
    parser.add_argument("--agent", action="append", default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    from azure.identity import AzureCliCredential

    profile = load_config(args.config)
    agents = {agent["name"]: agent for agent in profile["agents"]}
    names = args.agent or [agent["name"] for agent in profile["agents"] if agent["kind"] == "assistant"]
    unknown = [name for name in names if agents.get(name, {}).get("kind") != "assistant"]
    if unknown:
        raise SystemExit(f"not assistant Agents of {config_path(args.config)}: {', '.join(unknown)}")
    args.out.mkdir(parents=True, exist_ok=True)
    with AzureCliCredential(subscription=profile["subscription_id"], process_timeout=30) as credential:
        token = credential.get_token(profile["api_audience"] + "/.default").token
    headers = {"Authorization": "Bearer " + token}
    failed = []
    with httpx.Client(base_url=profile["api_url"], timeout=180, trust_env=False, follow_redirects=False) as client:
        for name in names:
            result, preparation = prepare(client, headers, args.out, agents[name], args.request_id, args.dry_run)
            if result["outcome"] != "dry-run":
                environment = {"hosted_agent_version": result["hosted_agent_version"]}
                result["problems"] = problems(preparation, agents[name], environment)
                result["prepared"] = not result["problems"]
                if preparation is not None:
                    result["preparation_request_id"] = preparation.get("request_id")
                if not result["prepared"]:
                    failed.append(name)
            print(json.dumps(result))
    del token
    if failed:
        raise SystemExit(f"not prepared: {', '.join(failed)}; rerun with the same --request-id to reconcile only")


if __name__ == "__main__":
    main()
