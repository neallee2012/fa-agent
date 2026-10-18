"""Environment deploy steps for GitHub Actions (ADR-0008). Standard library only; shells out to `az` and
`docker`.

One build = one commit: the API and Hosted images come from the same checkout and the code identity installed
in the built API image (code_sha256(), read by running the image) must equal the checkout's core. Every step
stops on the first error (only a read inside a wait loop is retried, at most 3 times); nothing is rolled back
(forward-fix policy). Writes are limited to the selected registry, sync job and Container Apps. The installed
configuration (the selected profile's runtime/config.json, set as the API's BEHAVIOR_SERVER_CONFIG_JSON at W7)
is never changed here: the API update stops if the deployed configuration differs. The other API ENV values
come from the selected profile (api_environment), so an ENV change is a normal PR that this deploy applies.

  python3 deploy/stage/ci_deploy.py meta            -> key=value lines for $GITHUB_OUTPUT (incl. the release version);
                                                       also stops on a Hosted overlay Agent sync would refuse, a
                                                       Hosted flag set that cannot run groups or routine proposals,
                                                       API routine flags the API would refuse at startup, and on
                                                       a malformed feed_app/feed_environment/api_secret_environment
  python3 deploy/stage/ci_deploy.py check           -> the same profile checks as meta, without the CI outputs
  python3 deploy/stage/ci_deploy.py guard           -> stops unless this commit contains the serving API revision's
                                                       commit (no rollback by re-running an old deploy; a revision
                                                       named elsewhere only warns); reports changed inputs; sync
                                                       verifies the full definition;
                                                       read-only warning if the one-time Web PubSub resource/role
                                                       is missing (never stops: hints are optional); stops if a
                                                       declared API secret or the named feed app does not exist,
                                                       or the API routine clock is on with minReplicas < 1
  python3 deploy/stage/ci_deploy.py build <kind>    -> builds and pushes one image, prints its digest reference
  python3 deploy/stage/ci_deploy.py gate <api-ref>  -> ADR-0009 phase 3b compatibility gate, before sync: the new API
                                                       image (sync job, API identity) reads every binding row and the
                                                       version definitions they name, never a session; stops only
                                                       when the profile arms it (compatibility_gate_armed) and no
                                                       approved release override applies (deploy/README.md#compat-gate)
  python3 deploy/stage/ci_deploy.py sync <api-ref> <hosted-ref>
  python3 deploy/stage/ci_deploy.py api <api-ref>
  python3 deploy/stage/ci_deploy.py follow <api-ref> <hosted-ref>  -> verify versions, then finish all Agent follows;
                                                       with the 2d-i switch on (FA_NATIVE_SESSION_GUARD_ENFORCE=1,
                                                       which guard, sync, api and follow each let on only over one
                                                       revision already running P5+P6, and never off again) it only
                                                       reports each Agent's version and environment, never follows
  python3 deploy/stage/ci_deploy.py console <console-ref>
  python3 deploy/stage/ci_deploy.py feed <feed-ref> -> the feed reader MCP app (README "Feed reader MCP"), after
                                                       follow; `build feed` prints no reference until the profile
                                                       names feed_app
"""
import argparse
import ast
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "src" / "fa-shared-context-poc"
sys.path.insert(0, str(ROOT / "deploy" / "shared-context"))
from deployment_config import config_path, load_config

# Replaced in main() by the selected profile; build only needs the registry, so the import itself never requires the
# runtime fields (a new azd environment builds its images before its Agents exist).
STAGE = load_config(require_runtime=False)
CONFIG = config_path()
RUNTIME = ROOT / "deploy" / "stage" / "runtime"
TOKEN = re.sub(r"[^a-z0-9-]+", "-", STAGE["environment"].lower()).strip("-") or "environment"
VERSIONS = APP / "shared_context" / "behavior_agent_versions.py"
# Overlay keys sync checks by value, not as "0"/"1" switches (behavior_agent_versions._check_environment).
HOSTED_VALUE_KEYS = frozenset({"FA_NATIVE_TURNS", "FA_NATIVE_HITL_TTL_SECONDS", "FA_PRIVATE_MEMORY_STORE", "TZ"})
# native_responses_wire.ACCEPTED_STATE_PROFILES (ADR-0009 2d-i), copied: meta cannot import shared_context. The API's
# own FA_NATIVE_TURNS stays v1 (api_routines) until 2d-ii.
HOSTED_STATE_PROFILES = frozenset({"foundry-native-v1", "foundry-native-v2"})
# ADR-0009 2d-i's one switch (src README#native-2di-switch): the API's FA_NATIVE_SESSION_GUARD_ENFORCE="1" also makes
# every assistant's Hosted definition declare v2; one is never deployed without the other.
SESSION_GUARD = "FA_NATIVE_SESSION_GUARD_ENFORCE"
NATIVE_V2 = "foundry-native-v2"
# The native_binding.NATIVE_2DI_SCHEMA the API must declare before the switch goes on: 2 = P5's background cut plus
# P6's in-request wait and verify-only follow (read from source by AST: the deploy never imports shared_context).
API_2DI_SCHEMA = 2
IMAGES = {
    "api": ("fa-behavior-api", "Dockerfile.behavior-api"),
    "hosted": ("fa-behavior-assistant-desktop", "Dockerfile.behavior-assistant-desktop"),
    "console": ("fa-web-console", "Dockerfile.web-console"),
    "feed": ("fa-feed-mcp", "Dockerfile.feed-mcp"),
}


def _az_prefix(system=os.name):
    # Windows `az` is a .cmd shim that subprocess cannot start; call the CLI module directly (as run_fresh_job does).
    if system == "nt":
        cli = os.environ.get("AZ_PYTHON", r"C:\Program Files\Microsoft SDKs\Azure\CLI2\python.exe")
        return (cli, "-X", "utf8", "-IBm", "azure.cli")
    return ("az",)


AZ = _az_prefix()


def run(*args, capture=True):
    command = (*AZ, *args[1:]) if args[:1] == ("az",) else args
    result = subprocess.run(command, check=False, text=True, encoding="utf-8", errors="replace",
                            capture_output=capture)
    if result.returncode != 0:
        sys.stderr.write((result.stderr or "")[-4000:])
        raise SystemExit(f"command failed ({result.returncode}): {' '.join(args[:4])}")
    return result.stdout if capture else ""


def core():
    files = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
             for path in sorted((APP / "shared_context").glob("*.py"))}
    raw = json.dumps(files, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(raw).hexdigest()


def check():
    """The profile checks meta runs before any build; an azd environment (deploy/azd) has no CI identity to print."""
    hosted_environment()
    api_routines()
    api_faults()
    realtime()
    feed_config()
    api_secrets()
    console_overlay(STAGE.get("console_environment") or {})
    gate_settings()


def meta():
    for key in ("tenant_id", "subscription_id", "deploy_client_id"):
        if not STAGE.get(key):
            raise SystemExit(f"deployment profile has no {key}")
    check()
    print(f"tenant_id={STAGE['tenant_id']}")
    print(f"subscription_id={STAGE['subscription_id']}")
    print(f"deploy_client_id={STAGE['deploy_client_id']}")
    print(f"core={core()}")
    print(f"release={release()}")


# Feed reader MCP (README "Feed reader MCP"). Checked in meta, before any build or write, so a typo never leaves a run
# with the API updated and the Agents not yet following.
SECRET_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,252}")
FEED_PUBLIC_KEY = re.compile(r"[A-Za-z0-9+/]{43}=")


