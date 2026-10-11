"""Offline checks for the read-only subscription / ACA preflight (fake az; no Azure)."""

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SUB = "00000000-0000-0000-0000-000000000018"
TENANT = "00000000-0000-0000-0000-000000000019"


def load():
    spec = importlib.util.spec_from_file_location(
        "preflight_subscription", ROOT / "deploy" / "shared-context" / "preflight_subscription.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def profile():
    return {
        "tenant_id": TENANT, "subscription_id": SUB, "location": "westus3",
        "resource_group": "rg-fa", "foundry_resource_group": "rg-fa",
        "names": {"foundry_account": "cog-fa", "container_apps_environment": "cae-fa"},
        "model_deployments": [
            {"name": "gpt-4.1-mini", "model": {"format": "OpenAI", "name": "gpt-4.1-mini", "version": "2025-04-14"},
             "sku": {"name": "GlobalStandard", "capacity": 500}, "versionUpgradeOption": "NoAutoUpgrade"},
            {"name": "gpt-6.1-sol", "model": {"format": "OpenAI", "name": "gpt-6.1-sol", "version": "2026-09-29"},
             "sku": {"name": "GlobalStandard", "capacity": 500}, "versionUpgradeOption": "NoAutoUpgrade"},
        ],
    }


class FakeAz:
    """Answers by the first words of the az command; records every call to prove reads only."""

    def __init__(self, **overrides):
        self.calls = []
        self.answers = {
            "account show": {"id": SUB, "tenantId": TENANT},
            "feature show": {"properties": {"state": "Registered"}},
            "provider show": {"registrationState": "Registered"},
            "cognitiveservices usage": [
                {"name": {"value": "OpenAI.GlobalStandard.gpt4.1-mini"}, "currentValue": 50.0, "limit": 15000.0},
                {"name": {"value": "OpenAI.GlobalStandard.gpt-6.1-sol"}, "currentValue": 0.0, "limit": 1000.0},
            ],
            "cognitiveservices account": None,
            "containerapp env": {"properties": {"provisioningState": "Succeeded", "staticIp": "203.0.113.10",
                                                "infrastructureResourceGroup": "ME_cae-fa"}},
            "network public-ip": [{"name": "capp-svc-lb-ip", "ipAddress": "203.0.113.10"}],
            "network lb": [{"name": "capp-svc-lb"}],
        }
        self.answers.update(overrides)

    def __call__(self, args):
        self.calls.append(args)
        key = " ".join(args[:2])
        value = self.answers[key]
        if callable(value):
            value = value(args)
        if isinstance(value, Exception):
            return 1, "", str(value)
        if value is None:
            return 3, "", "(ResourceNotFound) The Resource was not found."
        return 0, json.dumps(value), ""


def run(tmp_path, mode, fake):
    module = load()
    path = tmp_path / "deployment.json"
    path.write_text(json.dumps(profile()), encoding="utf-8")
    return module.main([mode, "--deployment", str(path)], run=fake)


def report(capsys):
    return json.loads(capsys.readouterr().out)


def test_subscription_passes_and_matches_the_gpt41_quota_name(tmp_path, capsys):
    fake = FakeAz()
    assert run(tmp_path, "subscription", fake) == 0
    value = report(capsys)
    assert value["ok"] is True and value["warnings"] == []
    quota = next(item for item in value["checks"] if item["name"] == "model-quota")["detail"]
    assert quota[0]["usage"] == "OpenAI.GlobalStandard.gpt4.1-mini" and quota[0]["available"] == 14950
    providers = next(item for item in value["checks"] if item["name"] == "providers")["detail"]
    assert {"Microsoft.Insights", "Microsoft.SignalRService", "Microsoft.App", "Microsoft.Network"} <= set(providers)
    assert all(args[0] in {"account", "feature", "provider", "cognitiveservices"} for args in fake.calls)
    assert not any(word in args for args in fake.calls
                   for word in ("register", "create", "update", "delete", "set"))


def test_missing_byoip_feature_fails_before_any_deployment(tmp_path, capsys):
    fake = FakeAz(**{"feature show": {"properties": {"state": "NotRegistered"}}})
    assert run(tmp_path, "subscription", fake) == 1
    check = next(item for item in report(capsys)["checks"]
                 if item["name"] == "feature:Microsoft.Network/AllowBringYourOwnPublicIpAddress")
    assert check == {"name": "feature:Microsoft.Network/AllowBringYourOwnPublicIpAddress", "ok": False,
                     "detail": {"state": "NotRegistered"}}


def test_wrong_tenant_and_unregistered_provider_fail(tmp_path, capsys):
    fake = FakeAz(**{"account show": {"id": SUB, "tenantId": "00000000-0000-0000-0000-000000000000"},
                     "provider show": {"registrationState": "Registering"}})
    assert run(tmp_path, "subscription", fake) == 1
    checks = {item["name"]: item["ok"] for item in report(capsys)["checks"]}
    assert checks["tenant"] is False and checks["providers"] is False


def test_quota_counts_an_existing_same_model_deployment_on_rerun(tmp_path, capsys):
    usage = [
        {"name": {"value": "OpenAI.GlobalStandard.gpt4.1-mini"}, "currentValue": 14900.0, "limit": 15000.0},
        {"name": {"value": "OpenAI.GlobalStandard.gpt-6.1-sol"}, "currentValue": 900.0, "limit": 1000.0},
    ]
    existing = [{"name": "gpt-6.1-sol", "sku": {"name": "GlobalStandard", "capacity": 500},
                 "properties": {"model": {"name": "gpt-6.1-sol"}}}]
    fake = FakeAz(**{"cognitiveservices usage": usage, "cognitiveservices account": existing})
    assert run(tmp_path, "subscription", fake) == 1
    quota = {row["deployment"]: row for row in next(
        item for item in report(capsys)["checks"] if item["name"] == "model-quota")["detail"]}
    assert quota["gpt-4.1-mini"] == {"deployment": "gpt-4.1-mini", "usage": "OpenAI.GlobalStandard.gpt4.1-mini",
                                     "needed": 500, "available": 100, "ok": False}
    assert quota["gpt-6.1-sol"]["needed"] == 0 and quota["gpt-6.1-sol"]["ok"] is True


def test_missing_quota_blocks_when_additional_capacity_is_required(tmp_path, capsys):
    fake = FakeAz(**{"cognitiveservices usage": []})
    assert run(tmp_path, "subscription", fake) == 1
    value = report(capsys)
    assert len(value["warnings"]) == 2 and "gpt-4.1-mini" in value["warnings"][0]
    quota = next(item for item in value["checks"] if item["name"] == "model-quota")
    assert quota["ok"] is False
    assert all(row["available"] is None and row["ok"] is False for row in quota["detail"])


def test_missing_quota_is_allowed_when_existing_deployments_need_no_more_capacity(tmp_path, capsys):
    existing = [
        {"name": name, "sku": {"name": "GlobalStandard", "capacity": 500},
         "properties": {"model": {"name": name}}}
        for name in ("gpt-4.1-mini", "gpt-6.1-sol")
    ]
    fake = FakeAz(**{"cognitiveservices usage": [], "cognitiveservices account": existing})
    assert run(tmp_path, "subscription", fake) == 0
    value = report(capsys)
    quota = next(item for item in value["checks"] if item["name"] == "model-quota")
    assert quota["ok"] is True
    assert all(row["needed"] == 0 and row["available"] is None and row["ok"] for row in quota["detail"])


def test_unregistered_alerts_provider_is_only_a_warning(tmp_path, capsys):
    def provider(args):
        namespace = args[args.index("--namespace") + 1]
        return {"registrationState": "NotRegistered" if namespace == "Microsoft.AlertsManagement" else "Registered"}

    assert run(tmp_path, "subscription", FakeAz(**{"provider show": provider})) == 0
    value = report(capsys)
    providers = next(item for item in value["checks"] if item["name"] == "providers")
    assert value["ok"] is True and "Microsoft.AlertsManagement" not in providers["detail"]
    assert len(value["warnings"]) == 1 and value["warnings"][0].startswith("Microsoft.AlertsManagement is NotRegistered")


def test_aca_ready_requires_static_ip_and_matching_platform_ip(tmp_path, capsys):
    assert run(tmp_path, "aca-ready", FakeAz()) == 0
    assert report(capsys)["ok"] is True


@pytest.mark.parametrize(
    "overrides, expected_calls",
    [
        ({"containerapp env": {"properties": {"provisioningState": "Succeeded", "staticIp": "203.0.113.10"}}},
         ["containerapp"]),
        ({"containerapp env": {"properties": {"provisioningState": "Succeeded", "staticIp": "203.0.113.10",
                                              "infrastructureResourceGroup": ""}}},
         ["containerapp"]),
        ({"network public-ip": []}, ["containerapp", "network", "network"]),
        ({"network public-ip": [{"name": "platform-ip", "ipAddress": ""}]},
         ["containerapp", "network", "network"]),
        ({"network public-ip": [{"name": "other", "ipAddress": "1.2.3.4"}]},
         ["containerapp", "network", "network"]),
        ({"network lb": []}, ["containerapp", "network", "network"]),
        ({"containerapp env": {"properties": {"provisioningState": "Succeeded", "staticIp": None,
                                              "infrastructureResourceGroup": "ME_cae-fa"}}},
         ["containerapp"]),
    ],
    ids=["missing-infra-rg", "empty-infra-rg", "missing-public-ip", "empty-public-ip",
         "mismatched-public-ip", "missing-load-balancer", "missing-static-ip"],
)
def test_aca_ready_blocks_missing_or_incomplete_platform_network(tmp_path, capsys, overrides, expected_calls):
    fake = FakeAz(**overrides)
    assert run(tmp_path, "aca-ready", fake) == 1
    value = report(capsys)
    assert value["ok"] is False
    assert next(item for item in value["checks"] if item["name"] == "platform-network")["ok"] is False
    assert [args[0] for args in fake.calls] == expected_calls


def test_az_failure_other_than_not_found_stops_the_check(tmp_path):
    fake = FakeAz(**{"provider show": RuntimeError("AuthorizationFailed: no read access")})
    with pytest.raises(SystemExit, match="AuthorizationFailed"):
        run(tmp_path, "subscription", fake)
