"""azd hooks of a new FA environment (docs/azd-new-environment.md, deploy/azd/README.md).

  python deploy/azd/fa_azd.py preprovision    read only: tools, sign-in, Team owner, model settings, subscription
  python deploy/azd/fa_azd.py postprovision   read-only ACA gate; then the Entra API and Portal apps, owner consent
  python deploy/azd/fa_azd.py deploy          first install, or the same upgrade steps as the stage CI
  python deploy/azd/fa_azd.py consent         only the owner's delegated grants (an admin with this azd environment;
                                              without one, the admin applies .azure/<env>/fa/admin-consent)
  python deploy/azd/fa_azd.py prereq          read only, before `azd up`: what this account still needs
  python deploy/azd/fa_azd.py predown         before `azd down`: the confirmed removal of FA's backup lock
  python deploy/azd/fa_azd.py postdown        after `azd down`: this environment's Entra apps and backup role

The hooks orchestrate; the work is done by the existing scripts (preflight_subscription.py, ci_deploy.py,
seed_agents.py, make_runtime.py, run_fresh_job.py, prepare_native_sessions.py, new_webiq_connection.py,
new_toolbox.py) and the existing Bicep, so stage and azd share one implementation of every step.

Safety, as in the manual flow:
- the scripts write an intent before each create/start and only read back when one exists; the hook's own ARM and
  Graph writes are idempotent (deployments by name, Graph upserts by alternate key, reads before creates); an unknown
  outcome stops the hook. Rerun with `azd hooks run <hook>` after reading back, never with another request ID;
- the empty-Table initialization needs the Team owner to answer y in this terminal (no CI, no --no-prompt); the
  written approval binds the printed plan and configuration hashes;
- the WebIQ key is typed in a hidden prompt by new_webiq_connection.py and never reaches the azd environment;
- private files (profile, runtime config, intents, plan, authorization, logs) stay in .azure/<env>/fa (git-ignored).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
APP = ROOT / "src" / "fa-shared-context-poc"
STAGE_DIR = ROOT / "deploy" / "stage"
SHARED_DIR = ROOT / "deploy" / "shared-context"
INFRA = ROOT / "infra"
REQUIREMENTS = APP / "requirements-behavior.txt"
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(SHARED_DIR))

import entra  # noqa: E402
import models  # noqa: E402
import profile as fa_profile  # noqa: E402

UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
DIGEST_REF = re.compile(r"[a-z0-9.-]+\.azurecr\.io/[a-z0-9-]+@sha256:[0-9a-f]{64}")
AZURE_CLI_APP_ID = fa_profile.AZURE_CLI_CLIENT_ID
API_COMMAND = ["-B", "-m", "shared_context.behavior_server", "serve"]
JOB_COMMAND = ["-B", "-c", "from shared_context.behavior_code_identity import code_sha256; "
                           "print('FA_W9_CORE '+code_sha256(), flush=True)"]
FIC_NAME = "fa-portal-managed-identity"
APPROVAL_MINUTES = 10  # the authorization window; the operator file allows at most 15 minutes


class Stop(SystemExit):
    """A gate did not pass. The message says what to read back or who must act; nothing is retried."""


@dataclass
class Result:
    code: int
    stdout: str
    stderr: str


Runner = Callable[..., Result]


def default_runner(args: list[str], *, env: dict[str, str] | None = None, cwd: Path | None = None,
                   capture: bool = True, timeout: int | None = None) -> Result:
    merged = {**os.environ, "PYTHONUTF8": "1", **(env or {})}
    process = subprocess.run(args, env=merged, cwd=str(cwd) if cwd else None, text=True, encoding="utf-8",
                             errors="replace", capture_output=capture, timeout=timeout, check=False)
    return Result(process.returncode, process.stdout or "", process.stderr or "")


def az_prefix(system: str = os.name) -> list[str]:
    """Windows `az` is a .cmd shim; like ci_deploy/run_fresh_job, call the CLI's own python in module mode."""
    if system == "nt":
        cli = os.environ.get("AZ_PYTHON", r"C:\Program Files\Microsoft SDKs\Azure\CLI2\python.exe")
        if Path(cli).exists():
            return [cli, "-X", "utf8", "-IBm", "azure.cli"]
    return [shutil.which("az") or "az"]


@dataclass
class Context:
    values: dict[str, str]
    run: Runner = default_runner
    prompt: Callable[[str], str] = input
    tty: bool = field(default_factory=lambda: sys.stdin.isatty())
    python: str = sys.executable
    az_cmd: list[str] = field(default_factory=az_prefix)
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
    state_root: Path = ROOT / ".azure"  # azd's own git-ignored environment folder
    resume: str = "the same azd command"  # main() names the command that asks the question again

    def ask(self, question: str) -> str:
        """Every question goes through here: no answer (stdin closed, or Ctrl+C) is a stop, never a traceback. The
        2026-10-21 azd e2e run saw EOFError while input() waited and azd had already exited; Ctrl+C in that terminal
        is the likely cause, not confirmed. Each caller asks before it writes anything."""
        try:
            return self.prompt(question)
        except (EOFError, KeyboardInterrupt):
            print(flush=True)  # end the unanswered prompt's line
            raise Stop(f"no answer was read (input closed or interrupted): nothing was approved and no answer was "
                       f"saved. Run `{self.resume}` in a terminal to answer it again") from None

    # ---- azd environment -------------------------------------------------------------------------------------
    def get(self, key: str, default: str | None = None) -> str | None:
        value = self.values.get(key)
        return value if value not in (None, "") else default

    def need(self, key: str) -> str:
        value = self.get(key)
        if value is None:
            raise Stop(f"azd environment value {key} is missing; run `azd provision` first")
        return value

    def set(self, key: str, value: str) -> None:
        if self.values.get(key) == value:
            return
        result = self.run(["azd", "env", "set", key, value])
        if result.code != 0:
            raise Stop(f"azd env set {key} failed: {result.stderr.strip()[:300]}")
        self.values[key] = value

    @property
    def env(self) -> str:
        name = self.need("AZURE_ENV_NAME").lower()
        if fa_profile.ENVIRONMENT.fullmatch(name) is None:
            raise Stop("AZURE_ENV_NAME must be 2-20 lowercase letters, digits or hyphens (it prefixes resource names)")
        return name

    @property
    def work(self) -> Path:
        path = self.state_root / self.need("AZURE_ENV_NAME") / "fa"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def profile(self) -> Path:
        return self.work / "profile.json"

    @property
    def group(self) -> str:
        return self.get("AZURE_RESOURCE_GROUP") or f"rg-{self.env}"

    # ---- tools -----------------------------------------------------------------------------------------------
    def az(self, *args: str, missing_ok: bool = False) -> Any:
        result = self.run([*self.az_cmd, *args, "-o", "json"])
        if result.code != 0:
            if missing_ok and any(marker in result.stderr for marker in ("ResourceNotFound", "ResourceGroupNotFound",
                                                                          "was not found", "could not be found",
                                                                          "RoleDefinitionDoesNotExist")):
                return None
            raise Stop(f"az {' '.join(args[:3])} failed: {_first_error(result.stderr)}")
        return json.loads(result.stdout) if result.stdout.strip() else None

    def script(self, path: Path, *args: str, env: dict[str, str] | None = None, capture: bool = False,
               check: bool = True, module: bool = False) -> Result:
        target = ["-m", str(path)] if module else [str(path)]
        base = {"PYTHONPATH": str(APP), "PYTHONDONTWRITEBYTECODE": "1"}
        profile = self.state_root / self.get("AZURE_ENV_NAME", "-") / "fa" / "profile.json"  # no mkdir here
        if profile.exists():
            base["FA_DEPLOY_CONFIG"] = str(profile)  # the scripts' import-time default, never deploy/stage/stage.json
        result = self.run([self.python, "-X", "utf8", *target, *args], capture=capture, cwd=ROOT,
                          env={**base, **(env or {})})
        if check and result.code != 0:
            label = path if module else Path(path).name
            raise Stop(f"{label} {' '.join(args[:1])} failed (exit {result.code}); its output says what to read back"
                       + (f": {_first_error(result.stderr)}" if capture else ""))
        return result

    def graph(self, method: str, path: str, body: Any, headers: dict[str, str]) -> Any:
        url = "https://graph.microsoft.com/v1.0" + urllib.parse.quote(path, safe="/()'=$?&,:-._~@")
        args = [*self.az_cmd, "rest", "--method", method, "--url", url, "--resource", "https://graph.microsoft.com"]
        body_file = None
        try:
            if body is not None:
                handle, name = tempfile.mkstemp(prefix="fa-graph-", suffix=".json")
                with os.fdopen(handle, "w", encoding="utf-8") as stream:
                    json.dump(body, stream)
                body_file = Path(name)
                args += ["--body", f"@{body_file}"]
            pairs = {"Content-Type": "application/json", **headers} if body is not None else dict(headers)
            if pairs:
                args += ["--headers", *[f"{key}={value}" for key, value in pairs.items()]]
            result = self.run([*args, "-o", "json"])
        finally:
            if body_file is not None:
                body_file.unlink(missing_ok=True)
        if result.code != 0:
            if any(marker in result.stderr for marker in ("Forbidden", "Authorization_RequestDenied",
                                                          "Insufficient privileges")):
                raise entra.GraphForbidden(_first_error(result.stderr))
            if method == "GET" and any(marker in result.stderr for marker in ("Request_ResourceNotFound", "NotFound")):
                raise LookupError(_first_error(result.stderr))
            raise Stop(f"Graph {method} {path.split('?')[0]} failed: {_first_error(result.stderr)}")
        return json.loads(result.stdout) if result.stdout.strip() else None