def feed_config():
    """(app, ENV) of the feed reader app, or None until the profile names feed_app; its only ENV is the public key."""
    app, environment = STAGE.get("feed_app"), STAGE.get("feed_environment")
    if app is None and environment is None:
        return None
    if (not isinstance(app, str) or re.fullmatch(r"[a-z][a-z0-9-]{0,30}[a-z0-9]", app) is None
            or not isinstance(environment, dict) or set(environment) != {"FEED_MCP_PUBLIC_KEY"}
            or not isinstance(environment["FEED_MCP_PUBLIC_KEY"], str)
            or FEED_PUBLIC_KEY.fullmatch(environment["FEED_MCP_PUBLIC_KEY"]) is None):
        raise SystemExit("feed_app needs feed_environment with exactly FEED_MCP_PUBLIC_KEY "
                         "(a standard base64 Ed25519 public key)")
    return app, environment


def api_secrets():
    """API ENV name -> the API app's own Container Apps secret name; the value never passes through the repo or CI."""
    secrets = STAGE.get("api_secret_environment") or {}
    if (not isinstance(secrets, dict) or set(secrets) & set(STAGE["api_environment"])
            or not all(isinstance(key, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", key)
                       and isinstance(ref, str) and SECRET_NAME.fullmatch(ref) for key, ref in secrets.items())):
        raise SystemExit("api_secret_environment maps distinct ENV names (not in api_environment) to secret names")
    return secrets


def _require_secrets(app, properties, secrets):
    defined = {item.get("name") for item in (properties.get("configuration") or {}).get("secrets") or []}
    missing = sorted(set(secrets.values()) - defined)
    if missing:
        raise SystemExit(f"{app}: secrets {missing} are not on the app yet; an operator adds them first")


def _release_file():
    return Path(STAGE["release_file"]) if STAGE.get("release_file") else CONFIG.parent / "release.json"


def release():
    """The selected profile's release version, for the run summary and the gate override's binding."""
    path = _release_file()
    if not path.exists():
        return "none"
    version = json.loads(path.read_text(encoding="utf-8")).get("version")
    if not isinstance(version, str) or re.fullmatch(r"[0-9A-Za-z._-]{1,40}", version) is None:
        raise SystemExit(f"{path} needs a short version string")
    return version


# ADR-0009 phase 3b compatibility gate (deploy/README.md#compat-gate). Arming is a reviewed profile change; the
# override is a reviewed release-file change bound to that one release (a stage release PR is Neal's decision), so
# both are logged in git, and the override again in the run summary and the gate's job log.
OVERRIDE_KEYS = {"release", "approved_by", "approved_at", "reason"}
# The gate's own wait (18 polls x 10 s = 3 min), not the sync job's 30 min: a hung gate execution is stopped, and
# then is a warning unarmed and a refusal armed (deploy/README.md#compat-gate, "時間").
GATE_POLL_ATTEMPTS = 18


def gate_settings():
    """(armed, override or None), checked in meta before any build: a stale or malformed override stops the run."""
    armed = STAGE.get("compatibility_gate_armed", False)
    if type(armed) is not bool:
        raise SystemExit("compatibility_gate_armed must be true or false")
    path = _release_file()
    override = (json.loads(path.read_text(encoding="utf-8")).get("compatibility_gate_override")
                if path.exists() else None)
    if override is None:
        return armed, None
    if (not isinstance(override, dict) or set(override) != OVERRIDE_KEYS
            or not all(isinstance(value, str) and value.strip() and len(value) <= 400 for value in override.values())):
        raise SystemExit(f"{path}: compatibility_gate_override needs exactly {sorted(OVERRIDE_KEYS)} as short text")
    if override["release"] != release():
        raise SystemExit(f"{path}: compatibility_gate_override names release {override['release']!r}, not this "
                         f"release {release()!r}; an override is for one release only, remove it")
    return armed, override


# Web PubSub hints (v16; shared_context/realtime_push.py). Same formats the API checks at startup.
REALTIME_ENDPOINT = re.compile(r"https://[a-z][a-z0-9-]{1,61}[a-z0-9]\.webpubsub\.azure\.com")
REALTIME_HUB = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,126}")
WEB_PUBSUB_SERVICE_OWNER = "12cf5a90-567b-43ae-8102-96cf46c7d9b4"


def realtime():
    """(endpoint, hub) from profile api_environment, or None; the console may only name the same endpoint."""
    api_env, console_env = STAGE["api_environment"], STAGE.get("console_environment") or {}
    endpoint, hub = api_env.get("FA_REALTIME_ENDPOINT"), api_env.get("FA_REALTIME_HUB", "fa")
    if endpoint is None:
        if "FA_REALTIME_HUB" in api_env or "FA_REALTIME_ENDPOINT" in console_env:
            raise SystemExit("realtime settings need FA_REALTIME_ENDPOINT in api_environment")
        return None
    if REALTIME_ENDPOINT.fullmatch(endpoint) is None or REALTIME_HUB.fullmatch(hub) is None:
        raise SystemExit("FA_REALTIME_ENDPOINT must be https://<name>.webpubsub.azure.com and FA_REALTIME_HUB a hub name")
    if console_env.get("FA_REALTIME_ENDPOINT", endpoint) != endpoint:
        raise SystemExit("the console's FA_REALTIME_ENDPOINT must be the API's endpoint")
    return endpoint, hub


