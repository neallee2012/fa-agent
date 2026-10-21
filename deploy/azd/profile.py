"""Build the FA deployment profile of an azd environment (docs/azd-new-environment.md).

Pure and offline: every identifier comes from the azd environment (Bicep outputs and values the hooks wrote back after
reading Azure); feature flags come from profile.defaults.json. Nothing is copied from deploy/stage/*.json or another
environment. The result is the same profile shape the stage scripts already accept (deploy/shared-context/
deployment_config.py), so seed_agents.py, make_runtime.py, run_fresh_job.py, ci_deploy.py,
prepare_native_sessions.py and select_all_capabilities.py run unchanged with --config.
"""

from __future__ import annotations

import copy
import json
import re
import sys
from pathlib import Path
from typing import Any, Mapping

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "shared-context"))
sys.path.insert(0, str(HERE))
from deployment_config import validate_config  # noqa: E402
import models  # noqa: E402

DEFAULTS = HERE / "profile.defaults.json"
# Microsoft Azure CLI, the first-party public client the operator scripts use for the owner's delegated token.
# A well-known application ID, the same in every tenant (it is not tenant or environment data).
AZURE_CLI_CLIENT_ID = "04b07795-8ddb-461a-bbee-02f9e1bf7b46"
AGENT_SUFFIXES = (("a", "assistant"), ("b", "assistant"), ("c", "executor"), ("d", "executor"))
AGENT_DESCRIPTIONS = {
    "a": "FA assistant A.",
    "b": "FA assistant B.",
    "c": "Configured target C; never invoked (the server configuration needs 4 targets).",
    "d": "Configured target D; never invoked (the server configuration needs 4 targets).",
}
ENVIRONMENT = re.compile(r"[a-z0-9][a-z0-9-]{1,19}")
TIMEZONE = re.compile(r"[A-Za-z][A-Za-z0-9_+\-/]{0,63}")


def defaults(path: Path = DEFAULTS) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    value.pop("_comment", None)
    return value


def _get(values: Mapping[str, str], key: str) -> str:
    value = values.get(key)
    if not isinstance(value, str) or not value.strip():
        raise SystemExit(f"azd environment value {key} is missing; run `azd provision` (or the hook that sets it) first")
    return value.strip()


def _flag(values: Mapping[str, str], key: str, default: str = "false") -> bool:
    value = (values.get(key) or default).strip().lower()
    if value not in {"true", "false"}:
        raise SystemExit(f"{key} must be true or false")
    return value == "true"


def environment_name(values: Mapping[str, str]) -> str:
    name = _get(values, "FA_ENVIRONMENT")
    if ENVIRONMENT.fullmatch(name) is None:
        raise SystemExit("FA_ENVIRONMENT must be 2-20 lowercase letters, digits or hyphens")
    return name


def agent_names(values: Mapping[str, str]) -> list[tuple[str, str, str]]:
    """(azd key suffix, Hosted Agent name, kind). make_runtime derives actor IDs from the last letter."""
    env = environment_name(values)
    return [(suffix.upper(), f"fa-{env}-{suffix}", kind) for suffix, kind in AGENT_SUFFIXES]


def agents(values: Mapping[str, str], base: Mapping[str, Any], *, require_runtime: bool) -> list[dict[str, Any]]:
    result = []
    for key, name, kind in agent_names(values):
        agent: dict[str, Any] = {"name": name, "kind": kind, "description": AGENT_DESCRIPTIONS[name[-1]]}
        if kind == "assistant":
            agent["hosted_environment"] = dict(base["assistant_hosted_environment"])
        for field, variable in (("agent_id", f"FA_AGENT_{key}_ID"), ("agent_oid", f"FA_AGENT_{key}_OID")):
            value = values.get(variable)
            if value:
                agent[field] = value
            elif require_runtime:
                raise SystemExit(f"{variable} is missing; the Hosted seed step sets it")
        result.append(agent)
    return result


