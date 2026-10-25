"""Read-only Azure gates for a new FA environment; never registers, creates or changes anything.

``subscription`` runs before the first Bicep deployment, using the deployment.json from
prepare_environment.py. It checks:

- the signed-in account is in the right tenant and subscription;
- ``Microsoft.Network/AllowBringYourOwnPublicIpAddress`` is Registered. Without it the 2026-10-07
  Container Apps environment showed ``Succeeded`` but ``staticIp=null``, no app revision was ever
  created, and the environment had to be rebuilt;
- the resource providers behind every namespace the shared-context Bicep deploys are Registered
  (``Microsoft.Insights`` for Application Insights and diagnostic settings; ``Microsoft.SignalRService``
  for Web PubSub, which was still NotRegistered in the 2026-10-07 subscription); with
  ``"network_mode": "private"`` (azd FA_NETWORK_MODE, ADR-0010) also ``Microsoft.ContainerService``
  (Hosted Agent network injection), and with ``"memory_backend": "cosmos"`` too ``Microsoft.DocumentDB``
  (the Cosmos DB long-term memory, ADR-0011);
- ``Microsoft.AlertsManagement`` is Registered (a warning only: Application Insights creates its
  "Failure Anomalies" smart detector alert rule through it; on 2026-10-07 that side step failed with
  ``MissingSubscriptionRegistration`` while the FA deployment itself succeeded);
- each model deployment fits into the regional quota that the subscription shares across accounts.

``aca-ready`` runs after foundation.bicep and before any app or Job:

- the environment is ``Succeeded`` and has a staticIp;
- its infrastructure resource group holds the platform load balancer and a public IP with that
  address.

Registering a feature or provider, or asking for quota, changes the subscription. Those steps stay
outside this script, and the operator needs approval for them. After the feature is registered, run
``az provider register --namespace Microsoft.Network`` again so the change propagates, then rerun this
check.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

Runner = Callable[[list[str]], tuple[int, str, str]]

FEATURE = ("Microsoft.Network", "AllowBringYourOwnPublicIpAddress")
PROVIDERS = (
    "Microsoft.App",
    "Microsoft.Network",
    "Microsoft.CognitiveServices",
    "Microsoft.ContainerRegistry",
    "Microsoft.Storage",
    "Microsoft.ManagedIdentity",
    "Microsoft.OperationalInsights",
    "Microsoft.Insights",
    "Microsoft.SignalRService",
)
PRIVATE_PROVIDERS = ("Microsoft.ContainerService",)
COSMOS_PROVIDERS = ("Microsoft.DocumentDB",)  # ADR-0011 Cosmos memory: private with "memory_backend": "cosmos"
ADVISORY_PROVIDERS = ("Microsoft.AlertsManagement",)
NOT_FOUND = ("ResourceNotFound", "ResourceGroupNotFound", "was not found", "could not be found")


class AzureCli:
    """``az`` with JSON output; ``--az`` may point at the CLI's own python.exe (module mode)."""

    def __init__(self, executable: str | None = None):
        executable = executable or shutil.which("az") or "az"
        if Path(executable).name.lower().startswith("python"):
            self.prefix = [executable, "-X", "utf8", "-IBm", "azure.cli"]
        else:
            self.prefix = [executable]

    def __call__(self, args: list[str]) -> tuple[int, str, str]:
        process = subprocess.run([*self.prefix, *args, "-o", "json"], capture_output=True, text=True,
                                 encoding="utf-8", errors="replace", timeout=180)
        return process.returncode, process.stdout, process.stderr


class Report:
    def __init__(self, mode: str, profile: dict[str, Any]):
        self.value = {"mode": mode, "subscription_id": profile["subscription_id"],
                      "resource_group": profile["resource_group"], "checks": [], "warnings": []}

    def check(self, name: str, ok: bool, detail: Any) -> None:
        self.value["checks"].append({"name": name, "ok": ok, "detail": detail})

    def warn(self, message: str) -> None:
        self.value["warnings"].append(message)

    @property
    def ok(self) -> bool:
        return all(item["ok"] for item in self.value["checks"])