def _first_error(stderr: str) -> str:
    return next((line.strip() for line in stderr.splitlines() if line.strip() and "WARNING" not in line), "")[:300]


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


def _write_x(path: Path, value: Any) -> None:
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, indent=1)
        stream.flush()
        os.fsync(stream.fileno())


# ---- preprovision ------------------------------------------------------------------------------------------------

def signed_in_user(ctx: Context) -> dict[str, str]:
    me = ctx.graph("GET", "/me?$select=id,userPrincipalName", None, {}) or {}
    if not isinstance(me.get("id"), str) or UUID.fullmatch(me["id"]) is None:
        raise Stop("the Azure CLI is not signed in as a user (Graph /me); this path deploys as a person, not a "
                   "service principal: `az login --tenant <tenant>`")
    return {"oid": me["id"], "upn": me.get("userPrincipalName") or ""}


def check_account(ctx: Context) -> dict[str, Any]:
    subscription = ctx.need("AZURE_SUBSCRIPTION_ID")
    account = ctx.az("account", "show", "--subscription", subscription) or {}
    if account.get("id") != subscription:
        raise Stop(f"the Azure CLI cannot use subscription {subscription}; `az login` with the same account as "
                   "`azd auth login`")
    tenant = ctx.get("AZURE_TENANT_ID")
    if tenant and tenant != account.get("tenantId"):
        raise Stop(f"az is signed in to tenant {account.get('tenantId')}, azd to {tenant}; use one tenant")
    return account


def model_deployments(ctx: Context, chosen: bool = True) -> list[dict[str, Any]]:
    """The Foundry deployments main.bicep creates: the catalog entries named in FA_MODEL_DEPLOYMENTS (every entry when
    `chosen` is False or nothing is chosen yet), with FA_MODEL_CAPACITY_JSON applied."""
    from prepare_environment import _model_deployments
    raw = ctx.get("FA_MODEL_CAPACITY_JSON", "")
    try:
        capacity = json.loads(raw) if raw else {}
    except ValueError:
        raise Stop("FA_MODEL_CAPACITY_JSON must be a JSON object such as {\"gpt-5.4-mini\": 50}") from None
    option = ctx.get("FA_MODEL_VERSION_UPGRADE_OPTION", "NoAutoUpgrade")
    every = _model_deployments({"model_capacity": capacity, "model_version_upgrade_option": option})
    names = models.names(ctx.get("FA_MODEL_DEPLOYMENTS")) if chosen else ()
    return [item for item in every if not names or item["name"] in names]


def region_offer(ctx: Context) -> list[dict[str, Any]]:
    """Read-only: for each catalog chat model, whether this region offers its version and SKU and how much of the
    subscription's regional quota is free (shared by every account in the subscription and region)."""
    from preflight_subscription import usage_names
    scope = ("--location", ctx.need("AZURE_LOCATION"), "--subscription", ctx.need("AZURE_SUBSCRIPTION_ID"))
    offered = {(item["model"]["name"], item["model"]["version"], sku["name"])
               for item in ctx.az("cognitiveservices", "model", "list", *scope) or []
               if item.get("kind") == "AIServices" for sku in item["model"].get("skus") or []}
    usage = {item["name"]["value"]: item for item in ctx.az("cognitiveservices", "usage", "list", *scope) or []}
    chat = set(models.chat_names(models.catalog()))
    rows = []
    for item in model_deployments(ctx, chosen=False):
        if item["name"] not in chat:
            continue
        model, sku, needed = item["model"]["name"], item["sku"]["name"], int(item["sku"]["capacity"])
        entry = next((usage[key] for key in usage_names(model, sku) if key in usage), None)
        free = None if entry is None else int(entry["limit"] - entry["currentValue"])
        available = (model, item["model"]["version"], sku) in offered
        rows.append({"name": item["name"], "offered": available, "free": free, "needed": needed,
                     "fits": available and free is not None and free >= needed})
    return rows


def pick_models(ctx: Context) -> None:
    """First run in a terminal without FA_MODELS: the person chooses from what this region can run."""
    rows = region_offer(ctx)
    print(f"FA: catalog chat models in {ctx.need('AZURE_LOCATION')} (GlobalStandard; FA_MODEL_CAPACITY_JSON lowers "
          "the capacity a model needs)", flush=True)
    for number, row in enumerate(rows, 1):
        state = ("not offered in this region" if not row["offered"] else
                 "no quota entry" if row["free"] is None else
                 # Usage above the limit (e.g. after a quota cut) would read as a negative free count.
                 f"quota free {max(row['free'], 0)}{'' if row['free'] >= 0 else ' (over the limit)'}, "
                 f"needs {row['needed']}" + ("" if row["fits"] else " (not enough)"))
        print(f"  {number}. {row['name']}: {state}", flush=True)
    suggested = ",".join(str(number) for number, row in enumerate(rows, 1) if row["fits"])
    if not suggested:
        raise Stop(f"no catalog chat model fits {ctx.need('AZURE_LOCATION')} with this subscription's quota; lower "
                   "the capacity with FA_MODEL_CAPACITY_JSON, ask for quota, or create the environment in another "
                   "region")
    answer = ctx.ask(f"Models to deploy (numbers separated by commas) [{suggested}]: ").strip() or suggested
    parts = [part.strip() for part in answer.split(",") if part.strip()]
    if not parts or not all(part.isdigit() and 1 <= int(part) <= len(rows) for part in parts):
        raise Stop(f"'{answer}' is not a list of the numbers above; nothing was changed")
    picked = [rows[int(part) - 1] for part in dict.fromkeys(parts)]
    unoffered = [row["name"] for row in picked if not row["offered"]]
    if unoffered:
        raise Stop(f"{', '.join(unoffered)} is not offered in {ctx.need('AZURE_LOCATION')}; nothing was changed")
    chosen = [row["name"] for row in picked]
    default = models.default_for(tuple(chosen))
    if len(chosen) > 1:
        listed = ", ".join(f"{number}. {name}" for number, name in enumerate(chosen, 1))
        answer = ctx.ask(f"Default model for new conversations ({listed}) "
                         f"[{chosen.index(default) + 1}]: ").strip()
        if answer:
            if not answer.isdigit() or not 1 <= int(answer) <= len(chosen):
                raise Stop(f"'{answer}' is not one of the numbers above; nothing was changed")
            default = chosen[int(answer) - 1]
    ctx.set("FA_MODELS", ",".join(chosen))
    ctx.set("FA_DEFAULT_MODEL", default)


def choose_models(ctx: Context) -> models.Choice:
    """One decision for the deployments, the Portal menu and default, and the memory store (models.py)."""
    if ctx.get("FA_MODELS") is None and ctx.tty and not models.before_choice(ctx.values):
        pick_models(ctx)
    try:
        choice = models.parse(ctx.values, models.catalog())
    except SystemExit as error:
        raise Stop(str(error)) from None
    for key, value in choice.environment().items():
        ctx.set(key, value)
    print(f"FA: models {', '.join(choice.models)}; default {choice.default}"
          + (f"; memory store chat model {choice.memory_chat_model}" if choice.memory_chat_model else ""), flush=True)
    return choice


def preflight_document(ctx: Context, tenant: str, deployments: list[dict[str, Any]]) -> Path:
    """The deployment.json shape preflight_subscription.py reads. Before the first provision the Foundry account has
    no name yet; a name that does not exist reads as 'nothing deployed' (the script's missing_ok)."""
    document = {
        "tenant_id": tenant, "subscription_id": ctx.need("AZURE_SUBSCRIPTION_ID"),
        "location": ctx.need("AZURE_LOCATION"), "resource_group": ctx.group, "foundry_resource_group": ctx.group,
        "names": {"foundry_account": ctx.get("FA_FOUNDRY_ACCOUNT", "not-yet-provisioned"),
                  "container_apps_environment": ctx.get("FA_CONTAINER_APPS_ENVIRONMENT", f"cae-{ctx.env}")},
        "model_deployments": deployments,
    }
    path = ctx.work / "preflight-deployment.json"
    _write_json(path, document)
    return path


