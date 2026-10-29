"""azd hooks (deploy/azd/fa_azd.py, entra.py) and the WebIQ connection script with fakes only: no az, Graph or ARM call.

What must hold without a cloud run: Graph writes are idempotent by alternate key and never touch an existing service
principal; grants are the owner's only; the hooks stop on a failed read-only gate and never approve the empty-Table
initialization or ask for a key without the owner at a terminal; the WebIQ key goes only into one PUT body.
"""

import contextlib
import dataclasses
from datetime import datetime, timedelta, timezone
import importlib.util
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
TENANT = "0a1b2c3d-1111-4222-8333-444455556666"
SUBSCRIPTION = "1b2c3d4e-2222-4333-8444-555566667777"
OWNER = "2c3d4e5f-3333-4444-8555-666677778888"
OTHER = "9c3d4e5f-3333-4444-8555-666677778888"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def hooks():
    return load("fa_azd_hooks", ROOT / "deploy" / "azd" / "fa_azd.py")


@pytest.fixture(scope="module")
def entra(hooks):
    return sys.modules["entra"]


class FakeGraph:
    """In-memory Graph: applications by uniqueName, service principals by appId, grants and FICs."""

    def __init__(self, deny_grants=False, principals=()):
        self.apps, self.sps, self.grants, self.fics, self.calls = {}, {}, [], {}, []
        self.deny_grants = deny_grants
        for app_id in principals:
            self.sps[app_id] = {"id": f"sp-{app_id[:8]}", "appId": app_id}

    def __call__(self, method, path, body, headers):
        self.calls.append((method, path, body, dict(headers)))
        if path.startswith("/applications(uniqueName="):
            name = path.split("'")[1]
            if method == "PATCH":
                assert headers == {"Prefer": "create-if-missing"}
                app = self.apps.setdefault(name, {"id": f"obj-{name}", "appId": f"app-{name}"})
                app.update(json.loads(json.dumps(body)))
                return None
            return self.apps[name]
        if path.startswith("/servicePrincipals(appId="):
            app_id = path.split("'")[1]
            if app_id not in self.sps:
                raise LookupError("Request_ResourceNotFound")
            return self.sps[app_id]
        if path == "/servicePrincipals":
            assert method == "POST"
            self.sps[body["appId"]] = {"id": f"sp-{body['appId'][:8]}", "appId": body["appId"]}
            return self.sps[body["appId"]]
        if path.startswith("/oauth2PermissionGrants"):
            if method == "GET":
                client = path.split("'")[1]
                return {"value": [grant for grant in self.grants if grant["clientId"] == client]}
            if self.deny_grants:
                raise sys.modules["entra"].GraphForbidden("Authorization_RequestDenied")
            if method == "POST":
                self.grants.append({**body, "id": f"grant-{len(self.grants)}"})
                return None
            grant = next(item for item in self.grants if path.endswith(item["id"]))
            grant.update(body)
            return None
        if "/federatedIdentityCredentials" in path:
            if method == "GET":
                return {"value": list(self.fics.values())}
            if method == "POST":
                self.fics[body["name"]] = {**body, "id": "fic-1"}
            else:
                next(iter(self.fics.values())).update(body)
            return None
        raise AssertionError(f"unexpected Graph call {method} {path}")

    def writes(self):
        return [call for call in self.calls if call[0] != "GET"]


def test_names_are_unique_per_subscription_and_group(entra):
    one = entra.names("dev", TENANT, SUBSCRIPTION, "rg-dev")
    assert one == entra.names("dev", TENANT, SUBSCRIPTION.upper(), "RG-DEV")
    assert one != entra.names("dev", TENANT, OTHER, "rg-dev")
    assert one["api_audience"].startswith(f"api://{TENANT}/fa-dev-")
    assert entra.scope_id(one["api_unique_name"], "Behavior.Read") == entra.scope_id(one["api_unique_name"], "Behavior.Read")


def test_api_and_portal_apps_are_upserts_with_v1_tokens_and_two_scopes(entra):
    graph = FakeGraph(principals=[entra.GRAPH_APP_ID])
    keys = entra.names("dev", TENANT, SUBSCRIPTION, "rg-dev")
    api = entra.ensure_api(graph, "dev", keys["api_unique_name"], keys["api_audience"])
    body = graph.apps[keys["api_unique_name"]]
    assert body["api"]["requestedAccessTokenVersion"] == 1 and body["signInAudience"] == "AzureADMyOrg"
    assert [scope["value"] for scope in body["api"]["oauth2PermissionScopes"]] == ["Behavior.Read", "Behavior.Approve"]
    redirect = "https://ca-console-dev.example.westus3.azurecontainerapps.io/.auth/login/aad/callback"
    console = entra.ensure_console(graph, "dev", keys["console_unique_name"], api["app_id"],
                                   keys["api_unique_name"], redirect)
    web = graph.apps[keys["console_unique_name"]]["web"]
    assert web["redirectUris"] == [redirect] and web["implicitGrantSettings"]["enableIdTokenIssuance"] is True
    assert "passwordCredentials" not in graph.apps[keys["console_unique_name"]]
    first = len(graph.writes())
    # A rerun sends the same upserts (same object, same scope IDs) and creates no second principal.
    entra.ensure_api(graph, "dev", keys["api_unique_name"], keys["api_audience"])
    entra.ensure_console(graph, "dev", keys["console_unique_name"], api["app_id"], keys["api_unique_name"], redirect)
    again = graph.writes()[first:]
    assert [call[0] for call in again] == ["PATCH", "PATCH"] and len(graph.apps) == 2
    assert console["sp_id"] == graph.sps[console["app_id"]]["id"]


def test_existing_service_principals_are_only_read(entra):
    graph = FakeGraph(principals=[entra.GRAPH_APP_ID])
    entra.ensure_service_principal(graph, entra.GRAPH_APP_ID)
    assert graph.writes() == []
    entra.ensure_service_principal(graph, "04b07795-8ddb-461a-bbee-02f9e1bf7b46")
    assert graph.writes() == [("POST", "/servicePrincipals", {"appId": "04b07795-8ddb-461a-bbee-02f9e1bf7b46"}, {})]


def test_grants_are_the_owners_and_only_add_missing_scopes(entra):
    graph = FakeGraph()
    assert entra.ensure_grant(graph, "cli", "api", OWNER, entra.SCOPES) == "created"
    assert graph.grants[0]["consentType"] == "Principal" and graph.grants[0]["principalId"] == OWNER
    assert entra.ensure_grant(graph, "cli", "api", OWNER, entra.SCOPES) == "already"
    graph.grants[0]["scope"] = "Behavior.Read Other.Scope"
    assert entra.ensure_grant(graph, "cli", "api", OWNER, entra.SCOPES) == "updated"
    assert graph.grants[0]["scope"] == "Behavior.Read Other.Scope Behavior.Approve"
    # Another person's grant is not this owner's grant.
    assert entra.ensure_grant(graph, "cli", "api", OTHER, entra.SCOPES) == "created" and len(graph.grants) == 2
    with pytest.raises(entra.ConsentRequired):
        entra.ensure_grant(FakeGraph(deny_grants=True), "cli", "api", OWNER, entra.SCOPES)


def test_portal_federated_credential_is_created_once_and_follows_the_identity(entra):
    graph = FakeGraph()
    body = entra.fic_body("fa-portal-managed-identity", TENANT, "mi-1")
    assert body["issuer"] == f"https://login.microsoftonline.com/{TENANT}/v2.0"
    assert body["audiences"] == ["api://AzureADTokenExchange"]
    assert entra.ensure_fic(graph, "obj", body) == "created"
    assert entra.ensure_fic(graph, "obj", body) == "already"
    assert entra.ensure_fic(graph, "obj", entra.fic_body("fa-portal-managed-identity", TENANT, "mi-2")) == "updated"
    assert graph.fics["fa-portal-managed-identity"]["subject"] == "mi-2"


class Runner:
    """Fake process runner: routes az/azd/script calls to handlers and records them."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __call__(self, args, **kwargs):
        self.calls.append(list(args))
        text = " ".join(str(arg) for arg in args)
        for key, value in self.routes.items():
            if key in text:
                return value(args) if callable(value) else value
        raise AssertionError(f"unexpected command {text}")


def context(hooks, tmp_path, routes, **values):
    base = {"AZURE_ENV_NAME": "dev", "AZURE_SUBSCRIPTION_ID": SUBSCRIPTION, "AZURE_LOCATION": "westus3",
            "AZURE_TENANT_ID": TENANT}
    return hooks.Context(values={**base, **values}, run=Runner(routes), state_root=tmp_path, az_cmd=["az"],
                         python="python", tty=True, sleep=lambda seconds: None,
                         prompt=lambda text: "y" if "Team owner" in text else "")


def ok(value=None):
    return SimpleNamespace(code=0, stdout=json.dumps(value) if value is not None else "", stderr="")


def failed(stderr="boom", stdout=""):
    return SimpleNamespace(code=1, stdout=stdout, stderr=stderr)


CATALOG = json.loads((ROOT / "src" / "fa-shared-context-poc" / "model-catalog.json").read_text(encoding="utf-8"))


def region(missing=(), free=None):
    """`az cognitiveservices model list` and `usage list` of a region that lacks `missing` and has `free` quota."""
    offered = [{"kind": "AIServices", "model": {"name": item["model"]["name"], "version": item["model"]["version"],
                                                "skus": [{"name": "GlobalStandard"}]}}
               for item in CATALOG if item["deployment"] not in missing]
    usage = [{"name": {"value": f"OpenAI.GlobalStandard.{item['model']['name'].replace('gpt-4.1', 'gpt4.1')}"},
              "limit": (free or {}).get(item["deployment"], 1000), "currentValue": 0} for item in CATALOG]
    return {"cognitiveservices model list": ok(offered), "cognitiveservices usage list": ok(usage)}


NO_ACCOUNT = failed("Not Found({\"error\":{\"code\":\"ResourceNotFound\"}})")


def preprovision_routes(preflight, me=OWNER, tenant=TENANT, roles=("Owner",), directory=(), create_apps=True,
                        cli_sp=True, offer=None, foundry=NO_ACCOUNT):
    """Order matters: the first matching key answers (the /me/... reads before /me). `foundry` answers the ARM read
    of the environment's Foundry account (FA_NETWORK_MODE immutability); by default it does not exist yet."""
    return {**(offer or region()), "providers/Microsoft.CognitiveServices/accounts/": foundry,
            "account show": ok({"id": SUBSCRIPTION, "tenantId": tenant}),
            "docker info": ok(),
            "role assignment list": ok(list(roles)),
            "transitiveMemberOf": ok({"value": [{"roleTemplateId": role} for role in directory]}),
            "authorizationPolicy": ok({"defaultUserRolePermissions": {"allowedToCreateApps": create_apps}}),
            "servicePrincipals(appId=": ok({"id": "sp"}) if cli_sp else failed("Request_ResourceNotFound"),
            "graph.microsoft.com/v1.0/me": ok({"id": me, "userPrincipalName": "owner@example.test"}),
            "azd env set": ok(), "preflight_subscription.py": preflight}


@pytest.fixture(autouse=True)
def tools_on_path(hooks, monkeypatch):
    monkeypatch.setattr(hooks.shutil, "which", lambda name: f"/bin/{name}")


def test_a_deployer_without_entra_admin_gets_admin_steps_not_a_stop(hooks, tmp_path):
    ctx = context(hooks, tmp_path, preprovision_routes(ok({"ok": True})))
    rows = {row["item"]: row for row in hooks.prerequisites(ctx, {"oid": OWNER})}
    assert rows["Entra: register applications"]["state"] == "ok"
    assert rows["Entra: owner consent (3 delegated grants)"]["state"] == "admin"
    assert not [row for row in rows.values() if row["state"] == "blocked"]
    hooks.preprovision(ctx)  # the consent comes later from an admin; nothing billed is blocked by it


def test_a_missing_azure_cli_principal_stops_a_non_admin_before_billing(hooks, tmp_path):
    ctx = context(hooks, tmp_path, preprovision_routes(ok({"ok": True}), cli_sp=False))
    rows = {row["item"]: row for row in hooks.prerequisites(ctx, {"oid": OWNER})}
    assert rows["Entra: Azure CLI service principal"]["state"] == "blocked"
    assert "az ad sp create --id 04b07795" in rows["Entra: Azure CLI service principal"]["detail"]
    with pytest.raises(SystemExit, match="Azure CLI service principal"):
        hooks.preprovision(ctx)
    assert not any("preflight_subscription.py" in " ".join(call) for call in ctx.run.calls)


def test_an_entra_refusal_is_a_readable_stop_not_a_traceback(hooks, monkeypatch, capsys):
    def refused(ctx):
        raise sys.modules["entra"].GraphForbidden("Authorization_RequestDenied")
    monkeypatch.setitem(hooks.PHASES, "postprovision", refused)
    monkeypatch.setenv("AZURE_ENV_NAME", "dev")
    assert hooks.main(["postprovision"]) == 1
    assert "事前準備 C" in capsys.readouterr().err


def test_an_entra_admin_needs_no_admin_step(hooks, tmp_path):
    ctx = context(hooks, tmp_path, preprovision_routes(ok({"ok": True}), roles=("Contributor", "User Access Administrator"),
                                                       directory=["158c047a-c907-4556-b7ef-446551a6b5f7"],
                                                       create_apps=False, cli_sp=False))
    assert {row["state"] for row in hooks.prerequisites(ctx, {"oid": OWNER})} == {"ok"}


@pytest.mark.parametrize("change, item", [
    ({"roles": ("Contributor",)}, "Subscription"),
    ({"create_apps": False}, "register applications"),
])
def test_missing_rights_stop_before_anything_is_billed(hooks, tmp_path, change, item):
    ctx = context(hooks, tmp_path, preprovision_routes(ok({"ok": True}), **change))
    with pytest.raises(SystemExit, match=item):
        hooks.preprovision(ctx)
    assert not any("preflight_subscription.py" in " ".join(call) for call in ctx.run.calls)


def test_docker_must_be_running_before_provisioning(hooks, tmp_path):
    ctx = context(hooks, tmp_path, {**preprovision_routes(ok({"ok": True})), "docker info": failed("daemon")})
    with pytest.raises(SystemExit, match="Docker engine running"):
        hooks.preprovision(ctx)


def test_admin_consent_hand_off_is_owner_only_and_self_contained(hooks, tmp_path):
    ctx = context(hooks, tmp_path, {})
    folder = hooks.admin_consent_files(ctx, OWNER, (("cli", "api", ("Behavior.Read", "Behavior.Approve")),
                                                    ("portal", "graph", ("openid", "profile"))))
    grants = [json.loads((folder / f"grant-{index}.json").read_text(encoding="utf-8")) for index in (1, 2)]
    assert {grant["consentType"] for grant in grants} == {"Principal"}
    assert {grant["principalId"] for grant in grants} == {OWNER}
    assert grants[0]["scope"] == "Behavior.Read Behavior.Approve"
    readme = (folder / "README.txt").read_text(encoding="utf-8")
    assert readme.count("az rest --method POST") == 2 and TENANT in readme and "AllPrincipals" not in readme


def test_preprovision_defaults_the_owner_and_runs_the_read_only_preflight(hooks, tmp_path):
    ctx = context(hooks, tmp_path, preprovision_routes(ok({"ok": True})), FA_MODEL_CAPACITY_JSON='{"gpt-5.4-mini": 50}')
    hooks.preprovision(ctx)
    assert ctx.values["FA_TEAM_OWNER_OID"] == OWNER
    document = json.loads((tmp_path / "dev" / "fa" / "preflight-deployment.json").read_text(encoding="utf-8"))
    assert document["resource_group"] == "rg-dev" and document["subscription_id"] == SUBSCRIPTION
    assert {item["name"]: item["sku"]["capacity"] for item in document["model_deployments"]}["gpt-5.4-mini"] == 50
    preflight = next(call for call in ctx.run.calls if "preflight_subscription.py" in " ".join(call))
    assert "subscription" in preflight
    # Read only: the hook never registers, creates or deploys anything itself.
    assert not any(word in " ".join(call) for call in ctx.run.calls
                   for word in ("register", "deployment group create", "--method POST", "--method PATCH"))


def _owner_asks(ctx):
    asked = []
    inner = ctx.prompt
    ctx.prompt = lambda text: asked.append(text) or inner(text)
    return asked


@pytest.mark.parametrize("answer", ["y", "yes", "Y", "YES", " Yes "])
def test_preprovision_confirms_the_deployer_as_owner_before_recording_it(hooks, tmp_path, capsys, answer):
    ctx = context(hooks, tmp_path, preprovision_routes(ok({"ok": True})))
    ctx.prompt = lambda text: answer if "Team owner" in text else ""
    asked = _owner_asks(ctx)
    hooks.preprovision(ctx)
    owner_questions = [text for text in asked if "Team owner" in text]
    assert owner_questions == ["Is owner@example.test the Team owner who will sign in to the Portal? [y/N]: "]
    assert ctx.values["FA_TEAM_OWNER_OID"] == OWNER
    assert f"FA: Team owner = the signed-in deployer owner@example.test ({OWNER})" in capsys.readouterr().out


@pytest.mark.parametrize("answer", ["n", "", "no", "maybe"])
def test_preprovision_stops_before_anything_when_the_deployer_is_not_the_owner(hooks, tmp_path, answer):
    ctx = context(hooks, tmp_path, preprovision_routes(ok({"ok": True})))
    ctx.prompt = lambda text: answer
    with pytest.raises(hooks.Stop, match="azd env set FA_TEAM_OWNER_OID"):
        hooks.preprovision(ctx)
    assert "FA_TEAM_OWNER_OID" not in ctx.values
    assert not any("azd env set" in " ".join(call) or "preflight_subscription.py" in " ".join(call)
                   or "role assignment" in " ".join(call) for call in ctx.run.calls)


def test_preprovision_never_asks_when_the_owner_is_already_set(hooks, tmp_path):
    other = OTHER
    ctx = context(hooks, tmp_path, preprovision_routes(ok({"ok": True})), FA_TEAM_OWNER_OID=other)
    ctx.prompt = lambda text: pytest.fail("asked about the owner") if "Team owner" in text else ""
    hooks.preprovision(ctx)
    assert ctx.values["FA_TEAM_OWNER_OID"] == other


def test_preprovision_without_a_terminal_uses_the_deployer_and_says_so(hooks, tmp_path, capsys):
    ctx = context(hooks, tmp_path, preprovision_routes(ok({"ok": True})))
    ctx.tty = False
    ctx.prompt = lambda text: pytest.fail("prompted without a terminal")
    hooks.preprovision(ctx)
    assert ctx.values["FA_TEAM_OWNER_OID"] == OWNER
    assert "FA: Team owner = the signed-in deployer" in capsys.readouterr().out


@pytest.mark.parametrize("change, message", [
    ({"routes": {"preflight_subscription.py": failed(stdout='{"ok": false}')}}, "subscription preflight failed"),
    ({"routes": {"account show": ok({"id": SUBSCRIPTION, "tenantId": OTHER})}}, "use one tenant"),
    ({"routes": {"graph.microsoft.com/v1.0/me": ok({})}}, "not signed in as a user"),
    ({"values": {"FA_MODEL_CAPACITY_JSON": '{"not-in-catalog": 1}'}}, "outside the model catalog"),
    ({"values": {"AZURE_ENV_NAME": "Dev_Env"}}, "AZURE_ENV_NAME"),
    ({"values": {"FA_TEAM_OWNER_OID": "neal"}}, "FA_TEAM_OWNER_OID"),
    ({"values": {"AZURE_LOCATION": ""}}, "azd env new"),
])
def test_preprovision_stops_on_each_gate(hooks, tmp_path, change, message):
    routes = {**preprovision_routes(ok({"ok": True})), **change.get("routes", {})}
    ctx = context(hooks, tmp_path, routes, **change.get("values", {}))
    with pytest.raises(SystemExit, match=message):
        hooks.preprovision(ctx)


def test_bicep_unique_string_matches_bicep_console(hooks):
    # Expected values printed by `bicep console` (Bicep 0.48.1) for the same uniqueString() arguments.
    assert hooks.bicep_unique_string("abc") == "cgtzqvhu4i23s"
    assert hooks.bicep_unique_string("abcdefg-hijk") == "bjmntbnflvbu2"
    assert hooks.bicep_unique_string("/subscriptions/00000000-0000-0000-0000-000000000000", "dev",
                                     "westus3") == "if6f5ucvkvxxu"
    assert hooks.bicep_unique_string(f"/subscriptions/{SUBSCRIPTION}", "dev", "westus3") == "k5daenmuhikji"


ACCOUNT = "cog-example000000"  # main.bicep's names.foundryAccount for SUBSCRIPTION, dev, westus3


def test_the_account_name_is_main_biceps_naming_rule(hooks, tmp_path):
    # foundry_account_name() repeats main.bicep's names.foundryAccount; this pins the Bicep side of the copy.
    lines = {line.strip() for line in (ROOT / "infra" / "azd" / "main.bicep").read_text(encoding="utf-8").splitlines()}
    assert {"var env = toLower(environmentName)", "var token = toLower(uniqueString(subscription().id, env, location))",
            "foundryAccount: 'cog-${token}'"} <= lines
    # ARM's subscription().id is lowercase and env is lowered; the location is hashed as given.
    ctx = context(hooks, tmp_path, {}, AZURE_ENV_NAME="Dev", AZURE_SUBSCRIPTION_ID=SUBSCRIPTION.upper())
    assert hooks.foundry_account_name(ctx) == ACCOUNT == "cog-" + hooks.bicep_unique_string(
        f"/subscriptions/{SUBSCRIPTION}", "dev", "westus3")


def test_a_recorded_account_other_than_the_bicep_name_stops_before_any_write(hooks, tmp_path):
    ctx = context(hooks, tmp_path, preprovision_routes(ok({"ok": True})), FA_FOUNDRY_ACCOUNT="cog-other")
    with pytest.raises(hooks.Stop, match=f"FA_FOUNDRY_ACCOUNT is cog-other, but main.bicep's naming rule gives "
                                         f"{ACCOUNT}.*nothing was changed"):
        hooks.preprovision(ctx)
    assert writes_nothing(ctx) and not any("Microsoft.CognitiveServices/accounts/" in " ".join(call)
                                           for call in ctx.run.calls)


def foundry_account(injected):
    """The ARM GET of an existing Foundry account, with or without the private-mode network injection."""
    injections = [{"scenario": "agent", "subnetArmId": f"/subscriptions/{SUBSCRIPTION}/resourceGroups/rg-dev/providers/"
                                                       "Microsoft.Network/virtualNetworks/vnet-dev/subnets/snet-agents",
                   "useMicrosoftManagedNetwork": False}] if injected else None
    return ok({"name": ACCOUNT, "properties": {"networkInjections": injections}})