def realtime_ready():
    """Read-only, never stops the deploy: warn when the one-time Web PubSub resource (Entra only) or the API
    identity's role is missing. Hints are optional (the workbench keeps polling), so a missing resource is a
    visible annotation, not a gate. The deploy identity cannot create either (stage-ci-identity.bicep); an operator
    deploys infra/shared-context/realtime.bicep once with Neal's approval."""
    settings = realtime()
    if settings is None:
        return
    name = settings[0].removeprefix("https://").split(".")[0]
    group = f"/subscriptions/{STAGE['subscription_id']}/resourceGroups/{STAGE['resource_group']}"
    resource = f"{group}/providers/Microsoft.SignalRService/webPubSub/{name}"
    step = (f"one-time operator step (Neal approves): deploy infra/shared-context/realtime.bicep with "
            f"the generated realtime.parameters.json into {STAGE['resource_group']}; until then the workbench keeps polling")
    found = subprocess.run([*AZ, "rest", "--method", "GET", "--url", f"{ARM}{resource}?api-version=2024-03-01",
                            "-o", "json"], check=False, text=True, encoding="utf-8", errors="replace",
                           capture_output=True)
    if found.returncode != 0:
        print(f"::warning::Web PubSub {name} is unreadable or missing; {step}")
        return
    try:
        properties = json.loads(found.stdout)["properties"]
    except (KeyError, TypeError, ValueError):
        properties = {}
    if properties.get("disableLocalAuth") is not True or properties.get("hostName") != f"{name}.webpubsub.azure.com":
        print(f"::warning::Web PubSub {name} must allow Entra only and serve {settings[0]}; {step}")
        return
    try:
        principal = rest("GET", f"{group}/providers/Microsoft.ManagedIdentity/userAssignedIdentities/"
                                f"{STAGE['api_identity']}?api-version=2023-01-31")["properties"]["principalId"]
        holders = json.loads(run("az", "role", "assignment", "list", "--scope", resource,
                                 "--role", WEB_PUBSUB_SERVICE_OWNER, "--fill-principal-name", "false",
                                 "--fill-role-definition-name", "false", "--query", "[].principalId", "-o", "json"))
    except (SystemExit, KeyError, TypeError, ValueError):
        print(f"::warning::the Web PubSub role of {STAGE['api_identity']} on {name} is unreadable; {step}")
        return
    if principal not in holders:
        print(f"::warning::{STAGE['api_identity']} has no Web PubSub Service Owner on {name}; {step}")
        return
    print(f"realtime {name} ready (Entra only, API identity is Service Owner)")


# Files the Hosted image is built from (Dockerfile.behavior-assistant-desktop); only shared_context/*.py is the core.
HOSTED_INPUTS = ("Dockerfile.behavior-assistant-desktop", "requirements-hosted.txt", "tools/playwright-mcp",
                 "shared_context", "hosted_bootstrap.py", "behavior_server_main.py", "behavior_assistant_main.py",
                 "runtime_probe.py")


# The revision suffix _suffix() writes: <commit[:7]>-r<run number>-<run attempt>.
OWN_SUFFIX = re.compile(r"([0-9a-f]{7,40})-r[0-9]+-[0-9]+")


def serving_commit(revision):
    """The serving commit named by an API revision this script created, or None (with a warning) for any other name.

    A revision named elsewhere (portal, `az containerapp update`) says nothing about its code, so it only warns: it
    must not block every later deploy. A commit this script named must be an ancestor of the deploying HEAD, so a
    re-run of an old deploy run never puts an older commit over a newer one (forward-fix only). A named commit that
    is no longer in the history at all (a rebased or dropped branch) stops too, unless an operator sets
    FA_ALLOW_ROLLBACK=true for that one run (azd deploy only; the stage workflow never sets it): then it warns."""
    match = OWN_SUFFIX.fullmatch(revision.removeprefix(STAGE["api_app"] + "--"))
    if match is None:
        print(f"::warning::revision {revision!r} was not named by this deploy script, so the serving commit is unknown; "
              "the rollback check and the change report are skipped")
        return None
    found = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--verify", "--quiet", f"{match[1]}^{{commit}}"],
                           check=False, text=True, capture_output=True)
    if found.returncode != 0:
        message = (f"serving commit {match[1]} (revision {revision!r}) is not in this checkout's history, so "
                   "this deploy cannot be shown to be newer")
        if os.environ.get("FA_ALLOW_ROLLBACK") != "true":
            raise SystemExit(f"{message}; an operator decides this deploy: set FA_ALLOW_ROLLBACK=true for that one "
                             "run to deploy anyway (the stage workflow never sets it)")
        print(f"::warning::{message}; FA_ALLOW_ROLLBACK=true: the rollback check and the change report are skipped "
              "for this run")
        return None
    base = found.stdout.strip()
    # Exit 0: base is HEAD or an ancestor of it; 1: it is not (an older or unrelated commit is being deployed).
    ancestor = subprocess.run(["git", "-C", str(ROOT), "merge-base", "--is-ancestor", base, "HEAD"],
                              check=False, text=True, capture_output=True)
    if ancestor.returncode == 1:
        raise SystemExit(f"this deploy's commit does not contain the serving commit {base[:12]} (revision "
                         f"{revision!r}): a re-run of an older deploy would roll the environment back. Deploy a "
                         "newer main commit instead (forward-fix); nothing was changed")
    if ancestor.returncode != 0:
        sys.stderr.write((ancestor.stderr or "")[-2000:])
        raise SystemExit(f"cannot compare the serving commit {base[:12]} with this deploy's commit")
    return base


def guard():
    """Compare against the serving commit; full Hosted-definition equality is enforced by sync, not core identity."""
    properties = rest("GET", f"{_resource('containerApps', STAGE['api_app'])}?{APPS_API}")["properties"]
    # Read-only, before any build or write: a declared API secret or the feed app missing stops here.
    _require_secrets(STAGE["api_app"], properties, api_secrets())
    # The routine clock runs inside the API process (README#native-routine-api-clock): it needs one resident replica.
    if api_routines() and (((properties.get("template") or {}).get("scale") or {}).get("minReplicas") or 0) < 1:
        raise SystemExit(f"{STAGE['api_app']}: FA_NATIVE_ROUTINE_CLOCK=api needs minReplicas >= 1; "
                         "an operator sets the API scale first")
    if feed_config() is not None:
        rest("GET", f"{_resource('containerApps', feed_config()[0])}?{APPS_API}")
    revision = properties.get("latestReadyRevisionName") or ""
    base = serving_commit(revision)
    switch_preconditions(properties, base)
    if base is None:
        realtime_ready()
        return

    def changed(*paths):
        return run("git", "-C", str(ROOT), "diff", "--name-only", base, "HEAD", "--", *paths).split()

    core_files = changed(":(glob)src/fa-shared-context-poc/shared_context/*.py")
    hosted_only = [path for path in changed(*(f"src/fa-shared-context-poc/{item}" for item in HOSTED_INPUTS))
                   if path not in core_files]
    print(f"serving {base[:12]}; core changed: {bool(core_files)}; Hosted-only changed: {len(hosted_only)};"
          " sync verifies and publishes the full Hosted definition")
    realtime_ready()


def build(kind):
    if kind == "feed" and feed_config() is None:
        # Until an operator creates the app (Neal approves) and the profile names it, there is nothing to build.
        print("feed_app is not in the deployment profile; feed image skipped", file=sys.stderr)
        return
    repository, dockerfile = IMAGES[kind]
    tag = f"{STAGE['registry']}/{repository}:{os.environ.get('GITHUB_SHA', 'local')[:12]}"
    run("az", "acr", "login", "--name", STAGE["registry_name"], "--subscription", STAGE["subscription_id"])
    result = subprocess.run(["docker", "build", "--progress=plain", "--platform", "linux/amd64", "-f",
                             str(APP / dockerfile), "-t", tag, str(APP)], check=False, text=True, capture_output=True)
    log = (result.stdout or "") + (result.stderr or "")
    if result.returncode != 0:
        sys.stderr.write(log[-8000:])
        raise SystemExit(f"{kind} image build failed")
    if kind == "api":
        built, expected = image_core(tag), core()
        if built != expected:
            raise SystemExit(f"API image core {built} differs from this checkout's {expected}; refusing to push")
    _push(tag)
    digest = run("docker", "inspect", "--format", "{{index .RepoDigests 0}}", tag).strip()
    if "@sha256:" not in digest:
        raise SystemExit("no pushed digest")
    print(digest)