# Built-in Entra role template IDs (the same in every tenant).
CONSENT_ROLES = {"62e90394-69f5-4237-9190-012177145e10": "Global Administrator",
                 "9b895d92-2cd3-44c7-9d02-a6ac2d5ea5c3": "Application Administrator",
                 "158c047a-c907-4556-b7ef-446551a6b5f7": "Cloud Application Administrator"}
APP_DEVELOPER = "cf1c38e5-3621-4004-a7cb-879624dced7c"


def _check(rows: list[dict[str, str]], item: str, state: str, detail: str) -> None:
    rows.append({"item": item, "state": state, "detail": detail})


def prerequisites(ctx: Context, me: dict[str, str]) -> list[dict[str, str]]:
    """Read-only list of what this deployer has and what someone else must do first (deploy/azd/README.md
    "事前準備"). state: ok, blocked (stop before anything is billed), admin (an admin step later), unknown."""
    rows: list[dict[str, str]] = []
    _check(rows, "Python >= 3.11", "ok" if sys.version_info >= (3, 11) else "blocked", sys.version.split()[0])
    for tool in ("git", "docker", "azd"):
        _check(rows, tool, "ok" if shutil.which(tool) else "blocked", "on PATH" if shutil.which(tool) else "missing")
    if shutil.which("docker"):
        running = ctx.run(["docker", "info", "--format", "{{.ServerVersion}}"])
        _check(rows, "Docker engine running", "ok" if running.code == 0 else "blocked",
               "azd deploy builds the images locally" if running.code else running.stdout.strip())
    if os.name == "nt":
        _check(rows, "PowerShell 7 (pwsh)", "ok" if shutil.which("pwsh") else "blocked", "Windows hooks use pwsh")

    scope = f"/subscriptions/{ctx.need('AZURE_SUBSCRIPTION_ID')}"
    try:
        roles = set(ctx.az("role", "assignment", "list", "--assignee", me["oid"], "--scope", scope,
                           "--include-inherited", "--include-groups", "--query", "[].roleDefinitionName") or [])
        enough = "Owner" in roles or {"Contributor", "User Access Administrator"} <= roles
        _check(rows, "Subscription: Owner, or Contributor + User Access Administrator", "ok" if enough else "blocked",
               ", ".join(sorted(roles)) or "no role at subscription scope")
    except Stop as error:
        _check(rows, "Subscription roles", "unknown", str(error))

    try:
        listed = ctx.graph("GET", "/me/transitiveMemberOf/microsoft.graph.directoryRole?$select=roleTemplateId",
                           None, {}) or {}
        directory = {item.get("roleTemplateId") for item in listed.get("value", [])}
    except (entra.GraphForbidden, LookupError, Stop):
        directory = None
    granter = directory is not None and bool(directory & set(CONSENT_ROLES))
    if directory is not None and (granter or APP_DEVELOPER in directory):
        _check(rows, "Entra: register applications", "ok", "directory role allows it")
    else:
        try:
            policy = ctx.graph("GET", "/policies/authorizationPolicy?$select=defaultUserRolePermissions", None, {})
            allowed = ((policy or {}).get("defaultUserRolePermissions") or {}).get("allowedToCreateApps")
            _check(rows, "Entra: register applications", "ok" if allowed else "blocked",
                   "allowed for all users" if allowed else "tenant blocks it: ask for the Application Developer role")
        except (entra.GraphForbidden, LookupError, Stop):
            _check(rows, "Entra: register applications", "unknown",
                   "cannot read the tenant policy; postprovision stops if registration is refused")
    try:
        ctx.graph("GET", f"/servicePrincipals(appId='{AZURE_CLI_APP_ID}')?$select=id", None, {})
        _check(rows, "Entra: Azure CLI service principal", "ok", "present")
    except LookupError:
        _check(rows, "Entra: Azure CLI service principal", "ok" if granter else "blocked",
               "missing; postprovision creates it with this account's admin role" if granter else
               f"missing; ask an admin to run `az ad sp create --id {AZURE_CLI_APP_ID}` before `azd up`")
    except (entra.GraphForbidden, Stop):
        _check(rows, "Entra: Azure CLI service principal", "unknown", "cannot read it")
    if granter:
        _check(rows, "Entra: owner consent (3 delegated grants)", "ok", "this account can grant them")
    else:
        _check(rows, "Entra: owner consent (3 delegated grants)", "admin" if directory is not None else "unknown",
               "after postprovision an admin (Cloud Application Administrator or higher) applies the files "
               "written to .azure/<env>/fa/admin-consent; the deploy pauses before the first session until then")
    return rows


def preprovision(ctx: Context) -> None:
    if sys.version_info < (3, 11):
        raise Stop("Python 3.11 or newer is required")
    ctx.env  # validates the name before anything is created from it
    for key in ("AZURE_SUBSCRIPTION_ID", "AZURE_LOCATION"):
        if ctx.get(key) is None:
            raise Stop(f"{key} is not set; create the environment with "
                       "`azd env new <name> --subscription <id> --location <region>` (or `azd env set`) first")
    account = check_account(ctx)
    me = signed_in_user(ctx)
    owner = ctx.get("FA_TEAM_OWNER_OID")
    if owner is None:
        owner = me["oid"]
        print(f"FA: Team owner = the signed-in deployer {me['upn']} ({me['oid']})", flush=True)
        if ctx.tty:
            answer = ctx.ask(f"Is {me['upn']} the Team owner who will sign in to the Portal? [y/N]: ").strip().lower()
            if answer not in ("y", "yes"):
                raise Stop("the Team owner is not the signed-in deployer. Run `azd env set FA_TEAM_OWNER_OID "
                           "<owner object ID>` and rerun; nothing was written")
    if UUID.fullmatch(owner) is None:
        raise Stop("FA_TEAM_OWNER_OID must be the object ID of the one person who owns this Team")
    ctx.set("FA_TEAM_OWNER_OID", owner.lower())
    print(f"FA: deployer {me['upn']} ({me['oid']}); Team owner {owner}", flush=True)
    rows = prerequisites(ctx, me)
    for row in rows:
        print(f"FA: prerequisite [{row['state']}] {row['item']}: {row['detail']}", flush=True)
    blocked = [row["item"] for row in rows if row["state"] == "blocked"]
    if blocked:
        raise Stop("prerequisites missing before anything is billed: " + "; ".join(blocked)
                   + ". See deploy/azd/README.md 事前準備")
    choose_models(ctx)
    deployments = model_deployments(ctx)
    document = preflight_document(ctx, account["tenantId"], deployments)
    result = ctx.script(SHARED_DIR / "preflight_subscription.py", "subscription", "--deployment", str(document),
                        *(_az_flag(ctx)), capture=True, check=False)
    print(result.stdout, flush=True)
    if result.code != 0:
        raise Stop("subscription preflight failed (read-only). Registering a feature/provider or asking for quota "
                   "changes the subscription: do it yourself, then rerun `azd provision`. See "
                   "deploy/README.md#new-environment-provider-precheck")


def _az_flag(ctx: Context) -> list[str]:
    return ["--az", ctx.az_cmd[0]] if ctx.az_cmd and Path(ctx.az_cmd[0]).name.lower().startswith("python") else []


# ---- postprovision -----------------------------------------------------------------------------------------------

def aca_ready(ctx: Context, tenant: str) -> str:
    document = preflight_document(ctx, tenant, [])
    result = ctx.script(SHARED_DIR / "preflight_subscription.py", "aca-ready", "--deployment", str(document),
                        *(_az_flag(ctx)), capture=True, check=False)
    print(result.stdout, flush=True)
    if result.code != 0:
        raise Stop("Container Apps platform is not ready (no staticIp or platform network). Do not deploy apps: "
                   "see deploy/README.md section 'ACA 平台就緒' (B1-B3) before anything else")
    environment = ctx.az("containerapp", "env", "show", "--resource-group", ctx.group, "--name",
                         ctx.need("FA_CONTAINER_APPS_ENVIRONMENT"), "--subscription", ctx.need("AZURE_SUBSCRIPTION_ID"))
    domain = ((environment or {}).get("properties") or {}).get("defaultDomain")
    if not isinstance(domain, str) or not domain:
        raise Stop("the Container Apps environment reports no defaultDomain")
    return domain


def entra_apps(ctx: Context, tenant: str, domain: str) -> None:
    keys = entra.names(ctx.env, tenant, ctx.need("AZURE_SUBSCRIPTION_ID"), ctx.group)
    api = entra.ensure_api(ctx.graph, ctx.env, keys["api_unique_name"], keys["api_audience"])
    entra.ensure_service_principal(ctx.graph, AZURE_CLI_APP_ID)
    redirect = f"https://{ctx.need('FA_CONSOLE_APP')}.{domain}/.auth/login/aad/callback"
    console = entra.ensure_console(ctx.graph, ctx.env, keys["console_unique_name"], api["app_id"],
                                   keys["api_unique_name"], redirect)
    for key, value in (("FA_API_AUDIENCE", keys["api_audience"]), ("FA_API_APP_ID", api["app_id"]),
                       ("FA_CONSOLE_APP_ID", console["app_id"]), ("FA_CONSOLE_APP_OBJECT_ID", console["object_id"]),
                       ("FA_API_URL", f"https://{ctx.need('FA_API_APP')}.{domain}"),
                       ("FA_CONSOLE_URL", f"https://{ctx.need('FA_CONSOLE_APP')}.{domain}")):
        ctx.set(key, value)


