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


def write_x(path: Path, value: Any) -> None:
    """Create `path` with indent-1 JSON and fsync it; an existing file raises FileExistsError, never replaced."""
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, indent=1)
        stream.flush()
        os.fsync(stream.fileno())


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


def _matches(pattern: re.Pattern[str], value: Any) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _is_nonempty_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value)


def _is_string_map(value: Any) -> bool:
    return isinstance(value, dict) and all(
        isinstance(key, str) and isinstance(item, str) for key, item in value.items()
    )


def _check_identifiers(profile: dict[str, Any]) -> None:
    for key in ("tenant_id", "subscription_id"):
        if not _matches(UUID, profile[key]):
            raise SystemExit(f"deployment config {key} must be a UUID")
    for key in ("resource_group", "registry_name", "api_app", "api_identity", "sync_job"):
        if not _matches(RESOURCE_NAME, profile[key]):
            raise SystemExit(f"deployment config {key} is not a valid Azure resource name")
    if not _matches(HTTPS_URL, profile["project_endpoint"]):
        raise SystemExit("deployment config project_endpoint must be an https URL")
    if not isinstance(profile["registry"], str) or not profile["registry"].endswith(".azurecr.io"):
        raise SystemExit("deployment config registry must be an Azure Container Registry hostname")
    if not _is_nonempty_text(profile["api_audience"]):
        raise SystemExit("deployment config api_audience must be a non-empty string")


def _check_settings(profile: dict[str, Any]) -> None:
    if not isinstance(profile["hosted_idle_timeout_seconds"], int) or profile["hosted_idle_timeout_seconds"] < 1:
        raise SystemExit("deployment config hosted_idle_timeout_seconds must be a positive integer")
    for key in ("api_environment", "assistant_environment"):
        if not _is_string_map(profile[key]):
            raise SystemExit(f"deployment config {key} must map strings to strings")


def _check_agent(index: int, agent: Any, require_runtime: bool) -> None:
    if not isinstance(agent, dict):
        raise SystemExit(f"deployment config agents[{index}] must be an object")
    for key in ("name", "kind", "description"):
        if not _is_nonempty_text(agent.get(key)):
            raise SystemExit(f"deployment config agents[{index}].{key} must be a non-empty string")
    if agent["kind"] not in {"assistant", "executor"}:
        raise SystemExit(f"deployment config agents[{index}].kind is invalid")
    if require_runtime:
        for key in ("agent_id", "agent_oid"):
            if not _matches(UUID, agent.get(key)):
                raise SystemExit(f"deployment config agents[{index}].{key} must be a UUID")


def _check_runtime(profile: dict[str, Any]) -> None:
    # deploy_client_id is the GitHub OIDC identity; only the CI's `ci_deploy.py meta` needs it (and checks it).
    # An azd environment (deploy/azd) deploys as the signed-in operator and has none.
    for key in ("api_identity_client_id", "api_url"):
        if not _is_nonempty_text(profile.get(key)):
            raise SystemExit(f"deployment config {key} is required for runtime deployment")
    if HTTPS_URL.fullmatch(profile["api_url"]) is None:
        raise SystemExit("deployment config api_url must be an https URL")
    runtime = profile.get("runtime")
    if not isinstance(runtime, dict):
        raise SystemExit("deployment config runtime must be an object")
    for key in ("authority_domain", "team_id", "human_oid", "admin_client_id",
                "table_account", "table_name", "blob_account", "sources_container",
                "backups_container"):
        if not _is_nonempty_text(runtime.get(key)):
            raise SystemExit(f"deployment config runtime.{key} must be a non-empty string")
    if UUID.fullmatch(runtime["human_oid"]) is None:
        raise SystemExit("deployment config runtime.human_oid must be a UUID")
    if "console_app" not in profile:
        raise SystemExit("deployment config console_app is required for runtime deployment")


def validate_config(profile: Any, *, path: Path | None = None, require_runtime: bool = True) -> None:
    """Validate the cross-script contract shared by infra and runtime steps; the first broken rule stops it."""
    where = f" in {path}" if path else ""
    if not isinstance(profile, dict):
        raise SystemExit(f"deployment config{where} must be a JSON object")
    missing = [key for key in COMMON_REQUIRED if key not in profile]
    if missing:
        raise SystemExit(f"deployment config{where} is missing: {', '.join(missing)}")
    _check_identifiers(profile)
    _check_settings(profile)
    if not isinstance(profile["agents"], list) or not profile["agents"]:
        raise SystemExit("deployment config agents must be a non-empty array")
    for index, agent in enumerate(profile["agents"]):
        _check_agent(index, agent, require_runtime)
    if require_runtime:
        _check_runtime(profile)


def set_config(value: str | os.PathLike[str] | None = None, *, require_runtime: bool = True) -> dict[str, Any]:
    """Compatibility helper for scripts that keep a module-level active profile."""
    return load_config(value, require_runtime=require_runtime)