def image_core(tag):
    """The code identity installed in the built image, read from the image itself: the build log's FA_DEPLOYED_CORE
    line is missing when Docker reuses that layer from its cache (an azd rerun). No network, nothing written."""
    result = subprocess.run(["docker", "run", "--rm", "--network", "none", "--platform", "linux/amd64",
                             "--entrypoint", "python", tag, "-B", "-c",
                             "from shared_context.behavior_code_identity import code_sha256; print(code_sha256())"],
                            check=False, text=True, capture_output=True)
    value = (result.stdout or "").strip()
    if result.returncode != 0 or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        sys.stderr.write((result.stderr or "")[-2000:])
        raise SystemExit("cannot read the code identity from the built API image; refusing to push")
    return value


# Waits before each retry of a failed push (2026-10-03: the registry refused TCP connections mid-push twice).
PUSH_RETRY_SECONDS = (10, 30, 60)


def _push(tag):
    """Push the image; retry only the push, at most len(PUSH_RETRY_SECONDS) times.

    Pushing the same local tag again sends the same content-addressed layers and manifest, so a retry cannot create
    a second image. The last failure still stops the run."""
    for attempt, wait in enumerate((*PUSH_RETRY_SECONDS, None), start=1):
        result = subprocess.run(["docker", "push", tag], check=False, text=True, capture_output=True)
        if result.returncode == 0:
            return
        sys.stderr.write((result.stderr or "")[-2000:])
        if wait is None:
            raise SystemExit(f"command failed ({result.returncode}): docker push {tag} (attempt {attempt}, no retry left)")
        print(f"docker push failed (attempt {attempt}); retrying in {wait}s", file=sys.stderr, flush=True)
        time.sleep(wait)


def _config_bytes():
    return (RUNTIME / "config.json").read_bytes()


def hosted_flags():
    """The Hosted overlay keys sync accepts: the one list in shared_context/behavior_agent_versions.py.

    meta runs on the runner's own python3 before any build, and shared_context needs pydantic (not installed there),
    so the list is read from the source instead of imported; tests keep the value rules below equal to sync's."""
    tree = ast.parse(VERSIONS.read_text(encoding="utf-8"))
    for node in tree.body:
        if (isinstance(node, ast.Assign) and [getattr(target, "id", None) for target in node.targets]
                == ["_ENVIRONMENT_FLAGS"] and isinstance(node.value, ast.Call) and len(node.value.args) == 1):
            return frozenset(ast.literal_eval(node.value.args[0]))
    raise SystemExit("cannot read _ENVIRONMENT_FLAGS from behavior_agent_versions.py")


def _check_hosted(name, environment, idle_timeout_seconds):
    """sync's _check_environment rules, so a bad key or value stops in meta before any image is built or pushed
    (sync itself would stop later, before writing any Agent)."""
    flags = hosted_flags()
    try:
        invalid = (not isinstance(environment, dict) or set(environment) - flags
                   or any(not isinstance(value, str) or not value or len(value.encode("utf-8")) > 2048
                          for value in environment.values()))
    except UnicodeError:
        invalid = True
    switches = flags - HOSTED_VALUE_KEYS
    if (invalid or type(idle_timeout_seconds) is not int or not 120 <= idle_timeout_seconds <= 3600
            or any(key in environment and environment[key] not in {"0", "1"} for key in switches)
            or environment.get("FA_NATIVE_TURNS", "foundry-native-v1") not in HOSTED_STATE_PROFILES
            or "FA_NATIVE_HITL_TTL_SECONDS" in environment
            and re.fullmatch(r"[1-9][0-9]{0,5}", environment["FA_NATIVE_HITL_TTL_SECONDS"]) is None
            or any("\x00" in value or "\r" in value or "\n" in value for value in environment.values())):
        raise SystemExit(f"{name}: hosted_environment accepts only the keys and values Agent sync accepts "
                         f"(shared_context/behavior_agent_versions.py _ENVIRONMENT_FLAGS)")


def hosted_environment():
    """Agent ID -> Hosted ENV overlay for sync, checked here with sync's own rules before any build or write.

    Obvious combinations stop too: every group turn runs on the Responses lane, so FA_NATIVE_GROUPS_ENABLED=1 must
    also carry FA_RESPONSES_ENABLED=1 (2026-10-04 stage start refused with 409); propose_routine is offered only with
    native routines on (README#native-routines-stage; HITL may come from the served definition, so it is not checked)."""
    on = native_2di()
    overlay = {}
    for agent in STAGE["agents"]:
        environment = agent.get("hosted_environment") or {}
        turns, assistant = environment.get("FA_NATIVE_TURNS"), agent["kind"] == "assistant"
        # v2 only on assistants and only through the switch: an executor never runs the 2d-i turn the API moves.
        if turns == NATIVE_V2 and not (on and assistant) or on and assistant and turns not in {None, NATIVE_V2}:
            raise SystemExit(f"{agent['name']}: FA_NATIVE_TURNS in hosted_environment must match the 2d-i switch "
                             f"(api_environment {SESSION_GUARD}); leave it out, the switch sets {NATIVE_V2} on "
                             "assistants only")
        if on and agent["kind"] == "assistant":
            environment = {**environment, "FA_NATIVE_TURNS": NATIVE_V2}
        if not environment:
            continue
        _check_hosted(agent["name"], environment, STAGE["hosted_idle_timeout_seconds"])
        if environment.get("FA_NATIVE_GROUPS_ENABLED") == "1" and environment.get("FA_RESPONSES_ENABLED") != "1":
            raise SystemExit(f"{agent['name']}: FA_NATIVE_GROUPS_ENABLED=1 needs FA_RESPONSES_ENABLED=1 "
                             "(group turns run on the Responses lane)")
        if environment.get("FA_NATIVE_ROUTINE_PROPOSALS") == "1" and environment.get("FA_NATIVE_ROUTINES_ENABLED") != "1":
            raise SystemExit(f"{agent['name']}: FA_NATIVE_ROUTINE_PROPOSALS=1 needs FA_NATIVE_ROUTINES_ENABLED=1 "
                             "(no schedule tool otherwise)")
        overlay[agent["agent_id"]] = environment
    return overlay