def consent(ctx: Context) -> bool:
    """The owner's three Principal grants. False (and instructions) when the signed-in account may not grant."""
    owner = ctx.need("FA_TEAM_OWNER_OID")
    api_sp = entra.ensure_service_principal(ctx.graph, ctx.need("FA_API_APP_ID"))["id"]
    cli_sp = entra.ensure_service_principal(ctx.graph, AZURE_CLI_APP_ID)["id"]
    console_sp = entra.ensure_service_principal(ctx.graph, ctx.need("FA_CONSOLE_APP_ID"))["id"]
    graph_sp = entra.ensure_service_principal(ctx.graph, entra.GRAPH_APP_ID)["id"]
    grants = ((cli_sp, api_sp, entra.SCOPES), (console_sp, api_sp, entra.SCOPES),
              (console_sp, graph_sp, entra.SIGN_IN_SCOPES))
    try:
        for client, resource, scopes in grants:
            print(f"FA: owner grant {' '.join(scopes)}: {entra.ensure_grant(ctx.graph, client, resource, owner, scopes)}",
                  flush=True)
    except entra.ConsentRequired:
        folder = admin_consent_files(ctx, owner, grants)
        print("FA: this account cannot grant delegated permissions (an admin role such as Cloud Application "
              f"Administrator can). Send the folder {folder} to an admin; its README.txt has the three commands. "
              "Deployment continues; it pauses before the first session until the grants exist.", flush=True)
        ctx.set("FA_CONSENT_PENDING", "true")
        return False
    ctx.set("FA_CONSENT_PENDING", "false")
    return True


def admin_consent_files(ctx: Context, owner: str, grants: Any) -> Path:
    """Self-contained hand-off for an admin without this repo or azd: one Graph body per grant plus the commands.
    Object IDs only (no secret). Each grant is for the one Team owner (consentType Principal), never tenant-wide."""
    folder = ctx.work / "admin-consent"
    folder.mkdir(exist_ok=True)
    lines = [f"FA environment {ctx.env}: delegated grants for the Team owner {owner} only (consentType=Principal).",
             "Run as an admin (Cloud Application Administrator, Application Administrator or Global Administrator)",
             f"signed in to tenant {ctx.get('AZURE_TENANT_ID', '<tenant>')} with `az login`, from this folder:", ""]
    for index, (client, resource, scopes) in enumerate(grants, start=1):
        name = f"grant-{index}.json"
        _write_json(folder / name, {"clientId": client, "consentType": "Principal", "principalId": owner,
                                    "resourceId": resource, "scope": " ".join(scopes)})
        lines.append(f"az rest --method POST --url https://graph.microsoft.com/v1.0/oauth2PermissionGrants "
                     f"--body @{name} --headers Content-Type=application/json")
    lines += ["", "A 409 Conflict means that grant already exists. Afterwards the deployer runs "
                  "`azd hooks run postdeploy` (it reads the grants back before continuing)."]
    (folder / "README.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return folder


def postprovision(ctx: Context) -> None:
    account = check_account(ctx)
    ctx.set("AZURE_TENANT_ID", account["tenantId"])  # profile, consent and the Portal credential use it
    domain = aca_ready(ctx, account["tenantId"])
    entra_apps(ctx, account["tenantId"], domain)
    consent(ctx)


# ---- deploy ------------------------------------------------------------------------------------------------------

def git_commit() -> str:
    result = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True, check=False)
    commit = result.stdout.strip()
    if result.returncode != 0 or re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        raise Stop("azd deploy runs from a git checkout: the revision suffix records the serving commit")
    return commit


def code_identity() -> str:
    sys.path.insert(0, str(APP))
    from shared_context.behavior_code_identity import code_sha256
    return code_sha256()


def build_env(ctx: Context, commit: str) -> dict[str, str]:
    """ci_deploy.py names image tags and revision suffixes from the CI's commit and run counters; azd supplies the
    same values (the commit and a per-environment run counter) so guard() can read the serving commit back."""
    values = {"GITHUB_SHA": commit, "GITHUB_RUN_NUMBER": ctx.need("FA_DEPLOY_RUN"), "GITHUB_RUN_ATTEMPT": "1",
              "FA_DEPLOY_CONFIG": str(ctx.profile)}
    if ctx.get("FA_ALLOW_ROLLBACK") == "true":  # one run only: guard() warns instead of stopping (see the README)
        values["FA_ALLOW_ROLLBACK"] = "true"
    return values


def write_profile(ctx: Context, *, require_runtime: bool) -> Path:
    path = ctx.profile
    values = {**ctx.values, "FA_ENVIRONMENT": ctx.env}
    fa_profile.write(path, fa_profile.build(values, require_runtime=require_runtime))
    return path


# The three images azd builds (ci_deploy.IMAGES: api, hosted, console), each from APP as the Docker context.
IMAGE_DOCKERFILES = ("Dockerfile.behavior-api", "Dockerfile.behavior-assistant-desktop", "Dockerfile.web-console")


def build_input_paths() -> list[str]:
    """Paths under APP (relative, POSIX) that end up in an image: each Dockerfile, .dockerignore and every COPY
    source from the build context (`COPY --from=` copies from another stage, not from the working tree)."""
    paths = {".dockerignore", *IMAGE_DOCKERFILES}
    for name in IMAGE_DOCKERFILES:
        for line in (APP / name).read_text(encoding="utf-8").splitlines():
            words = line.split()
            if not words or words[0].upper() != "COPY" or any(word.startswith("--from") for word in words):
                continue
            sources = [word for word in words[1:] if not word.startswith("--")][:-1]  # the last word is the target
            paths.update(source.rstrip("/") for source in sources)
    return sorted(paths)


def build_input_changes(ctx: Context) -> list[str]:
    """Uncommitted (incl. untracked) files among the image build inputs: every image is built from APP's working
    tree, while the image cache, tags and revision suffix are named after the commit. Other files under APP (docs,
    READMEs) never reach an image, so editing them does not block a deploy."""
    prefix = APP.relative_to(ROOT).as_posix()
    result = ctx.run(["git", "-C", str(ROOT), "status", "--porcelain", "--untracked-files=all", "--",
                      *(f"{prefix}/{path}" for path in build_input_paths())])
    if result.code != 0:
        raise Stop(f"git status failed: {result.stderr.strip()[:300]}")
    return [line[3:] for line in result.stdout.splitlines() if line.strip()]


def images(ctx: Context, commit: str, core: str) -> dict[str, str]:
    changes = build_input_changes(ctx)
    if changes and ctx.get("FA_ALLOW_DIRTY_BUILD") != "true":
        raise Stop(f"{len(changes)} uncommitted change(s) in the image build inputs (first: {changes[0]}): the images "
                   f"would be built from the working tree but recorded as commit {commit[:12]}. Commit them, or "
                   "set FA_ALLOW_DIRTY_BUILD=true for this run to build the working tree as is (never reused later)")
    if changes:
        print(f"FA: FA_ALLOW_DIRTY_BUILD=true: building {len(changes)} uncommitted change(s); these images are not "
              "reused by a later deploy", flush=True)
    elif ctx.get("FA_IMAGES_CORE") == core and ctx.get("FA_IMAGES_COMMIT") == commit:
        found = {kind: ctx.get(f"FA_{kind.upper()}_IMAGE") for kind in ("api", "hosted", "console")}
        if all(found.values()):
            return found  # type: ignore[return-value]
    result = {}
    for kind in ("api", "hosted", "console"):
        print(f"FA: building the {kind} image (Docker, linux/amd64)", flush=True)
        output = ctx.script(STAGE_DIR / "ci_deploy.py", "--config", str(ctx.profile), "build", kind,
                            env=build_env(ctx, commit), capture=True)
        reference = (output.stdout.strip().splitlines() or [""])[-1]
        if DIGEST_REF.fullmatch(reference) is None or not reference.startswith(ctx.need("FA_REGISTRY") + "/"):
            raise Stop(f"{kind} build printed no digest reference of {ctx.need('FA_REGISTRY')}")
        result[kind] = reference
        ctx.set(f"FA_{kind.upper()}_IMAGE", reference)
    ctx.set("FA_IMAGES_CORE", core)
    # A working-tree build is recorded with a marker, so a later clean run of the same commit never reuses it.
    ctx.set("FA_IMAGES_COMMIT", f"{commit}+dirty" if changes else commit)
    return result