def writes_nothing(ctx):
    calls = [" ".join(map(str, call)) for call in ctx.run.calls]
    return not any(word in call for call in calls
                   for word in ("azd env set", "preflight_subscription.py", "--method put", "--method PATCH",
                                "deployment group create", "register"))


PRIVATE = {"FA_NETWORK_MODE": "private", "FA_ENABLE_MEMORY": "false"}


@pytest.mark.parametrize("values, foundry, message", [
    ({"FA_NETWORK_MODE": "isolated"}, NO_ACCOUNT, "must be public or private"),
    ({**PRIVATE, "AZURE_LOCATION": "ukwest"}, NO_ACCOUNT, "ukwest is not in it.*one of: australiaeast, .*, westus3;"),
    ({**PRIVATE, "AZURE_LOCATION": "UKWest "}, NO_ACCOUNT, "ukwest is not in it"),
    ({}, foundry_account(injected=True), f"{ACCOUNT} already exists in rg-dev as FA_NETWORK_MODE=private"),
    (PRIVATE, foundry_account(injected=False), "as FA_NETWORK_MODE=public"),
])
def test_preprovision_refuses_each_network_mode_conflict_before_any_write(hooks, tmp_path, values, foundry, message):
    ctx = context(hooks, tmp_path, preprovision_routes(ok({"ok": True}), foundry=foundry), **values)
    with pytest.raises(hooks.Stop, match=message):
        hooks.preprovision(ctx)
    assert writes_nothing(ctx) and "FA_TEAM_OWNER_OID" not in ctx.values


READ_FAILURES = {
    "AuthorizationFailed": failed("Forbidden({\"error\":{\"code\":\"AuthorizationFailed\",\"message\":\"no read\"}})"),
    "MissingSubscriptionRegistration": failed(
        "Bad Request({\"error\":{\"code\":\"MissingSubscriptionRegistration\",\"message\":\"The subscription is not "
        "registered to use namespace 'Microsoft.CognitiveServices'\"}})"),
    "BadGateway": failed("Bad Gateway({\"error\":{\"code\":\"BadGateway\"}})"),
}


@pytest.mark.parametrize("code, stops", [("AuthorizationFailed", True), ("MissingSubscriptionRegistration", False),
                                         ("BadGateway", True)])
def test_an_unreadable_foundry_account_stops_unless_its_provider_is_unregistered(hooks, tmp_path, code, stops):
    ctx = context(hooks, tmp_path, preprovision_routes(ok({"ok": True}), foundry=READ_FAILURES[code]))
    if stops:
        with pytest.raises(hooks.Stop, match=f"Cannot confirm whether Foundry account {ACCOUNT} exists, so "
                                             f"FA_NETWORK_MODE cannot be checked.*az sign-in and permissions.*"
                                             f"nothing was changed.*{code}"):
            hooks.preprovision(ctx)
        assert writes_nothing(ctx) and "FA_TEAM_OWNER_OID" not in ctx.values
    else:
        # No account can exist without the provider; the subscription preflight that follows names it.
        hooks.preprovision(ctx)
        assert any("preflight_subscription.py" in " ".join(call) for call in ctx.run.calls)
        read = next(index for index, call in enumerate(ctx.run.calls)
                    if "Microsoft.CognitiveServices/accounts/" in " ".join(call))
        assert writes_nothing(SimpleNamespace(run=SimpleNamespace(calls=ctx.run.calls[:read + 1])))


@pytest.mark.parametrize("values, backend", [
    ({}, "foundry"), ({"FA_ENABLE_MEMORY": "false"}, "foundry"), ({"FA_NETWORK_MODE": "private"}, "cosmos"),
    (PRIVATE, "foundry"),  # private without memory: no store and no Cosmos DB
])
def test_preprovision_writes_the_memory_backend_bicep_reads(hooks, tmp_path, values, backend):
    # ADR-0011: private keeps memory on (memory is on unless FA_ENABLE_MEMORY=false), in Cosmos DB, not refused.
    ctx = context(hooks, tmp_path, preprovision_routes(ok({"ok": True})), **values)
    hooks.preprovision(ctx)
    assert ctx.values["FA_MEMORY_BACKEND"] == backend
    assert ["azd", "env", "set", "FA_MEMORY_BACKEND", backend] in ctx.run.calls


@pytest.mark.parametrize("values, injected", [({}, False), (PRIVATE, True)])
def test_an_existing_account_of_the_same_mode_passes(hooks, tmp_path, values, injected):
    ctx = context(hooks, tmp_path, preprovision_routes(ok({"ok": True}), foundry=foundry_account(injected)), **values)
    hooks.preprovision(ctx)
    read, *hosts = [call for call in ctx.run.calls if "Microsoft.CognitiveServices/accounts/" in " ".join(call)]
    assert read[:4] == ["az", "rest", "--method", "get"]
    # private also lists the project's capability hosts, read only (a Failed one would stop provision)
    assert [call[:4] + [call[5].split(ACCOUNT)[1]] for call in hosts] == (
        [["az", "rest", "--method", "get", "/projects/fa-dev/capabilityHosts?api-version=2025-06-01"]] if injected
        else [])
    assert read[5] == (f"/subscriptions/{SUBSCRIPTION}/resourceGroups/rg-dev/providers/Microsoft.CognitiveServices/"
                       f"accounts/{ACCOUNT}?api-version=2025-06-01")


@pytest.mark.parametrize("values", [{"AZURE_LOCATION": "WestUS3"}, {"FA_ENABLE_MEMORY": " False "}])
def test_private_reads_the_region_and_memory_flag_in_any_case(hooks, tmp_path, values):
    # Same as models.memory_on and profile._flag: surrounding spaces and case do not matter.
    ctx = context(hooks, tmp_path, preprovision_routes(ok({"ok": True})), **{**PRIVATE, **values})
    hooks.preprovision(ctx)


@pytest.mark.parametrize("values, backend", [(PRIVATE, "foundry"), ({"FA_NETWORK_MODE": "private"}, "cosmos")])
def test_a_new_private_environment_passes_and_asks_the_preflight_for_its_providers(hooks, tmp_path, values, backend):
    # The preflight checks Microsoft.DocumentDB only for the Cosmos DB memory (memory on), not for every private.
    ctx = context(hooks, tmp_path, preprovision_routes(ok({"ok": True})), **values)
    hooks.preprovision(ctx)
    document = json.loads((tmp_path / "dev" / "fa" / "preflight-deployment.json").read_text(encoding="utf-8"))
    assert document["network_mode"] == "private" and document["memory_backend"] == backend


def test_public_keeps_every_region_main_bicep_allows(hooks, tmp_path):
    # ukwest is a pre-existing inconsistency (allowed by main.bicep, absent from the Agent Service table): only
    # private refuses it, public behaves as before.
    ctx = context(hooks, tmp_path, preprovision_routes(ok({"ok": True})), AZURE_LOCATION="ukwest")
    hooks.preprovision(ctx)
    document = json.loads((tmp_path / "dev" / "fa" / "preflight-deployment.json").read_text(encoding="utf-8"))
    assert document["network_mode"] == "public" and document["location"] == "ukwest"


def test_postprovision_stops_before_entra_when_the_aca_platform_is_not_ready(hooks, tmp_path):
    ctx = context(hooks, tmp_path, {"account show": ok({"id": SUBSCRIPTION, "tenantId": TENANT}), "azd env set": ok(),
                                    "preflight_subscription.py": failed(stdout='{"ok": false}')},
                  FA_CONTAINER_APPS_ENVIRONMENT="cae-dev")
    with pytest.raises(SystemExit, match="not ready"):
        hooks.postprovision(ctx)
    assert not any("graph.microsoft.com" in " ".join(call) for call in ctx.run.calls)


def test_consent_without_admin_rights_is_reported_not_forced(hooks, tmp_path, monkeypatch, capsys):
    entra = sys.modules["entra"]
    graph = FakeGraph(deny_grants=True,
                      principals=["app-api", entra.GRAPH_APP_ID, hooks.AZURE_CLI_APP_ID, "app-console"])
    ctx = context(hooks, tmp_path, {"azd env set": ok()}, FA_TEAM_OWNER_OID=OWNER, FA_API_APP_ID="app-api",
                  FA_CONSOLE_APP_ID="app-console")
    monkeypatch.setattr(ctx, "graph", graph)
    assert hooks.consent(ctx) is False
    assert graph.grants == [] and all(call[0] == "GET" or "oauth2PermissionGrants" in call[1] for call in graph.calls)
    assert ctx.values["FA_CONSENT_PENDING"] == "true"
    assert "admin-consent" in capsys.readouterr().out
    assert len(list((tmp_path / "dev" / "fa" / "admin-consent").glob("grant-*.json"))) == 3
    with pytest.raises(SystemExit, match="still missing"):
        hooks.consent_only(ctx)


def plan_value():
    observed = datetime(2026, 10, 10, 8, 0, tzinfo=timezone.utc)
    return SimpleNamespace(request_id="dev-fresh-001", installation_id="dev-native-v1", configuration_sha256="c" * 64,
                           sha256="abcdef0123456789" + "0" * 48, empty_table_observed_at=observed)


def approval_context(hooks, tmp_path, me=OWNER, answer="y", tty=True, **values):
    ctx = context(hooks, tmp_path, {"graph.microsoft.com/v1.0/me": ok({"id": me})}, FA_TEAM_OWNER_OID=OWNER, **values)
    ctx.tty, ctx.prompt = tty, (lambda text: answer)
    ctx.now = lambda: datetime(2026, 10, 10, 9, 0, tzinfo=timezone.utc)
    return ctx


def test_an_authorization_for_another_plan_is_never_reused(hooks, tmp_path, monkeypatch):
    work = tmp_path / "dev" / "fa" / "fresh"
    work.mkdir(parents=True)
    (work / "authorization.json").write_text(json.dumps({
        "plan_sha256": "f" * 64, "configuration_sha256": "c" * 64, "expires_at": "2026-10-10T09:10:00Z"}),
        encoding="utf-8")
    plan = SimpleNamespace(sha256="a" * 64, configuration_sha256="c" * 64,
                           model_dump_json=lambda indent: "{}")
    fake = SimpleNamespace(plan_from_log=lambda lines: ({}, {}), rebuild_plan=lambda *args: plan)
    monkeypatch.setitem(sys.modules, "run_fresh_job", fake)
    monkeypatch.setattr(hooks, "_fresh", lambda *args: [])
    ctx = context(hooks, tmp_path, {})
    with pytest.raises(SystemExit, match="approves another plan"):
        hooks.fresh_install(ctx, "img", tmp_path / "runtime")
    assert "FA_DEPLOY_CONFIG" not in os.environ  # set only for run_fresh_job's import


@pytest.mark.parametrize("answer", ["y", "yes", " Y ", "YES"])
def test_the_owner_approves_the_exact_plan_in_a_terminal(hooks, tmp_path, capsys, answer):
    path = tmp_path / "authorization.json"
    asked = []
    ctx = approval_context(hooks, tmp_path, answer=answer)
    ctx.prompt = lambda text: asked.append(text) or answer
    hooks.authorize(ctx, plan_value(), path)
    assert asked == ["Approve this initialization of the plan above? [y/N]: "]
    shown = capsys.readouterr().out
    assert all(item in shown for item in ("request dev-fresh-001", "installation dev-native-v1", "c" * 64,
                                          plan_value().sha256))
    value = json.loads(path.read_text(encoding="utf-8"))
    assert value["authorizing_human_oid"] == OWNER and value["plan_sha256"] == plan_value().sha256
    assert value["configuration_sha256"] == "c" * 64
    window = (datetime.fromisoformat(value["expires_at"].replace("Z", "+00:00"))
              - datetime.fromisoformat(value["not_before"].replace("Z", "+00:00")))
    assert timedelta(0) < window <= timedelta(minutes=15)
    with pytest.raises(FileExistsError):  # an approval file is never replaced
        hooks.authorize(approval_context(hooks, tmp_path), plan_value(), path)


@pytest.mark.parametrize("kwargs, message", [
    ({"tty": False}, "interactive terminal"),
    ({"me": OTHER}, "only the Team owner"),
    ({"answer": "abcdef012345"}, "not approved"),  # the old 12-character hash answer no longer approves
    ({"answer": ""}, "not approved"),
    ({"answer": "n"}, "not approved"),
    ({"answer": "yep"}, "not approved"),
])
def test_nobody_else_and_nothing_automatic_approves_the_initialization(hooks, tmp_path, kwargs, message):
    path = tmp_path / "authorization.json"
    with pytest.raises(SystemExit, match=message):
        hooks.authorize(approval_context(hooks, tmp_path, **kwargs), plan_value(), path)
    assert not path.exists()


def no_prompt(text):
    raise AssertionError(f"asked {text!r}")


@pytest.mark.parametrize("tty", [True, False])
def test_the_owner_can_approve_the_initialization_ahead_by_env_name(hooks, tmp_path, capsys, tty):
    """Neal 2026-10-24: FA_APPROVE_INITIALIZATION=<azd env name> replaces the typed y, never the owner check."""
    from shared_context.behavior_fresh_installation import FreshInstallationAuthorization
    path = tmp_path / "authorization.json"
    ctx = approval_context(hooks, tmp_path, tty=tty, FA_APPROVE_INITIALIZATION="dev")
    ctx.prompt = no_prompt
    hooks.authorize(ctx, plan_value(), path)
    shown = capsys.readouterr().out
    assert all(item in shown for item in ("request dev-fresh-001", "c" * 64, plan_value().sha256,
                                          "FA_APPROVE_INITIALIZATION=dev (no typed answer)"))
    value = json.loads(path.read_text(encoding="utf-8"))
    assert value["decision_id"] == "dev-fresh-preapproved-20261010T090000Z"
    assert value["authorizing_human_oid"] == OWNER and value["plan_sha256"] == plan_value().sha256
    assert value["approval_source"] == "operator-approved-file"
    FreshInstallationAuthorization.model_validate_json(path.read_text(encoding="utf-8"))  # the Jobs' own schema