def api_routines():
    """True when api_environment turns the API's routine clock on. The same values the API checks at startup
    (shared_context/native_routine_wire.py, native_routine_auth.py): a bad value would stop the new API revision."""
    environment = STAGE["api_environment"]
    routines = environment.get("FA_NATIVE_ROUTINES_ENABLED")
    clock = environment.get("FA_NATIVE_ROUTINE_CLOCK")
    proposals = environment.get("FA_NATIVE_ROUTINE_PROPOSALS")
    if routines not in {None, "0", "1"} or clock not in {None, "0", "api"} or proposals not in {None, "0", "1"}:
        raise SystemExit("api_environment: FA_NATIVE_ROUTINES_ENABLED and FA_NATIVE_ROUTINE_PROPOSALS accept only "
                         "\"0\" or \"1\", FA_NATIVE_ROUTINE_CLOCK only \"0\" or \"api\"")
    if clock == "api" and (routines != "1" or environment.get("FA_NATIVE_TURNS") != "foundry-native-v1"):
        raise SystemExit("api_environment: FA_NATIVE_ROUTINE_CLOCK=api needs FA_NATIVE_ROUTINES_ENABLED=1 and "
                         "FA_NATIVE_TURNS (the API refuses to start or never starts its clock)")
    if proposals == "1" and (clock != "api" or environment.get("FA_NATIVE_HITL_ENABLED") != "1"):
        raise SystemExit("api_environment: FA_NATIVE_ROUTINE_PROPOSALS=1 needs FA_NATIVE_ROUTINE_CLOCK=api and "
                         "FA_NATIVE_HITL_ENABLED=1 (every schedule card would be refused otherwise)")
    # The Agents and the workbench only reach routines through this API.
    if proposals != "1" and any((agent.get("hosted_environment") or {}).get("FA_NATIVE_ROUTINE_PROPOSALS") == "1"
                                for agent in STAGE["agents"]):
        raise SystemExit("a Hosted FA_NATIVE_ROUTINE_PROPOSALS=1 needs api_environment FA_NATIVE_ROUTINE_PROPOSALS=1 "
                         "(the owner's allow on every schedule card would be refused)")
    if routines != "1" and (STAGE.get("console_environment") or {}).get("FA_NATIVE_ROUTINES_ENABLED") == "1":
        raise SystemExit("the workbench's FA_NATIVE_ROUTINES_ENABLED=1 needs api_environment FA_NATIVE_ROUTINES_ENABLED=1 "
                         "(its 排程 tab reads routes the API would not install)")
    return clock == "api"


def api_faults():
    """ADR-0009 gate 2's fault hook (FA_NATIVE_CUT_FAULT, behavior_native_sessions.py) is for dev only: a stage API with
    it would hold every files fence to its bound."""
    if STAGE["environment"] == "stage" and "FA_NATIVE_CUT_FAULT" in STAGE["api_environment"]:
        raise SystemExit("api_environment: FA_NATIVE_CUT_FAULT is a dev-only fault hook; stage refuses it")


def _api_source(name, commit=None):
    """One shared_context file of this checkout, or of ``commit`` (the serving API's); None when it does not exist."""
    if commit is None:
        path = APP / "shared_context" / name
        return path.read_text(encoding="utf-8") if path.exists() else None
    found = subprocess.run(["git", "-C", str(ROOT), "show", f"{commit}:src/fa-shared-context-poc/shared_context/{name}"],
                           check=False, text=True, encoding="utf-8", errors="replace", capture_output=True)
    return found.stdout if found.returncode == 0 else None


def api_2di_schema(commit=None):
    """The module-level int ``NATIVE_2DI_SCHEMA`` native_binding.py assigns (this checkout's, or ``commit``'s); 0 when
    it assigns none or the source does not parse."""
    try:
        tree = ast.parse(_api_source("native_binding.py", commit) or "")
    except SyntaxError:
        return 0
    for node in tree.body:
        names = [getattr(item, "id", None) for item in node.targets] if isinstance(node, ast.Assign) else []
        if names == ["NATIVE_2DI_SCHEMA"] and isinstance(node.value, ast.Constant) and type(node.value.value) is int:
            return node.value.value
    return 0


def missing_2di(commit=None):
    """[] when the API code can run 2d-i (NATIVE_2DI_SCHEMA >= API_2DI_SCHEMA), else what it lacks."""
    found = api_2di_schema(commit)
    return [] if found >= API_2DI_SCHEMA else [f"native_binding.NATIVE_2DI_SCHEMA={found}, needs {API_2DI_SCHEMA}"]


def native_2di():
    """True when api_environment switches ADR-0009 2d-i on (src README#native-2di-switch). The Hosted half
    (assistants declare foundry-native-v2) is derived from it in hosted_environment(), so the two never part."""
    value = STAGE["api_environment"].get(SESSION_GUARD, "0")
    if value not in {"0", "1"}:
        raise SystemExit(f"api_environment: {SESSION_GUARD} accepts only \"0\" or \"1\" (the 2d-i switch)")
    if value == "0" and STAGE["assistant_environment"].get("FA_NATIVE_TURNS") == NATIVE_V2:
        raise SystemExit(f"assistant_environment: FA_NATIVE_TURNS={NATIVE_V2} needs the 2d-i switch "
                         f"(api_environment {SESSION_GUARD}=\"1\"); leave it at foundry-native-v1")
    missing = missing_2di() if value == "1" else []
    if missing:
        raise SystemExit(f"api_environment {SESSION_GUARD}=\"1\" needs an API with ADR-0009 2d-i P5 and P6; this "
                         f"checkout (= the API image built from it) lacks {missing}")
    return value == "1"


def switch_preconditions(properties=None, base=None):
    """Read-only, from guard() and again from sync/follow and api (the dev line runs those without guard): 2d-i only
    goes on over a single serving API revision that already runs P5+P6 (older replicas decode the rows P5 writes as
    409 NATIVE_BINDING_INVALID), and it never goes off again (roll forward: no API below P6, no Hosted below H2d once
    v2 conversations exist; ADR-0009 M1 2d-i). Without ``properties`` it reads the API app (and its serving commit)."""
    read = properties is None
    app = STAGE["api_app"]
    if read:
        properties = rest("GET", f"{_resource('containerApps', app)}?{APPS_API}")["properties"]
    container = ((properties.get("template") or {}).get("containers") or [{}])[0]
    deployed = {item.get("name"): item.get("value") for item in container.get("env") or []}.get(SESSION_GUARD)
    if not native_2di():
        if deployed == "1":
            raise SystemExit(f"{app}: 2d-i is on ({SESSION_GUARD}=\"1\") and this profile turns it off; it cannot be "
                             "turned off (ADR-0009: roll forward only). Keep \"1\" and fix forward")
        return
    mode = (properties.get("configuration") or {}).get("activeRevisionsMode")
    if mode != "Single":
        raise SystemExit(f"{app}: the 2d-i switch needs single revision mode (mode {mode!r}); an operator sets it "
                         "first (az containerapp revision set-mode --mode single)")
    if deployed == "1":
        return  # already on: a later deploy's old revision may still be deactivating, so only the mode is checked
    # shortcut: first page only (no nextLink), upgrade if an API app ever keeps more than one page of revisions.
    revisions = (rest("GET", f"{_resource('containerApps', app)}/revisions?{APPS_API}") or {}).get("value") or []
    active = [item.get("name") for item in revisions if (item.get("properties") or {}).get("active")]
    ready = properties.get("latestReadyRevisionName") or ""
    if active != [ready]:
        raise SystemExit(f"{app}: turning 2d-i on needs one active revision, the latest ready one ({ready!r}; active "
                         f"{active}); wait for the old revision to deactivate, or an operator deactivates it")
    if read:
        base = serving_commit(ready)
    missing = ["the serving commit is unknown"] if base is None else missing_2di(base)
    if missing:
        raise SystemExit(f"{app}: turning 2d-i on needs the serving API to run P5+P6 already ({missing}); deploy this "
                         f"commit with {SESSION_GUARD}=\"0\" first, then switch it on")