def seed(ctx: Context, hosted: str, core: str) -> None:
    out = ctx.work / "seed"
    ctx.script(STAGE_DIR / "seed_agents.py", "--config", str(ctx.profile), "--out", str(out),
               "--image", hosted, "--core", core)
    results = {item["agent"]: item for item in json.loads((out / "seed-result.json").read_text(encoding="utf-8"))
               if "version" in item}
    for key, name, _ in fa_profile.agent_names({"FA_ENVIRONMENT": ctx.env}):
        item = results.get(name) or {}
        guid, principal = item.get("agent_guid"), item.get("principal_id")
        if item.get("status") != "active" or not (isinstance(guid, str) and isinstance(principal, str)):
            raise Stop(f"{name}: the seed version is not active with an identity; read it back, do not seed again")
        ctx.set(f"FA_AGENT_{key}_ID", guid)
        ctx.set(f"FA_AGENT_{key}_OID", principal)


def deploy_bicep(ctx: Context, name: str, template: Path, parameters: dict[str, Any]) -> dict[str, Any]:
    path = ctx.work / "deployments" / f"{name}.parameters.json"
    _write_json(path, {"$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentParameters.json#",
                       "contentVersion": "1.0.0.0",
                       "parameters": {key: {"value": value} for key, value in parameters.items()}})
    result = ctx.az("deployment", "group", "create", "--subscription", ctx.need("AZURE_SUBSCRIPTION_ID"),
                    "--resource-group", ctx.group, "--name", name, "--template-file", str(template),
                    "--parameters", f"@{path}")
    state = ((result or {}).get("properties") or {}).get("provisioningState")
    if state != "Succeeded":
        raise Stop(f"deployment {name} is {state}; read it back in the resource group before any rerun")
    return {key: value.get("value") for key, value in ((result["properties"].get("outputs") or {}).items())}


def agent_roles(ctx: Context) -> None:
    for key, name, _ in fa_profile.agent_names({"FA_ENVIRONMENT": ctx.env}):
        deploy_bicep(ctx, f"fa-{ctx.env}-access-{name[-1]}", INFRA / "shared-context" / "project-service-access.bicep",
                     {"accountName": ctx.need("FA_FOUNDRY_ACCOUNT"), "projectName": ctx.need("FA_PROJECT"),
                      "principalId": ctx.need(f"FA_AGENT_{key}_OID")})


def runtime(ctx: Context) -> Path:
    out = ctx.work / "runtime"
    ctx.script(STAGE_DIR / "make_runtime.py", "--config", str(ctx.profile), "--out", str(out))
    return out


def digest_of(reference: str) -> str:
    return reference.rpartition("@sha256:")[2]


def revision_suffix(ctx: Context, commit: str, attempt: int) -> str:
    """ci_deploy._suffix's shape: guard() reads the serving commit from its first part."""
    return f"{commit[:7]}-r{ctx.need('FA_DEPLOY_RUN')}-{attempt}"


def tags(ctx: Context) -> dict[str, str]:
    return {"azd-env-name": ctx.need("AZURE_ENV_NAME"), "workload": "foundry-agents", "environment": ctx.env}


def sync_job(ctx: Context, api_image: str) -> None:
    deploy_bicep(ctx, f"fa-{ctx.env}-job", INFRA / "shared-context" / "behavior-job.bicep", {
        "location": ctx.need("AZURE_LOCATION"), "jobName": ctx.need("FA_SYNC_JOB"),
        "environmentName": ctx.need("FA_CONTAINER_APPS_ENVIRONMENT"), "identityName": ctx.need("FA_API_IDENTITY"),
        "registryName": ctx.need("FA_REGISTRY_NAME"), "imageRepository": "fa-behavior-api",
        "imageSha256": digest_of(api_image), "commandArguments": JOB_COMMAND,
        "environmentVariables": [{"name": "AZURE_CLIENT_ID", "value": ctx.need("FA_API_IDENTITY_CLIENT_ID")},
                                 {"name": "PYTHONUTF8", "value": "1"},
                                 {"name": "PYTHONDONTWRITEBYTECODE", "value": "1"}],
        "timeoutSeconds": 1800, "containerResources": {"cpu": "0.5", "memory": "1Gi"}, "tags": tags(ctx)})


def _fresh(ctx: Context, action: str, api_image: str, runtime_dir: Path, work: Path) -> list[str]:
    """Run one run_fresh_job.py action once (its intent stops a second start) and return the saved log lines."""
    execution = work / f"{action}-execution.txt"
    common = ["--config", str(ctx.profile), "--runtime-dir", str(runtime_dir), "--work", str(work)]
    if not execution.exists():
        if (work / f"{action}-intent.json").exists():
            raise Stop(f"fresh {action} was started with an unknown outcome; list the Job executions and read the "
                       f"log with run_fresh_job.py logs --execution <name>; it is never started twice")
        ctx.script(STAGE_DIR / "run_fresh_job.py", *common, action, "--image", api_image)
    name = execution.read_text(encoding="utf-8").strip()
    log = work / f"{name}-log.txt"
    if not log.exists():
        ctx.script(STAGE_DIR / "run_fresh_job.py", *common, "logs", "--execution", name)
    return [line.strip() for line in log.read_text(encoding="utf-8").splitlines()]


def _result_line(lines: list[str]) -> dict[str, Any]:
    for line in lines:
        if line.startswith("{"):
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict) and "receipt_sha256" in value:
                return value
    return {}


def authorize(ctx: Context, plan: Any, path: Path) -> None:
    """The owner's approval of this exact plan, typed in this terminal. Never in CI or without a terminal."""
    owner = ctx.need("FA_TEAM_OWNER_OID")
    if not ctx.tty:
        raise Stop("the empty-Table initialization needs the Team owner's typed approval; run "
                   "`azd hooks run postdeploy` in an interactive terminal")
    if signed_in_user(ctx)["oid"].lower() != owner.lower():
        raise Stop("only the Team owner approves the initialization: the owner signs in to az and runs "
                   "`azd hooks run postdeploy`")
    print("\nFA empty-Table initialization (writes the first records; never resets or deletes data)\n"
          f"  request {plan.request_id}, installation {plan.installation_id}\n"
          f"  configuration {plan.configuration_sha256}\n  plan {plan.sha256}\n"
          f"  empty Table observed at {plan.empty_table_observed_at.isoformat()}\n", flush=True)
    answer = ctx.ask("Approve this initialization of the plan above? [y/N]: ").strip().lower()
    if answer not in ("y", "yes"):
        raise Stop("not approved; nothing was written. Rerun `azd hooks run postdeploy` to decide again")
    not_before = max(ctx.now(), plan.empty_table_observed_at)
    _write_x(path, {
        "schema_version": "fa-fresh-installation-authorization-v1",
        "purpose": "initialize-empty-native-authority", "approval_source": "operator-approved-file",
        "decision_id": f"{ctx.env}-fresh-{not_before.strftime('%Y%m%dT%H%M%SZ')}",
        "authorizing_human_oid": owner, "configuration_sha256": plan.configuration_sha256,
        "plan_sha256": plan.sha256,
        "not_before": not_before.isoformat().replace("+00:00", "Z"),
        "expires_at": (not_before + timedelta(minutes=APPROVAL_MINUTES)).isoformat().replace("+00:00", "Z"),
    })


def fresh_install(ctx: Context, api_image: str, runtime_dir: Path) -> Path:
    """run_fresh_job plan -> local rebuild with the same hash -> owner approval -> apply -> reconcile."""
    sys.path.insert(0, str(STAGE_DIR))
    sys.path.insert(0, str(APP))
    previous = os.environ.get("FA_DEPLOY_CONFIG")
    os.environ["FA_DEPLOY_CONFIG"] = str(ctx.profile)  # run_fresh_job loads its profile at import
    try:
        import run_fresh_job
    finally:
        if previous is None:
            os.environ.pop("FA_DEPLOY_CONFIG", None)
        else:
            os.environ["FA_DEPLOY_CONFIG"] = previous

    work = ctx.work / "fresh"
    work.mkdir(exist_ok=True)
    plan_path = work / "plan.json"
    if ctx.get("FA_FRESH_INSTALLED") == "true":
        return plan_path
    scalars, planned = run_fresh_job.plan_from_log(_fresh(ctx, "plan", api_image, runtime_dir, work))
    plan = run_fresh_job.rebuild_plan(runtime_dir, scalars, planned)
    data = (plan.model_dump_json(indent=2) + "\n").encode("utf-8")
    if plan_path.exists() and plan_path.read_bytes() != data:
        raise Stop(f"{plan_path} holds another plan; it is never replaced")
    if not plan_path.exists():
        plan_path.write_bytes(data)
    authorization = work / "authorization.json"
    if not (work / "apply-execution.txt").exists() and not (work / "apply-intent.json").exists():
        if authorization.exists():
            saved = json.loads(authorization.read_text(encoding="utf-8"))
            if saved.get("plan_sha256") != plan.sha256 or saved.get("configuration_sha256") != plan.configuration_sha256:
                raise Stop(f"{authorization} approves another plan; keep it as that decision's record and move it aside")
            expires = saved["expires_at"]
            if datetime.fromisoformat(expires.replace("Z", "+00:00")) <= ctx.now():
                raise Stop(f"{authorization} expired before apply started (no apply intent exists). Keep it as the "
                           "record of that decision: move it aside yourself, then rerun to approve again")
        else:
            authorize(ctx, plan, authorization)
    results = []
    for action in ("apply", "reconcile"):
        lines = _fresh(ctx, action, api_image, runtime_dir, work)
        value = _result_line(lines)
        if "FA_W9_EXIT 0" not in lines or value.get("state") != "initialized" or value.get("budget_reset") is not False:
            raise Stop(f"fresh {action} did not read back state=initialized; keep {work} and read the log, do not "
                       "apply again")
        results.append(value["receipt_sha256"])
    if results[0] != results[1]:
        raise Stop("apply and reconcile receipts differ; read both logs before anything else")
    ctx.set("FA_FRESH_INSTALLED", "true")
    return plan_path