def test_a_typed_approval_keeps_the_plain_decision_id(hooks, tmp_path):
    from shared_context.behavior_fresh_installation import FreshInstallationAuthorization
    path = tmp_path / "authorization.json"
    hooks.authorize(approval_context(hooks, tmp_path), plan_value(), path)
    assert json.loads(path.read_text(encoding="utf-8"))["decision_id"] == "dev-fresh-20261010T090000Z"
    FreshInstallationAuthorization.model_validate_json(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("tty", [True, False])
@pytest.mark.parametrize("me, value, message", [
    (OTHER, "dev", "only the Team owner"),
    (OWNER, "prod", "not this environment's name"),
    (OWNER, "Dev", "not this environment's name"),  # exact match, as FA_CONFIRM_DELETE
    (OWNER, "de", "not this environment's name"),   # a prefix of the name
    (OWNER, "dev-2", "not this environment's name"),  # the name is a prefix of it
])
def test_a_pre_approval_never_skips_the_owner_or_names_another_env(hooks, tmp_path, tty, me, value, message):
    path = tmp_path / "authorization.json"
    ctx = approval_context(hooks, tmp_path, me=me, tty=tty, FA_APPROVE_INITIALIZATION=value)
    ctx.prompt = no_prompt
    with pytest.raises(SystemExit, match=message):
        hooks.authorize(ctx, plan_value(), path)
    assert not path.exists()


def test_without_a_pre_approval_a_terminal_still_asks(hooks, tmp_path):
    asked = []
    ctx = approval_context(hooks, tmp_path, FA_APPROVE_INITIALIZATION="")  # empty reads as unset
    ctx.prompt = lambda text: asked.append(text) or "n"
    with pytest.raises(SystemExit, match="not approved"):
        hooks.authorize(ctx, plan_value(), tmp_path / "authorization.json")
    assert asked == ["Approve this initialization of the plan above? [y/N]: "]
    with pytest.raises(SystemExit, match="interactive terminal.*FA_APPROVE_INITIALIZATION=dev"):
        hooks.authorize(approval_context(hooks, tmp_path, tty=False), plan_value(), tmp_path / "authorization.json")
    assert not (tmp_path / "authorization.json").exists()


@pytest.mark.parametrize("cut", [EOFError, KeyboardInterrupt])
def test_a_closed_or_interrupted_prompt_stops_and_writes_nothing(hooks, tmp_path, cut):
    """azd exiting or Ctrl+C while a question waits: a Stop, not a traceback, and nothing recorded. The 2026-10-21
    azd e2e run saw EOFError inside input() after azd had exited; Ctrl+C is the likely cause, not confirmed."""
    def gone(text):
        raise cut
    path = tmp_path / "authorization.json"
    ctx = approval_context(hooks, tmp_path)
    ctx.prompt, ctx.resume = gone, "azd hooks run postdeploy"
    with pytest.raises(hooks.Stop, match="no answer was read.*`azd hooks run postdeploy`"):
        hooks.authorize(ctx, plan_value(), path)
    assert not path.exists()
    ctx = context(hooks, tmp_path, preprovision_routes(ok({"ok": True})))
    ctx.prompt = gone
    with pytest.raises(hooks.Stop, match="no answer was read"):
        hooks.preprovision(ctx)
    assert "FA_TEAM_OWNER_OID" not in ctx.values
    assert not any("azd env set" in " ".join(call) for call in ctx.run.calls)


@pytest.mark.parametrize("phase, resume", [("preprovision", "azd up"), ("deploy", "azd hooks run postdeploy"),
                                           ("predown", "azd down")])
def test_a_cut_prompt_names_the_command_that_asks_again(hooks, tmp_path, monkeypatch, capsys, phase, resume):
    """main() picks the rerun command per phase; the model choice, the approval and the backup-lock confirmation
    each print it. Only the runner and prompt of main()'s own Context are replaced."""
    def gone(text):
        raise EOFError
    path = tmp_path / "authorization.json"
    site, routes = {
        "preprovision": (hooks.pick_models, region()),
        "deploy": (lambda ctx: hooks.authorize(ctx, plan_value(), path),
                   {"graph.microsoft.com/v1.0/me": ok({"id": OWNER})}),
        "predown": (hooks.predown, {**outside(hooks), "lock list": ok([LOCK])}),
    }[phase]
    runner = Runner(routes)
    monkeypatch.setitem(hooks.PHASES, phase, lambda ctx: site(dataclasses.replace(
        ctx, run=runner, prompt=gone, tty=True, state_root=tmp_path, az_cmd=["az"])))
    for key, value in {"AZURE_ENV_NAME": "dev", "AZURE_SUBSCRIPTION_ID": SUBSCRIPTION, "AZURE_LOCATION": "westus3",
                       "AZURE_TENANT_ID": TENANT, "FA_TEAM_OWNER_OID": OWNER, "FA_AZD_VENV": "1"}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("FA_MODELS", raising=False)
    monkeypatch.delenv("FA_CONFIRM_DELETE", raising=False)
    monkeypatch.delenv("FA_APPROVE_INITIALIZATION", raising=False)
    assert hooks.main([phase]) == 1
    assert f"no answer was read (input closed or interrupted): nothing was approved and no answer was saved. " \
           f"Run `{resume}`" in capsys.readouterr().err
    assert not path.exists()
    assert not any("azd env set" in " ".join(call) or "delete" in call for call in runner.calls)


def test_a_started_fresh_action_with_unknown_outcome_is_never_started_again(hooks, tmp_path):
    ctx = context(hooks, tmp_path, {})
    work = tmp_path / "fresh"
    work.mkdir()
    (work / "apply-intent.json").write_text("{}", encoding="utf-8")
    with pytest.raises(SystemExit, match="never started twice"):
        hooks._fresh(ctx, "apply", "cr.azurecr.io/fa-behavior-api@sha256:" + "0" * 64, tmp_path / "runtime", work)
    assert ctx.run.calls == []


def test_webiq_is_off_by_default_and_a_rerun_needs_no_terminal(hooks, tmp_path):
    ctx = context(hooks, tmp_path, {})
    hooks.webiq(ctx)
    assert ctx.run.calls == []
    # Without a terminal the step still runs: an existing connection is only read back (the script asks only when absent).
    ctx = context(hooks, tmp_path, {"new_webiq_connection.py": ok(), "new_toolbox.py": ok()}, FA_ENABLE_WEBIQ="true")
    ctx.tty = False
    hooks.webiq(ctx)
    assert [Path(call[3]).name for call in ctx.run.calls] == ["new_webiq_connection.py", "new_toolbox.py"]
    for call in ctx.run.calls:  # the key is never an argument; the script asks for it itself
        assert all("key" not in str(arg).lower() or arg.endswith(".py") for arg in call)


class FakeHttp:
    def __init__(self, exists):
        self.exists, self.requests = exists, []

    def get(self, url, headers):
        self.requests.append(("GET", url, None))
        status = 200 if self.exists or any(item[0] == "PUT" for item in self.requests) else 404
        return SimpleNamespace(status_code=status, json=lambda: {
            "name": "dev-webiq-key", "properties": {"category": "RemoteTool", "authType": "CustomKeys",
                                                    "target": "https://api.microsoft.ai/v3/mcp"}})

    def put(self, url, headers, content):
        self.requests.append(("PUT", url, content))
        return SimpleNamespace(status_code=201)


class UnreadAfterPut(FakeHttp):
    """Absent before the PUT, unreadable after it."""

    def __init__(self):
        super().__init__(exists=False)

    def get(self, url, headers):
        self.requests.append(("GET", url, None))
        status = 503 if any(item[0] == "PUT" for item in self.requests) else 404
        return SimpleNamespace(status_code=status, json=lambda: {})


def test_webiq_key_goes_only_into_one_put_and_is_never_asked_twice(tmp_path, capsys, monkeypatch):
    script = load("new_webiq_connection_azd", ROOT / "deploy" / "stage" / "new_webiq_connection.py")
    profile = {"environment": "dev", "subscription_id": SUBSCRIPTION, "resource_group": "rg-dev",
               "foundry_account": "cog-tok3n", "project": "fa-dev"}
    asked = []
    secret = "synthetic-key-value"
    client = FakeHttp(exists=False)
    outcome, value = script.ensure(client, {"Authorization": "Bearer t"}, profile,
                                   lambda: asked.append(1) or secret, toolbox=set)
    puts = [item for item in client.requests if item[0] == "PUT"]
    assert outcome == "created" and len(puts) == 1 and len(asked) == 1
    assert json.loads(puts[0][2])["properties"]["credentials"]["keys"]["x-apikey"] == secret
    assert "/connections/dev-webiq-key?" in puts[0][1]
    assert secret not in json.dumps(value) and secret not in capsys.readouterr().out
    assert not list(tmp_path.iterdir())
    unread = UnreadAfterPut()
    with pytest.raises(SystemExit, match="did not read back"):
        script.ensure(unread, {}, profile, lambda: secret, toolbox=set)
    assert [item[0] for item in unread.requests].count("PUT") == 1
    monkeypatch.setattr(script.sys.stdin, "isatty", lambda: False)
    with pytest.raises(SystemExit, match="interactive terminal"):
        script.ensure(FakeHttp(exists=False), {}, profile, script.ask_key, toolbox=set)
    again = FakeHttp(exists=True)
    assert script.ensure(again, {}, profile, lambda: pytest.fail("asked for the key again"))[0] == "already"
    assert [item[0] for item in again.requests] == ["GET"]


def test_webiq_rotate_puts_a_new_key_even_when_the_connection_exists(capsys, monkeypatch):
    script = load("new_webiq_connection_rotate", ROOT / "deploy" / "stage" / "new_webiq_connection.py")
    profile = {"environment": "dev", "subscription_id": SUBSCRIPTION, "resource_group": "rg-dev",
               "foundry_account": "cog-tok3n", "project": "fa-dev"}
    asked, secret = [], "synthetic-new-key"
    client = FakeHttp(exists=True)
    outcome, value = script.ensure(client, {}, profile, lambda: asked.append(1) or secret, rotate=True)
    puts = [item for item in client.requests if item[0] == "PUT"]
    assert outcome == "rotated" and len(puts) == 1 and len(asked) == 1
    assert json.loads(puts[0][2])["properties"]["credentials"]["keys"]["x-apikey"] == secret
    assert [item[0] for item in client.requests] == ["GET", "PUT", "GET"]  # read first, then the 200 read-back
    assert secret not in json.dumps(value) and secret not in capsys.readouterr().out
    missing = FakeHttp(exists=False)
    with pytest.raises(SystemExit, match="does not exist in .*nothing was written"):
        script.ensure(missing, {}, profile, lambda: pytest.fail("asked for a key"), rotate=True)
    assert [item[0] for item in missing.requests] == ["GET"]

    class UnreadAfterRotate(FakeHttp):
        def get(self, url, headers):
            if not any(item[0] == "PUT" for item in self.requests):
                return super().get(url, headers)
            self.requests.append(("GET", url, None))
            return SimpleNamespace(status_code=503, json=lambda: {})

    with pytest.raises(SystemExit, match="rotate again"):
        script.ensure(UnreadAfterRotate(exists=True), {}, profile, lambda: secret, rotate=True)
    monkeypatch.setattr(script.sys.stdin, "isatty", lambda: False)
    with pytest.raises(SystemExit, match="interactive terminal"):
        script.ensure(FakeHttp(exists=True), {}, profile, script.ask_key, rotate=True)
    with pytest.raises(SystemExit, match="--rotate"):
        script.main(["--rotate"])
    assert script.ensure(FakeHttp(exists=True), {}, profile, lambda: pytest.fail("asked"))[0] == "already"


@pytest.mark.parametrize("properties", [{"category": "RemoteTool", "target": "https://other.example/mcp"},
                                        {"category": "ApiKey", "target": "https://api.microsoft.ai/v3/mcp"}, {}])
def test_webiq_rotate_never_overwrites_a_connection_that_is_not_webiq(properties):
    script = load("new_webiq_connection_other", ROOT / "deploy" / "stage" / "new_webiq_connection.py")
    profile = {"environment": "dev", "subscription_id": SUBSCRIPTION, "resource_group": "rg-dev",
               "foundry_account": "cog-tok3n", "project": "fa-dev", "webiq_connection": "some-other-connection"}

    class Other(FakeHttp):
        def get(self, url, headers):
            self.requests.append(("GET", url, None))
            return SimpleNamespace(status_code=200, json=lambda: {"name": "some-other-connection",
                                                                  "properties": properties})

    client = Other(exists=True)
    with pytest.raises(SystemExit, match="is not the WebIQ connection .*; nothing was written"):
        script.ensure(client, {}, profile, lambda: pytest.fail("asked for a key"), rotate=True)
    assert [item[0] for item in client.requests] == ["GET"]


class ClientHttp(FakeHttp):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.mark.parametrize("exists, rotate, outcome", [(True, False, "already"), (True, True, "rotated"),
                                                     (False, False, "created")])
def test_webiq_main_gets_the_data_plane_token_only_to_create(monkeypatch, capsys, exists, rotate, outcome):
    script = load("new_webiq_connection_tokens", ROOT / "deploy" / "stage" / "new_webiq_connection.py")
    profile = {"environment": "dev", "subscription_id": SUBSCRIPTION, "resource_group": "rg-dev",
               "foundry_account": "cog-tok3n", "project": "fa-dev",
               "project_endpoint": "https://cog-tok3n.services.ai.azure.com/api/projects/fa-dev"}
    scopes, client = [], ClientHttp(exists=exists)

    class Credential:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get_token(self, scope):
            scopes.append(scope)
            return SimpleNamespace(token="synthetic-token")

    monkeypatch.setitem(sys.modules, "httpx", SimpleNamespace(Client=lambda **kwargs: client))
    monkeypatch.setitem(sys.modules, "azure.identity", SimpleNamespace(AzureCliCredential=Credential))
    monkeypatch.setattr(script, "load_config", lambda config, require_runtime: profile)
    monkeypatch.setattr(script.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(script.getpass, "getpass", lambda prompt: "synthetic-main-key")
    script.main(["--rotate"] if rotate else [])
    out = capsys.readouterr().out
    assert [json.loads(line)["connection"] for line in out.splitlines() if line.startswith("{")] == [outcome]
    assert "synthetic-main-key" not in out and "synthetic-token" not in out
    assert scopes == ([script.ARM_SCOPE, script.DATA_SCOPE] if outcome == "created" else [script.ARM_SCOPE])


@pytest.mark.parametrize("exists, rotate", [(False, False), (True, True)])
def test_webiq_names_the_connection_and_project_before_asking_for_the_key(capsys, exists, rotate):
    script = load("new_webiq_connection_where", ROOT / "deploy" / "stage" / "new_webiq_connection.py")
    profile = {"environment": "dev", "subscription_id": SUBSCRIPTION, "resource_group": "rg-dev",
               "foundry_account": "cog-tok3n", "project": "fa-dev"}
    shown, secret = [], "synthetic-where-key"
    script.ensure(FakeHttp(exists=exists), {}, profile, lambda: shown.append(capsys.readouterr().out) or secret,
                  rotate=rotate, toolbox=set)
    assert shown == ["WebIQ connection dev-webiq-key in cog-tok3n/fa-dev\n"]
    assert secret not in capsys.readouterr().out


class FakeVersions:
    def __init__(self, status, versions=()):
        self.status, self.versions, self.requests = status, versions, []

    def get(self, url, headers):
        self.requests.append(url)
        return SimpleNamespace(status_code=self.status, json=lambda: {"data": list(self.versions)})


def test_webiq_never_adds_a_second_connection_beside_the_one_the_toolbox_uses():
    """2026-10-22: dev's connection is webiq-key (made before the naming rule), not the computed dev-webiq-key."""
    script = load("new_webiq_connection_second", ROOT / "deploy" / "stage" / "new_webiq_connection.py")
    profile = {"environment": "dev", "subscription_id": SUBSCRIPTION, "resource_group": "rg-dev",
               "foundry_account": "cog-tok3n", "project": "fa-dev",
               "project_endpoint": "https://cog-tok3n.services.ai.azure.com/api/projects/fa-dev"}
    client = FakeHttp(exists=False)
    with pytest.raises(SystemExit, match="already uses connection webiq-key, not dev-webiq-key; nothing was written"):
        script.ensure(client, {}, profile, lambda: pytest.fail("asked for a key"), toolbox=lambda: {"webiq-key"})
    assert [item[0] for item in client.requests] == ["GET"]
    with pytest.raises(SystemExit, match="toolbox read-back"):
        script.ensure(FakeHttp(exists=False), {}, profile, lambda: pytest.fail("asked for a key"))
    # Named in the profile, the same commands reach the real connection.
    named = {**profile, "webiq_connection": "webiq-key"}
    rotated = FakeHttp(exists=True)
    assert script.ensure(rotated, {}, named, lambda: "synthetic-rotated-key", rotate=True)[0] == "rotated"
    assert [url for _, url, _ in rotated.requests] == [script.connection_url(named, "webiq-key")] * 3
    assert "/connections/webiq-key?" in rotated.requests[0][1]
    # A first install (no toolbox yet) or a toolbox that already uses this name still creates.
    for used in (set(), {"dev-webiq-key"}):
        assert script.ensure(FakeHttp(exists=False), {}, profile, lambda: "synthetic-key", toolbox=lambda: used)[0] == "created"
    with pytest.raises(SystemExit, match="not a connection name"):
        script.connection_name({**profile, "webiq_connection": "../other"})
    toolbox = load("new_toolbox_named", ROOT / "deploy" / "stage" / "new_toolbox.py")
    assert toolbox.definition(named)[0] == "webiq-key" and toolbox.definition(profile)[0] == "dev-webiq-key"

    versions = FakeVersions(200, [{"tools": [{"project_connection_id": "webiq-key"}]},
                                  {"tools": [{"type": "mcp", "project_connection_id": "other-key"}, {"type": "x"}]}])
    assert script.toolbox_connections(versions, {}, profile) == {"webiq-key", "other-key"}
    assert versions.requests == [profile["project_endpoint"] + "/toolboxes/fa-org-tools/versions?api-version=v1"]
    assert script.toolbox_connections(FakeVersions(404), {}, profile) == set()
    with pytest.raises(SystemExit, match="cannot read toolbox fa-org-tools"):
        script.toolbox_connections(FakeVersions(403), {}, profile)


def test_stage_keeps_its_original_webiq_names(monkeypatch):
    toolbox = load("new_toolbox_azd", ROOT / "deploy" / "stage" / "new_toolbox.py")
    stage = json.loads((ROOT / "deploy" / "stage" / "stage.json").read_text(encoding="utf-8"))
    connection, tools, body = toolbox.definition(stage)
    assert connection == "stage-webiq-key"
    assert tools == [{"type": "mcp", "server_label": "WebIQ", "server_url": "https://api.microsoft.ai/v3/mcp",
                      "require_approval": "never", "project_connection_id": "stage-webiq-key",
                      "allowed_tools": ["web", "browse"]}]
    assert body["description"] == "FA org tools (N5): WebIQ via project connection stage-webiq-key"
    script = load("new_webiq_connection_stage", ROOT / "deploy" / "stage" / "new_webiq_connection.py")
    assert script.connection_name(stage) == "stage-webiq-key"
    assert "/resourceGroups/rg-faexample/" in script.connection_url(stage, "stage-webiq-key")
    # dev names its #412 connection; its API no longer holds a WebIQ key (WebIQ-MCP retired 2026-10-22).
    dev = json.loads((ROOT / "deploy" / "dev" / "dev.json").read_text(encoding="utf-8"))
    assert script.connection_name(dev) == toolbox.definition(dev)[0] == "webiq-key"
    for profile in (stage, dev):
        assert set(profile.get("api_secret_environment") or {}) == {"FA_FEED_MCP_SIGNING_KEY"}


def deploy_case(hooks, tmp_path, monkeypatch, api_exists, keep_select=False, routes=None, steps=None, tty=True,
                **values):
    steps = [] if steps is None else steps
    record = lambda name, result=None: (lambda *args, **kwargs: steps.append(name) or result)  # noqa: E731
    built = {"api": "a", "hosted": "h", "console": "c"}
    monkeypatch.setattr(hooks, "git_commit", lambda: "f" * 40)
    monkeypatch.setattr(hooks, "code_identity", lambda: "e" * 64)
    monkeypatch.setattr(hooks, "write_profile", record("profile"))
    monkeypatch.setattr(hooks, "images", record("images", built))
    names = ["seed", "agent_roles", "sync_job", "api_app", "console_app", "prepare_sessions", "summary", "webiq",
             "memory_store"] + ([] if keep_select else ["select_defaults"])
    for name in names:
        monkeypatch.setattr(hooks, name, record(name))
    monkeypatch.setattr(hooks, "runtime", record("runtime", tmp_path / "runtime"))
    monkeypatch.setattr(hooks, "fresh_install", record("fresh_install", tmp_path / "plan.json"))
    monkeypatch.setattr(hooks, "ci_step", lambda ctx, commit, command, *args: steps.append(f"ci:{command}"))
    show = ok({"name": "ca-api-dev"}) if api_exists else failed("ResourceNotFound")
    # The FA_NETWORK_MODE check reads the account (a public one by default); a test's own account route answers instead.
    ctx = context(hooks, tmp_path, {"Microsoft.CognitiveServices/accounts/": foundry_account(injected=False),
                                    **(routes or {}),
                                    "containerapp show": show, "azd env set": ok()},
                  FA_PROJECT_ENDPOINT="https://p", FA_API_URL="https://a", FA_API_AUDIENCE="api://x",
                  FA_CONSOLE_APP_ID="c", FA_API_APP="ca-api-dev", **values)
    ctx.tty = tty
    hooks.deploy(ctx)
    return steps, ctx


def test_first_install_runs_the_standard_flow_order(hooks, tmp_path, monkeypatch):
    steps, ctx = deploy_case(hooks, tmp_path, monkeypatch, api_exists=False)
    # No follow on a first install: a never-prepared Agent has nothing to follow (live run 2026-10-10).
    assert steps == ["profile", "images", "webiq", "memory_store", "seed", "agent_roles", "profile", "ci:check",
                     "runtime",
                     "sync_job", "fresh_install", "ci:sync", "api_app", "console_app", "prepare_sessions",
                     "select_defaults", "summary"]
    assert ctx.values["FA_SESSIONS_PREPARED"] == "true" and ctx.values["FA_DEPLOY_RUN"] == "1"


@pytest.mark.parametrize("answer", [NO_ACCOUNT, READ_FAILURES["MissingSubscriptionRegistration"]])
def test_deploy_stops_before_any_build_or_write_without_the_provisioned_account(hooks, tmp_path, monkeypatch, answer):
    # A changed AZURE_LOCATION derives another name: preprovision's "new environment" is not one for deploy.
    steps = []
    with pytest.raises(hooks.Stop, match=f"Foundry account {ACCOUNT} was not found in rg-dev, and azd deploy needs "
                                         r"the provisioned account.*AZURE_LOCATION.*`azd provision` first; nothing "
                                         "was changed"):
        deploy_case(hooks, tmp_path, monkeypatch, api_exists=True, steps=steps,
                    routes={"Microsoft.CognitiveServices/accounts/": answer})
    assert steps == []


def test_a_failed_default_selection_is_retried_on_the_first_install_path(hooks, tmp_path, monkeypatch):
    monkeypatch.setattr(hooks, "select_defaults", lambda ctx: (_ for _ in ()).throw(SystemExit("select failed")))
    with pytest.raises(SystemExit, match="select failed"):
        deploy_case(hooks, tmp_path, monkeypatch, api_exists=False, keep_select=True)
    # Not marked complete, so the next run resumes the first install (and selects) instead of upgrading.
    steps, _ = deploy_case(hooks, tmp_path, monkeypatch, api_exists=True, FA_CONSOLE_DEPLOYED="true", **SEEDED)
    assert "select_defaults" in steps and "ci:guard" not in steps


SEEDED = {f"FA_AGENT_{letter}_OID": "done" for letter in "ABCD"}


def test_a_stopped_first_install_resumes_without_creating_again(hooks, tmp_path, monkeypatch):
    partial = deploy_case(hooks, tmp_path, monkeypatch, api_exists=False, FA_AGENT_D_OID="done")[0]
    assert "seed" in partial  # one identity is not a finished seed
    steps, _ = deploy_case(hooks, tmp_path, monkeypatch, api_exists=True, FA_CONSOLE_DEPLOYED="true",
                           FA_DEPLOY_RUN="3", **SEEDED)
    assert "seed" not in steps and "api_app" not in steps and "console_app" not in steps
    assert steps[-6:] == ["ci:sync", "ci:api", "ci:console", "prepare_sessions", "select_defaults", "summary"]
    assert "ci:follow" not in steps
    # The same images already serving are not deployed again.
    steps, _ = deploy_case(hooks, tmp_path, monkeypatch, api_exists=True, FA_CONSOLE_DEPLOYED="true",
                           FA_API_IMAGE_DEPLOYED="a", FA_CONSOLE_IMAGE_DEPLOYED="c", **SEEDED)
    assert steps[-4:] == ["ci:sync", "prepare_sessions", "select_defaults", "summary"]


def test_a_resumed_install_over_a_serving_api_runs_the_gate_before_any_switch(hooks, tmp_path, monkeypatch):
    """ADR-0009 2d-ii: an API already serves, so its binding rows may be v1 from an older release; a first install
    (no API app, test_first_install_runs_the_standard_flow_order) has no rows and runs no gate."""
    steps, _ = deploy_case(hooks, tmp_path, monkeypatch, api_exists=True, FA_CONSOLE_DEPLOYED="true", **SEEDED)
    assert steps.count("ci:gate") == 1 and steps.index("ci:check") < steps.index("ci:gate") < steps.index("runtime")
    assert steps.index("ci:gate") < min(steps.index(step) for step in ("fresh_install", "ci:sync", "ci:api"))


def test_an_armed_gate_refusal_tells_the_operator_what_to_do_with_each_agent(hooks, monkeypatch):
    steps = []

    def refused(ctx, commit, command, *args):
        steps.append(command)
        if command == "gate":
            raise hooks.Stop("ci_deploy.py --config failed (exit 1); its output says what to read back")

    monkeypatch.setattr(hooks, "ci_step", refused)
    with pytest.raises(hooks.Stop, match=r"git checkout v2026\.10\.20-33.*FA_NATIVE_2DI true.*azd deploy.*retire the Agent.*#upgrade-2dii"):
        hooks.upgrade(None, {"api": "a", "hosted": "h", "console": "c"}, "f" * 40)
    assert steps == ["check", "gate"]  # nothing after the refusal


def test_a_later_deploy_is_the_stage_ci_upgrade(hooks, tmp_path, monkeypatch):
    steps, _ = deploy_case(hooks, tmp_path, monkeypatch, api_exists=True, FA_SESSIONS_PREPARED="true")
    # WebIQ before the API update, so enabling it later never advertises a toolbox that does not exist (F6).
    # Upgrades never touch the owner's new-conversation selection. The ADR-0009 phase 3b gate runs before any write.
    assert steps == ["profile", "images", "profile", "ci:check", "ci:gate", "webiq", "memory_store", "ci:guard",
                     "ci:sync", "ci:api", "ci:console", "ci:follow", "summary"]


def test_default_selection_runs_once_as_the_owner_without_an_initial_template(hooks, tmp_path):
    ctx = context(hooks, tmp_path, {"select_all_capabilities.py": ok(), "azd env set": ok(),
                                    "graph.microsoft.com/v1.0/me": ok({"id": OWNER})}, FA_TEAM_OWNER_OID=OWNER)
    hooks.select_defaults(ctx)
    call = next(call for call in ctx.run.calls if "select_all_capabilities.py" in " ".join(call))
    assert "--initial-template" not in call and "--keep-capabilities" not in call
    assert call[call.index("--request-id") + 1] == "dev-azd-select-1"
    assert ctx.values["FA_DEFAULTS_SELECTED"] == "true"
    ctx.run.calls.clear()
    hooks.select_defaults(ctx)  # a later run never replaces the owner's own choice
    assert ctx.run.calls == []


def test_postdown_removes_only_this_environments_objects_and_keeps_the_records(hooks, tmp_path, monkeypatch):
    entra = sys.modules["entra"]
    keys = entra.names("dev", TENANT, SUBSCRIPTION, "rg-dev")
    graph = FakeGraph()
    graph.apps = {keys["api_unique_name"]: {"id": "obj-api"}, keys["console_unique_name"]: {"id": "obj-portal"},
                  "fa-api-other-env": {"id": "obj-other"}}

    def answer(method, path, body, headers):
        graph.calls.append((method, path))
        if method == "GET":
            name = path.split("'")[1]
            if name not in graph.apps:
                raise LookupError("Request_ResourceNotFound")
            return graph.apps[name]
        graph.apps = {name: app for name, app in graph.apps.items() if path != f"/applications/{app['id']}"}
        return None
    ours = backup_role(hooks)
    roles = FakeRoles(ours)
    routes = {"account show": ok({"id": SUBSCRIPTION, "tenantId": TENANT}), "rest --method": roles,
              "azd env set": ok()}
    ctx = context(hooks, tmp_path, routes, FA_BACKUP_ROLE_ID=ours["id"], FA_FRESH_INSTALLED="true",
                  FA_TEAM_OWNER_OID=OWNER, FA_ENABLE_WEBIQ="false", FA_NETWORK_MODE="private")
    monkeypatch.setattr(ctx, "graph", answer)
    (tmp_path / "dev" / "fa").mkdir(parents=True)
    (tmp_path / "dev" / "fa" / "intent.json").write_text("{}", encoding="utf-8")
    hooks.postdown(ctx)
    assert set(graph.apps) == {"fa-api-other-env"}
    assert roles.deleted == [ours["id"].lower()]
    kept = [path for path in tmp_path.joinpath("dev").iterdir() if path.name.startswith("fa-down-")]
    assert len(kept) == 1 and (kept[0] / "intent.json").exists() and not (tmp_path / "dev" / "fa").exists()
    # azd 1.32 has no `azd env unset`; an empty value reads as unset.
    cleared = {call[3] for call in ctx.run.calls if call[:3] == ["azd", "env", "set"] and call[4] == ""}
    assert "FA_FRESH_INSTALLED" in cleared and "FA_BACKUP_ROLE_ID" in cleared
    assert not cleared & {"FA_TEAM_OWNER_OID", "FA_ENABLE_WEBIQ", "FA_NETWORK_MODE"}  # the next azd up keeps the mode
    assert not [call for call in ctx.run.calls if "unset" in call]
    graph.calls.clear()
    hooks.postdown(ctx)  # a rerun finds nothing left and deletes nothing
    assert [method for method, _ in graph.calls] == ["GET", "GET"]


class FakeRoles:
    """ARM role definitions by exact ID: GET reads one or answers RoleDefinitionDoesNotExist, DELETE removes it."""

    def __init__(self, *roles):
        self.roles, self.deleted = {role["id"].lower(): role for role in roles}, []

    def __call__(self, args):
        role_id = args[args.index("--url") + 1].split("?")[0].lower()
        if args[args.index("--method") + 1] == "delete":
            self.deleted.append(role_id)
            self.roles.pop(role_id, None)
            return ok()
        if role_id in self.roles:
            return ok(self.roles[role_id])
        return failed("ERROR: Not Found({\"error\":{\"code\":\"RoleDefinitionDoesNotExist\"}})")


def backup_role(hooks, group="rg-dev", token="abcdefghijklm"):
    """The role infra/azd/main.bicep makes for an environment named dev whose resource group is `group`."""
    guid = hooks.bicep_guid(f"/subscriptions/{SUBSCRIPTION}/resourceGroups/{group}", "fa-m1-backup-data-read-write-v1")
    return {"id": f"/subscriptions/{SUBSCRIPTION}/providers/Microsoft.Authorization/roleDefinitions/{guid}",
            "name": guid, "properties": {"roleName": f"FA M1 Backup Data Read Write (dev-{token})"}}


def test_bicep_guid_reproduces_the_stage_backup_role_guid(hooks):
    # Test vector from the repo: guid(<stage RG id>, 'fa-m1-backup-data-read-write-v1') is the GUID the stage
    # m3-artifacts parameters carry (src/fa-shared-context-poc/README.md, W4 Blob).
    stage = json.loads((ROOT / "deploy" / "stage" / "stage.json").read_text(encoding="utf-8"))
    shared = ROOT / "infra" / "shared-context"
    group = json.loads((shared / "backup-role.stage.parameters.json").read_text(encoding="utf-8"))
    artifacts = json.loads((shared / "m3-artifacts.stage.parameters.json").read_text(encoding="utf-8"))
    rg_id = (f"/subscriptions/{stage['subscription_id']}/resourceGroups/"
             f"{group['parameters']['backupResourceGroupName']['value']}")
    assert hooks.bicep_guid(rg_id, hooks.BACKUP_ROLE_SEED) == "00000000-0000-0000-0000-000000000058"
    assert artifacts["parameters"]["readWriteRoleDefinitionGuid"]["value"] == "00000000-0000-0000-0000-000000000058"


# azd new-user test 2026-10-15 #12: without FA_BACKUP_ROLE_ID postdown said nothing about the backup role.
# The role is found by the exact ID backup-role.bicep derives from the group, never by its name: another environment
# named dev (another group or region) has a role named alike, and it is never touched.
@pytest.mark.parametrize("present, recorded, deleted, line", [
    ("both", None, "ours", "FA M1 Backup Data Read Write (dev-abcdefghijklm) ({guid}): deleted"),
    ("other", None, None, "FA: backup custom role {guid}: already gone"),
    ("both", "other", None, "none deleted, check and delete it yourself"),
])
def test_postdown_deletes_only_the_backup_role_with_this_groups_id(hooks, tmp_path, monkeypatch, capsys, present,
                                                                  recorded, deleted, line):
    ours, other = backup_role(hooks), backup_role(hooks, group="rg-dev-west", token="nopqrstuvwxyz")
    roles = FakeRoles(*([ours, other] if present == "both" else [other]))
    routes = {"account show": ok({"id": SUBSCRIPTION, "tenantId": TENANT}), "rest --method": roles,
              "azd env set": ok()}
    ctx = context(hooks, tmp_path, routes, **({"FA_BACKUP_ROLE_ID": other["id"]} if recorded else {}))

    def gone(method, path, body, headers):
        raise LookupError("Request_ResourceNotFound")
    monkeypatch.setattr(ctx, "graph", gone)
    hooks.postdown(ctx)
    assert roles.deleted == ([ours["id"].lower()] if deleted else [])
    assert other["id"].lower() in roles.roles
    assert line.format(guid=ours["name"]) in capsys.readouterr().out
    cleared = {call[3] for call in ctx.run.calls if call[:3] == ["azd", "env", "set"]}
    assert "FA_BACKUP_ROLE_ID" not in cleared  # a mismatched record stays to point at the role to check


def test_only_the_team_owner_prepares_and_selects(hooks, tmp_path):
    ctx = context(hooks, tmp_path, {"graph.microsoft.com/v1.0/me": ok({"id": OTHER})}, FA_TEAM_OWNER_OID=OWNER)
    for step in (hooks.prepare_sessions, hooks.select_defaults):
        with pytest.raises(SystemExit, match="Team owner"):
            step(ctx)
    assert not any("select_all" in " ".join(call) or "prepare_native" in " ".join(call) for call in ctx.run.calls)


class FakeStores:
    def __init__(self, existing=None, fail_create=False):
        self.existing, self.fail_create, self.creates = existing, fail_create, []

    def get(self, name):
        if self.existing is None:
            raise LookupError("not found")
        return SimpleNamespace(as_dict=lambda: {"name": name, "definition": self.existing})

    def create(self, name, definition, description):
        self.creates.append((name, dict(definition)))
        if self.fail_create:
            raise TimeoutError("unknown")
        self.existing = dict(definition)


def test_a_store_for_another_chat_model_is_created_with_that_model_only(tmp_path):
    script = load("new_memory_store_azd", ROOT / "deploy" / "stage" / "new_memory_store.py")
    stores = FakeStores()
    expected = script.definition("gpt-5.4-mini")
    assert script.ensure(stores, tmp_path, "poc-team-memory", expected=expected)[0] == "created"
    assert stores.creates[0][1]["chat_model"] == "gpt-5.4-mini"
    assert stores.creates[0][1]["embedding_model"] == "text-embedding-3-small"
    with pytest.raises(SystemExit, match="another definition"):  # a store keeps the chat model it was made with
        script.ensure(stores, tmp_path / "x", "poc-team-memory")


def test_memory_store_is_created_once_with_the_stage_definition(tmp_path):
    script = load("new_memory_store_azd", ROOT / "deploy" / "stage" / "new_memory_store.py")
    stores = FakeStores()
    assert script.ensure(stores, tmp_path, "poc-team-memory")[0] == "created"
    name, definition = stores.creates[0]
    assert name == "poc-team-memory" and definition["chat_model"] == "gpt-4.1-mini"
    assert definition["embedding_model"] == "text-embedding-3-small"
    assert definition["options"] == {"user_profile_enabled": True, "chat_summary_enabled": True, "default_ttl_seconds": 0}
    assert script.ensure(stores, tmp_path, "poc-team-memory")[0] == "already" and len(stores.creates) == 1
    with pytest.raises(SystemExit, match="another definition"):
        script.ensure(FakeStores({**script.DEFINITION, "chat_model": "other"}), tmp_path / "x", "poc-team-memory")
    unknown = FakeStores(fail_create=True)
    (tmp_path / "u").mkdir()
    with pytest.raises(SystemExit, match="outcome unknown"):
        script.ensure(unknown, tmp_path / "u", "poc-team-memory")
    with pytest.raises(SystemExit, match="do not create twice"):
        script.ensure(unknown, tmp_path / "u", "poc-team-memory")
    assert len(unknown.creates) == 1


LOCK = {"name": "m1-backup-account-delete-protection", "id": "/subscriptions/s/resourcegroups/rg-dev/providers/"
        "Microsoft.Storage/storageAccounts/stbk/providers/Microsoft.Authorization/locks/m1-backup-account-delete-protection"}


def outside(hooks, roles=None):
    """What predown reads first (read only) to list what goes outside the resource group."""
    return {"account show": ok({"id": SUBSCRIPTION, "tenantId": TENANT}),
            "rest --method": roles or FakeRoles(backup_role(hooks))}


@pytest.mark.parametrize("tty, answer, values, removed", [
    (True, "dev", {}, True),                               # the person types the environment name
    (False, None, {"FA_CONFIRM_DELETE": "dev"}, True),     # an explicit confirmation for a run without a terminal
    (True, "yes", {}, False),                              # anything else removes nothing
    (False, None, {}, False),                              # no terminal and no confirmation
    (False, None, {"FA_CONFIRM_DELETE": "other"}, False),  # a confirmation for another environment
])
def test_predown_removes_only_the_backup_lock_after_confirmation(hooks, tmp_path, tty, answer, values, removed):
    ctx = context(hooks, tmp_path, {**outside(hooks), "lock list": ok([LOCK]), "lock delete": ok()}, **values)
    ctx.tty, ctx.prompt = tty, (lambda text: answer)
    if removed:
        hooks.predown(ctx)
    else:
        with pytest.raises(SystemExit):
            hooks.predown(ctx)
    deleted = [call for call in ctx.run.calls if "delete" in call]
    assert bool(deleted) is removed
    if removed:
        assert deleted[0][deleted[0].index("--ids") + 1] == LOCK["id"]


def test_predown_prints_how_to_put_the_lock_back(hooks, tmp_path, capsys):
    # azd asks its own y/N after predown; a "no" there leaves the backups without their lock.
    ctx = context(hooks, tmp_path, {**outside(hooks), "lock list": ok([{**LOCK, "level": "CanNotDelete"}]),
                                    "lock delete": ok()}, FA_CONFIRM_DELETE="dev")
    hooks.predown(ctx)
    assert ("az lock create --name m1-backup-account-delete-protection --lock-type CanNotDelete --resource "
            "/subscriptions/s/resourcegroups/rg-dev/providers/Microsoft.Storage/storageAccounts/stbk\n"
            ) in capsys.readouterr().out


def test_a_new_console_is_internal_until_its_sign_in_config_exists(hooks, tmp_path, monkeypatch):
    deployed = []
    monkeypatch.setattr(hooks, "console_bundle", lambda *args: {})
    monkeypatch.setattr(hooks, "deploy_bicep", lambda ctx, name, template, parameters: deployed.append(
        (template, parameters)) or {"publicOrigin": "https://ca-console.example.io", "identityPrincipalId": "p"})
    monkeypatch.setattr(sys.modules["entra"], "ensure_fic", lambda *args: "created")
    keys = ("AZURE_LOCATION", "FA_CONTAINER_APPS_ENVIRONMENT", "FA_REGISTRY_NAME", "FA_VNET", "FA_CONSOLE_APP",
            "FA_CONSOLE_IDENTITY", "FA_CONSOLE_STORAGE", "FA_CONSOLE_APP_ID", "FA_TEAM_OWNER_OID", "FA_API_AUDIENCE",
            "FA_CONSOLE_APP_OBJECT_ID", "FA_DEPLOY_RUN")
    ctx = context(hooks, tmp_path, {"azd env set": ok()}, FA_CONSOLE_URL="https://ca-console.example.io",
                  **{key: "x" for key in keys})
    hooks.console_app(ctx, "cr/fa-web-console@sha256:" + "a" * 64, tmp_path, tmp_path / "plan.json", "f" * 40)
    assert [parameters["ingressExternal"] for _, parameters in deployed] == [False, True]
    # Every name passed is a parameter of the template, so the flag cannot be dropped by a rename.
    declared = set(re.findall(r"^param (\w+)", deployed[0][0].read_text(encoding="utf-8"), re.MULTILINE))
    assert set(deployed[0][1]) <= declared and set(deployed[1][1]) <= declared


@pytest.mark.parametrize("values, account", [
    ({"FA_NETWORK_MODE": "private"}, True),  # Cosmos memory embeds at the account root
    ({"FA_NETWORK_MODE": "private", "FA_ENABLE_MEMORY": "false"}, False),
    ({"FA_NETWORK_MODE": "public"}, False),
    ({}, False),
])
def test_agent_roles_add_account_openai_user_only_for_cosmos_memory(hooks, tmp_path, monkeypatch, values, account):
    # fa-priv-1024 (2026-10-24): with project roles only, every account-root embedding failed and nothing was written.
    deployed = []
    monkeypatch.setattr(hooks, "deploy_bicep", lambda ctx, name, template, parameters: deployed.append(
        (name, template, parameters)) or {})
    keys = [key for key, _, _ in hooks.fa_profile.agent_names({"FA_ENVIRONMENT": "dev"})]
    oids = {f"FA_AGENT_{key}_OID": f"oid-{key}" for key in keys}
    ctx = context(hooks, tmp_path, {}, FA_FOUNDRY_ACCOUNT="acct", FA_PROJECT="proj", **oids, **values)
    hooks.agent_roles(ctx)
    first = list(deployed)
    hooks.agent_roles(ctx)
    assert deployed == first + first  # a rerun sends the same deployments; the Bicep guid() names keep one each
    granted = [(name, parameters) for name, template, parameters in first if template.name == "account-access.bicep"]
    assert granted == ([(f"fa-dev-account-access-{key.lower()}", {
        "accountName": "acct", "principalId": f"oid-{key}", "roleId": "5e0bd9bd-7b93-4f28-af87-19fc36ad61bd"})
        for key in keys] if account else [])
    assert len(first) - len(granted) == len(keys)  # the project roles stay as they were
    for _, template, parameters in first:
        assert set(parameters) <= set(re.findall(r"^param (\w+)", template.read_text(encoding="utf-8"), re.MULTILINE))


def test_predown_leaves_someone_elses_lock_alone(hooks, tmp_path):
    ctx = context(hooks, tmp_path, {**outside(hooks), "lock list": ok([LOCK, {"name": "team-hold", "id": "x"}])},
                  FA_CONFIRM_DELETE="dev")
    with pytest.raises(SystemExit, match="team-hold"):
        hooks.predown(ctx)
    assert not [call for call in ctx.run.calls if "delete" in call]


def test_predown_lists_what_goes_outside_the_group_before_it_asks(hooks, tmp_path, capsys):
    # azd new-user test 2026-10-15 #14: the person confirms without seeing the Entra apps and the backup role.
    keys = sys.modules["entra"].names("dev", TENANT, SUBSCRIPTION, "rg-dev")
    ctx = context(hooks, tmp_path, {**outside(hooks), "lock list": ok([LOCK]), "lock delete": ok()})
    asked = []
    ctx.prompt = lambda text: asked.append(capsys.readouterr().out) or "dev"
    hooks.predown(ctx)
    assert keys["console_unique_name"] in asked[0] and keys["api_unique_name"] in asked[0]
    assert f"FA M1 Backup Data Read Write (dev-abcdefghijklm) ({backup_role(hooks)['name']})" in asked[0]
    assert not [call for call in ctx.run.calls if "delete" in call and "rest" in call]  # read only


def test_predown_goes_on_when_the_backup_role_cannot_be_read(hooks, tmp_path, capsys):
    # The role line is information only; a failed read must not stop `azd down`.
    routes = {**outside(hooks, lambda args: failed("ERROR: (AuthorizationFailed) no read")), "lock list": ok([LOCK]),
              "lock delete": ok()}
    ctx = context(hooks, tmp_path, routes, FA_CONFIRM_DELETE="dev")
    hooks.predown(ctx)
    assert "backup custom role: could not be read" in capsys.readouterr().out
    assert [call for call in ctx.run.calls if "lock" in call and "delete" in call]


def test_the_model_picker_offers_what_the_region_runs_and_records_one_decision(hooks, tmp_path, capsys):
    offer = region(missing=("gpt-6.1-sol",), free={"gpt-6-luna": 100})
    ctx = context(hooks, tmp_path, preprovision_routes(ok({"ok": True}), offer=offer), FA_TEAM_OWNER_OID=OWNER)
    answers = iter(["", "2"])  # the suggested models, then the second of them as the default
    ctx.prompt = lambda text: next(answers)
    hooks.preprovision(ctx)
    out = capsys.readouterr().out
    assert "4. gpt-6.1-sol: not offered in this region" in out
    assert "3. gpt-6-luna: quota free 100, needs 500 (not enough)" in out
    assert ctx.values["FA_MODELS"] == "gpt-4.1-mini,gpt-5.4-mini"
    assert ctx.values["FA_DEFAULT_MODEL"] == "gpt-5.4-mini"
    assert ctx.values["FA_MEMORY_CHAT_MODEL"] == "gpt-5.4-mini"  # memory uses a chosen model, not a fixed one
    assert ctx.values["FA_MODEL_DEPLOYMENTS"] == "gpt-4.1-mini,gpt-5.4-mini,text-embedding-3-small"
    document = json.loads((tmp_path / "dev" / "fa" / "preflight-deployment.json").read_text(encoding="utf-8"))
    assert [item["name"] for item in document["model_deployments"]] == [
        "gpt-4.1-mini", "text-embedding-3-small", "gpt-5.4-mini"]


def test_the_model_picker_shows_usage_over_the_limit_as_none_free(hooks, tmp_path, capsys):
    # azd new-user test 2026-10-15 #4: it printed "quota free -250, needs 500 (not enough)".
    offer = region(free={"gpt-6-luna": -250})
    ctx = context(hooks, tmp_path, preprovision_routes(ok({"ok": True}), offer=offer), FA_TEAM_OWNER_OID=OWNER)
    answers = iter(["", "2"])
    ctx.prompt = lambda text: next(answers)
    hooks.preprovision(ctx)
    out = capsys.readouterr().out
    assert "gpt-6-luna: quota free 0 (over the limit), needs 500 (not enough)" in out
    assert "-250" not in out


@pytest.mark.parametrize("answer, message", [("4", "gpt-6.1-sol is not offered"), ("0", "not a list"),
                                             ("two", "not a list")])
def test_the_model_picker_changes_nothing_on_a_choice_it_cannot_deploy(hooks, tmp_path, answer, message):
    ctx = context(hooks, tmp_path, preprovision_routes(ok({"ok": True}), offer=region(missing=("gpt-6.1-sol",))),
                  FA_TEAM_OWNER_OID=OWNER)
    ctx.prompt = lambda text: answer
    with pytest.raises(SystemExit, match=message):
        hooks.preprovision(ctx)
    assert "FA_MODELS" not in ctx.values
    assert not any("preflight_subscription.py" in " ".join(call) for call in ctx.run.calls)


def test_a_region_where_no_model_fits_stops_before_billing(hooks, tmp_path):
    offer = region(free={item["deployment"]: 0 for item in CATALOG})
    ctx = context(hooks, tmp_path, preprovision_routes(ok({"ok": True}), offer=offer))
    with pytest.raises(SystemExit, match="no catalog chat model fits westus3"):
        hooks.preprovision(ctx)


def test_an_environment_provisioned_before_the_choice_is_not_asked_and_keeps_its_models(hooks, tmp_path):
    ctx = context(hooks, tmp_path, preprovision_routes(ok({"ok": True}), offer={}), FA_FOUNDRY_ACCOUNT=ACCOUNT,
                  FA_TEAM_OWNER_OID=OWNER)
    ctx.prompt = lambda text: pytest.fail("asked an installed environment to choose")
    hooks.preprovision(ctx)
    assert ctx.values["FA_MODELS"] == "gpt-4.1-mini,gpt-5.4-mini,gpt-6-luna,gpt-6.1-sol"
    assert ctx.values["FA_MEMORY_CHAT_MODEL"] == "gpt-4.1-mini"


def test_without_a_terminal_every_catalog_model_is_chosen_without_reading_the_region(hooks, tmp_path):
    ctx = context(hooks, tmp_path, preprovision_routes(ok({"ok": True}), offer={}))
    ctx.tty = False
    hooks.preprovision(ctx)
    assert ctx.values["FA_MODELS"] == "gpt-4.1-mini,gpt-5.4-mini,gpt-6-luna,gpt-6.1-sol"
    assert ctx.values["FA_DEFAULT_MODEL"] == ctx.values["FA_MEMORY_CHAT_MODEL"] == "gpt-6.1-sol"


@pytest.mark.parametrize("values, expected", [
    ({"FA_MODELS": "gpt-6-luna,gpt-5.4-mini"},
     ("gpt-6-luna", "gpt-6-luna", "gpt-6-luna,gpt-5.4-mini,text-embedding-3-small")),
    ({"FA_MODELS": "gpt-5.4-mini", "FA_ENABLE_MEMORY": "false"}, ("gpt-5.4-mini", None, "gpt-5.4-mini")),
    # An environment provisioned before this choice keeps every model and its gpt-4.1-mini memory store.
    ({"FA_FOUNDRY_ACCOUNT": "cog-x"}, ("gpt-6.1-sol", "gpt-4.1-mini", None)),
    ({"FA_FOUNDRY_ACCOUNT": "cog-x", "FA_MODEL_DEPLOYMENTS": "gpt-5.4-mini,text-embedding-3-small",
      "FA_MODELS": "gpt-5.4-mini"}, ("gpt-5.4-mini", "gpt-5.4-mini", None)),
])
def test_one_choice_names_the_default_the_memory_model_and_the_deployments(values, expected):
    models = sys.modules["models"]
    choice = models.parse(values, CATALOG)
    default, memory, deployments = expected
    assert (choice.default, choice.memory_chat_model) == (default, memory)
    if deployments:
        assert ",".join(choice.deployments) == deployments


@pytest.mark.parametrize("values, message", [
    ({"FA_MODELS": "gpt-5.4-mini,text-embedding-3-small"}, "outside the catalog's chat models"),
    ({"FA_MODELS": "gpt-5.4-mini", "FA_DEFAULT_MODEL": "gpt-6.1-sol"}, "must be one of FA_MODELS"),
    ({"FA_MODELS": "gpt-5.4-mini", "FA_MEMORY_CHAT_MODEL": "gpt-4.1-mini"}, "keep it in FA_MODELS"),
])
def test_a_choice_that_cannot_serve_stops(hooks, values, message):
    with pytest.raises(SystemExit, match=message):
        sys.modules["models"].parse(values, CATALOG)


def test_the_cosmos_memory_creates_no_memory_store(hooks, tmp_path):
    ctx = context(hooks, tmp_path, {}, FA_NETWORK_MODE="private", FA_MODELS="gpt-5.4-mini")
    hooks.memory_store(ctx)  # the Runner raises on any command: none is run
    assert ctx.run.calls == []


def test_the_memory_store_is_made_with_the_chosen_chat_model(hooks, tmp_path):
    ctx = context(hooks, tmp_path, {"new_memory_store.py": ok()}, FA_MODELS="gpt-5.4-mini",
                  FA_MEMORY_CHAT_MODEL="gpt-5.4-mini")
    (tmp_path / "dev" / "fa").mkdir(parents=True)
    (tmp_path / "dev" / "fa" / "profile.json").write_text("{}", encoding="utf-8")
    hooks.memory_store(ctx)
    call = next(call for call in ctx.run.calls if "new_memory_store.py" in " ".join(call))
    assert call[call.index("--chat-model") + 1] == "gpt-5.4-mini"


REGISTRY = "crdev.azurecr.io"
BUILT = {kind: f"{REGISTRY}/fa-{kind}@sha256:{letter * 64}" for kind, letter in (("api", "a"), ("hosted", "b"),
                                                                                    ("console", "c"))}


def image_case(hooks, tmp_path, porcelain, **values):
    def build(args):
        kind = args[args.index("build") + 1]
        return SimpleNamespace(code=0, stdout=f"pushing\n{BUILT[kind]}\n", stderr="")
    routes = {" status --porcelain ": SimpleNamespace(code=0, stdout=porcelain, stderr=""),
              "ci_deploy.py": build, "azd env set": ok()}
    return context(hooks, tmp_path, routes, FA_REGISTRY=REGISTRY, FA_DEPLOY_RUN="1", **values)


def builds(ctx):
    return [call for call in ctx.run.calls if "ci_deploy.py" in " ".join(call)]


CACHED = {"FA_IMAGES_CORE": "e" * 64, "FA_IMAGES_COMMIT": "f" * 40,
          **{f"FA_{kind.upper()}_IMAGE": ref for kind, ref in BUILT.items()}}


def test_images_stop_on_uncommitted_build_inputs(hooks, tmp_path):
    ctx = image_case(hooks, tmp_path, " M src/fa-shared-context-poc/shared_context/api.py\n?? src/fa-shared-context-poc/new.py\n",
                     **CACHED)
    with pytest.raises(hooks.Stop, match="2 uncommitted change.*shared_context/api.py.*FA_ALLOW_DIRTY_BUILD"):
        hooks.images(ctx, "f" * 40, "e" * 64)
    assert builds(ctx) == []  # neither built nor reused
    status = next(call for call in ctx.run.calls if "status" in call)
    assert "--untracked-files=all" in status
    assert "src/fa-shared-context-poc/shared_context" in status and "src/fa-shared-context-poc/README.md" not in status


def test_build_inputs_are_the_dockerfiles_and_what_they_copy(hooks):
    paths = hooks.build_input_paths()
    assert {"shared_context", "tool_catalog", "web_test_console", "behavior_server_main.py", "model-catalog.json",
            "requirements-behavior.txt", "tools/playwright-mcp/package.json", "Dockerfile.web-console"} <= set(paths)
    assert not {"README.md", "tests", "docs", "feed_mcp", "hosted_main.py"} & set(paths)
    assert not any(path.startswith(("/", "--")) or path.endswith("/") for path in paths)  # no flags, targets or slashes


def test_only_files_an_image_copies_count_as_uncommitted_build_inputs(hooks, tmp_path, monkeypatch):
    """Real git: a doc-only edit under src/fa-shared-context-poc does not block; a copied file or a new one does."""
    root = tmp_path / "repo"
    app = root / "src" / "fa-shared-context-poc"
    for folder in ("shared_context", "web_test_console", "tool_catalog", "tools/playwright-mcp"):
        (app / folder).mkdir(parents=True)
    for name in hooks.IMAGE_DOCKERFILES:
        (app / name).write_text((hooks.APP / name).read_text(encoding="utf-8"), encoding="utf-8")
    for path in hooks.build_input_paths():
        if not (app / path).exists():
            (app / path).write_text("x", encoding="utf-8")
    for folder in ("shared_context", "web_test_console", "tool_catalog"):
        (app / folder / "a.py").write_text("x", encoding="utf-8")
    (app / "README.md").write_text("doc", encoding="utf-8")
    identity = ["-c", "user.name=t", "-c", "user.email=t@example.invalid"]

    def repo_git(*args):
        return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True)
    repo_git("init", "-q")
    repo_git(*identity, "add", "-A")
    repo_git(*identity, "commit", "-q", "-m", "base")
    monkeypatch.setattr(hooks, "ROOT", root)
    monkeypatch.setattr(hooks, "APP", app)

    def real_run(args, **kwargs):
        done = subprocess.run(args, capture_output=True, text=True, check=False)
        return SimpleNamespace(code=done.returncode, stdout=done.stdout, stderr=done.stderr)
    ctx = context(hooks, tmp_path, {})
    ctx.run = real_run

    (app / "README.md").write_text("edited doc", encoding="utf-8")
    (app / "notes.md").write_text("new untracked doc", encoding="utf-8")
    assert hooks.build_input_changes(ctx) == []
    (app / "shared_context" / "a.py").write_text("edited", encoding="utf-8")
    (app / "tool_catalog" / "new.json").write_text("{}", encoding="utf-8")
    assert sorted(hooks.build_input_changes(ctx)) == ["src/fa-shared-context-poc/shared_context/a.py",
                                                      "src/fa-shared-context-poc/tool_catalog/new.json"]


def test_rollback_override_reaches_ci_deploy_only_when_exactly_true(hooks, tmp_path):
    for value, passed in (("true", True), ("false", False), ("1", False), (None, False)):
        extra = {} if value is None else {"FA_ALLOW_ROLLBACK": value}
        env = hooks.build_env(context(hooks, tmp_path, {}, FA_DEPLOY_RUN="1", **extra), "f" * 40)
        assert ("FA_ALLOW_ROLLBACK" in env) is passed


def test_images_of_a_clean_checkout_are_reused_for_the_same_commit_and_core(hooks, tmp_path):
    ctx = image_case(hooks, tmp_path, "", **CACHED)
    assert hooks.images(ctx, "f" * 40, "e" * 64) == BUILT and builds(ctx) == []


def test_an_allowed_dirty_build_is_built_and_never_reused(hooks, tmp_path):
    ctx = image_case(hooks, tmp_path, " M src/fa-shared-context-poc/shared_context/x.py\n",
                     FA_ALLOW_DIRTY_BUILD="true", **CACHED)
    assert hooks.images(ctx, "f" * 40, "e" * 64) == BUILT
    assert len(builds(ctx)) == 3 and ctx.values["FA_IMAGES_COMMIT"] == "f" * 40 + "+dirty"
    # The next clean run of the same commit builds again instead of reusing the working-tree images.
    clean = image_case(hooks, tmp_path, "", **{**CACHED, "FA_IMAGES_COMMIT": ctx.values["FA_IMAGES_COMMIT"]})
    hooks.images(clean, "f" * 40, "e" * 64)
    assert len(builds(clean)) == 3 and clean.values["FA_IMAGES_COMMIT"] == "f" * 40


# ---- ADR-0010 section 5: the deploy window of FA_NETWORK_MODE=private ----------------------------------------------

IP = "198.51.100.7"
# A read answering ARM's HTML 503 page (live teardown 2026-10-28, shortened): az rest prints the reason phrase and body.
SERVICE_UNAVAILABLE = failed("ERROR: Service Unavailable(<!DOCTYPE html PUBLIC '-//W3C//DTD XHTML 1.0 Transitional//EN' "
                             "'http://www.w3.org/TR/xhtml1/DTD/xhtml1-transitional.dtd'><html><head><title>Service "
                             "Unavailable</title></head><body>The service is unavailable.</body></html>)")
OPEN_BODY = {"properties": {"publicNetworkAccess": "Enabled", "networkAcls": {
    "defaultAction": "Deny", "bypass": "AzureServices", "ipRules": [{"value": IP}]}}}
CLOSE_BODY = {"properties": {"publicNetworkAccess": "Disabled", "networkAcls": {
    "defaultAction": "Deny", "bypass": "AzureServices", "ipRules": []}}}
PRIVATE_DEPLOY = {**PRIVATE, "FA_FOUNDRY_ACCOUNT": ACCOUNT}


class FakeAccount:
    """The private environment's Foundry account: PATCH bodies change it, GETs read it back, and its data plane
    answers only while public network access is on. `close` = "refused" (the PATCH fails) or "ignored" (it answers
    200 but nothing changes) breaks the closing PATCH only. `unavailable` = (patches, count): once that many PATCHes
    were sent, the next `count` account GETs answer SERVICE_UNAVAILABLE (1: the close's own read before its PATCH,
    as on 2026-10-28; 2: its read-back)."""

    def __init__(self, open_to=None, close=None, data_plane=True, log=None, unavailable=(0, 0)):
        self.properties = {"provisioningState": "Succeeded", "networkInjections": [{"scenario": "agent"}],
                           **json.loads(json.dumps((OPEN_BODY if open_to else CLOSE_BODY)["properties"]))}
        if open_to:
            self.properties["networkAcls"]["ipRules"] = [{"value": open_to}]
        self.close, self.data_plane_on, self.patches = close, data_plane, []
        self.log = [] if log is None else log
        self.unavailable_after, self.unavailable = unavailable

    def answers_unavailable(self):
        if self.unavailable and len(self.patches) >= self.unavailable_after:
            self.unavailable -= 1
            return True
        return False

    def __call__(self, args):
        if args[args.index("--method") + 1] == "patch":
            body = json.loads(Path(args[args.index("--body") + 1][1:]).read_text(encoding="utf-8"))
            self.patches.append(body)
            opening = body["properties"]["publicNetworkAccess"] == "Enabled"
            self.log.append("window:open" if opening else "window:close")
            if not opening and self.close == "refused":
                return failed("ERROR: (AuthorizationFailed) no write")
            if opening or self.close != "ignored":
                self.properties.update(json.loads(json.dumps(body["properties"])))
            return ok({})
        if self.answers_unavailable():
            return SERVICE_UNAVAILABLE
        return ok({"name": ACCOUNT, "properties": json.loads(json.dumps(self.properties))})

    def data_plane(self, args):
        if self.data_plane_on and self.properties["publicNetworkAccess"] == "Enabled":
            return ok({"data": []})
        return failed("ERROR: Forbidden({\"error\":{\"message\":\"Public access is disabled.\"}})")

    def routes(self):
        return {"/agents?api-version=v1": self.data_plane, "Microsoft.CognitiveServices/accounts/": self}


@pytest.fixture
def deployer(hooks, monkeypatch):
    """The echo service's answer; a test that must not reach it replaces this with a failure."""
    monkeypatch.setattr(hooks, "deployer_ipv4", lambda: IP)


def private_deploy(hooks, tmp_path, monkeypatch, account, api_exists=False, **values):
    return deploy_case(hooks, tmp_path, monkeypatch, api_exists, routes=account.routes(), steps=account.log,
                       **{**PRIVATE_DEPLOY, "FA_CONFIRM_DEPLOY_WINDOW": IP, **values})


def window_context(hooks, tmp_path, account, **values):
    return context(hooks, tmp_path, account.routes(), **PRIVATE_DEPLOY, FA_PROJECT_ENDPOINT="https://p", **values)


def test_private_deploy_opens_the_window_only_around_the_data_plane_steps(hooks, tmp_path, monkeypatch, deployer,
                                                                          capsys):
    account = FakeAccount()
    steps, ctx = private_deploy(hooks, tmp_path, monkeypatch, account)
    assert steps[:8] == ["profile", "images", "window:open", "webiq", "memory_store", "seed", "window:close",
                         "agent_roles"]
    # Only the two properties are written, never networkInjections; the account reads back closed.
    assert account.patches == [OPEN_BODY, CLOSE_BODY]
    assert account.properties["networkInjections"] == [{"scenario": "agent"}]
    assert hooks.window_closed({"properties": account.properties})
    probes = [call for call in ctx.run.calls if "/agents?api-version=v1" in " ".join(call)]
    assert probes and all(call[call.index("--resource") + 1] == "https://ai.azure.com" for call in probes)
    out = capsys.readouterr().out
    assert f"opens it to {IP} alone" in out and "shared NAT" in out and "closed again (read back" in out
    question = out.split("FA: Foundry account")[0]  # what the person reads before confirming
    assert "up to about 10 min" in question and "stays open until you answer" in question
    assert "data plane" not in question


def test_a_private_upgrade_wraps_webiq_and_skips_the_window_without_data_plane_steps(hooks, tmp_path, monkeypatch,
                                                                                      deployer):
    account = FakeAccount()
    steps, _ = private_deploy(hooks, tmp_path, monkeypatch, account, api_exists=True, FA_SESSIONS_PREPARED="true",
                              FA_ENABLE_WEBIQ="true")
    assert steps[steps.index("ci:gate"):steps.index("ci:guard")] == ["ci:gate", "window:open", "webiq",
                                                                     "memory_store", "window:close"]
    monkeypatch.setattr(hooks, "deployer_ipv4", lambda: pytest.fail("no data-plane step, so no IP is read"))
    account = FakeAccount()
    steps, _ = private_deploy(hooks, tmp_path, monkeypatch, account, api_exists=True, FA_SESSIONS_PREPARED="true")
    assert account.patches == [] and "window:open" not in steps
    # Private memory is Cosmos DB (ADR-0011): no memory store on the Foundry data plane, so no window for it either.
    account = FakeAccount()
    steps, _ = private_deploy(hooks, tmp_path, monkeypatch, account, api_exists=True, FA_SESSIONS_PREPARED="true",
                              FA_ENABLE_MEMORY="true")
    assert account.patches == [] and "window:open" not in steps


def test_the_window_keeps_every_other_network_acl_field(hooks, tmp_path, deployer):
    # Bicep sets virtualNetworkRules: [] today; phase 2b may add rules, and the PATCH replaces networkAcls whole.
    account = FakeAccount()
    account.properties["networkAcls"]["virtualNetworkRules"] = [{"id": "/subscriptions/x/.../subnets/snet-api"}]
    ctx = window_context(hooks, tmp_path, account, FA_CONFIRM_DEPLOY_WINDOW=IP)
    with hooks.data_plane_window(ctx, hooks.window_ip(ctx, seeding=True)):
        pass
    rules = [{"id": "/subscriptions/x/.../subnets/snet-api"}]
    assert [patch["properties"]["networkAcls"].pop("virtualNetworkRules") for patch in account.patches] == [rules] * 2
    assert account.patches == [OPEN_BODY, CLOSE_BODY]
    assert account.properties["networkAcls"]["virtualNetworkRules"] == rules


@pytest.mark.parametrize("close", ["refused", "ignored"])
def test_a_failed_close_is_loud_names_the_command_and_stops(hooks, tmp_path, monkeypatch, deployer, capsys, close):
    account = FakeAccount(close=close)
    with pytest.raises(hooks.Stop, match=f"Foundry account {ACCOUNT} could not be closed again"):
        private_deploy(hooks, tmp_path, monkeypatch, account)
    assert "agent_roles" not in account.log  # nothing after the window runs
    err = capsys.readouterr().err
    assert f"WARNING: Foundry account {ACCOUNT} may still be open" in err
    close_file = tmp_path / "dev" / "fa" / "foundry-window-close.json"
    assert (f"az rest --method patch --url \"/subscriptions/{SUBSCRIPTION}/resourceGroups/rg-dev/providers/"
            f"Microsoft.CognitiveServices/accounts/{ACCOUNT}?api-version=2025-06-01\" --body @{close_file} "
            "--headers Content-Type=application/json") in err
    assert json.loads(close_file.read_text(encoding="utf-8")) == CLOSE_BODY


def test_a_failed_step_and_a_failed_close_report_both(hooks, tmp_path, deployer, capsys):
    account = FakeAccount(close="refused")
    ctx = window_context(hooks, tmp_path, account, FA_CONFIRM_DEPLOY_WINDOW=IP)
    with pytest.raises(hooks.Stop, match="could not be closed again"):
        with hooks.data_plane_window(ctx, hooks.window_ip(ctx, seeding=True)):
            raise hooks.Stop("seed went wrong")
    assert "the steps stopped: seed went wrong" in capsys.readouterr().err  # not lost behind the close failure


@pytest.mark.parametrize("after", [1, 2])  # the close's read before its PATCH (2026-10-28), then its read-back
def test_a_transient_503_on_a_close_read_is_read_again_and_the_deploy_goes_on(hooks, tmp_path, monkeypatch, deployer,
                                                                             capsys, after):
    account = FakeAccount(unavailable=(after, 2))
    steps, _ = private_deploy(hooks, tmp_path, monkeypatch, account)
    assert steps[:8] == ["profile", "images", "window:open", "webiq", "memory_store", "seed", "window:close",
                         "agent_roles"]
    assert account.patches == [OPEN_BODY, CLOSE_BODY] and hooks.window_closed({"properties": account.properties})
    out, err = capsys.readouterr()
    assert out.count("answered Service Unavailable (transient); reading it again in") == 2 and err == ""
    assert "closed again (read back: publicNetworkAccess Disabled, no IP rule)" in out


def test_a_close_pre_read_that_keeps_failing_sends_the_open_time_close_body_once(hooks, tmp_path, monkeypatch,
                                                                                    deployer, capsys):
    account = FakeAccount(unavailable=(1, 4))  # the close's read before its PATCH fails all 4 times; the read-back is fine
    account.properties["networkAcls"]["virtualNetworkRules"] = [{"id": "snet-api"}]
    steps, _ = private_deploy(hooks, tmp_path, monkeypatch, account)
    assert steps[:8] == ["profile", "images", "window:open", "webiq", "memory_store", "seed", "window:close",
                         "agent_roles"]
    assert [patch["properties"]["networkAcls"].pop("virtualNetworkRules") for patch in account.patches] == [
        [{"id": "snet-api"}]] * 2  # the fallback body is the one built from the open's read
    assert account.patches == [OPEN_BODY, CLOSE_BODY] and hooks.window_closed({"properties": account.properties})
    out = capsys.readouterr().out
    assert out.count("answered Service Unavailable (transient); reading it again in") == 3
    assert "closing anyway: sending the close body built from the read when the window opened" in out
    assert "closed again (read back: publicNetworkAccess Disabled, no IP rule)" in out


@pytest.mark.parametrize("after", [1, 2])  # 1: the pre-read fails too, so the open-time close body is sent once
def test_a_close_read_that_keeps_failing_warns_with_the_ip_and_both_commands(hooks, tmp_path, monkeypatch, deployer,
                                                                            capsys, after):
    account = FakeAccount(unavailable=(after, 10_000))
    with pytest.raises(hooks.Stop, match=f"Foundry account {ACCOUNT} could not be closed again"):
        private_deploy(hooks, tmp_path, monkeypatch, account)
    # the close PATCH is sent once, never twice, and nothing after the window runs
    assert account.patches == [OPEN_BODY, CLOSE_BODY] and "agent_roles" not in account.log
    out, err = capsys.readouterr()
    assert ("closing anyway: sending the close body built from the read when the window opened" in out) == (after == 1)
    assert f"WARNING: Foundry account {ACCOUNT} may still be open to {IP} (the steps finished)" in err
    assert "kept answering Service Unavailable 4 times" in err
    url = (f"/subscriptions/{SUBSCRIPTION}/resourceGroups/rg-dev/providers/Microsoft.CognitiveServices/accounts/"
           f"{ACCOUNT}?api-version=2025-06-01")
    assert f'az rest --method patch --url "{url}" --body @' in err and f'az rest --method get --url "{url}" --query' in err


def interrupting(account, when):
    """The account's routes, recording the SIGINT handler at each ARM call and raising KeyboardInterrupt (a Ctrl+C)
    at the first call `when(args)` picks."""
    handlers, routes = [], account.routes()

    def call(args):
        handlers.append(signal.getsignal(signal.SIGINT))
        if when(args):
            raise KeyboardInterrupt
        return account(args)

    return {**routes, "Microsoft.CognitiveServices/accounts/": call}, handlers


def test_ctrl_c_in_the_window_steps_still_closes_it_with_ctrl_c_deferred(hooks, tmp_path, deployer, capsys):
    account = FakeAccount()
    routes, handlers = interrupting(account, lambda args: False)
    ctx = context(hooks, tmp_path, routes, **PRIVATE_DEPLOY, FA_PROJECT_ENDPOINT="https://p",
                  FA_CONFIRM_DEPLOY_WINDOW=IP)
    before = signal.getsignal(signal.SIGINT)
    with pytest.raises(KeyboardInterrupt):
        with hooks.data_plane_window(ctx, hooks.window_ip(ctx, seeding=True)):
            raise KeyboardInterrupt
    assert account.patches == [OPEN_BODY, CLOSE_BODY] and hooks.window_closed({"properties": account.properties})
    # The open (read, PATCH) ran with the usual handler; the close and its read-back with Ctrl+C ignored.
    assert handlers[0] == before and len(handlers) > 3 and set(handlers[2:]) == {signal.SIG_IGN}
    assert signal.getsignal(signal.SIGINT) == before
    assert "closed again (read back: publicNetworkAccess Disabled, no IP rule); the steps stopped: KeyboardInterrupt" \
        in capsys.readouterr().out


def test_ctrl_c_during_the_close_is_loud_and_goes_on(hooks, tmp_path, deployer, capsys):
    account = FakeAccount()
    closing = lambda args: "patch" in args and "foundry-window-close.json" in " ".join(args)  # noqa: E731
    routes, _ = interrupting(account, closing)
    ctx = context(hooks, tmp_path, routes, **PRIVATE_DEPLOY, FA_PROJECT_ENDPOINT="https://p",
                  FA_CONFIRM_DEPLOY_WINDOW=IP)
    with pytest.raises(KeyboardInterrupt):
        with hooks.data_plane_window(ctx, hooks.window_ip(ctx, seeding=True)):
            pass
    out, err = capsys.readouterr()
    assert "Ctrl+C is ignored until it reads back closed" in out and "press it again afterwards" in out
    assert f"WARNING: Foundry account {ACCOUNT} may still be open" in err and "failed: KeyboardInterrupt" in err
    assert "az rest --method patch --url" in err and "foundry-window-close.json" in err


def test_the_windows_console_handler_is_restored_also_when_the_body_raises(hooks, monkeypatch):
    calls = []
    kernel32 = SimpleNamespace(SetConsoleCtrlHandler=lambda handler, add: calls.append((handler, add)))
    monkeypatch.setattr(hooks, "ctypes", SimpleNamespace(windll=SimpleNamespace(kernel32=kernel32)))
    monkeypatch.setattr(hooks.os, "name", "nt")
    with hooks.interrupt_deferred():
        assert calls == [(None, True)]
    with pytest.raises(RuntimeError):
        with hooks.interrupt_deferred():
            raise RuntimeError("the close failed")
    assert calls == [(None, True), (None, False)] * 2


@pytest.mark.parametrize("hang", [False, True])
def test_every_window_az_call_is_bounded_and_a_hung_close_prints_the_command(hooks, tmp_path, deployer, capsys, hang):
    # The close ignores Ctrl+C, so an az that never answers must time out, not hang the deploy.
    account = FakeAccount()
    ctx = window_context(hooks, tmp_path, account, FA_CONFIRM_DEPLOY_WINDOW=IP)
    fake, timeouts = ctx.run, []

    def run(args, **kwargs):
        if "Microsoft.CognitiveServices/accounts/" in " ".join(args):
            timeouts.append(kwargs.get("timeout"))
            if hang and "foundry-window-close.json" in " ".join(args):
                raise subprocess.TimeoutExpired(args, kwargs.get("timeout"))
        return fake(args, **kwargs)

    ctx.run = run
    expected = (pytest.raises(hooks.Stop, match=f"{ACCOUNT} could not be closed again") if hang
                else contextlib.nullcontext())
    with expected:
        with hooks.data_plane_window(ctx, hooks.window_ip(ctx, seeding=True)):
            pass
    assert len(timeouts) >= 4 and set(timeouts) == {90}  # open GET+PATCH, close GET+PATCH, and each read-back
    err = capsys.readouterr().err
    if hang:
        assert f"WARNING: Foundry account {ACCOUNT} may still be open" in err and "timed out after 90" in err
        assert "az rest --method patch --url" in err and "foundry-window-close.json" in err
    else:
        assert err == "" and hooks.window_closed({"properties": account.properties})


def test_a_close_pre_read_that_hangs_still_sends_the_open_time_close_body_once(hooks, tmp_path, deployer, capsys):
    account = FakeAccount()
    ctx = window_context(hooks, tmp_path, account, FA_CONFIRM_DEPLOY_WINDOW=IP)
    fake, closing, hung = ctx.run, [], []

    def run(args, **kwargs):  # only the close's read before its PATCH hangs; the read-back answers
        if closing and "Microsoft.CognitiveServices/accounts/" in " ".join(args) and "get" in args and not hung:
            hung.append(kwargs.get("timeout"))
            raise subprocess.TimeoutExpired(args, kwargs.get("timeout"))
        return fake(args, **kwargs)

    ctx.run = run
    with hooks.data_plane_window(ctx, hooks.window_ip(ctx, seeding=True)):
        closing.append(True)
    assert hung == [90] and account.patches == [OPEN_BODY, CLOSE_BODY] and hooks.window_closed({"properties": account.properties})
    out, err = capsys.readouterr()
    assert "the read before the close PATCH failed" in out and "timed out after 90" in out
    assert "closing anyway: sending the close body built from the read when the window opened" in out
    assert "closed again (read back: publicNetworkAccess Disabled, no IP rule)" in out and err == ""


class FakeChild:
    """A Popen whose first wait() is cut by Ctrl+C."""

    def __init__(self):
        self.waits = 0

    def wait(self):
        self.waits += 1
        if self.waits == 1:
            raise KeyboardInterrupt
        return 7

    def kill(self):
        pytest.fail("the child is never killed: its finally closes the window")

    terminate = kill


def test_the_parent_keeps_waiting_through_ctrl_c_and_returns_the_childs_code(hooks, capsys):
    child = FakeChild()
    assert hooks.wait_child(child) == 7 and child.waits == 2
    assert "waiting for the deploy to stop" in capsys.readouterr().err


def test_the_main_entry_exits_non_zero_when_the_window_does_not_close(hooks, tmp_path, monkeypatch, deployer, capsys):
    account = FakeAccount(close="refused")

    def deploy(ctx):  # main()'s own Context is replaced by one with the fake account
        window = window_context(hooks, tmp_path, account, FA_CONFIRM_DEPLOY_WINDOW=IP)
        with hooks.data_plane_window(window, hooks.window_ip(window, True)):
            pass

    monkeypatch.setitem(hooks.PHASES, "deploy", deploy)
    for key, value in {"AZURE_ENV_NAME": "dev", "FA_AZD_VENV": "1"}.items():
        monkeypatch.setenv(key, value)
    assert hooks.main(["deploy"]) == 1
    assert "FA: stopped: Foundry account" in capsys.readouterr().err


def test_a_hung_opening_call_is_a_readable_stop_and_still_closes(hooks, tmp_path, monkeypatch, deployer, capsys):
    account = FakeAccount()
    window = window_context(hooks, tmp_path, account, FA_CONFIRM_DEPLOY_WINDOW=IP)
    fake = window.run

    def run(args, **kwargs):
        if "foundry-window-open.json" in " ".join(args):
            raise subprocess.TimeoutExpired(args, kwargs.get("timeout"))
        return fake(args, **kwargs)

    def deploy(ctx):
        window.run = run
        with hooks.data_plane_window(window, hooks.window_ip(window, True)):
            pytest.fail("the steps never run")

    monkeypatch.setitem(hooks.PHASES, "deploy", deploy)
    for key, value in {"AZURE_ENV_NAME": "dev", "FA_AZD_VENV": "1"}.items():
        monkeypatch.setenv(key, value)
    assert hooks.main(["deploy"]) == 1
    out, err = capsys.readouterr()
    assert "FA: stopped: Command" in err and "timed out after 90 seconds" in err and "Traceback" not in err
    assert "closed again (read back" in out and account.patches == [CLOSE_BODY]


@pytest.mark.parametrize("api_exists, values, patches", [
    (False, {}, [CLOSE_BODY, OPEN_BODY, CLOSE_BODY]),        # first install: closed first, then its own window
    (True, {"FA_SESSIONS_PREPARED": "true"}, [CLOSE_BODY]),  # an upgrade without data-plane steps still closes it
])
def test_a_window_left_open_by_an_interrupted_run_is_closed_first(hooks, tmp_path, monkeypatch, deployer, capsys,
                                                                  api_exists, values, patches):
    account = FakeAccount(open_to="192.0.2.44")
    steps, _ = private_deploy(hooks, tmp_path, monkeypatch, account, api_exists=api_exists, **values)
    assert account.patches == patches and steps[:2] == ["window:close", "profile"]
    assert "192.0.2.44" in capsys.readouterr().out
    assert hooks.window_closed({"properties": account.properties})


@pytest.mark.parametrize("tty, confirmation", [(False, ""), (False, "192.0.2.1"), (True, "")])
@pytest.mark.parametrize("api_exists, values", [(False, {}),
                                                (True, {"FA_SESSIONS_PREPARED": "true", "FA_ENABLE_WEBIQ": "true"})])
def test_the_window_is_confirmed_before_the_image_build(hooks, tmp_path, monkeypatch, deployer, tty, confirmation,
                                                        api_exists, values):
    # The terminal's answer is "" (context()'s prompt), FA_CONFIRM_DEPLOY_WINDOW is unset or for another IP.
    account = FakeAccount()
    with pytest.raises(hooks.Stop, match="nothing was changed"):
        private_deploy(hooks, tmp_path, monkeypatch, account, api_exists=api_exists, tty=tty,
                       FA_CONFIRM_DEPLOY_WINDOW=confirmation, **values)
    assert account.log == [] and account.patches == []  # no profile, no build, no window


def test_an_ip_changed_during_the_build_opens_nothing(hooks, tmp_path, monkeypatch):
    answers = iter([IP, "203.0.113.9"])
    monkeypatch.setattr(hooks, "deployer_ipv4", lambda: next(answers))
    account = FakeAccount()
    with pytest.raises(hooks.Stop, match=f"is now 203.0.113.9, not the {IP} confirmed before the image build; "
                                         "nothing was opened"):
        private_deploy(hooks, tmp_path, monkeypatch, account)
    assert account.log == ["profile", "images"] and account.patches == []


def make_public(account):
    """What a public account reads: no network injection, public network access on, no IP rule."""
    account.properties.update(networkInjections=None, publicNetworkAccess="Enabled")
    return account


@pytest.mark.parametrize("api_exists, values", [(False, {}), (True, {"FA_SESSIONS_PREPARED": "true"})])
def test_a_mis_set_private_mode_never_touches_a_public_account(hooks, tmp_path, monkeypatch, deployer, api_exists,
                                                               values):
    # A public account is "open" by window_closed(): without the check the leftover close would PATCH it Disabled.
    account = make_public(FakeAccount())
    with pytest.raises(hooks.Stop, match=f"{ACCOUNT} already exists in rg-dev as FA_NETWORK_MODE=public.*nothing "
                                         "was changed"):
        private_deploy(hooks, tmp_path, monkeypatch, account, api_exists=api_exists, **values)
    assert account.patches == [] and account.log == []  # stopped before the profile, the build and any window


@pytest.mark.parametrize("tty, answer, values", [
    (True, "y", {}),                                           # a y/N habit is not the IP
    (True, "198.51.100.8", {}),                                # another IP
    (False, None, {}),                                         # no terminal, no confirmation
    (False, None, {"FA_CONFIRM_DEPLOY_WINDOW": "192.0.2.1"}),  # a confirmation made on another network
])
def test_the_window_opens_only_for_the_confirmed_ip(hooks, tmp_path, deployer, tty, answer, values):
    account = FakeAccount()
    ctx = window_context(hooks, tmp_path, account, **values)
    ctx.tty, ctx.prompt = tty, (lambda text: answer)
    with pytest.raises(hooks.Stop, match="nothing was changed"):
        hooks.window_ip(ctx, seeding=True)
    assert account.patches == [] and ctx.run.calls == []


def test_a_typed_ip_opens_and_closes_the_window(hooks, tmp_path, deployer):
    account = FakeAccount()
    ctx = window_context(hooks, tmp_path, account)
    asked = []
    ctx.prompt = lambda text: asked.append(text) or IP
    with hooks.data_plane_window(ctx, hooks.window_ip(ctx, seeding=True)):
        assert account.properties["publicNetworkAccess"] == "Enabled"
    assert asked == [f"Type the IP ({IP}) to allow it for these steps: "]
    assert account.patches == [OPEN_BODY, CLOSE_BODY]


def test_a_data_plane_that_never_answers_stops_and_still_closes(hooks, tmp_path, deployer):
    account = FakeAccount(data_plane=False)
    ctx = window_context(hooks, tmp_path, account, FA_CONFIRM_DEPLOY_WINDOW=IP)
    ran = []
    with pytest.raises(hooks.Stop, match="the Foundry data plane to answer .*not reached within 3 min"):
        with hooks.data_plane_window(ctx, hooks.window_ip(ctx, seeding=True)):
            ran.append("step")
    assert ran == [] and account.patches == [OPEN_BODY, CLOSE_BODY]


def test_public_deploy_only_reads_the_foundry_account_and_never_opens_it(hooks, tmp_path, monkeypatch):
    # One GET for the FA_NETWORK_MODE check; any PATCH would fail the test.
    monkeypatch.setattr(hooks, "deployer_ipv4", lambda: pytest.fail("public mode reads no IP"))
    for values in ({}, {"FA_NETWORK_MODE": "public"}):
        account = make_public(FakeAccount())
        steps, ctx = deploy_case(hooks, tmp_path, monkeypatch, api_exists=False, routes=account.routes(),
                                 FA_ENABLE_WEBIQ="true", FA_CONFIRM_DEPLOY_WINDOW=IP, FA_FOUNDRY_ACCOUNT=ACCOUNT,
                                 **values)
        assert steps[:6] == ["profile", "images", "webiq", "memory_store", "seed", "agent_roles"]
        assert [call[call.index("--method") + 1] for call in ctx.run.calls if "rest" in call] == ["get"]
        assert account.patches == []
    steps, ctx = deploy_case(hooks, tmp_path, monkeypatch, api_exists=True, FA_SESSIONS_PREPARED="true",
                             FA_ENABLE_WEBIQ="true")
    assert "webiq" in steps and [call[call.index("--method") + 1] for call in ctx.run.calls if "rest" in call] == [
        "get"]


@pytest.mark.parametrize("values, account, stop", [
    ({"FA_NETWORK_MODE": "isolated"}, None, "must be public or private, not 'isolated'"),  # mistyped
    ({}, FakeAccount(), f"{ACCOUNT} already exists in rg-dev as FA_NETWORK_MODE=private"),  # cleared on a private one
])
def test_deploy_alone_checks_the_network_mode_before_anything(hooks, tmp_path, monkeypatch, values, account, stop):
    # `azd deploy` without provision: a wrong mode would drop FA_NETWORK_MODE (the Work IQ guard) from the API.
    monkeypatch.setattr(hooks, "deployer_ipv4", lambda: pytest.fail("stopped before the IP"))
    steps = []
    with pytest.raises(hooks.Stop, match=stop):
        deploy_case(hooks, tmp_path, monkeypatch, api_exists=True, steps=steps,
                    routes=account.routes() if account else None, FA_FOUNDRY_ACCOUNT=ACCOUNT, **values)
    assert steps == [] and (account is None or account.patches == [])


class FakeEcho:
    def __init__(self, body):
        self.body = body

    def __call__(self, url, timeout):
        assert url == "https://api.ipify.org" and timeout == 15
        if isinstance(self.body, Exception):
            raise self.body
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, size):
        return self.body


