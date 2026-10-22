"""Run one fresh-initialization step inside an environment VNet as a Container Apps Job execution (ADR-0008, #415 W9).

The job uses the selected environment's API image and API managed identity (the only Table writer). The operator files are embedded
in the start override and checked by sha256 inside the container; only one-line results are printed, never the
files themselves. Every start writes an intent file first (open mode "x"); an existing intent means "read back the
original execution", never a second start. A start or apply with an unknown outcome is followed only by reconcile.

usage (operator workstation, AZURE_CONFIG_DIR set):
  python deploy/stage/run_fresh_job.py --work <private dir> plan --image <api image@digest>
  python deploy/stage/run_fresh_job.py --work <private dir> apply|reconcile --image <api image@digest>
  python deploy/stage/run_fresh_job.py --work <private dir> logs --execution <name>
"""
import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import zlib
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "shared-context"))
from deployment_config import config_path, load_config

STAGE = load_config()
RUNTIME = HERE / "runtime"
API = "api-version=2025-01-01"
REQUEST_ID, INSTALLATION_ID = "stage-fresh-001", "stage-native-v1"


def _az_command():
    # Windows `az` is a .cmd shim; cmd.exe would reinterpret `|` and quotes in KQL, so call the CLI module directly.
    if os.name == "nt":
        cli = os.environ.get("AZ_PYTHON", r"C:\Program Files\Microsoft SDKs\Azure\CLI2\python.exe")
        return [cli, "-IBm", "azure.cli"]
    return ["az"]


def az(*args):
    result = subprocess.run([*_az_command(), *args, "-o", "json"], check=False, text=True, capture_output=True)
    if result.returncode != 0:
        raise SystemExit("az failed: " + (result.stderr or "")[-1500:])
    return json.loads(result.stdout) if result.stdout.strip() else None


def rest(method, path, body=None, work=None):
    args = ["rest", "--method", method, "--url", f"https://management.azure.com{path}"]
    if body is not None:
        body_file = work / f".body-{os.getpid()}-{time.monotonic_ns()}.json"
        body_file.write_text(json.dumps(body), encoding="utf-8")
        args += ["--body", f"@{body_file}", "--headers", "Content-Type=application/json"]
    try:
        return az(*args)
    finally:
        if body is not None:
            body_file.unlink(missing_ok=True)


def job_path():
    return (f"/subscriptions/{STAGE['subscription_id']}/resourceGroups/{STAGE['resource_group']}"
            f"/providers/Microsoft.App/jobs/{STAGE['sync_job']}")


def script(action, files, hosted_image=None):
    blob = base64.b64encode(zlib.compress(json.dumps(
        {name: base64.b64encode(data).decode() for name, data in files.items()}).encode(), 9)).decode()
    hashes = {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}
    module = "shared_context.behavior_fresh_operator"
    if action == "plan":
        argv = ["plan", "--config", "/tmp/fa-w9/config.json", "--inputs", "/tmp/fa-w9/fresh-inputs.json",
                "--request-id", REQUEST_ID, "--installation-id", INSTALLATION_ID, "--output", "/tmp/fa-w9/plan.json"]
        after = ("import json\n"
                 "p=json.load(open('/tmp/fa-w9/plan.json',encoding='utf-8'))\n"
                 "print('FA_W9_PLAN_SCALARS '+json.dumps({k:p[k] for k in ('empty_table_observed_at','code_sha256','job_input_sha256')}),flush=True)\n")
    elif action == "sync":
        module = "shared_context.behavior_agent_versions"
        # No --native-v2-registered (ci_deploy._sync_script): fresh initialization starts from an empty table
        # (plan's empty_table_observed_at), so no onboarded (registered) Agent exists yet for it to match.
        argv = ["sync", "--config", "/tmp/fa-w9/config.json", "--image", hosted_image, "--route", "--defer-follow",
                "--idle-timeout-seconds", str(STAGE["hosted_idle_timeout_seconds"])]
        after = ""
    else:
        argv = [action, "--config", "/tmp/fa-w9/config.json", "--plan", "/tmp/fa-w9/plan.json",
                "--authorization", "/tmp/fa-w9/authorization.json"]
        after = ""
    return "\n".join([
        "import base64,hashlib,json,sys,zlib",
        "from pathlib import Path",
        f"files=json.loads(zlib.decompress(base64.b64decode({blob!r})))",
        f"hashes={hashes!r}",
        "root=Path('/tmp/fa-w9'); root.mkdir(mode=0o700)",
        "for name,text in files.items():",
        " raw=base64.b64decode(text,validate=True)",
        " assert hashlib.sha256(raw).hexdigest()==hashes[name]",
        " (root/name).write_bytes(raw)",
        "from shared_context.behavior_code_identity import code_sha256",
        "print('FA_W9_CORE '+code_sha256(),flush=True)",
        f"from {module} import main",
        f"rc=main({argv!r})",
        "sys.stdout.flush(); sys.stderr.flush()",
        *(["if rc==0:", *[" " + line for line in after.splitlines()]] if after else []),
        "print('FA_W9_EXIT '+str(rc),flush=True)",
        "raise SystemExit(rc)",
    ])