def _sync_script(hosted_ref, *, follow=False):
    """Keep the installed config unchanged; pass deployment-only ENV to definition sync and publish its selector."""
    raw = _config_bytes()
    blob = base64.b64encode(zlib.compress(raw, 9)).decode()
    environment = json.dumps(hosted_environment(), sort_keys=True, separators=(",", ":"))
    runtime_root = f"/tmp/fa-{TOKEN}-runtime"
    argv = ["sync", "--config", f"{runtime_root}/config.json", "--image", hosted_ref, "--core", core(), "--route",
            "--hosted-environment", f"{runtime_root}/hosted-environment.json",
            "--idle-timeout-seconds", str(STAGE["hosted_idle_timeout_seconds"])]
    on = native_2di()
    if not on:
        # A turn-on that stopped after sync left v2 definitions: carrying them on with the API off is refused.
        argv.append("--refuse-native-v2")
    if not follow:
        argv.append("--defer-follow")
    elif on:
        # 2d-i moves each Agent after its turn end (P5 background cut): the deploy only reports, never follows.
        argv.append("--verify-follow")
    return "\n".join([
        "import base64,hashlib,sys,zlib",
        "from pathlib import Path",
        "from shared_context.behavior_code_identity import code_sha256",
        f"assert code_sha256()=={core()!r}",
        f"raw=zlib.decompress(base64.b64decode({blob!r}))",
        f"assert hashlib.sha256(raw).hexdigest()=={hashlib.sha256(raw).hexdigest()!r}",
        f"root=Path({runtime_root!r}); root.mkdir(mode=0o700,exist_ok=True)",
        "(root/'config.json').write_bytes(raw)",
        f"(root/'hosted-environment.json').write_text({environment!r},encoding='utf-8')",
        "from shared_context.behavior_agent_versions import main",
        f"raise SystemExit(main({argv!r}))",
    ])


ARM = "https://management.azure.com"
APPS_API = "api-version=2025-01-01"


def _resource(kind, name):
    return (f"/subscriptions/{STAGE['subscription_id']}/resourceGroups/{STAGE['resource_group']}"
            f"/providers/Microsoft.App/{kind}/{name}")


def rest(method, path, body=None):
    """ARM call through `az rest`; the body goes through a temp file so long startup scripts never hit argv parsing."""
    args = ["az", "rest", "--method", method, "--url", f"{ARM}{path}", "-o", "json"]
    if body is not None:
        body_file = Path(os.environ.get("RUNNER_TEMP") or tempfile.gettempdir()) / \
            f"fa-{TOKEN}-{os.getpid()}-{time.monotonic_ns()}.json"
        body_file.write_text(json.dumps(body), encoding="utf-8")
        args += ["--body", f"@{body_file}", "--headers", "Content-Type=application/json"]
    try:
        out = run(*args)
    finally:
        if body is not None:
            body_file.unlink(missing_ok=True)
    return json.loads(out) if out.strip() else None


# Waits before each retry of a failed read inside a wait loop: one transient `az` error (token refresh, throttling,
# a dropped connection) must not stop a run halfway, after the API was already updated. Reads only, never a write.
READ_RETRY_SECONDS = (5, 15, 30)


def _poll(path):
    """GET for a wait loop; a failed read is retried at most len(READ_RETRY_SECONDS) times, then stops the run."""
    for attempt, wait in enumerate((*READ_RETRY_SECONDS, None), start=1):
        try:
            return rest("GET", path)
        except SystemExit as error:
            if wait is None:
                raise SystemExit(f"{error} (read {attempt} times; read back before any rerun)") from None
            print(f"read failed (attempt {attempt}): {error}; retrying in {wait}s", file=sys.stderr, flush=True)
            time.sleep(wait)


def sync(api_ref, hosted_ref, *, follow=False):
    if not hosted_ref.startswith(STAGE["registry"] + "/fa-behavior-assistant-desktop@sha256:"):
        raise SystemExit("hosted image must be a digest reference from the selected registry")
    switch_preconditions()
    status, name = _run_job(api_ref, _sync_script(hosted_ref, follow=follow))
    if status != "Succeeded":
        hint = "" if native_2di() else (f"; an Agent failed with NATIVE_V2_SERVED_SWITCH_OFF already serves "
                                        f"{NATIVE_V2} (a turn-on stopped after sync): rerun with {SESSION_GUARD}=\"1\"")
        raise SystemExit(f"sync {name} {status}; read the job log in Log Analytics, do not rerun blindly{hint}")
    print(f"sync {name} Succeeded")


def _gate_script(armed, override):
    """The gate from the new API image: its SUPPORTED_STATE_PROFILES is the new API's set (no second source)."""
    raw = _config_bytes()
    blob = base64.b64encode(zlib.compress(raw, 9)).decode()
    runtime_root = f"/tmp/fa-{TOKEN}-runtime"
    argv = ["--config", f"{runtime_root}/config.json"]
    if armed:
        argv.append("--armed")
    if override is not None:
        argv += ["--override", json.dumps(override, sort_keys=True, ensure_ascii=False)]
    return "\n".join([
        "import base64,hashlib,logging,zlib",
        "from pathlib import Path",
        "from shared_context.behavior_code_identity import code_sha256",
        f"assert code_sha256()=={core()!r}",
        f"raw=zlib.decompress(base64.b64decode({blob!r}))",
        f"assert hashlib.sha256(raw).hexdigest()=={hashlib.sha256(raw).hexdigest()!r}",
        f"root=Path({runtime_root!r}); root.mkdir(mode=0o700,exist_ok=True)",
        "(root/'config.json').write_bytes(raw)",
        "logging.basicConfig(level=logging.INFO)",
        "from shared_context.native_deploy_gate import main",
        f"raise SystemExit(main({argv!r}))",
    ])