@pytest.mark.parametrize("body, stop", [
    (b"8.8.4.4\n", None),
    (OSError("timed out"), r"cannot read this computer's public IPv4 from https://api\.ipify\.org \(timed out\)"),
    (b"2001:4860:4860::8888", "cannot read this computer's public IPv4"),  # IPv6 or junk is not an IPv4
    (b"10.1.2.3", r"10\.1\.2\.3, which is not a public IPv4"),
    (b"224.0.0.1", r"224\.0\.0\.1, which is not a public IPv4"),  # multicast reads as global
])
def test_the_deployer_ip_is_one_public_ipv4_or_a_stop(hooks, monkeypatch, body, stop):
    monkeypatch.setattr(hooks.urllib.request, "urlopen", FakeEcho(body))
    if stop is None:
        assert hooks.deployer_ipv4() == "8.8.4.4"
    else:
        with pytest.raises(hooks.Stop, match=stop):
            hooks.deployer_ipv4()


# ---- ADR-0010 section 7: predown of FA_NETWORK_MODE=private --------------------------------------------------------

NOT_FOUND = failed("ERROR: Not Found({\"error\":{\"code\":\"ResourceNotFound\"}})")
GATEWAY_TIMEOUT = failed("ERROR: (GatewayTimeout) The gateway did not receive a response")
def workspace_error(what, inner="NotFoundError"):
    """A project capability host GET's error (live run 2026-10-28, names replaced): az prints the service's reason
    phrase, so neither "Not Found" nor "ResourceNotFound" is in the text."""
    return failed(
        f"ERROR: {what} not found in workspace '{ACCOUNT}@fa-dev@AM({{\"error\":{{\"code\":\"UserError\","
        f"\"message\":\"{what} not found in workspace '{ACCOUNT}@fa-dev@AML'\",\"details\":[],\"additionalInfo\":"
        "[{\"type\":\"ComponentName\",\"info\":{\"value\":\"managementfrontend\"}},{\"type\":\"InnerError\","
        f"\"info\":{{\"value\":{{\"code\":\"{inner}\",\"innerError\":null}}}}}}]}}}})")