def start(work, action, image, files, hosted_image=None):
    label = action if hosted_image is None else f"{action}-{hosted_image.rsplit(':', 1)[-1][:12]}"
    intent = work / f"{label}-intent.json"
    if intent.exists():
        raise SystemExit(f"{intent.name} exists: read back the original execution with 'logs'; never start twice")
    job = rest("GET", f"{job_path()}?{API}")
    container = job["properties"]["template"]["containers"][0]
    with intent.open("x", encoding="utf-8") as stream:
        json.dump({"at": datetime.now(timezone.utc).isoformat(), "action": action, "image": image,
                   "hosted_image": hosted_image,
                   "files_sha256": {k: hashlib.sha256(v).hexdigest() for k, v in files.items()},
                   "automatic_retry": False}, stream, indent=1)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        started = rest("POST", f"{job_path()}/start?{API}", {"containers": [{
            "name": container["name"], "image": image, "command": ["python"],
            "args": ["-B", "-c", script(action, files, hosted_image)], "env": container.get("env") or [],
            "resources": container["resources"]}]}, work)
    except SystemExit as error:
        (work / f"{label}-start-unknown.txt").write_text(str(error)[:2000], encoding="utf-8")
        raise SystemExit("start outcome unknown: list executions and read logs; do not start again") from None
    name = started["name"]
    (work / f"{label}-execution.txt").write_text(name, encoding="utf-8")
    print(json.dumps({"action": action, "execution": name}))
    for _ in range(120):
        status = rest("GET", f"{job_path()}/executions/{name}?{API}")["properties"]["status"]
        if status not in {"Running", "Processing"}:
            print(json.dumps({"action": action, "execution": name, "status": status}))
            return name
        time.sleep(10)
    print(json.dumps({"action": action, "execution": name, "status": "still running"}))
    return name


def logs(work, execution):
    workspace = az("monitor", "log-analytics", "workspace", "show", "--subscription", STAGE["subscription_id"],
                   "-g", STAGE["resource_group"],
                   "-n", STAGE.get("log_analytics_workspace", f"log-fa-{STAGE['environment']}"))["customerId"]
    # Resource-specific table written by the CAE's Azure Monitor diagnostic setting (not the legacy *_CL table).
    query = ("ContainerAppConsoleLogs | where ContainerGroupName startswith '%s' or JobName startswith '%s' "
             "| where Log has 'FA_W9_' or Log startswith '{' | project TimeGenerated, Log | order by TimeGenerated asc"
             % (execution, execution))
    body_file = work / f".query-{os.getpid()}.json"
    body_file.write_text(json.dumps({"query": query, "timespan": "PT6H"}), encoding="utf-8")
    print("waiting for the job log in Log Analytics (a new workspace can take 5-15 minutes the first time)", flush=True)
    began = shown = time.monotonic()
    try:
        for _ in range(40):
            if time.monotonic() - shown >= 60:
                shown = time.monotonic()
                print(f"still waiting for the job log ({int(shown - began) // 60} min so far)", flush=True)
            try:
                result = az("rest", "--method", "post", "--resource", "https://api.loganalytics.io",
                            "--url", f"https://api.loganalytics.io/v1/workspaces/{workspace}/query",
                            "--body", f"@{body_file}", "--headers", "Content-Type=application/json")
            except SystemExit as error:
                if "SEM0100" not in str(error):  # the table appears only after the first ingestion
                    raise
                time.sleep(30)
                continue
            table = result["tables"][0]
            index = [column["name"] for column in table["columns"]].index("Log")
            lines = [row[index] for row in table["rows"]]
            if any("FA_W9_EXIT" in line for line in lines):
                (work / f"{execution}-log.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
                for line in lines:
                    print(line)
                return
            time.sleep(30)
    finally:
        body_file.unlink(missing_ok=True)
    raise SystemExit("logs not ingested yet; run 'logs' again later")


def plan_from_log(lines):
    """(scalars, planned) of one plan execution's saved log: the FA_W9_PLAN_SCALARS line and the operator's one-line
    `state=planned` summary. Both must be present exactly once and the execution must have exited 0."""
    scalars, planned = [], []
    for line in lines:
        if line.startswith("FA_W9_PLAN_SCALARS "):
            scalars.append(json.loads(line.split(" ", 1)[1]))
        elif line.startswith("{"):
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict) and value.get("state") == "planned":
                planned.append(value)
    if "FA_W9_EXIT 0" not in lines or len(scalars) != 1 or len(planned) != 1:
        raise SystemExit("the plan log has no single successful plan; read the execution again, do not start another")
    return scalars[0], planned[0]