def api_app(ctx: Context, api_image: str, runtime_dir: Path, commit: str) -> None:
    profile = json.loads(ctx.profile.read_text(encoding="utf-8"))
    environment = [{"name": "BEHAVIOR_SERVER_CONFIG_JSON",
                    "value": (runtime_dir / "config.json").read_text(encoding="utf-8")}]
    environment += [{"name": key, "value": value} for key, value in profile["api_environment"].items()]
    deploy_bicep(ctx, f"fa-{ctx.env}-api", INFRA / "shared-context" / "behavior-api.bicep", {
        "location": ctx.need("AZURE_LOCATION"), "appName": ctx.need("FA_API_APP"),
        "environmentName": ctx.need("FA_CONTAINER_APPS_ENVIRONMENT"), "identityName": ctx.need("FA_API_IDENTITY"),
        "registryName": ctx.need("FA_REGISTRY_NAME"), "imageSha256": digest_of(api_image),
        "revisionSuffix": revision_suffix(ctx, commit, 1),
        "environmentVariables": environment, "commandArguments": API_COMMAND, "minReplicas": 1,
        "computeProfile": "assistant-large", "startupFailureThreshold": 120, "tags": tags(ctx)})
    ctx.set("FA_API_IMAGE_DEPLOYED", api_image)


def console_bundle(ctx: Context, runtime_dir: Path, plan_path: Path) -> dict[str, Any]:
    export, bundle = ctx.work / "console" / "export", ctx.work / "console" / "bundle"
    client = ctx.need("FA_CONSOLE_APP_ID")
    if not (export / "agents.json").exists():
        ctx.script(Path("web_test_console.fresh_export"), "--config", str(runtime_dir / "config.json"),
                   "--plan", str(plan_path), "--backend-origin", ctx.need("FA_API_URL"), "--client-id", client,
                   "--output-directory", str(export), module=True)
    printed = None
    if not (bundle / "configFiles.json").exists():
        agents = (export / "agents.json").read_bytes()
        result = ctx.script(Path("web_test_console"), "remote-bundle", "--agents", str(export / "agents.json"),
                            "--agents-sha256", hashlib.sha256(agents).hexdigest(), "--client-id", client,
                            "--out", str(bundle), module=True, capture=True)
        printed = json.loads(result.stdout)
    files = json.loads((bundle / "configFiles.json").read_text(encoding="utf-8"))
    digest = {name: hashlib.sha256(files[name].encode("utf-8")).hexdigest()
              for name in ("console.json", "instructions.json", "agents.json")}
    value = {"consoleConfigSha256": digest["console.json"], "instructionsSha256": digest["instructions.json"],
             "agentsSha256": digest["agents.json"]}
    if printed is not None and printed != value:
        raise Stop("the Portal bundle hashes differ from what remote-bundle printed; do not deploy it")
    return {"configFiles": files, **value}


def console_app(ctx: Context, console_image: str, runtime_dir: Path, plan_path: Path, commit: str) -> None:
    files = console_bundle(ctx, runtime_dir, plan_path)
    parameters = {
        "location": ctx.need("AZURE_LOCATION"), "environmentName": ctx.need("FA_CONTAINER_APPS_ENVIRONMENT"),
        "registryName": ctx.need("FA_REGISTRY_NAME"), "vnetName": ctx.need("FA_VNET"),
        "appName": ctx.need("FA_CONSOLE_APP"), "identityName": ctx.need("FA_CONSOLE_IDENTITY"),
        "storageName": ctx.need("FA_CONSOLE_STORAGE"), "environmentTag": ctx.env,
        "entraClientId": ctx.need("FA_CONSOLE_APP_ID"), "ownerOid": ctx.need("FA_TEAM_OWNER_OID"),
        "apiAudience": ctx.need("FA_API_AUDIENCE"), "imageSha256": digest_of(console_image),
        "minReplicas": 1, **files,
    }
    template = INFRA / "web-console" / "web-console.bicep"
    # Internal ingress until the sign-in config exists: otherwise the console is public for the minutes in between,
    # and only the sign-in layer keeps clients from sending the principal headers themselves.
    outputs = deploy_bicep(ctx, f"fa-{ctx.env}-console", template,
                           {**parameters, "revisionSuffix": revision_suffix(ctx, commit, 1), "ingressExternal": False})
    if outputs.get("publicOrigin") != ctx.need("FA_CONSOLE_URL"):
        raise Stop(f"Portal origin {outputs.get('publicOrigin')} differs from the registered {ctx.need('FA_CONSOLE_URL')}")
    print("FA: Portal federated credential: " + entra.ensure_fic(
        ctx.graph, ctx.need("FA_CONSOLE_APP_OBJECT_ID"),
        entra.fic_body(FIC_NAME, ctx.need("AZURE_TENANT_ID"), outputs["identityPrincipalId"])), flush=True)
    # The app exists one step before its sign-in config, so its first replica can start without the sign-in sidecar
    # (web_test_console README, 2026-10-02): a second revision starts with it; single-revision mode retires the first.
    # The sign-in config exists now, so this deployment opens the external ingress.
    deploy_bicep(ctx, f"fa-{ctx.env}-console", template,
                 {**parameters, "revisionSuffix": revision_suffix(ctx, commit, 2), "ingressExternal": True})
    ctx.set("FA_CONSOLE_DEPLOYED", "true")
    ctx.set("FA_CONSOLE_IMAGE_DEPLOYED", console_image)


def webiq(ctx: Context) -> None:
    if ctx.get("FA_ENABLE_WEBIQ", "false").lower() != "true":
        return
    config = str(ctx.profile)  # the script asks for the key (terminal only) when the connection does not exist yet
    ctx.script(STAGE_DIR / "new_webiq_connection.py", "--config", config)
    ctx.script(STAGE_DIR / "new_toolbox.py", "--config", config, "--out", str(ctx.work / "toolbox"))


def memory_store(ctx: Context) -> None:
    """Private and Team memory (on unless FA_ENABLE_MEMORY=false): the store exists before any Agent version uses it."""
    if ctx.get("FA_ENABLE_MEMORY", "true").lower() == "false":
        return
    try:
        chat_model = models.parse(ctx.values, models.catalog()).memory_chat_model
    except SystemExit as error:
        raise Stop(str(error)) from None
    ctx.script(STAGE_DIR / "new_memory_store.py", "--config", str(ctx.profile), "--out", str(ctx.work / "memory"),
               "--chat-model", chat_model)


def require_owner(ctx: Context, step: str) -> None:
    """The first session preparation and the default selection are the Team owner's own records."""
    if signed_in_user(ctx)["oid"].lower() != ctx.need("FA_TEAM_OWNER_OID").lower():
        raise Stop(f"{step} is the Team owner's: the owner signs in to az and runs `azd hooks run postdeploy`")


def prepare_sessions(ctx: Context) -> None:
    if ctx.get("FA_CONSENT_PENDING") == "true" and not consent(ctx):
        raise Stop("the owner's API consent is missing, so the first session preparation cannot run yet; after an "
                   f"admin applied the files in {ctx.work / 'admin-consent'}, rerun `azd hooks run postdeploy`")
    require_owner(ctx, "the first session preparation")
    ctx.script(STAGE_DIR / "prepare_native_sessions.py", "--config", str(ctx.profile),
               "--out", str(ctx.work / "prepare"), "--request-id", f"{ctx.env}-azd-prepare-1")