HOST_MISSING = workspace_error("CapabilityHost 'caphostproj'")
# The same front-end text about something other than the host: not "the host is gone".
CONNECTION_MISSING = workspace_error("Connection 'x'")
def nested(*ids):
    """The account DELETE's CannotDeleteResource, in the shape Azure answers (live run 2026-10-24)."""
    listed = ", ".join(f"'Microsoft.CognitiveServices/accounts/{ACCOUNT}/{item}'" for item in ids)
    return failed("ERROR: (CannotDeleteResource) Cannot delete resource while nested resources exist. Some existing "
                  f"nested resource IDs include: {listed}.\nCode: CannotDeleteResource")


STALE_PROJECT = nested("projects/fa-dev")  # the same text whether the project is deleted or still there


class FakeFoundry(FakeAccount):
    """A private environment being taken down. The account goes Deleting on its DELETE (refused while the project
    exists) and disappears after `deleting_polls` reads, leaving a soft-deleted copy; the purge removes it after one
    read; snet-agents loses one service association link per read. `delete_answer` replaces the DELETE's answer
    (the state change happens anyway, as when the CLI times out). `project` is True, False or "Deleting" (gone after
    one read). `stale_project`: how many account DELETEs are still refused for the deleted project. `faults`:
    "no-start" (the account DELETE changes nothing), "project-timeout" (the project DELETE answers GatewayTimeout
    but goes on), "sticky-agents" (agent DELETEs answer 200, the agents stay), "agents-404" (agent DELETEs answer
    404 and the agents go), "purge-fails" (the purge is refused and the copy stays), "project-refused" (the project
    DELETE answers STALE_PROJECT and the project stays), "host-timeout" (the capability host DELETE answers
    GatewayTimeout but goes on), "hosts-404" (the list of the project's capability hosts answers 404), "hosts-500" (it
    answers InternalServerError). `refuse_delete`: the answer of every account DELETE. `host`: the project capability
    host's provisioningState ("Deleting" goes after `host_polls` reads), or None (provisioned before it existed);
    `host_name`: its name as the list answers it. [文件] Assumed, not probed: the project DELETE is refused while the
    host is there (the official cleanup tool deletes it first; no probe has tried otherwise). Fault "host-gone-text":
    a gone host's GET answers HOST_MISSING; "host-other-text": CONNECTION_MISSING."""

    def __init__(self, account="Succeeded", project=True, agents=("fa-dev-a", "fa-dev-b"), soft_deleted=False,
                 links=1, deleting_polls=2, delete_answer=None, refuse_delete=False, open_to=None, faults=(),
                 stale_project=0, host="Succeeded", host_polls=1, host_name="caphostproj", unavailable=(0, 0),
                 deleting_unavailable=0):
        super().__init__(open_to=open_to, unavailable=unavailable)
        self.deleting_unavailable = deleting_unavailable  # account GETs answering 503 while it is Deleting
        self.host, self.host_polls, self.host_name = host, host_polls, host_name
        self.account, self.project, self.agents, self.soft_deleted = account, project, list(agents), soft_deleted
        self.links, self.deleting_polls, self.delete_answer = links, deleting_polls, delete_answer
        self.stale_project = stale_project
        self.refuse_delete, self.purging, self.faults = refuse_delete, False, set(faults)

    def __call__(self, args):
        text, method = " ".join(args), args[args.index("--method") + 1]
        if "subnets/snet-agents" in text:
            links, self.links = self.links, max(0, self.links - 1)
            return ok({"properties": {"serviceAssociationLinks": [{"name": "legionservicelink"}] * links}})
        if "deletedAccounts/" in text:
            if method == "delete":
                self.log.append("purge")
                if "purge-fails" in self.faults:
                    return failed("ERROR: (Conflict) the purge was refused")
                self.purging = True
                return ok()
            if self.purging:
                self.soft_deleted, self.purging = False, False
                return ok({"name": ACCOUNT})
            return ok({"name": ACCOUNT}) if self.soft_deleted else NOT_FOUND
        if "/capabilityHosts" in text:
            assert self.project is True, "the capability hosts are read and deleted only while their project is there"
            if "/capabilityHosts?api-version=2025-06-01" in text:
                assert method == "get"
                if "hosts-404" in self.faults:
                    return NOT_FOUND
                if "hosts-500" in self.faults:
                    return failed("ERROR: (InternalServerError) Something went wrong")
                return ok({"value": [{"name": self.host_name, "properties": {"provisioningState": self.host}}]
                           if self.host else []})
            if f"/capabilityHosts/{self.host_name.rsplit('/', 1)[-1]}?api-version=2025-06-01" not in text:
                return NOT_FOUND
            if method == "delete":
                self.log.append("host:delete")
                self.host = "Deleting"
                return GATEWAY_TIMEOUT if "host-timeout" in self.faults else ok()
            if self.host == "Deleting":
                self.host_polls -= 1
                if self.host_polls < 0:
                    self.host = None
            if self.host is None:
                if "host-other-text" in self.faults:
                    return CONNECTION_MISSING
                return HOST_MISSING if "host-gone-text" in self.faults else NOT_FOUND
            return ok({"name": "caphostproj", "properties": {"provisioningState": self.host}})
        if "/projects/" in text:
            if method == "delete":
                self.log.append("project:delete")
                if self.host:
                    return failed("ERROR: (Conflict) the project still has a capability host")  # [文件] assumed
                if "project-refused" in self.faults:
                    return STALE_PROJECT
                self.project = False
                return GATEWAY_TIMEOUT if "project-timeout" in self.faults else ok()
            if not self.project:
                return NOT_FOUND
            state, self.project = ("Deleting", False) if self.project == "Deleting" else ("Succeeded", True)
            return ok({"name": f"{ACCOUNT}/fa-dev", "properties": {"provisioningState": state}})
        if method == "patch":
            return super().__call__(args)
        if method == "delete":
            self.log.append("account:delete")
            if self.refuse_delete:
                return self.refuse_delete
            if self.project:
                return STALE_PROJECT
            if self.stale_project:
                self.stale_project -= 1
                return STALE_PROJECT
            if "no-start" not in self.faults:
                self.account = "Deleting"
            return self.delete_answer or ok()
        if self.answers_unavailable():
            return SERVICE_UNAVAILABLE
        if self.account == "Deleting" and self.deleting_unavailable:
            self.deleting_unavailable -= 1
            return SERVICE_UNAVAILABLE
        if self.account == "Deleting":
            self.deleting_polls -= 1
            if self.deleting_polls < 0:
                self.account, self.soft_deleted = None, True
        if self.account is None:
            return NOT_FOUND
        return ok({"name": ACCOUNT, "properties": {**json.loads(json.dumps(self.properties)),
                                                   "provisioningState": self.account}})

    def data_plane(self, args):
        if self.properties["publicNetworkAccess"] != "Enabled":
            return failed("ERROR: Forbidden({\"error\":{\"message\":\"Public access is disabled.\"}})")
        if args[args.index("--method") + 1] == "delete":
            url = args[args.index("--url") + 1]
            assert url.endswith("?force=true&api-version=v1")
            name = url.split("/agents/")[1].split("?")[0]
            self.log.append(f"agent:{name}")
            if "sticky-agents" not in self.faults:
                self.agents.remove(name)
            return NOT_FOUND if "agents-404" in self.faults else ok()
        return ok({"data": [{"name": name} for name in self.agents]})

    def routes(self):
        return {"/agents": self.data_plane, "snet-agents": self, "Microsoft.CognitiveServices/": self}