def gate(api_ref):
    """ADR-0009 phase 3b, before sync, the API update and follow: nothing is written to the environment if it stops.
    Unarmed it only reports (a failed run is a warning); armed, a refusal or an unreadable row/definition stops."""
    if not api_ref.startswith(STAGE["registry"] + "/fa-behavior-api@sha256:"):
        raise SystemExit("api image must be a digest reference from the selected registry")
    armed, override = gate_settings()
    if override is not None:
        note = (f"compatibility gate override for release {override['release']} approved by {override['approved_by']} "
                f"at {override['approved_at']}: {override['reason']}")
        print(f"::warning::{note}")
        _gate_summary(f"OVERRIDE ({note})")
    try:
        status, name = _run_job(api_ref, _gate_script(armed, override), attempts=GATE_POLL_ATTEMPTS,
                                stop_on_timeout=True)
    except (Exception, SystemExit) as error:
        _gate_summary(f"did not run to the end (armed={armed}): {type(error).__name__}: {error}")
        if armed:
            if isinstance(error, SystemExit):
                raise
            raise SystemExit(f"compatibility gate could not run ({type(error).__name__}: {error}); nothing was "
                             "deployed. Read the sync job history before any rerun") from error
        # ADR-0009 §7: the unarmed gate never blocks a deploy, not even when its job cannot start or finish, or an
        # ARM read comes back empty or malformed (TypeError/KeyError/JSONDecodeError out of _run_job).
        print(f"::warning::compatibility gate did not run to the end while not armed "
              f"({type(error).__name__}: {error}); the deploy goes on")
        return
    where = f"read DEPLOY_COMPAT_GATE in the {STAGE['sync_job']} execution {name} log (Log Analytics)"
    decision = _gate_decision(status, armed, override)
    _gate_summary(f"job {name} {status} (armed={armed}, override={override is not None}); decision: {decision}; "
                  f"refusal count and per-Agent verdicts: {where}")
    if status == "Succeeded":
        # "finished", not "passed": unarmed, a Succeeded job may still carry decision=observe with refusals.
        print(f"compatibility gate {name} finished (armed={armed}, override={override is not None}); "
              f"decision: {decision}; report: {where}")
        return
    if not armed:
        print(f"::warning::compatibility gate {name} {status} while not armed; the deploy goes on. {where}")
        return
    raise SystemExit(f"compatibility gate {name} {status}: a binding row records a profile this API does not support, "
                     f"or the rows/definitions could not be read; nothing was deployed. {where}. Move those Agents "
                     "first, or merge an approved one-release override (deploy/README.md#compat-gate)")


def _gate_decision(status, armed, override):
    """What the job status proves about the gate's decision. An ARM job execution carries only a status (no exit
    code, no output), so the refusal count is in the job log alone; main() exits 1 only for an armed refusal."""
    if status != "Succeeded":
        return "refuse, or the rows/definitions could not be read" if armed else f"unknown (job {status})"
    if not armed:
        return "pass or observe (refusals, if any, are only reported)"
    return "pass or override (refusals, if any, passed by the override)" if override else "pass (0 refusals)"


def _gate_summary(text):
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as stream:
            stream.write(f"- compatibility gate: {text}\n")


def _run_job(api_ref, script, *, attempts=180, stop_on_timeout=False):
    """Start the sync job once with ``script`` on ``api_ref`` and wait up to ``attempts`` polls 10 s apart;
    (final status, execution name). ``stop_on_timeout`` stops an execution that outlives the wait (the gate: the
    job's own replicaTimeout is up to 3600 s, which no deploy should wait for)."""
    job = _resource("jobs", STAGE["sync_job"])
    container = rest("GET", f"{job}?{APPS_API}")["properties"]["template"]["containers"][0]
    # The deploy-time follow publishes its "agent.environment" hint as the API identity (same UAMI as the job).
    realtime_env = [{"name": key, "value": value} for key, value in STAGE["api_environment"].items()
                    if key.startswith("FA_REALTIME_")]
    env = [item for item in container.get("env") or [] if not item["name"].startswith("FA_REALTIME_")] + realtime_env
    # One start per call; an unknown start outcome is never retried here (forward-fix, read the job history).
    started = rest("POST", f"{job}/start?{APPS_API}", {"containers": [{
        "name": container["name"], "image": api_ref, "command": ["python"], "args": ["-B", "-c", script],
        "env": env, "resources": container["resources"]}]})
    name = started["name"]
    for _ in range(attempts):
        execution = _poll(f"{job}/executions/{name}?{APPS_API}")
        status = execution["properties"]["status"]
        if status in {"Succeeded", "Failed", "Stopped", "Degraded"}:
            return status, name
        time.sleep(10)
    if not stop_on_timeout:
        raise SystemExit(f"job execution {name} did not finish; read back before any rerun")
    try:
        # Jobs - Stop Execution (Microsoft.App 2025-01-01): POST .../jobs/{job}/executions/{name}/stop, 200 or 202.
        rest("POST", f"{job}/executions/{name}/stop?{APPS_API}")
        stopped = "stop requested"
    except (Exception, SystemExit) as error:  # the timeout is the verdict; a failed stop is only reported
        stopped = f"stop failed ({type(error).__name__}), stop it by hand"
    raise SystemExit(f"job execution {name} did not finish within {attempts * 10} s; {stopped}")


def console_overlay(environment):
    """The workbench's non-secret flag overlay (stage.json console_environment). Checked in meta, before any build or
    write, and again at the console step; a bad value never leaves the API updated and the Agents not following.
    FA_CONSOLE_PREWARM is the workbench's typing prewarm (shared README#typing-prewarm), FA_NATIVE_ROUTINES_ENABLED its
    排程 settings tab and scheduled-turn reads (README#native-routines-stage), "0" or "1" like the flags."""
    allowed = {"FA_NATIVE_HITL_ENABLED", "FA_NATIVE_HITL_TTL_SECONDS", "FA_NATIVE_GROUPS_ENABLED",
               "FA_REALTIME_ENDPOINT", "FA_CONSOLE_PREWARM", "FA_NATIVE_ROUTINES_ENABLED"}

    def valid(key, value):
        if not isinstance(value, str):
            return False
        if key == "FA_NATIVE_HITL_TTL_SECONDS":
            return re.fullmatch(r"[1-9][0-9]{0,5}", value) is not None
        if key == "FA_REALTIME_ENDPOINT":
            return REALTIME_ENDPOINT.fullmatch(value) is not None
        return value in {"0", "1"}
    if (not isinstance(environment, dict) or set(environment) - allowed
            or not all(valid(key, value) for key, value in environment.items())):
        raise SystemExit("console overlay accepts only the declared non-secret HITL/groups/realtime/prewarm/routines settings")
    return environment