def select_defaults(ctx: Context) -> None:
    """First install only: the owner's new-conversation selection for each assistant, so the Portal works at once
    (Neal 2026-10-10: `azd up` must end usable). The template is computed by select_all_capabilities.py from the
    server's own options and the profile's conversation_defaults: the Responses lane, every offered capability
    (desktop, skills, shell, browser, delegation, ...) except the excluded ones, and every approved Team skill. It is
    not copied from another Agent. Later deploys never touch it, so the owner's own changes on the settings page stay."""
    if ctx.get("FA_DEFAULTS_SELECTED") == "true":
        return
    require_owner(ctx, "the new-conversation selection")
    ctx.script(STAGE_DIR / "select_all_capabilities.py", "--config", str(ctx.profile),
               "--out", str(ctx.work / "select"), "--request-id", f"{ctx.env}-azd-select-1")
    ctx.set("FA_DEFAULTS_SELECTED", "true")


def ci_step(ctx: Context, commit: str, *args: str) -> None:
    ctx.script(STAGE_DIR / "ci_deploy.py", "--config", str(ctx.profile), "--runtime-dir",
               str(ctx.work / "runtime"), *args, env=build_env(ctx, commit))


def gate(ctx: Context, commit: str, api_ref: str) -> None:
    """ADR-0009 phase 3b, armed (profile.py): before any switch of an API that already serves. From 2d-ii the new API
    serves v2 only, so a binding row still at v1 would meet it with a new SID and no files, or 503s."""
    try:
        ci_step(ctx, commit, "gate", api_ref)
    except Stop as error:
        raise Stop(f"{error}. The compatibility gate stopped this deploy before the API switch: a binding row still "
                   "records a profile the new API does not serve (v1), or the rows could not be read (the "
                   "DEPLOY_COMPAT_GATE line in the sync job log names them). To go on: git checkout v2026.10.20-33, "
                   "azd env set FA_NATIVE_2DI true, azd deploy, message each named Agent once, then check out this "
                   "release again and azd deploy; or retire the Agent (deploy/azd/README.md#upgrade-2dii)") from None


def upgrade(ctx: Context, built: dict[str, str], commit: str) -> None:
    """A later `azd deploy`: the stage CI's steps (deploy-staging.yml) against this profile. WebIQ first, so turning
    FA_ENABLE_WEBIQ on later never leaves the API offering a toolbox that does not exist (F6). The ADR-0009 phase 3b
    compatibility gate runs before any write (deploy/README.md#compat-gate); a first install has no binding rows yet."""
    ci_step(ctx, commit, "check")
    gate(ctx, commit, built["api"])
    webiq(ctx)
    memory_store(ctx)
    ci_step(ctx, commit, "guard")
    ci_step(ctx, commit, "sync", built["api"], built["hosted"])
    ci_step(ctx, commit, "api", built["api"])
    ci_step(ctx, commit, "console", built["console"])
    ci_step(ctx, commit, "follow", built["api"], built["hosted"])


def summary(ctx: Context) -> None:
    profile = ctx.profile
    print(f"""
FA deploy finished for {ctx.env}. New conversations of both assistants use the Responses lane with every offered
capability; change them per assistant on the Portal's settings page (only new conversations are affected).
Sign in at {ctx.get('FA_CONSOLE_URL', '<Portal URL>')} as the Team owner, open a new conversation and send a short
message; it counts only when its receipt is completed with failure_code=null. Profile: {profile}
""", flush=True)


def deploy(ctx: Context) -> None:
    for key in ("FA_PROJECT_ENDPOINT", "FA_API_URL", "FA_API_AUDIENCE", "FA_CONSOLE_APP_ID"):
        ctx.need(key)
    commit, core = git_commit(), code_identity()
    ctx.set("FA_DEPLOY_RUN", str(int(ctx.get("FA_DEPLOY_RUN", "0")) + 1))
    write_profile(ctx, require_runtime=False)
    built = images(ctx, commit, core)
    existing = ctx.az("containerapp", "show", "--resource-group", ctx.group, "--name", ctx.need("FA_API_APP"),
                      "--subscription", ctx.need("AZURE_SUBSCRIPTION_ID"), missing_ok=True)
    # Upgrade only once a first install finished; a stopped first install resumes below (every step reads back).
    if existing is not None and ctx.get("FA_SESSIONS_PREPARED") == "true":
        write_profile(ctx, require_runtime=True)
        upgrade(ctx, built, commit)
        summary(ctx)
        return
    webiq(ctx)
    memory_store(ctx)
    if not all(ctx.get(f"FA_AGENT_{key}_OID") for key, _, _ in fa_profile.agent_names({"FA_ENVIRONMENT": ctx.env})):
        seed(ctx, built["hosted"], core)
    agent_roles(ctx)
    write_profile(ctx, require_runtime=True)
    ci_step(ctx, commit, "check")
    if existing is not None:
        gate(ctx, commit, built["api"])  # a resumed install over a serving API: its rows may be from an older release
    runtime_dir = runtime(ctx)
    sync_job(ctx, built["api"])
    plan_path = fresh_install(ctx, built["api"], runtime_dir)
    # The stage order (W9 -> W7): versions and route first, the API once the Table is initialized, then follow.
    ci_step(ctx, commit, "sync", built["api"], built["hosted"])
    if existing is None:
        api_app(ctx, built["api"], runtime_dir, commit)
    elif ctx.get("FA_API_IMAGE_DEPLOYED") != built["api"]:
        ci_step(ctx, commit, "api", built["api"])  # a rerun after a partial install: the guarded CI update
        ctx.set("FA_API_IMAGE_DEPLOYED", built["api"])
    if ctx.get("FA_CONSOLE_DEPLOYED") != "true":
        console_app(ctx, built["console"], runtime_dir, plan_path, commit)
    elif ctx.get("FA_CONSOLE_IMAGE_DEPLOYED") != built["console"]:
        ci_step(ctx, commit, "console", built["console"])
        ctx.set("FA_CONSOLE_IMAGE_DEPLOYED", built["console"])
    # No follow here: a never-prepared Agent has no earlier environment to follow, and the server refuses with
    # NATIVE_FILES_CONFIRMATION_REQUIRED (azd live run 2026-10-10). The first preparation binds the routed version;
    # later deploys follow from it (upgrade()).
    prepare_sessions(ctx)
    select_defaults(ctx)
    # The first install is complete only now; a failure above resumes on this path, never as an upgrade.
    ctx.set("FA_SESSIONS_PREPARED", "true")
    summary(ctx)


# ---- entry -------------------------------------------------------------------------------------------------------

def ensure_venv(ctx: Context) -> str:
    """deploy needs the API packages (make_runtime, plan rebuild, Portal export): one private venv per environment,
    reinstalled only when requirements-behavior.txt changes."""
    venv = ctx.work / "venv"
    python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    marker = venv / "fa-requirements.sha256"
    wanted = hashlib.sha256(REQUIREMENTS.read_bytes() + (APP / "requirements.txt").read_bytes()).hexdigest()
    if not python.exists():
        result = ctx.run([sys.executable, "-m", "venv", str(venv)], capture=False)
        if result.code != 0:
            raise Stop("cannot create the deploy virtual environment")
    if not marker.exists() or marker.read_text(encoding="utf-8").strip() != wanted:
        result = ctx.run([str(python), "-m", "pip", "install", "--quiet", "--disable-pip-version-check",
                          "-r", str(REQUIREMENTS)], capture=False, cwd=APP)
        if result.code != 0:
            raise Stop("installing the API packages for the deploy hook failed")
        marker.write_text(wanted + "\n", encoding="utf-8")
    return str(python)


# azd environment values the user sets (deploy/azd/README.md); everything else FA_* is written by provision or hooks.
USER_INPUTS = frozenset({"FA_TEAM_OWNER_OID", "FA_MODELS", "FA_DEFAULT_MODEL", "FA_MODEL_CAPACITY_JSON",
                         "FA_MODEL_VERSION_UPGRADE_OPTION", "FA_API_SUBNET_NAT_EGRESS", "FA_ENABLE_AGENT_ONBOARDING",
                         "FA_ENABLE_WEBIQ", "FA_TIMEZONE", "FA_HOSTED_IDLE_TIMEOUT_SECONDS", "FA_ENABLE_MEMORY",
                         "FA_ENABLE_TEAM_MCP_CONNECTIONS", "FA_DEPLOY_UPGRADE_SNAPSHOTS"})


BACKUP_LOCK = "m1-backup-account-delete-protection"  # infra/shared-context/backup-foundation.bicep
BACKUP_ROLE_SEED = "fa-m1-backup-data-read-write-v1"  # infra/shared-context/backup-role.bicep
BICEP_GUID_NAMESPACE = uuid.UUID("11fb06fb-712d-4ddd-98c7-e71bbd588830")
ROLE_API = "api-version=2022-04-01"  # the roleDefinitions version backup-role.bicep deploys with


def bicep_guid(*parts: str) -> str:
    """Bicep's guid(): a version-5 UUID of the parts joined with '-'. Checked against the stage role GUID in
    infra/shared-context/m3-artifacts.stage.parameters.json."""
    return str(uuid.uuid5(BICEP_GUID_NAMESPACE, "-".join(parts)))