def private_down(hooks, tmp_path, foundry, locks=(LOCK,), tty=True, answers=("dev",), **values):
    def lock_delete(args):
        foundry.log.append("lock:delete")
        return ok()

    routes = {**foundry.routes(), **outside(hooks), "lock list": ok(list(locks)), "lock delete": lock_delete}
    ctx = context(hooks, tmp_path, routes, **{**PRIVATE, **values})
    asked = []
    ctx.tty, ctx.prompt = tty, lambda text: asked.append(text) or answers[len(asked) - 1]
    return ctx, asked


def writes(ctx):
    return [call for call in ctx.run.calls
            if "--method" in call and call[call.index("--method") + 1] in ("patch", "delete", "put")
            or "lock" in call and "delete" in call]


def test_private_predown_asks_once_and_deletes_in_the_probed_order_plus_the_documented_host_step(hooks, tmp_path,
                                                                                                 deployer, capsys):
    # [探針] agents, project, account, purge, link; [文件] the capability host before the project (no probe saw it).
    foundry = FakeFoundry(links=3)
    ctx, asked = private_down(hooks, tmp_path, foundry)
    hooks.predown(ctx)
    assert asked == [f"Type the environment name (dev) to permanently delete and purge Foundry account {ACCOUNT} and "
                     "remove the backup lock: "]
    assert foundry.log == ["window:open", "agent:fa-dev-a", "agent:fa-dev-b", "window:close", "host:delete",
                           "project:delete", "account:delete", "purge", "lock:delete"]
    assert foundry.account is None and not foundry.soft_deleted and foundry.links == 0
    assert foundry.patches == [OPEN_BODY, CLOSE_BODY]
    out = capsys.readouterr().out
    assert (f"permanently deletes the Foundry account {ACCOUNT} with its models and Hosted Agents" in out
            and "first deleting its project capability host caphostproj (that alone can wait up to 30 min)" in out
            and f"opens the account to {IP} alone" in out and "azd asks its own y/N for the rest" in out
            and "waiting for Foundry account" in out and "snet-agents has no service association link" in out)
    # Short lines, one point each, before the question.
    explanation = out.split("FA: FA_NETWORK_MODE=private: before")[1].split("FA: Foundry account")[0]
    assert len(explanation.splitlines()) >= 5 and max(map(len, explanation.splitlines())) < 120