def _update_container(kind, name, image, suffix, guarded=None, environment=None, environment_overrides=None,
                      secret_environment=None):
    if environment_overrides is not None:
        console_overlay(environment_overrides)
    path = _resource(kind, name)
    properties = rest("GET", f"{path}?{APPS_API}")["properties"]
    template = properties["template"]
    container = template["containers"][0]
    current = {item["name"]: item for item in container.get("env") or []}
    for key, value in (guarded or {}).items():
        deployed = current.get(key, {}).get("value")
        if deployed is None or json.loads(deployed) != json.loads(value):
            raise SystemExit(f"{name}: deployed {key} differs from the repo; a configuration change is an "
                             "operator action (fresh installation is bound to it), not a CI image update")
    secrets = dict(secret_environment or {})
    if secrets:
        # ENV name -> the app's own Container Apps secret (format checked by api_secrets in meta); read back again.
        if environment is None or set(secrets) & (set(environment) | set(guarded or {})):
            raise SystemExit(f"{name}: a secret ENV needs a complete plain ENV and a distinct name")
        _require_secrets(name, properties, secrets)
    if environment is not None:
        # The repo decides the ENV; a deployed name the repo does not know is never dropped silently.
        unknown = sorted(set(current) - set(environment) - set(guarded or {}) - set(secrets))
        if unknown:
            raise SystemExit(f"{name}: deployed ENV {unknown} is not in the deployment profile; add it first")
        container["env"] = ([current[key] for key in guarded or {}]
                            + [{"name": key, "value": value} for key, value in environment.items()]
                            + [{"name": key, "secretRef": ref} for key, ref in secrets.items()])
    if environment_overrides is not None:
        if environment is not None or set(environment_overrides) & set(guarded or {}):
            raise SystemExit("a flag overlay cannot replace a guarded or complete environment")
        current.update({key: {"name": key, "value": value} for key, value in environment_overrides.items()})
        container["env"] = list(current.values())
    # Read-only fields returned by GET that a PATCH must not send back (same as the dev deploy line's update).
    container.pop("imageType", None)
    container.get("resources", {}).pop("ephemeralStorage", None)
    container["image"] = image
    template["revisionSuffix"] = suffix
    rest("PATCH", f"{path}?{APPS_API}", {"properties": {"template": template}})
    # The PATCH is a long-running operation (202): wait for this run's own revision, never read back the old one.
    expected = f"{name}--{suffix}"
    for _ in range(60):
        properties = _poll(f"{path}?{APPS_API}")["properties"]
        if properties.get("latestRevisionName") == expected:
            return expected
        if properties.get("provisioningState") == "Failed":
            raise SystemExit(f"{name}: update failed before revision {expected} was created")
        time.sleep(10)
    raise SystemExit(f"{name}: revision {expected} was not created in time; read back before any rerun")


def _suffix():
    # Unique per run attempt (a re-run must not collide with an existing revision name); the leading commit prefix
    # is what guard() reads back as the serving commit.
    return (f"{os.environ.get('GITHUB_SHA', 'local')[:7]}-r{os.environ.get('GITHUB_RUN_NUMBER', '0')}"
            f"-{os.environ.get('GITHUB_RUN_ATTEMPT', '1')}")


def api(api_ref):
    # The image and the profile's API ENV change here; the command and the installed
    # configuration (BEHAVIOR_SERVER_CONFIG_JSON) stay as the operator set them at W7.
    switch_preconditions()
    revision = _update_container("containerApps", STAGE["api_app"], api_ref, _suffix(),
                                 guarded={"BEHAVIOR_SERVER_CONFIG_JSON": _config_bytes().decode("utf-8")},
                                 environment=STAGE["api_environment"],
                                 secret_environment=api_secrets())
    _wait_revision(STAGE["api_app"], revision)
    _healthz(STAGE["api_url"] + "/healthz")
    print(f"api {revision} ready")


def console(console_ref):
    # The remote console app is created by its own work line (3ef6, Z5); until then there is nothing to update.
    exists = subprocess.run([*AZ, "resource", "show", "--ids", _resource("containerApps", STAGE["console_app"]),
                             "--query", "id", "-o", "tsv"], check=False, text=True, encoding="utf-8",
                            errors="replace", capture_output=True)
    if exists.returncode != 0:
        if "ResourceNotFound" in (exists.stderr or ""):
            print(f"console app {STAGE['console_app']} not created yet; skipped")
            return
        sys.stderr.write((exists.stderr or "")[-2000:])
        raise SystemExit("console app lookup failed")
    # A new suffix every run (3ef6); a revision that never gets ready stops the run, nothing is retried.
    revision = _update_container("containerApps", STAGE["console_app"], console_ref, _suffix(),
                                 environment_overrides=STAGE["console_environment"])
    _wait_revision(STAGE["console_app"], revision)
    print("console ready")


def feed(feed_ref):
    """The feed reader MCP app: image and its complete ENV (only FEED_MCP_PUBLIC_KEY) from the profile. Runs after
    follow, so a feed app problem never leaves the API updated and the Agents not following."""
    app, environment = feed_config() or (None, None)
    if app is None:
        raise SystemExit("feed_app is not in the deployment profile")
    revision = _update_container("containerApps", app, feed_ref, _suffix(), environment=environment)
    _wait_revision(app, revision)
    print(f"feed {revision} ready")


def _wait_revision(app, revision):
    for _ in range(90):
        value = _poll(f"{_resource('containerApps', app)}/revisions/{revision}?{APPS_API}")
        state = value["properties"].get("runningState")
        if state in {"Running", "RunningAtMaxScale"} and value["properties"].get("healthState") == "Healthy":
            return
        if state in {"Failed", "Degraded"}:
            raise SystemExit(f"{app} revision {revision} {state}")
        time.sleep(10)
    raise SystemExit(f"{app} revision {revision} not healthy in time")


def _healthz(url):
    for _ in range(30):
        try:
            with urllib.request.urlopen(url, timeout=10) as response:
                if response.status == 200:
                    return
        except OSError:
            pass
        time.sleep(10)
    raise SystemExit("API /healthz did not return 200")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", help="environment profile; defaults to FA_DEPLOY_CONFIG or the checked-in stage profile")
    parser.add_argument("--runtime-dir", type=Path,
                        help="generated runtime directory; defaults to <config>/runtime")
    parser.add_argument("command", choices=("meta", "check", "guard", "build", "gate", "sync", "api", "console", "feed",
                                            "follow"))
    parser.add_argument("args", nargs="*")
    parsed = parser.parse_args()
    global CONFIG, RUNTIME, STAGE, TOKEN
    # build only needs the registry: a new azd environment builds its images before its Agents exist (deploy/azd).
    STAGE = load_config(parsed.config, require_runtime=parsed.command != "build")
    CONFIG = config_path(parsed.config)
    TOKEN = re.sub(r"[^a-z0-9-]+", "-", STAGE["environment"].lower()).strip("-") or "environment"
    if parsed.runtime_dir is not None:
        RUNTIME = parsed.runtime_dir
    else:
        RUNTIME = config_path(parsed.config).parent / "runtime"
    command, args = parsed.command, parsed.args
    {"meta": meta, "check": check, "guard": guard, "build": build, "gate": gate, "sync": sync, "api": api, "console": console, "feed": feed,
     "follow": lambda api_ref, hosted_ref: sync(api_ref, hosted_ref, follow=True)}[command](*args)


if __name__ == "__main__":
    main()