def backup_role(ctx: Context) -> tuple[str, dict[str, Any] | None]:
    """This environment's backup custom role, read by the exact ID backup-role.bicep gives it,
    guid(<RG id>, BACKUP_ROLE_SEED), never by name: another environment of the same name (other group or region)
    has a role named alike. A GET by ID also finds a role whose only assignable scope is the group, which a
    subscription-scope list can leave out. Returns the ID and the role, or None when it does not exist."""
    subscription = ctx.need("AZURE_SUBSCRIPTION_ID").lower()  # ARM's subscription().id is lowercase
    role_id = (f"/subscriptions/{subscription}/providers/Microsoft.Authorization/roleDefinitions/"
               + bicep_guid(f"/subscriptions/{subscription}/resourceGroups/{ctx.group}", BACKUP_ROLE_SEED))
    # az rest prefixes a URL that starts with a resource ID with the current cloud's ARM endpoint.
    return role_id, ctx.az("rest", "--method", "get", "--url", f"{role_id}?{ROLE_API}", missing_ok=True)


def predown(ctx: Context) -> None:
    """`azd down` cannot delete the resource group while the M1 backup account keeps its CanNotDelete lock, which
    backup-foundation.bicep sets on purpose. Removing it lets the backups go with the group, so the person types the
    environment name first (or sets FA_CONFIRM_DELETE=<name> for a run without a terminal). Only FA's own lock is
    removed; any other lock stops here for its owner to decide. First, read only: what goes outside the group."""
    account = check_account(ctx)
    keys = entra.names(ctx.env, account["tenantId"], ctx.need("AZURE_SUBSCRIPTION_ID"), ctx.group)
    try:  # informational: a failed read must not stop `azd down`
        role_id, role = backup_role(ctx)
        found = (f"{role['properties']['roleName']} ({role['name']})" if role
                 else f"none found ({role_id.rsplit('/', 1)[-1]})")
    except Stop as error:
        found = f"could not be read ({error})"
    print(f"FA: besides {ctx.group}, the postdown hook deletes: Entra apps {keys['console_unique_name']} and "
          f"{keys['api_unique_name']} (if present; restorable for 30 days); backup custom role: {found}", flush=True)
    locks = ctx.az("lock", "list", "--resource-group", ctx.group, "--subscription", ctx.need("AZURE_SUBSCRIPTION_ID"),
                   missing_ok=True) or []
    if not locks:
        return
    foreign = [lock.get("name") for lock in locks if lock.get("name") != BACKUP_LOCK]
    if foreign:
        raise Stop(f"{ctx.group} has locks FA did not create ({', '.join(map(str, foreign))}); remove them yourself first")
    name = ctx.need("AZURE_ENV_NAME")
    if ctx.get("FA_CONFIRM_DELETE") != name:
        if not ctx.tty:
            raise Stop(f"deleting {ctx.group} also deletes its M1 backups; set FA_CONFIRM_DELETE={name} for this run "
                       "or run `azd down` in a terminal")
        answer = ctx.ask(f"Deleting {ctx.group} deletes its Table data and M1 backups for good. "
                         f"Type the environment name ({name}) to remove the backup lock: ").strip()
        if answer != name:
            raise Stop("not confirmed; nothing was removed")
    for lock in locks:
        ctx.az("lock", "delete", "--ids", lock["id"])
        # azd asks its own y/N after this hook; whoever answers no puts the lock back with this command.
        print(f"FA: removed lock {lock['name']} on the M1 backup account; to restore it if you stop here: "
              f"az lock create --name {lock['name']} --lock-type {lock.get('level') or 'CanNotDelete'} "
              f"--resource {lock['id'].rsplit('/providers/', 1)[0]}", flush=True)


def postdown(ctx: Context) -> None:
    """After `azd down` removed the resource group: what `azd up` made outside it. Each object is read first and
    deleted only when it is this environment's own (the uniqueNames the hooks wrote, the role ID derived from the
    group), so a rerun converges.
    Deleted Entra apps stay restorable for 30 days. Local records are renamed, never deleted, and the hook-written
    values are cleared so the same environment name starts a fresh install."""
    account = check_account(ctx)
    keys = entra.names(ctx.env, account["tenantId"], ctx.need("AZURE_SUBSCRIPTION_ID"), ctx.group)
    for unique in (keys["console_unique_name"], keys["api_unique_name"]):
        try:
            app = ctx.graph("GET", f"/applications(uniqueName='{unique}')?$select=id", None, {})
        except LookupError:
            print(f"FA: Entra app {unique}: already gone", flush=True)
            continue
        ctx.graph("DELETE", f"/applications/{app['id']}", None, {})
        print(f"FA: Entra app {unique}: deleted (restorable for 30 days)", flush=True)
    role_id, role = backup_role(ctx)
    recorded, keep = ctx.get("FA_BACKUP_ROLE_ID"), set()
    if recorded and recorded.lower() != role_id.lower():  # the group is not the one this role was made for
        keep.add("FA_BACKUP_ROLE_ID")  # the only pointer to that role stays for whoever checks it
        print(f"FA: backup custom role: the recorded {recorded} is not {role_id} (from {ctx.group}); "
              "none deleted, check and delete it yourself", flush=True)
    elif role:
        ctx.az("rest", "--method", "delete", "--url", f"{role_id}?{ROLE_API}", missing_ok=True)
        print(f"FA: backup custom role {role['properties']['roleName']} ({role['name']}): deleted", flush=True)
    else:
        print(f"FA: backup custom role {role_id.rsplit('/', 1)[-1]}: already gone", flush=True)
    folder = ctx.state_root / ctx.need("AZURE_ENV_NAME") / "fa"
    if folder.exists():
        kept = folder.with_name(f"fa-down-{ctx.now().strftime('%Y%m%dT%H%M%SZ')}")
        folder.rename(kept)
        print(f"FA: local records kept in {kept}", flush=True)
    # An empty value reads as unset (Context.get); azd 1.32 has no `azd env unset` and exits 0 on the unknown command.
    for key in sorted(key for key in ctx.values
                      if key.startswith("FA_") and key not in USER_INPUTS | keep and ctx.values[key]):
        ctx.set(key, "")


def consent_only(ctx: Context) -> None:
    if not consent(ctx):
        raise Stop("the owner's grants are still missing")


def prereq(ctx: Context) -> None:
    """Read only, before `azd up`: what this account has and what someone else must do first."""
    check_account(ctx)
    rows = prerequisites(ctx, signed_in_user(ctx))
    for row in rows:
        print(f"[{row['state']:>7}] {row['item']}: {row['detail']}", flush=True)
    if any(row["state"] == "blocked" for row in rows):
        raise Stop("some prerequisites are missing; see deploy/azd/README.md 事前準備")


def azd_values(run: Runner = default_runner) -> dict[str, str]:
    """Outside a hook (prereq, consent): the selected azd environment's values, as a hook would see them."""
    result = run(["azd", "env", "get-values", "--output", "json"])
    if result.code != 0:
        raise Stop("no azd environment is selected: `azd env new <name> --subscription <id>` (add "
                   "`--location <region>` to skip azd's region menu later)")
    return {key: str(value) for key, value in json.loads(result.stdout).items()}


PHASES = {"preprovision": preprovision, "postprovision": postprovision, "deploy": deploy, "consent": consent_only,
          "prereq": prereq, "predown": predown, "postdown": postdown}
# The command that asks a phase's question again: rerunning only preprovision would not provision, and predown's
# question belongs to `azd down`.
RESUME = {"preprovision": "azd up", "deploy": "azd hooks run postdeploy", "predown": "azd down"}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 1 or argv[0] not in PHASES:
        print(__doc__, file=sys.stderr)
        return 2
    try:
        values = dict(os.environ) if "AZURE_ENV_NAME" in os.environ else {**os.environ, **azd_values()}
    except Stop as error:
        print(f"FA: stopped: {error}", file=sys.stderr, flush=True)
        return 1
    ctx = Context(values=values, resume=RESUME.get(argv[0], f"azd hooks run {argv[0]}"))
    if argv[0] == "deploy" and os.environ.get("FA_AZD_VENV") != "1":
        python = ensure_venv(ctx)
        return subprocess.run([python, "-X", "utf8", str(Path(__file__).resolve()), "deploy"],
                              env={**os.environ, "FA_AZD_VENV": "1", "PYTHONUTF8": "1"}, check=False).returncode
    try:
        PHASES[argv[0]](ctx)
    except Stop as error:
        print(f"FA: stopped: {error}", file=sys.stderr, flush=True)
        return 1
    except entra.GraphForbidden as error:
        print(f"FA: stopped: Entra refused a write this account may not make ({error}); see deploy/azd/README.md "
              "事前準備 C for the admin step, then rerun the hook", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