@pytest.mark.parametrize("tty, answers, values, locks", [
    (True, ("yes",), {}, (LOCK,)),                         # anything but the name
    (True, ("Dev",), {}, ()),                              # private asks even without the backup lock
    (False, (), {}, ()),                                   # no terminal, no confirmation
    (False, (), {"FA_CONFIRM_DELETE": "other"}, (LOCK,)),  # a confirmation for another environment
])
def test_private_predown_without_the_name_writes_nothing(hooks, tmp_path, deployer, tty, answers, values, locks):
    foundry = FakeFoundry()
    ctx, asked = private_down(hooks, tmp_path, foundry, locks=locks, tty=tty, answers=answers, **values)
    with pytest.raises(hooks.Stop, match="nothing was removed"):
        hooks.predown(ctx)
    assert writes(ctx) == [] and foundry.log == [] and len(asked) == (1 if tty else 0)


@pytest.mark.parametrize("tty, answers, values, ip", [
    (True, ("yes",), {}, IP),                       # the wrong name
    (False, (), {}, IP),                            # no terminal, no confirmation
    (True, ("dev",), {}, None),                     # the IP read fails
])
def test_private_predown_closes_a_leftover_window_before_it_asks(hooks, tmp_path, monkeypatch, capsys, tty, answers,
                                                                 values, ip):
    def echo():
        if ip is None:
            raise hooks.Stop("cannot read this computer's public IPv4")
        return ip

    monkeypatch.setattr(hooks, "deployer_ipv4", echo)
    foundry = FakeFoundry(open_to="192.0.2.44")
    ctx, asked = private_down(hooks, tmp_path, foundry, tty=tty, answers=answers, **values)
    with pytest.raises(hooks.Stop):
        hooks.predown(ctx)
    assert foundry.log == ["window:close"] and foundry.patches == [CLOSE_BODY]
    assert hooks.window_closed({"properties": foundry.properties})
    out = capsys.readouterr().out
    assert f"Foundry account {ACCOUNT} is open (publicNetworkAccess Enabled, IP rules ['192.0.2.44']" in out
    assert "closed again (read back" in out


@pytest.mark.parametrize("project", [True, False])
def test_a_mis_set_private_predown_never_deletes_a_public_account(hooks, tmp_path, monkeypatch, project):
    monkeypatch.setattr(hooks, "deployer_ipv4", lambda: pytest.fail("refused before the IP"))
    foundry = make_public(FakeFoundry(project=project))
    ctx, asked = private_down(hooks, tmp_path, foundry, FA_CONFIRM_DELETE="dev")
    with pytest.raises(hooks.Stop, match="as FA_NETWORK_MODE=public.*nothing was changed"):
        hooks.predown(ctx)
    assert writes(ctx) == [] and foundry.log == [] and asked == []


def test_private_predown_without_a_terminal_runs_on_the_name_alone(hooks, tmp_path, deployer, capsys):
    foundry = FakeFoundry(agents=())
    ctx, asked = private_down(hooks, tmp_path, foundry, locks=(), tty=False, FA_CONFIRM_DELETE="dev")
    hooks.predown(ctx)
    assert asked == [] and foundry.log == ["window:open", "window:close", "host:delete", "project:delete",
                                           "account:delete", "purge"]
    assert "Hosted Agents: none left" in capsys.readouterr().out


@pytest.mark.parametrize("state, log", [
    # the project is gone (and its agents with it): no window, no IP
    ({"project": False}, ["account:delete", "purge"]),
    # the account is Deleting already: the delete is not sent again
    ({"project": False, "account": "Deleting"}, ["purge"]),
    # the account is gone and soft-deleted
    ({"project": False, "account": None, "soft_deleted": True}, ["purge"]),
    # gone and purged; only the subnet link is still there
    ({"project": False, "account": None, "links": 2}, []),
    # the project is Deleting from an earlier run: polled, never deleted again, no window
    ({"project": "Deleting"}, ["account:delete", "purge"]),
])
def test_private_predown_continues_from_each_state(hooks, tmp_path, monkeypatch, state, log):
    monkeypatch.setattr(hooks, "deployer_ipv4", lambda: pytest.fail("no project, so no window and no IP"))
    foundry = FakeFoundry(**state)
    ctx, _ = private_down(hooks, tmp_path, foundry, locks=())
    hooks.predown(ctx)
    assert foundry.log == log and foundry.patches == []
    assert foundry.account is None and not foundry.soft_deleted and foundry.links == 0


def test_a_gateway_timeout_on_the_account_delete_is_polled_never_resent(hooks, tmp_path, capsys):
    foundry = FakeFoundry(project=False, deleting_polls=4, delete_answer=GATEWAY_TIMEOUT)
    ctx, _ = private_down(hooks, tmp_path, foundry, locks=())
    hooks.predown(ctx)
    assert foundry.log.count("account:delete") == 1 and foundry.log == ["account:delete", "purge"]
    assert "reading its state instead of sending it again" in capsys.readouterr().out


def test_a_gateway_timeout_without_a_start_stops_after_about_90_s(hooks, tmp_path, capsys):
    foundry = FakeFoundry(project=False, delete_answer=GATEWAY_TIMEOUT, faults={"no-start"})
    ctx, _ = private_down(hooks, tmp_path, foundry, locks=())
    with pytest.raises(hooks.Stop, match=f"Foundry account {ACCOUNT} to start deleting after the GatewayTimeout: not "
                                         r"reached within 2 min \(last: Succeeded\); nothing more was sent.*The delete "
                                         "did not start; the rerun reads the state again"):
        hooks.predown(ctx)
    assert foundry.log == ["account:delete"] and foundry.account == "Succeeded"  # sent once, no purge


def test_predown_reads_a_transient_503_again_on_the_close_and_the_deleting_poll(hooks, tmp_path, deployer, capsys):
    # 2026-10-28: an HTML 503 on the close's read, then on the poll of the Deleting account; both are reads.
    foundry = FakeFoundry(unavailable=(1, 2), deleting_unavailable=3)
    ctx, _ = private_down(hooks, tmp_path, foundry)
    hooks.predown(ctx)
    assert foundry.log == ["window:open", "agent:fa-dev-a", "agent:fa-dev-b", "window:close", "host:delete",
                           "project:delete", "account:delete", "purge", "lock:delete"]  # each DELETE sent once
    assert foundry.patches == [OPEN_BODY, CLOSE_BODY] and foundry.account is None
    assert capsys.readouterr().out.count("answered Service Unavailable (transient); reading it again in") == 5


def test_a_poll_reports_the_minutes_that_passed_not_its_reads(hooks, tmp_path, capsys):
    ctx = context(hooks, tmp_path, {})
    clock = [datetime(2026, 10, 28, 9, 0, tzinfo=timezone.utc)]
    ctx.now = lambda: clock[0]
    ctx.sleep = lambda seconds: clock.__setitem__(0, clock[0] + timedelta(seconds=seconds))

    def check():
        ctx.sleep(50)  # each read answered a transient error three times: 5 + 15 + 30 s
        return False, "still there"

    with pytest.raises(hooks.Stop, match="x: not reached"):
        hooks.wait_until(ctx, "x", check, attempts=25, every=5)
    assert [line for line in capsys.readouterr().out.splitlines() if "waiting for x" in line] == [
        "FA: waiting for x: still there (0 min so far, read 1 of 25)",
        "FA: waiting for x: still there (11 min so far, read 13 of 25)"]  # 13 x 50 s + 12 x 5 s, not "1 of at most 2"


def test_a_poll_that_keeps_answering_503_stops_and_never_resends_the_delete(hooks, tmp_path):
    foundry = FakeFoundry(project=False, deleting_unavailable=10_000)
    ctx, _ = private_down(hooks, tmp_path, foundry)
    sleeps = []
    ctx.sleep = sleeps.append
    with pytest.raises(hooks.Stop, match=f"az rest get .*/accounts/{ACCOUNT} kept answering Service Unavailable 4 "
                                         r"times over about 50 s \(a transient Azure error\); nothing more was sent"):
        hooks.predown(ctx)
    assert foundry.log == ["account:delete"] and sleeps == [5, 15, 30]  # no resend, no purge, the lock stays


def test_a_gateway_timeout_then_a_poll_that_keeps_answering_503_names_the_read(hooks, tmp_path):
    foundry = FakeFoundry(project=False, delete_answer=GATEWAY_TIMEOUT, deleting_unavailable=10_000)
    ctx, _ = private_down(hooks, tmp_path, foundry, locks=())
    with pytest.raises(hooks.Stop) as stop:
        hooks.predown(ctx)
    assert "kept answering Service Unavailable 4 times" in str(stop.value)
    assert "The delete did not start" not in str(stop.value)  # it may have: only the read failed
    assert foundry.log == ["account:delete"]


def test_a_delete_answering_an_html_503_is_not_sent_again(hooks, tmp_path):
    foundry = FakeFoundry(project=False, delete_answer=SERVICE_UNAVAILABLE, faults={"no-start"})
    ctx, _ = private_down(hooks, tmp_path, foundry, locks=())
    with pytest.raises(hooks.Stop, match=rf"deleting Foundry account {ACCOUNT} failed \(ERROR: Service Unavailable"
                                         r"\(<!DOCTYPE.*\); it is still Succeeded"):
        hooks.predown(ctx)
    assert foundry.log == ["account:delete"]  # a write is sent once, whatever it answered


def test_predown_warns_with_the_ip_when_the_window_cannot_be_read_closed(hooks, tmp_path, deployer, capsys):
    foundry = FakeFoundry(unavailable=(1, 10_000))
    ctx, _ = private_down(hooks, tmp_path, foundry)
    with pytest.raises(hooks.Stop, match=f"Foundry account {ACCOUNT} could not be closed again"):
        hooks.predown(ctx)
    # the close body from the open's read is sent once; nothing else is deleted, the lock is kept
    assert foundry.log == ["window:open", "agent:fa-dev-a", "agent:fa-dev-b", "window:close"]
    err = capsys.readouterr().err
    assert f"WARNING: Foundry account {ACCOUNT} may still be open to {IP}" in err
    assert "kept answering Service Unavailable" in err and "az rest --method patch --url" in err


def test_a_gateway_timeout_on_the_project_delete_is_polled_never_resent(hooks, tmp_path, deployer, capsys):
    foundry = FakeFoundry(agents=(), faults={"project-timeout"})
    ctx, _ = private_down(hooks, tmp_path, foundry, locks=())
    hooks.predown(ctx)
    assert foundry.log == ["window:open", "window:close", "host:delete", "project:delete", "account:delete", "purge"]
    assert "the delete of project fa-dev answered: ERROR: (GatewayTimeout)" in capsys.readouterr().out


def test_a_gateway_timeout_on_the_capability_host_delete_is_polled_never_resent(hooks, tmp_path, deployer, capsys):
    foundry = FakeFoundry(agents=(), faults={"host-timeout"}, host_polls=3)
    ctx, _ = private_down(hooks, tmp_path, foundry, locks=())
    hooks.predown(ctx)
    assert foundry.log == ["window:open", "window:close", "host:delete", "project:delete", "account:delete", "purge"]
    out = capsys.readouterr().out
    assert ("the delete of project capability host caphostproj answered: ERROR: (GatewayTimeout)" in out
            and "project capability host caphostproj: deleted (read back)" in out)


def test_a_deleted_capability_host_answering_its_own_404_text_reads_as_gone(hooks, tmp_path, deployer, capsys):
    foundry = FakeFoundry(agents=(), faults={"host-gone-text"}, host_polls=2)
    ctx, _ = private_down(hooks, tmp_path, foundry, locks=())
    hooks.predown(ctx)
    assert foundry.log == ["window:open", "window:close", "host:delete", "project:delete", "account:delete", "purge"]
    assert "project capability host caphostproj: deleted (read back)" in capsys.readouterr().out


def test_another_not_found_in_workspace_error_on_the_host_read_stops(hooks, tmp_path, deployer):
    foundry = FakeFoundry(agents=(), faults={"host-other-text"}, host_polls=2)
    ctx, _ = private_down(hooks, tmp_path, foundry, locks=())
    with pytest.raises(hooks.Stop, match="az rest get .*/capabilityHosts/caphostproj failed: ERROR: Connection 'x' "
                                         "not found in workspace"):
        hooks.predown(ctx)
    assert foundry.log == ["window:open", "window:close", "host:delete"]  # the project is not deleted


@pytest.mark.parametrize("host, faults", [
    (None, ()),             # provisioned before the project capability host existed: an empty list
    (None, {"hosts-404"}),  # the list answers 404: none either, so `azd down` of such an environment goes on
    ("Deleting", ()),       # an earlier run's delete is still going: polled, never sent again
])
def test_a_missing_or_deleting_capability_host_is_not_deleted_again(hooks, tmp_path, deployer, capsys, host, faults):
    foundry = FakeFoundry(agents=(), host=host, host_polls=2, faults=faults)
    ctx, _ = private_down(hooks, tmp_path, foundry, locks=())
    hooks.predown(ctx)
    assert foundry.log == ["window:open", "window:close", "project:delete", "account:delete", "purge"]
    assert foundry.host is None
    out = capsys.readouterr().out
    assert ("project capability host caphostproj: deleted (read back)" in out) == bool(host)
    assert ("first deleting its project capability host" in out) == bool(host)


def test_a_capability_host_of_another_name_is_listed_and_deleted(hooks, tmp_path, deployer, capsys):
    # Not only foundry.bicep's caphostproj: whatever the project has, as the official cleanup tool lists them.
    foundry = FakeFoundry(agents=(), host_name=f"{ACCOUNT}/fa-dev/older-host")
    ctx, _ = private_down(hooks, tmp_path, foundry, locks=())
    hooks.predown(ctx)
    assert foundry.log == ["window:open", "window:close", "host:delete", "project:delete", "account:delete", "purge"]
    deleted = [call for call in writes(ctx) if "/capabilityHosts/" in " ".join(call)]
    assert len(deleted) == 1 and "/projects/fa-dev/capabilityHosts/older-host?api-version=2025-06-01" in deleted[0][5]
    assert "project capability host older-host: deleted (read back)" in capsys.readouterr().out


def test_a_failed_read_of_the_capability_hosts_stops_before_anything_is_written(hooks, tmp_path, deployer):
    foundry = FakeFoundry(agents=(), faults={"hosts-500"})
    ctx, asked = private_down(hooks, tmp_path, foundry)
    with pytest.raises(hooks.Stop, match="capabilityHosts failed: ERROR: .InternalServerError."):
        hooks.predown(ctx)
    assert asked == [] and writes(ctx) == [] and foundry.log == []


def test_a_capability_host_that_never_goes_stops_with_the_project_and_backups_kept(hooks, tmp_path, deployer):
    foundry = FakeFoundry(agents=(), host="Deleting", host_polls=10_000)
    ctx, _ = private_down(hooks, tmp_path, foundry)
    with pytest.raises(hooks.Stop, match="project capability host caphostproj to be deleted: not reached within "
                                         "30 min.*nothing more was sent"):
        hooks.predown(ctx)
    assert foundry.log == ["window:open", "window:close"] and foundry.project is True  # no delete sent, lock stays
    assert hooks.window_closed({"properties": foundry.properties})


def test_an_agent_delete_answering_404_counts_as_deleted(hooks, tmp_path, deployer, capsys):
    foundry = FakeFoundry(agents=("fa-dev-a",), faults={"agents-404"})
    ctx, _ = private_down(hooks, tmp_path, foundry, locks=())
    hooks.predown(ctx)
    assert foundry.log[:5] == ["window:open", "agent:fa-dev-a", "window:close", "host:delete", "project:delete"]
    assert "Hosted Agent fa-dev-a: already deleted (404)" in capsys.readouterr().out


@pytest.mark.parametrize("state, message, log", [
    ({"faults": {"sticky-agents"}, "agents": ("fa-dev-a",)}, "still listed after three rounds",
     ["window:open"] + ["agent:fa-dev-a"] * 3 + ["window:close"]),
    ({"project": False, "account": None, "soft_deleted": True, "faults": {"purge-fails"}},
     f"soft-deleted Foundry account {ACCOUNT} to be purged: not reached within 10 min", ["purge"]),
    ({"project": False, "account": None, "links": 10_000},
     "snet-agents to drop its service association link: not reached within 20 min", []),
])
def test_each_teardown_step_that_never_finishes_stops_with_the_backups_locked(hooks, tmp_path, deployer, state,
                                                                              message, log):
    foundry = FakeFoundry(**state)
    ctx, _ = private_down(hooks, tmp_path, foundry)
    with pytest.raises(hooks.Stop, match=message):
        hooks.predown(ctx)
    assert foundry.log == log and "lock:delete" not in foundry.log
    assert hooks.window_closed({"properties": foundry.properties})


@pytest.mark.parametrize("ids", [
    ("projects/fa-dev/connections/webiq-key",),       # deeper than the project
    ("capabilityHosts/agents",),                      # another kind of nested resource
    ("projects/fa-dev2",),                            # another project, the name a prefix of it
    ("projects/fa-dev", "projects/fa-dev/connections/webiq-key"),  # the project and more
    (),                                               # no nested ID listed at all
])
def test_an_account_delete_refused_for_more_than_its_own_project_stops_at_once(hooks, tmp_path, ids):
    foundry = FakeFoundry(project=False, refuse_delete=nested(*ids))
    ctx, _ = private_down(hooks, tmp_path, foundry)
    sleeps = []
    ctx.sleep = sleeps.append
    with pytest.raises(hooks.Stop, match="CannotDeleteResource.*it is still Succeeded. Read it back"):
        hooks.predown(ctx)
    assert foundry.log == ["account:delete"] and sleeps == []  # no wait, no purge, and the backup lock stays


def test_a_refused_project_delete_is_never_resent(hooks, tmp_path, deployer):
    foundry = FakeFoundry(agents=(), faults={"project-refused"})
    ctx, _ = private_down(hooks, tmp_path, foundry)
    with pytest.raises(hooks.Stop, match="deleting project fa-dev failed.*CannotDeleteResource"):
        hooks.predown(ctx)
    assert foundry.log == ["window:open", "window:close", "host:delete", "project:delete"]


def test_a_deleted_project_still_blocking_the_account_delete_is_resent_until_it_starts(hooks, tmp_path, deployer,
                                                                                      capsys):
    foundry = FakeFoundry(agents=(), stale_project=2)
    ctx, _ = private_down(hooks, tmp_path, foundry, locks=())
    hooks.predown(ctx)
    assert foundry.log == (["window:open", "window:close", "host:delete", "project:delete"] + ["account:delete"] * 3
                           + ["purge"])
    assert "still listed (CannotDeleteResource); sending it again every 15 s for at most 5 min" in capsys.readouterr().out


def test_a_deleted_project_blocking_past_the_bound_stops_with_the_account_still_there(hooks, tmp_path):
    foundry = FakeFoundry(project=False, stale_project=10_000)
    ctx, _ = private_down(hooks, tmp_path, foundry)
    sleeps = []
    ctx.sleep = sleeps.append
    with pytest.raises(hooks.Stop, match="CannotDeleteResource.*it is still Succeeded. Read it back"):
        hooks.predown(ctx)
    assert foundry.log == ["account:delete"] * 21  # the first and 20 resends; no purge, the lock stays
    assert sleeps == [15] * 20


def test_a_gateway_timeout_after_a_resent_delete_is_polled_never_resent(hooks, tmp_path):
    foundry = FakeFoundry(project=False, stale_project=1, deleting_polls=4, delete_answer=GATEWAY_TIMEOUT)
    ctx, _ = private_down(hooks, tmp_path, foundry, locks=())
    hooks.predown(ctx)
    assert foundry.log == ["account:delete", "account:delete", "purge"]


def test_an_account_that_never_goes_stops_after_the_bounded_wait(hooks, tmp_path):
    foundry = FakeFoundry(project=False, account="Deleting", deleting_polls=10_000)
    ctx, _ = private_down(hooks, tmp_path, foundry, locks=())
    with pytest.raises(hooks.Stop, match="to be deleted: not reached within 45 min.*nothing more was sent"):
        hooks.predown(ctx)
    assert writes(ctx) == []


@pytest.mark.parametrize("answer, outcome", [
    (NOT_FOUND, None),
    (failed("ERROR: Not Found({\"error\":{\"code\":\"DeletedAccountNotFound\"}})"), None),  # any 404 of the resource
    (HOST_MISSING, None),  # a project capability host's 404, whose text has no "Not Found"
    (workspace_error("CapabilityHost 'caphostproj'", inner="OtherError"), "az rest get .* failed: ERROR: CapabilityHost"),
    (CONNECTION_MISSING, "az rest get .* failed: ERROR: Connection 'x' not found in workspace"),
    (failed("ERROR: Not Found({\"error\":{\"code\":\"SubscriptionNotFound\"}})"), "subscription .* was not found"),
    (failed("ERROR: Not Found({\"error\":{\"code\":\"ResourceGroupNotFound\"}})"), "resource group rg-dev was not"),
    (failed("ERROR: (AuthorizationFailed) no read"), "az rest get .* failed: ERROR: \\(AuthorizationFailed\\)"),
    # A transient answer is never read as gone: read four times, then a stop that names it.
    (SERVICE_UNAVAILABLE, "az rest get .*/providers/y kept answering Service Unavailable"),
    (failed("ERROR: Internal Server Error(<html><body>error</body></html>)"), "kept answering Internal Server Error"),
    (failed("ERROR: Not Found(<html><body>gone</body></html>)"), None),  # a 404 is read as before, page or not
])
def test_only_a_404_of_the_resource_itself_reads_as_gone(hooks, tmp_path, answer, outcome):
    ctx = context(hooks, tmp_path, {"rest --method get": answer})
    if outcome is None:
        assert hooks._arm_get(ctx, "/subscriptions/x/resourceGroups/rg-dev/providers/y?api-version=1") is None
    else:
        with pytest.raises(hooks.Stop, match=outcome):
            hooks._arm_get(ctx, "/subscriptions/x/resourceGroups/rg-dev/providers/y?api-version=1")