def build(values: Mapping[str, str], *, require_runtime: bool = True,
          base: Mapping[str, Any] | None = None) -> dict[str, Any]:
    base = copy.deepcopy(dict(base if base is not None else defaults()))
    env = environment_name(values)
    tenant = _get(values, "AZURE_TENANT_ID")
    subscription = _get(values, "AZURE_SUBSCRIPTION_ID")
    group = _get(values, "AZURE_RESOURCE_GROUP")
    project_id = _get(values, "FA_PROJECT_RESOURCE_ID")
    realtime = _get(values, "FA_REALTIME_ENDPOINT")
    timezone = (values.get("FA_TIMEZONE") or "UTC").strip()
    if TIMEZONE.fullmatch(timezone) is None:
        raise SystemExit("FA_TIMEZONE must be an IANA time zone name such as UTC or Asia/Taipei")
    idle = (values.get("FA_HOSTED_IDLE_TIMEOUT_SECONDS") or str(base["hosted_idle_timeout_seconds"])).strip()
    if not idle.isdigit() or not 120 <= int(idle) <= 3600:
        raise SystemExit("FA_HOSTED_IDLE_TIMEOUT_SECONDS must be 120-3600 (the range Agent sync accepts)")

    api_environment = {"AZURE_CLIENT_ID": _get(values, "FA_API_IDENTITY_CLIENT_ID"), **base["api_environment"]}
    if not _flag(values, "FA_ENABLE_TEAM_MCP_CONNECTIONS", "true"):
        # Bicep then does not give the API identity Contributor, so adding a keyed connection would be a 403.
        for key in ("FA_TEAM_MCP_ENABLED", "FA_TEAM_MCP_SINGLE_TEAM"):
            api_environment.pop(key, None)
    if api_environment.get("FA_TEAM_MCP_ENABLED") == "1":
        api_environment["FA_TEAM_MCP_PROJECT_RESOURCE_ID"] = project_id
    api_environment["FA_REALTIME_ENDPOINT"] = realtime
    # ADR-0009 phase 5: only the Bicep output of the opt-in dedicated snapshot account (empty when not deployed).
    if values.get("FA_UPGRADE_SNAPSHOT_CONTAINER_URL"):
        api_environment["FA_UPGRADE_SNAPSHOT_CONTAINER_URL"] = values["FA_UPGRADE_SNAPSHOT_CONTAINER_URL"]
    console_client = values.get("FA_CONSOLE_APP_ID")
    if console_client:
        api_environment["FA_REMOTE_CONSOLE_CLIENT_ID"] = console_client
    elif require_runtime:
        raise SystemExit("FA_CONSOLE_APP_ID is missing; the postprovision hook registers the Portal app")
    # The Bicep output, not FA_ENABLE_AGENT_ONBOARDING: the settings follow the delegation provision really created.
    # A missing output means off: without it nothing proves provision created the delegation, and the settings alone
    # would make "+" fail on a missing role-assignment permission.
    if "FA_AGENT_ONBOARDING" not in values:
        print("FA: warning: the Bicep output FA_AGENT_ONBOARDING is missing, so \"+\" onboarding settings are left out; "
              "re-run `azd provision`.", file=sys.stderr)
    if _flag(values, "FA_AGENT_ONBOARDING", "false"):
        api_environment["FA_ONBOARDING_ENDPOINT_PROTOCOLS"] = "true"
        api_environment["FA_ONBOARDING_ROLE_SCOPE"] = project_id
    if _flag(values, "FA_ENABLE_WEBIQ"):
        api_environment["FA_CAPABILITY_TOOLBOX_ENTRIES"] = "WebIQ"
    memory = _flag(values, "FA_ENABLE_MEMORY", "true")
    if memory:
        api_environment.update(base["memory_environment"]["api"])
    choice = models.parse(values, models.catalog())
    api_environment["FA_INSTRUCTION_MODELS"] = ",".join(choice.models)  # the Portal offers only deployed models
    api_environment["FA_INSTRUCTION_DEFAULT_MODEL"] = choice.default

    api_app = _get(values, "FA_API_APP")
    profile: dict[str, Any] = {
        "environment": env,
        "tenant_id": tenant,
        "subscription_id": subscription,
        "resource_group": group,
        "foundry_resource_group": group,
        "foundry_account": _get(values, "FA_FOUNDRY_ACCOUNT"),
        "project": _get(values, "FA_PROJECT"),
        "project_endpoint": _get(values, "FA_PROJECT_ENDPOINT"),
        "registry": _get(values, "FA_REGISTRY"),
        "registry_name": _get(values, "FA_REGISTRY_NAME"),
        "container_apps_environment": _get(values, "FA_CONTAINER_APPS_ENVIRONMENT"),
        "api_app": api_app,
        "api_audience": _get(values, "FA_API_AUDIENCE"),
        "api_identity": _get(values, "FA_API_IDENTITY"),
        "api_identity_client_id": _get(values, "FA_API_IDENTITY_CLIENT_ID"),
        "sync_job": _get(values, "FA_SYNC_JOB"),
        "console_app": _get(values, "FA_CONSOLE_APP"),
        "log_analytics_workspace": _get(values, "FA_LOG_ANALYTICS"),
        "hosted_idle_timeout_seconds": int(idle),
        "assistant_environment": {**base["assistant_environment"], "TZ": timezone,
                                  **(base["memory_environment"]["assistant"] if memory else {})},
        "agents": agents(values, base, require_runtime=require_runtime),
        "conversation_defaults": base["conversation_defaults"],
        "api_environment": api_environment,
        "console_environment": {**base["console_environment"], "FA_REALTIME_ENDPOINT": realtime},
        "runtime": {
            "team_id": f"team-{env}",
            "authority_domain": f"fa-{env}-v1",
            "human_oid": _get(values, "FA_TEAM_OWNER_OID"),
            "admin_client_id": AZURE_CLI_CLIENT_ID,
            "table_account": _get(values, "FA_TABLE_ACCOUNT"),
            "table_name": _get(values, "FA_TABLE_NAME"),
            "blob_account": _get(values, "FA_BACKUP_STORAGE"),
            "sources_container": _get(values, "FA_ARTIFACTS_CONTAINER"),
            "backups_container": _get(values, "FA_BACKUP_CONTAINER"),
        },
        # ADR-0009 2d-ii (deploy/README.md#compat-gate): the API serves v2 only, so a v1 binding row stops a deploy
        # over a serving API; a first install has no rows and runs no gate.
        "compatibility_gate_armed": True,
    }
    api_url = values.get("FA_API_URL")
    if api_url:
        profile["api_url"] = api_url
    elif require_runtime:
        raise SystemExit("FA_API_URL is missing; the postprovision hook reads the Container Apps domain")
    validate_config(profile, require_runtime=require_runtime)
    return profile


def write(path: Path, profile: Mapping[str, Any]) -> bool:
    """Write the derived profile; True when the bytes changed. It is regenerated each run, never an applied record."""
    data = (json.dumps(profile, indent=2, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    if path.exists() and path.read_bytes() == data:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_bytes(data)
    temporary.replace(path)
    return True