def rebuild_plan(runtime_dir, scalars, planned):
    """The exact plan the Job computed, from the same config/inputs plus its three observed scalars (the Job prints
    only these, never the plan). Its digest must equal the Job's plan_sha256, or nothing may be authorized.
    Needs the API packages (PYTHONPATH=src/fa-shared-context-poc)."""
    from shared_context.behavior_fresh_installation import (
        FreshInstallationInputs, FreshInstallationPlan, _settings,
    )
    from shared_context.behavior_server_config import load_configuration, read_document
    from shared_context.source_materials import digest

    config = load_configuration(Path(runtime_dir) / "config.json")
    inputs = read_document(Path(runtime_dir) / "fresh-inputs.json", FreshInstallationInputs)
    # Validated from JSON like every operator file: the models are strict, the logged time is an ISO string.
    plan = FreshInstallationPlan.model_validate_json(json.dumps({
        "request_id": planned["request_id"], "installation_id": planned["installation_id"],
        "configuration_sha256": config.sha256, "code_sha256": scalars["code_sha256"],
        "inputs": inputs.model_dump(mode="json"), "input_sha256": digest(inputs.model_dump(mode="json")),
        "peer_policy": _settings(config, inputs).model_dump(mode="json"),
        "job_input_sha256": scalars["job_input_sha256"], "empty_table_observed_at": scalars["empty_table_observed_at"],
    }))
    if (plan.sha256 != planned["plan_sha256"] or config.sha256 != planned["configuration_sha256"]
            or plan.input_sha256 != planned["input_sha256"]):
        raise SystemExit("the rebuilt plan differs from the Job's plan; do not authorize it")
    return plan


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", help="environment profile; defaults to FA_DEPLOY_CONFIG or the checked-in stage profile")
    parser.add_argument("--runtime-dir", type=Path,
                        help="generated runtime directory; defaults to <config>/runtime")
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("action", choices=("plan", "apply", "reconcile", "sync", "logs"))
    parser.add_argument("--image")
    parser.add_argument("--hosted-image", help="sync only: the same build's Hosted desktop image by digest")
    parser.add_argument("--execution")
    args = parser.parse_args()
    global REQUEST_ID, INSTALLATION_ID, RUNTIME, STAGE
    STAGE = load_config(args.config)
    token = re.sub(r"[^a-z0-9-]+", "-", STAGE["environment"].lower()).strip("-") or "environment"
    REQUEST_ID, INSTALLATION_ID = f"{token}-fresh-001", f"{token}-native-v1"
    RUNTIME = args.runtime_dir or config_path(args.config).parent / "runtime"
    work = args.work
    work.mkdir(parents=True, exist_ok=True)
    if args.action == "logs":
        return logs(work, args.execution)
    if not (args.image or "").startswith(STAGE["registry"] + "/fa-behavior-api@sha256:"):
        raise SystemExit("--image must be the selected environment's API image by digest")
    files = {"config.json": (RUNTIME / "config.json").read_bytes()}
    if args.action == "plan":
        files["fresh-inputs.json"] = (RUNTIME / "fresh-inputs.json").read_bytes()
    elif args.action == "sync":
        if not (args.hosted_image or "").startswith(STAGE["registry"] + "/fa-behavior-assistant-desktop@sha256:"):
            raise SystemExit("--hosted-image must be the selected environment's Hosted image by digest")
    else:
        files["plan.json"] = (work / "plan.json").read_bytes()
        files["authorization.json"] = (work / "authorization.json").read_bytes()
    execution = start(work, args.action, args.image, files, args.hosted_image if args.action == "sync" else None)
    logs(work, execution)


if __name__ == "__main__":
    main()