def _json(run: Runner, args: list[str], *, missing_ok: bool = False) -> Any:
    code, stdout, stderr = run(args)
    if code != 0:
        if missing_ok and any(marker in stderr for marker in NOT_FOUND):
            return None
        first = next((line for line in stderr.splitlines() if line.strip() and "WARNING" not in line), "")
        raise SystemExit(f"az {' '.join(args[:3])} failed: {first[:300]}")
    return json.loads(stdout) if stdout.strip() else None


def usage_names(model: str, sku: str) -> tuple[str, ...]:
    """Quota names usually repeat the model name, but the gpt-4.1 family drops the hyphen (gpt4.1-mini)."""
    names = [f"OpenAI.{sku}.{model}"]
    if model.startswith("gpt-"):
        names.append(f"OpenAI.{sku}.gpt{model[4:]}")
    return tuple(names)


def check_subscription(profile: dict[str, Any], run: Runner) -> Report:
    report = Report("subscription", profile)
    subscription = profile["subscription_id"]
    scope = ["--subscription", subscription]
    account = _json(run, ["account", "show", *scope]) or {}
    report.check("tenant", account.get("tenantId") == profile["tenant_id"] and account.get("id") == subscription,
                 {"expected_tenant": profile["tenant_id"], "signed_in_tenant": account.get("tenantId"),
                  "subscription": account.get("id")})

    namespace, name = FEATURE
    feature = _json(run, ["feature", "show", "--namespace", namespace, "--name", name, *scope], missing_ok=True)
    state = ((feature or {}).get("properties") or {}).get("state", "NotRegistered")
    report.check(f"feature:{namespace}/{name}", state == "Registered", {"state": state})

    states = {}
    private = PRIVATE_PROVIDERS if profile.get("network_mode") == "private" else ()
    private += COSMOS_PROVIDERS if private and profile.get("memory_backend") == "cosmos" else ()
    for provider in PROVIDERS + private:
        value = _json(run, ["provider", "show", "--namespace", provider, *scope], missing_ok=True)
        states[provider] = (value or {}).get("registrationState", "NotRegistered")
    report.check("providers", all(value == "Registered" for value in states.values()), states)
    for provider in private:
        if states[provider] != "Registered":
            memory = " with memory on" if provider in COSMOS_PROVIDERS else ""
            report.warn(f"{provider} is {states[provider]}: FA_NETWORK_MODE=private{memory} needs it. Someone "
                        f"allowed to change the subscription registers it (this check never does): "
                        f"az provider register --namespace {provider} --subscription {subscription}")
    for provider in ADVISORY_PROVIDERS:
        value = _json(run, ["provider", "show", "--namespace", provider, *scope], missing_ok=True)
        state = (value or {}).get("registrationState", "NotRegistered")
        if state != "Registered":
            report.warn(f"{provider} is {state}: Application Insights cannot create its smart detection alert "
                        "rule; FA does not need it, register it (approval) only if you want that alert")

    usage = {item["name"]["value"]: item for item in
             _json(run, ["cognitiveservices", "usage", "list", "--location", profile["location"], *scope]) or []}
    names = profile["names"]
    existing = _json(run, ["cognitiveservices", "account", "deployment", "list",
                           "--resource-group", profile.get("foundry_resource_group") or profile["resource_group"],
                           "--name", names["foundry_account"], *scope], missing_ok=True) or []
    current = {item["name"]: item for item in existing}
    quota = []
    for item in profile.get("model_deployments") or []:
        model, sku = item["model"]["name"], item["sku"]["name"]
        entry = next((usage[key] for key in usage_names(model, sku) if key in usage), None)
        deployed = current.get(item["name"])
        counted = 0
        if deployed and (deployed.get("sku") or {}).get("name") == sku \
                and ((deployed.get("properties") or {}).get("model") or {}).get("name") == model:
            counted = int((deployed.get("sku") or {}).get("capacity") or 0)
        needed = max(0, int(item["sku"]["capacity"]) - counted)
        if entry is None:
            report.warn(f"no {sku} quota entry for {model} in {profile['location']}; check the Foundry portal")
            quota.append({"deployment": item["name"], "needed": needed, "available": None, "ok": needed == 0})
            continue
        available = int(entry["limit"] - entry["currentValue"])
        quota.append({"deployment": item["name"], "usage": entry["name"]["value"], "needed": needed,
                      "available": available, "ok": available >= needed})
    report.check("model-quota", all(row.get("ok", True) for row in quota), quota)
    return report


