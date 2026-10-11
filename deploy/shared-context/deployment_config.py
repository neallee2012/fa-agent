"""Load and validate an environment-specific FA deployment profile.

The profile is intentionally JSON so it can be generated outside the repository and
passed to every deployment step.  The checked-in stage profile remains the default
for backwards compatibility; callers can select another profile with ``--config``
or ``FA_DEPLOY_CONFIG``.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

UUID = re.compile(r"^[0-9a-fA-F-]{36}$")
RESOURCE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")
HTTPS_URL = re.compile(r"^https://\S+$")

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = ROOT / "deploy" / "stage" / "stage.json"
CONFIG_ENV = "FA_DEPLOY_CONFIG"

COMMON_REQUIRED = (
    "environment",
    "tenant_id",
    "subscription_id",
    "resource_group",
    "project_endpoint",
    "registry",
    "registry_name",
    "api_app",
    "api_audience",
    "api_identity",
    "sync_job",
    "hosted_idle_timeout_seconds",
    "agents",
    "api_environment",
    "assistant_environment",
)


def config_path(value: str | os.PathLike[str] | None = None) -> Path:
    """Resolve an explicit path, the environment override, or the stage default."""
    selected = value or os.environ.get(CONFIG_ENV)
    path = Path(selected) if selected else DEFAULT_CONFIG
    path = path.expanduser()
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()
    return path


def load_config(
    value: str | os.PathLike[str] | None = None,
    *,
    require_runtime: bool = True,
) -> dict[str, Any]:
    """Read one profile and fail before any Azure operation if it is malformed."""
    path = config_path(value)
    if not path.is_file():
        raise SystemExit(f"deployment config does not exist: {path}")
    try:
        profile = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise SystemExit(f"cannot read deployment config {path}: {error}") from None
    validate_config(profile, path=path, require_runtime=require_runtime)
    return profile


def validate_config(profile: Any, *, path: Path | None = None, require_runtime: bool = True) -> None:
    """Validate the cross-script contract shared by infra and runtime steps."""
    where = f" in {path}" if path else ""
    if not isinstance(profile, dict):
        raise SystemExit(f"deployment config{where} must be a JSON object")
    missing = [key for key in COMMON_REQUIRED if key not in profile]
    if missing:
        raise SystemExit(f"deployment config{where} is missing: {', '.join(missing)}")

    for key in ("tenant_id", "subscription_id"):
        if not isinstance(profile[key], str) or UUID.fullmatch(profile[key]) is None:
            raise SystemExit(f"deployment config {key} must be a UUID")
    for key in ("resource_group", "registry_name", "api_app", "api_identity", "sync_job"):
        if not isinstance(profile[key], str) or RESOURCE_NAME.fullmatch(profile[key]) is None:
            raise SystemExit(f"deployment config {key} is not a valid Azure resource name")
    for key in ("project_endpoint",):
        if not isinstance(profile[key], str) or HTTPS_URL.fullmatch(profile[key]) is None:
            raise SystemExit(f"deployment config {key} must be an https URL")
    for key in ("registry",):
        if not isinstance(profile[key], str) or not profile[key].endswith(".azurecr.io"):
            raise SystemExit(f"deployment config {key} must be an Azure Container Registry hostname")
    if not isinstance(profile["api_audience"], str) or not profile["api_audience"]:
        raise SystemExit("deployment config api_audience must be a non-empty string")
    if not isinstance(profile["hosted_idle_timeout_seconds"], int) or profile["hosted_idle_timeout_seconds"] < 1:
        raise SystemExit("deployment config hosted_idle_timeout_seconds must be a positive integer")
    if not isinstance(profile["api_environment"], dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in profile["api_environment"].items()
    ):
        raise SystemExit("deployment config api_environment must map strings to strings")
    if not isinstance(profile["assistant_environment"], dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in profile["assistant_environment"].items()
    ):
        raise SystemExit("deployment config assistant_environment must map strings to strings")
    if not isinstance(profile["agents"], list) or not profile["agents"]:
        raise SystemExit("deployment config agents must be a non-empty array")
    for index, agent in enumerate(profile["agents"]):
        if not isinstance(agent, dict):
            raise SystemExit(f"deployment config agents[{index}] must be an object")
        for key in ("name", "kind", "description"):
            if not isinstance(agent.get(key), str) or not agent[key]:
                raise SystemExit(f"deployment config agents[{index}].{key} must be a non-empty string")
        if agent["kind"] not in {"assistant", "executor"}:
            raise SystemExit(f"deployment config agents[{index}].kind is invalid")
        if require_runtime:
            for key in ("agent_id", "agent_oid"):
                if not isinstance(agent.get(key), str) or UUID.fullmatch(agent[key]) is None:
                    raise SystemExit(f"deployment config agents[{index}].{key} must be a UUID")

    if require_runtime:
        # deploy_client_id is the GitHub OIDC identity; only the CI's `ci_deploy.py meta` needs it (and checks it).
        # An azd environment (deploy/azd) deploys as the signed-in operator and has none.
        for key in ("api_identity_client_id", "api_url"):
            if not isinstance(profile.get(key), str) or not profile[key]:
                raise SystemExit(f"deployment config {key} is required for runtime deployment")
        if HTTPS_URL.fullmatch(profile["api_url"]) is None:
            raise SystemExit("deployment config api_url must be an https URL")
        runtime = profile.get("runtime")
        if not isinstance(runtime, dict):
            raise SystemExit("deployment config runtime must be an object")
        for key in ("authority_domain", "team_id", "human_oid", "admin_client_id",
                    "table_account", "table_name", "blob_account", "sources_container",
                    "backups_container"):
            if not isinstance(runtime.get(key), str) or not runtime[key]:
                raise SystemExit(f"deployment config runtime.{key} must be a non-empty string")
        if UUID.fullmatch(runtime["human_oid"]) is None:
            raise SystemExit("deployment config runtime.human_oid must be a UUID")
        if "console_app" not in profile:
            raise SystemExit("deployment config console_app is required for runtime deployment")


def set_config(value: str | os.PathLike[str] | None = None, *, require_runtime: bool = True) -> dict[str, Any]:
    """Compatibility helper for scripts that keep a module-level active profile."""
    return load_config(value, require_runtime=require_runtime)