@pytest.mark.parametrize("copy, line", [
    (NOT_FOUND, None),                                                      # purged already: nothing to say
    (ok({"name": ACCOUNT}), "is still soft-deleted; to purge it yourself"),
    (failed("ERROR: (AuthorizationFailed) no read"), "may still be soft-deleted (the read failed"),
])
def test_private_predown_of_a_deleted_group_says_so_and_removes_nothing(hooks, tmp_path, monkeypatch, capsys, copy,
                                                                        line):
    monkeypatch.setattr(hooks, "deployer_ipv4", lambda: pytest.fail("nothing to delete, so no IP"))
    gone = failed("ERROR: Not Found({\"error\":{\"code\":\"ResourceGroupNotFound\"}})")
    ctx = context(hooks, tmp_path, {"deletedAccounts/": copy, "Microsoft.CognitiveServices/": gone, **outside(hooks),
                                    "lock list": ok([])}, **PRIVATE)
    ctx.prompt = lambda text: pytest.fail("nothing to confirm")
    hooks.predown(ctx)  # never stops: postdown still has the Entra apps to delete
    assert writes(ctx) == []
    out = capsys.readouterr().out
    assert f"resource group rg-dev does not exist, so Foundry account {ACCOUNT}" in out
    if line is None:
        assert "soft-deleted" not in out
    else:
        assert (f"Foundry account {ACCOUNT} {line}" in out and f"az rest --method delete --url \"/subscriptions/"
                f"{SUBSCRIPTION}/providers/Microsoft.CognitiveServices/locations/westus3/resourceGroups/rg-dev/"
                f"deletedAccounts/{ACCOUNT}?api-version=2025-06-01\"" in out)


def test_private_predown_of_a_deleted_group_without_a_region_still_goes_on(hooks, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(hooks, "deployer_ipv4", lambda: pytest.fail("nothing to delete, so no IP"))
    gone = failed("ERROR: Not Found({\"error\":{\"code\":\"ResourceGroupNotFound\"}})")
    ctx = context(hooks, tmp_path, {"Microsoft.CognitiveServices/": gone, **outside(hooks), "lock list": ok([])},
                  **PRIVATE, FA_FOUNDRY_ACCOUNT=ACCOUNT, AZURE_LOCATION="")
    ctx.prompt = lambda text: pytest.fail("nothing to confirm")
    hooks.predown(ctx)  # the soft-deleted copy's URL needs the region; its read is informational, so no stop
    assert writes(ctx) == []
    out = capsys.readouterr().out
    assert (f"Foundry account {ACCOUNT} may still be soft-deleted (the read failed: azd environment value "
            "AZURE_LOCATION is missing" in out and "to purge it yourself" not in out)


def test_public_predown_never_touches_foundry(hooks, tmp_path, monkeypatch):
    # Public behaves as before: no lock means no question and no write; FA's routes answer no Foundry call.
    monkeypatch.setattr(hooks, "deployer_ipv4", lambda: pytest.fail("public mode reads no IP"))
    for values in ({}, {"FA_NETWORK_MODE": "public"}):
        ctx = context(hooks, tmp_path, {**outside(hooks), "lock list": ok([])}, **values)
        ctx.prompt = lambda text: pytest.fail("no question without the lock")
        hooks.predown(ctx)
        assert not any("Microsoft.CognitiveServices" in " ".join(call) for call in ctx.run.calls)
        ctx = context(hooks, tmp_path, {**outside(hooks), "lock list": ok([LOCK]), "lock delete": ok()}, **values)
        asked = []
        ctx.prompt = lambda text: asked.append(text) or "dev"
        hooks.predown(ctx)
        assert asked == ["Deleting rg-dev deletes its Table data and M1 backups for good. Type the environment name "
                         "(dev) to remove the backup lock: "]
        assert not any("Microsoft.CognitiveServices" in " ".join(call) for call in ctx.run.calls)


# ---- ADR-0010 phase 2b: an existing VNet --------------------------------------------------------------------------

USER_VNET = (f"/subscriptions/{SUBSCRIPTION}/resourceGroups/rg-net/providers/Microsoft.Network/virtualNetworks/"
             "vnet-hub")
BYO = {**PRIVATE, "FA_VNET_RESOURCE_ID": USER_VNET}
NO_RESOURCE = failed("Not Found({\"error\":{\"code\":\"ResourceNotFound\"}})")
DELEGATED = [{"name": "aca", "properties": {"serviceName": "Microsoft.App/environments"}}]


def app_subnet(prefix, links=0, delegations=DELEGATED, egress=True):
    return {"properties": {"addressPrefix": prefix, "delegations": delegations,
                           "serviceAssociationLinks": [{"name": "legionservicelink"}] * links,
                           **({"natGateway": {"id": "nat"}} if egress else {})}}


def user_vnet(location="westus3", dns=(), space=("10.20.0.0/16",), **subnets):
    """ARM GETs of the user's VNet and its subnets (None: the subnet does not exist); anything but a GET fails."""
    found = {"snet-api": app_subnet("10.20.0.0/27"), "snet-agents": app_subnet("10.20.1.0/24"),
             "snet-private-endpoints": {"properties": {"addressPrefix": "10.20.2.0/27"}}, **subnets}

    def read(args):
        assert args[args.index("--method") + 1] == "get", args
        path = args[args.index("--url") + 1].split("?")[0]
        if path == USER_VNET:
            return ok({"location": location, "properties": {"dhcpOptions": {"dnsServers": list(dns)},
                                                             "addressSpace": {"addressPrefixes": list(space)}}})
        subnet = found.get(path.rsplit("/", 1)[-1])
        return NO_RESOURCE if subnet is None else ok(subnet)
    return read


def byo_context(hooks, tmp_path, vnet=None, foundry=NO_ACCOUNT, cae=NO_RESOURCE, **values):
    routes = {"Microsoft.Network/virtualNetworks/": vnet or user_vnet(), "Microsoft.App/managedEnvironments/": cae,
              **preprovision_routes(ok({"ok": True}), foundry=foundry)}
    return context(hooks, tmp_path, routes, **{**BYO, **values})


def injected_into(subnet):
    return ok({"name": ACCOUNT, "properties": {"networkInjections": [
        {"scenario": "agent", "subnetArmId": subnet, "useMicrosoftManagedNetwork": False}]}})


def test_an_existing_vnet_that_fits_passes_with_reads_only(hooks, tmp_path, capsys):
    ctx = byo_context(hooks, tmp_path)
    hooks.preprovision(ctx)
    network = [call for call in ctx.run.calls if "Microsoft.Network/" in " ".join(call)]
    assert len(network) == 4 and all(call[:4] == ["az", "rest", "--method", "get"] for call in network)
    assert [call[5].split("?")[0] for call in network] == [USER_VNET] + [
        f"{USER_VNET}/subnets/{name}" for name in ("snet-api", "snet-agents", "snet-private-endpoints")]
    out = capsys.readouterr().out
    assert ("FA: existing VNet vnet-hub in rg-net (read only): snet-api and snet-agents delegated to "
            "Microsoft.App/environments, private endpoints in snet-private-endpoints; DNS azd") in out
    assert "warning" not in out
    assert any("preflight_subscription.py" in " ".join(call) for call in ctx.run.calls)


def test_an_existing_vnet_takes_the_users_subnet_names(hooks, tmp_path):
    vnet = user_vnet(app=app_subnet("10.20.0.0/26"), agents=app_subnet("10.20.4.0/23"),
                     pe={"properties": {"addressPrefix": "10.20.2.0/28"}})
    ctx = byo_context(hooks, tmp_path, vnet, FA_API_SUBNET_NAME="app", FA_AGENT_SUBNET_NAME="agents",
                      FA_PRIVATE_ENDPOINT_SUBNET_NAME="pe")
    hooks.preprovision(ctx)


def test_a_rerun_accepts_the_links_of_this_environments_own_account_and_environment(hooks, tmp_path):
    vnet = user_vnet(**{"snet-api": app_subnet("10.20.0.0/27", links=1),
                        "snet-agents": app_subnet("10.20.1.0/24", links=1)})
    cae = ok({"properties": {"vnetConfiguration": {"infrastructureSubnetId": f"{USER_VNET}/subnets/snet-api"}}})
    ctx = byo_context(hooks, tmp_path, vnet, foundry=injected_into(f"{USER_VNET.upper()}/subnets/snet-agents"),
                      cae=cae)
    hooks.preprovision(ctx)
    (read,) = [call for call in ctx.run.calls if "managedEnvironments/" in " ".join(call)]
    assert read[5].startswith(f"/subscriptions/{SUBSCRIPTION}/resourceGroups/rg-dev/providers/Microsoft.App/"
                              "managedEnvironments/cae-dev?")


def test_existing_dns_takes_a_vnet_with_its_own_dns_servers(hooks, tmp_path):
    ctx = byo_context(hooks, tmp_path, user_vnet(dns=["10.0.0.4"]), FA_DNS_MODE="existing")
    hooks.preprovision(ctx)


def test_missing_egress_and_a_small_agent_subnet_only_warn(hooks, tmp_path, capsys):
    # FA adds no egress; a Virtual WAN hub can route a subnet without a route table, so this cannot be judged here.
    vnet = user_vnet(**{"snet-agents": app_subnet("10.20.1.0/26", egress=False)})
    hooks.preprovision(byo_context(hooks, tmp_path, vnet))
    out = capsys.readouterr().out
    assert "warning: subnet snet-agents has no NAT gateway or route table. FA adds no egress" in out
    assert "warning: subnet snet-agents is 10.20.1.0/26; Foundry recommends /24" in out


@pytest.mark.parametrize("vnet, values, foundry, cae, message", [
    (user_vnet(location="eastus"), {}, NO_ACCOUNT, NO_RESOURCE, "vnet-hub is in eastus.*use a VNet in westus3"),
    (user_vnet(**{"snet-agents": app_subnet("10.20.1.0/24", delegations=[])}), {}, NO_ACCOUNT, NO_RESOURCE,
     "subnet snet-agents must be delegated to Microsoft.App/environments"),
    (user_vnet(**{"snet-agents": app_subnet("10.20.1.0/24", links=1)}), {}, NO_ACCOUNT, NO_RESOURCE,
     "snet-agents already has a service association link, so another Foundry account uses it"),
    (user_vnet(**{"snet-private-endpoints": None}), {}, NO_ACCOUNT, NO_RESOURCE,
     "vnet-hub has no subnet snet-private-endpoints"),
    (None, {"FA_NETWORK_MODE": "public"}, NO_ACCOUNT, NO_RESOURCE, "needs FA_NETWORK_MODE=private"),
    (None, {"FA_VNET_RESOURCE_ID": USER_VNET.replace("rg-net", "rg-dev")}, NO_ACCOUNT, NO_RESOURCE,
     "is in rg-dev, the resource group `azd down` deletes"),
    (None, {"FA_VNET_RESOURCE_ID": USER_VNET.replace(SUBSCRIPTION, OTHER[:36])}, NO_ACCOUNT, NO_RESOURCE,
     "not this environment's"),
    (None, {"FA_VNET_RESOURCE_ID": USER_VNET + "/"}, NO_ACCOUNT, NO_RESOURCE, "must be a VNet resource ID"),
    (None, {"FA_API_SUBNET_NAT_EGRESS": "true"}, NO_ACCOUNT, NO_RESOURCE, "FA_API_SUBNET_NAT_EGRESS creates FA's own"),
    (None, {"FA_AGENT_SUBNET_NAME": "snet-api"}, NO_ACCOUNT, NO_RESOURCE, "three different subnets"),
    (None, {"FA_AGENT_SUBNET_NAME": "a" * 64}, NO_ACCOUNT, NO_RESOURCE, "at most 63 bytes"),
    (None, {"FA_DNS_MODE": "hub"}, NO_ACCOUNT, NO_RESOURCE, "FA_DNS_MODE must be azd or existing"),
    (lambda args: NO_RESOURCE, {}, NO_ACCOUNT, NO_RESOURCE, "was not found; check FA_VNET_RESOURCE_ID"),
    (user_vnet(dns=["10.0.0.4"]), {}, NO_ACCOUNT, NO_RESOURCE, "uses its own DNS servers.*FA_DNS_MODE existing"),
    (user_vnet(**{"snet-agents": app_subnet("10.20.1.0/28")}), {}, NO_ACCOUNT, NO_RESOURCE, "/27 or larger"),
    (user_vnet(**{"snet-agents": app_subnet("100.64.1.0/24")}), {}, NO_ACCOUNT, NO_RESOURCE, "RFC 1918"),
    (user_vnet(**{"snet-api": app_subnet("172.30.0.0/27")}), {}, NO_ACCOUNT, NO_RESOURCE,
     "subnet snet-api .172.30.0.0/27. overlaps a range Container Apps and Foundry reserve"),
    (user_vnet(**{"snet-api": app_subnet("100.100.130.0/27")}), {}, NO_ACCOUNT, NO_RESOURCE,
     "subnet snet-api .100.100.130.0/27. overlaps a range Container Apps and Foundry reserve"),
    (user_vnet(space=["10.20.0.0/16", "100.100.224.0/20"]), {}, NO_ACCOUNT, NO_RESOURCE,
     "VNet vnet-hub address space 100.100.224.0/20 overlaps a range Container Apps and Foundry reserve"),
    (user_vnet(**{"snet-api": app_subnet("10.20.0.0/27", links=1)}), {}, NO_ACCOUNT, NO_RESOURCE,
     "another Container Apps environment does"),
    (user_vnet(**{"snet-private-endpoints": app_subnet("10.20.2.0/27")}), {}, NO_ACCOUNT, NO_RESOURCE,
     "private endpoints cannot use a delegated subnet"),
    (user_vnet(**{"snet-private-endpoints": {"properties": {"addressPrefix": "10.20.2.0/29"}}}), {}, NO_ACCOUNT,
     NO_RESOURCE, "/28 or larger"),
    (None, {}, injected_into("/subscriptions/x/resourceGroups/rg-old/providers/Microsoft.Network/virtualNetworks/v/"
                             "subnets/snet-agents"), NO_RESOURCE, "injection cannot change"),
])
def test_an_existing_vnet_that_does_not_fit_stops_before_any_write(hooks, tmp_path, vnet, values, foundry, cae,
                                                                    message):
    ctx = byo_context(hooks, tmp_path, vnet, foundry=foundry, cae=cae, **values)
    with pytest.raises(hooks.Stop, match=message):
        hooks.preprovision(ctx)
    assert writes_nothing(ctx) and "FA_TEAM_OWNER_OID" not in ctx.values
    assert all(call[:4] == ["az", "rest", "--method", "get"] for call in ctx.run.calls
               if "Microsoft.Network/" in " ".join(call))


@pytest.mark.parametrize("values", [{"FA_DNS_MODE": "existing"}, {"FA_AGENT_SUBNET_NAME": "agents"},
                                    {"FA_NETWORK_MODE": "public", "FA_API_SUBNET_NAME": "app"}])
def test_existing_vnet_inputs_without_the_vnet_stop(hooks, tmp_path, values):
    ctx = context(hooks, tmp_path, preprovision_routes(ok({"ok": True})), **{**PRIVATE, **values})
    with pytest.raises(hooks.Stop, match="only apply with an existing VNet .FA_VNET_RESOURCE_ID."):
        hooks.preprovision(ctx)
    assert writes_nothing(ctx)


def test_clearing_the_vnet_of_an_account_injected_into_it_stops_before_any_write(hooks, tmp_path):
    # Injection cannot change, so dropping FA_VNET_RESOURCE_ID would fail mid-deploy on the account PUT.
    ctx = context(hooks, tmp_path, preprovision_routes(ok({"ok": True}),
                                                       foundry=injected_into(f"{USER_VNET}/subnets/snet-agents")),
                  **PRIVATE)
    with pytest.raises(hooks.Stop, match=f"{ACCOUNT} was created injected into {USER_VNET}/subnets/snet-agents, "
                                         "not FA's own vnet-dev, and injection cannot change"):
        hooks.preprovision(ctx)
    assert writes_nothing(ctx) and "FA_TEAM_OWNER_OID" not in ctx.values


OWN_AGENT_SUBNET = (f"/subscriptions/{SUBSCRIPTION}/resourceGroups/rg-dev/providers/Microsoft.Network/virtualNetworks/"
                    "vnet-dev/subnets/snet-agents")


def provisioned_private(hooks, tmp_path, hosts):
    """preprovision of a private environment whose injected account exists; `hosts` answers the project's list."""
    routes = {"/projects/fa-dev/capabilityHosts?": hosts,
              **preprovision_routes(ok({"ok": True}), foundry=injected_into(OWN_AGENT_SUBNET))}
    return context(hooks, tmp_path, routes, FA_TEAM_OWNER_OID=OWNER, **PRIVATE)


def test_a_failed_project_capability_host_stops_provision_with_the_delete_command(hooks, tmp_path):
    # Learn: a capability host cannot be updated, so the Bicep PUT cannot repair it; the delete stays the operator's.
    hosts = ok({"value": [{"name": f"{ACCOUNT}/fa-dev/caphostproj", "properties": {"provisioningState": "Failed"}}]})
    ctx = provisioned_private(hooks, tmp_path, hosts)
    url = (f"/subscriptions/{SUBSCRIPTION}/resourceGroups/rg-dev/providers/Microsoft.CognitiveServices/accounts/"
           f"{ACCOUNT}/projects/fa-dev/capabilityHosts/caphostproj?api-version=2025-06-01")
    with pytest.raises(hooks.Stop) as stop:
        hooks.preprovision(ctx)
    assert f"project capability host caphostproj of Foundry account {ACCOUNT} is Failed" in str(stop.value)
    assert f'`az rest --method delete --url "{url}"`' in str(stop.value)
    assert str(stop.value).endswith("then rerun `azd provision`; nothing was changed")
    assert writes_nothing(ctx) and "FA_MODELS" not in ctx.values
    assert not [call for call in ctx.run.calls if "--method" in call and call[call.index("--method") + 1] != "get"]


@pytest.mark.parametrize("hosts", [
    ok({"value": [{"name": "caphostproj", "properties": {"provisioningState": "Succeeded"}}]}),
    ok({"value": []}),
    NOT_FOUND,  # no project yet (a provision that stopped after the account): none
    failed("ERROR: (InternalServerError) Something went wrong"),  # only a warning: provision goes on as before
])
def test_provision_goes_on_unless_a_project_capability_host_is_failed(hooks, tmp_path, hosts):
    ctx = provisioned_private(hooks, tmp_path, hosts)
    hooks.preprovision(ctx)
    assert any("preflight_subscription.py" in " ".join(call) for call in ctx.run.calls)
    assert [call for call in ctx.run.calls if "/capabilityHosts?" in " ".join(call)]


ENDPOINTS = ok([{"name": "pe-table", "customDnsConfigs": [
    {"fqdn": "stx.table.core.windows.net", "ipAddresses": ["10.20.2.4"]}]}, {"name": "pe-cog", "customDnsConfigs": [
        {"fqdn": "cog-x.cognitiveservices.azure.com", "ipAddresses": ["10.20.2.5"]},
        {"fqdn": "cog-x.services.ai.azure.com", "ipAddresses": ["10.20.2.5"]}]}])


def test_existing_dns_prints_the_records_and_holds_the_deploy_until_they_are_served(hooks, tmp_path, capsys):
    ctx = context(hooks, tmp_path, {"private-endpoint list": ENDPOINTS}, **BYO, FA_DNS_MODE="existing")
    with pytest.raises(hooks.Stop, match="azd env set FA_DNS_RECORDS_READY true"):
        hooks.require_dns_records(ctx)
    out = capsys.readouterr().out
    assert ("FA:   stx.table.core.windows.net -> 10.20.2.4\nFA:   cog-x.cognitiveservices.azure.com -> 10.20.2.5\n"
            "FA:   cog-x.services.ai.azure.com -> 10.20.2.5") in out
    assert ctx.run.calls == [["az", "network", "private-endpoint", "list", "--resource-group", "rg-dev",
                              "--subscription", SUBSCRIPTION, "-o", "json"]]
    ctx.values["FA_DNS_RECORDS_READY"] = "true"
    hooks.require_dns_records(ctx)  # served: the deploy goes on
    assert "FA_DNS_RECORDS_READY" not in hooks.USER_INPUTS  # postdown clears it; a new install has new IPs
    assert "FA_APPROVE_INITIALIZATION" not in hooks.USER_INPUTS  # postdown clears it; a rebuilt env opts in again


@pytest.mark.parametrize("values", [{}, PRIVATE, BYO])
def test_fa_managed_dns_reads_no_private_endpoint(hooks, tmp_path, values):
    hooks.require_dns_records(context(hooks, tmp_path, {}, **values))  # no route: any call would fail


def test_the_portal_endpoint_goes_into_the_users_subnet(hooks, tmp_path, monkeypatch):
    deployed = []
    monkeypatch.setattr(hooks, "console_bundle", lambda *args: {})
    monkeypatch.setattr(hooks, "deploy_bicep", lambda ctx, name, template, parameters: deployed.append(
        (template, parameters)) or {"publicOrigin": "https://ca-console.example.io", "identityPrincipalId": "p"})
    monkeypatch.setattr(sys.modules["entra"], "ensure_fic", lambda *args: "created")
    keys = ("FA_CONTAINER_APPS_ENVIRONMENT", "FA_REGISTRY_NAME", "FA_VNET", "FA_CONSOLE_APP", "FA_CONSOLE_IDENTITY",
            "FA_CONSOLE_STORAGE", "FA_CONSOLE_APP_ID", "FA_TEAM_OWNER_OID", "FA_API_AUDIENCE",
            "FA_CONSOLE_APP_OBJECT_ID", "FA_DEPLOY_RUN")
    ctx = context(hooks, tmp_path, {"azd env set": ok()}, FA_CONSOLE_URL="https://ca-console.example.io",
                  **{key: "x" for key in keys}, **BYO, FA_DNS_MODE="existing", FA_PRIVATE_ENDPOINT_SUBNET_NAME="pe")
    hooks.console_app(ctx, "cr/fa-web-console@sha256:" + "a" * 64, tmp_path, tmp_path / "plan.json", "f" * 40)
    declared = set(re.findall(r"^param (\w+)", deployed[0][0].read_text(encoding="utf-8"), re.MULTILINE))
    for _, parameters in deployed:
        assert parameters["privateEndpointSubnetResourceId"] == f"{USER_VNET}/subnets/pe"
        assert parameters["privateDnsZones"] is False and set(parameters) <= declared


def test_private_predown_waits_on_the_users_agent_subnet_and_never_writes_it(hooks, tmp_path, deployer, capsys):
    foundry = FakeFoundry(links=2)
    ctx, _ = private_down(hooks, tmp_path, foundry, FA_VNET_RESOURCE_ID=USER_VNET)
    hooks.predown(ctx)
    reads = [call[5] for call in ctx.run.calls if "subnets/" in " ".join(call)]
    assert reads and all(url.startswith(f"{USER_VNET}/subnets/snet-agents?") for url in reads)
    assert not [call for call in writes(ctx) if "rg-net" in " ".join(call) or "Microsoft.Network" in " ".join(call)]
    assert foundry.links == 0 and "snet-agents has no service association link" in capsys.readouterr().out
