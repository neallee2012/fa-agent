"""azd hooks (deploy/azd/fa_azd.py, entra.py) and the WebIQ connection script with fakes only: no az, Graph or ARM call.

What must hold without a cloud run: Graph writes are idempotent by alternate key and never touch an existing service
principal; grants are the owner's only; the hooks stop on a failed read-only gate and never approve the empty-Table
initialization or ask for a key without the owner at a terminal; the WebIQ key goes only into one PUT body.
"""

import dataclasses
from datetime import datetime, timedelta, timezone
import importlib.util
import json
import os
from pathlib import Path
import re
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
                         python="python", tty=True,
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


def preprovision_routes(preflight, me=OWNER, tenant=TENANT, roles=("Owner",), directory=(), create_apps=True,
                        cli_sp=True, offer=None):
    """Order matters: the first matching key answers (the /me/... reads before /me)."""
    return {**(offer or region()), "account show": ok({"id": SUBSCRIPTION, "tenantId": tenant}),
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


def approval_context(hooks, tmp_path, me=OWNER, answer="y", tty=True):
    ctx = context(hooks, tmp_path, {"graph.microsoft.com/v1.0/me": ok({"id": me})}, FA_TEAM_OWNER_OID=OWNER)
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


def deploy_case(hooks, tmp_path, monkeypatch, api_exists, keep_select=False, **values):
    steps = []
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
    ctx = context(hooks, tmp_path, {"containerapp show": show, "azd env set": ok()},
                  FA_PROJECT_ENDPOINT="https://p", FA_API_URL="https://a", FA_API_AUDIENCE="api://x",
                  FA_CONSOLE_APP_ID="c", FA_API_APP="ca-api-dev", **values)
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
                  FA_TEAM_OWNER_OID=OWNER, FA_ENABLE_WEBIQ="false")
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
    assert not cleared & {"FA_TEAM_OWNER_OID", "FA_ENABLE_WEBIQ"}
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
    ctx = context(hooks, tmp_path, preprovision_routes(ok({"ok": True}), offer={}), FA_FOUNDRY_ACCOUNT="cog-x",
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