def check_aca_ready(profile: dict[str, Any], run: Runner) -> Report:
    report = Report("aca-ready", profile)
    scope = ["--subscription", profile["subscription_id"]]
    name = profile["names"]["container_apps_environment"]
    environment = _json(run, ["containerapp", "env", "show", "--resource-group", profile["resource_group"],
                              "--name", name, *scope], missing_ok=True)
    properties = (environment or {}).get("properties") or {}
    static_ip = properties.get("staticIp")
    infra = properties.get("infrastructureResourceGroup")
    valid_static_ip = isinstance(static_ip, str) and bool(static_ip.strip())
    infra_name = infra.strip() if isinstance(infra, str) else ""
    report.check("environment", properties.get("provisioningState") == "Succeeded" and valid_static_ip,
                 {"name": name, "provisioningState": properties.get("provisioningState"),
                  "staticIp": static_ip, "infrastructureResourceGroup": infra})
    if not valid_static_ip:
        report.warn("Container Apps environment has no staticIp; verify AllowBringYourOwnPublicIpAddress "
                    "registration propagated before rebuilding the environment")
    if not infra_name:
        report.check("platform-network", False, {"infrastructureResourceGroup": infra,
                                                  "reason": "Container Apps did not report its infrastructure resource group"})
        report.warn("Container Apps environment has no infrastructureResourceGroup; do not deploy apps or jobs")
        return report
    if not valid_static_ip:
        report.check("platform-network", False, {"infrastructureResourceGroup": infra_name,
                                                  "reason": "staticIp is required to match the platform public IP"})
        return report
    public_ips = [item.get("ipAddress") for item in
                  _json(run, ["network", "public-ip", "list", "--resource-group", infra_name, *scope]) or []
                  if isinstance(item, dict)]
    balancers = [item.get("name") for item in
                 _json(run, ["network", "lb", "list", "--resource-group", infra_name, *scope]) or []
                 if isinstance(item, dict) and isinstance(item.get("name"), str) and item["name"].strip()]
    network_ok = static_ip in public_ips and bool(balancers)
    report.check("platform-network", network_ok,
                 {"infrastructureResourceGroup": infra_name, "staticIp": static_ip,
                  "public_ip_addresses": public_ips, "load_balancers": balancers})
    if not network_ok:
        report.warn("Container Apps platform network is incomplete: the infrastructure resource group must "
                    "contain a load balancer and a public IP whose address exactly matches staticIp")
    return report


def _profile(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise SystemExit(f"cannot read {path}: {error}") from None
    for key in ("tenant_id", "subscription_id", "location", "resource_group", "names"):
        if not value.get(key):
            raise SystemExit(f"{path} is missing {key}; pass the deployment.json from prepare_environment.py")
    return value


def main(argv: list[str] | None = None, run: Runner | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("mode", choices=("subscription", "aca-ready"))
    parser.add_argument("--deployment", type=Path, required=True, help="deployment.json from prepare_environment.py")
    parser.add_argument("--az", help="az executable, or the Azure CLI python.exe")
    args = parser.parse_args(argv)
    profile = _profile(args.deployment)
    runner = run or AzureCli(args.az)
    report = (check_subscription if args.mode == "subscription" else check_aca_ready)(profile, runner)
    report.value["ok"] = report.ok
    print(json.dumps(report.value, indent=2, ensure_ascii=False))
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
