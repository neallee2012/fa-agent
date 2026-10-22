"""ADR-0009 phase 3b: the pre-deploy compatibility gate and the version/image retention rule.

The gate reads every binding row and the version definitions they name, and nothing else: it never calls a session.
It refuses a deploy only when armed (the ADR connects it after the v1->v2 migration); an approved override bound to
one release passes an armed refusal and is logged. Referenced versions and images never reach a cleanup list.
Offline: the Table and Foundry are recording doubles; ci_deploy is loaded from its file and never reaches Azure.
"""

import copy
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import re
from types import SimpleNamespace

import pytest

from conftest import InMemoryRepository
from shared_context import behavior_agent_versions, behavior_team_registry, native_deploy_gate as gate
from shared_context.errors import Problem
from shared_context.native_binding import (
    NativeBindingObservation, NativeBindingRow, NativeBindingSession, NativeBindingStore, binding_store,
)
from shared_context.native_version_veto import VETO_PARTITION, NativeVersionVetoStore

ROOT = Path(__file__).resolve().parents[2]
V1, V2 = "foundry-native-v1", "foundry-native-v2"
A = SimpleNamespace(agent_id="agent-a", agent_name="fa-stage-a", project_endpoint="https://project.example/a")
B = SimpleNamespace(agent_id="agent-b", agent_name="fa-stage-b", project_endpoint="https://project.example/a")
IMAGE = "cr.example.azurecr.io/fa-behavior-assistant-desktop@sha256:"


class RecordingRepository(InMemoryRepository):
    """Records every method the gate calls on the Table."""

    def __init__(self):
        super().__init__()
        self.calls = []

    def __getattribute__(self, name):
        if not name.startswith("_") and name not in {"calls", "data", "lock", "sequence"}:
            object.__getattribute__(self, "calls").append(name)
        return object.__getattribute__(self, name)


class RecordingFoundry:
    """A Foundry client double: only ``get_version`` answers; any other attribute (sessions, invocations, files,
    stop, list) is recorded and fails, so a session call cannot pass unnoticed."""

    def __init__(self, definitions):
        self.definitions, self.calls = definitions, []

    def get_version(self, name, version):
        self.calls.append(("get_version", name, version))
        definition = self.definitions.get((name, version))
        if definition is None:
            raise Problem(404, "NOT_FOUND", "no such version")
        return {"name": name, "version": version, "status": "active", "definition": definition}

    def __getattr__(self, name):
        self.calls.append((name,))
        raise AssertionError(f"the gate called {name}; it may only read version definitions")


def definition(profile, digest):
    return {"kind": "hosted", "environment_variables": {"FA_NATIVE_TURNS": profile},
            "container_configuration": {"image": IMAGE + digest * 64}}


def row(agent_id, sid, created, observed=None, *, profile=V1, previous=()):
    return NativeBindingRow(
        agent_id=agent_id, hosted_session_id=sid, created_agent_version=created,
        drift=observed not in (None, created), notice_to_version=observed if observed not in (None, created) else None,
        observed=NativeBindingObservation(agent_version=observed or created, observed_at="2026-10-14T01:00:00+00:00",
                                          source="vm"),
        state_profile=profile, previous_sessions=tuple(previous))


def store_with(*rows):
    repository = RecordingRepository()
    store = NativeBindingStore(repository, tenant_id="tenant-1", team_id="team-1")
    for item in rows:
        repository.create(store.partition_key, store.row_key(item.agent_id), item.model_dump(mode="json"))
    repository.calls.clear()
    return store, repository


def read_with(foundry):
    return lambda target, version: foundry.get_version(target.agent_name, version)["definition"]


# ----- the gate's verdict -----------------------------------------------------------------------------------------


def test_armed_gate_refuses_when_the_support_set_misses_a_recorded_row_profile():
    store, _ = store_with(row("agent-a", "sid-a", "141"))
    foundry = RecordingFoundry({("fa-stage-a", "141"): definition(V1, "a")})
    # The 2d-ii API supports only v2; the row still records v1.
    report = gate.evaluate([A], store.rows, read_with(foundry), armed=True, supported=frozenset({V2}))
    assert report["decision"] == "refuse"
    assert report["refused"] == [{"agent_id": "agent-a", "reason": "row_profile_unsupported"}]
    # Control: the same rows pass when the new API still supports the recorded profile.
    assert gate.evaluate([A], store.rows, read_with(foundry), armed=True,
                         supported=frozenset({V1}))["decision"] == "pass"


def test_armed_gate_refuses_a_session_whose_recorded_version_declares_an_unsupported_profile():
    # M1: the API stops speaking the old wire only when no session runs an old image; the row may lag, the
    # definition of the recorded actual version decides too.
    store, _ = store_with(row("agent-a", "sid-a", "150", profile=V2), row("agent-b", "sid-b", "150", "141", profile=V2))
    foundry = RecordingFoundry({("fa-stage-a", "150"): definition(V2, "c"), ("fa-stage-b", "150"): definition(V2, "c"),
                                ("fa-stage-b", "141"): definition(V1, "a")})
    report = gate.evaluate([A, B], store.rows, read_with(foundry), armed=True, supported=frozenset({V2}))
    assert report["decision"] == "refuse"
    assert report["refused"] == [{"agent_id": "agent-b", "reason": "definition_profile_unsupported"}]


def test_the_2dii_api_reports_v1_rows_incompatible_and_v2_rows_compatible():
    # ADR-0009 M1 2d-ii: the new API's own supported set (no override) is {v2}; unarmed it only reports.
    store, _ = store_with(row("agent-a", "sid-a", "141"), row("agent-b", "sid-b", "150", profile=V2))
    foundry = RecordingFoundry({("fa-stage-a", "141"): definition(V1, "a"), ("fa-stage-b", "150"): definition(V2, "c")})
    report = gate.evaluate([A, B], store.rows, read_with(foundry), armed=True)
    assert report["supported"] == [V2] and report["decision"] == "refuse"
    assert report["refused"] == [{"agent_id": "agent-a", "reason": "row_profile_unsupported"}]
    assert [entry["verdict"] for entry in report["agents"]][1] == "compatible"
    assert gate.evaluate([A, B], store.rows, read_with(foundry), armed=False)["decision"] == "observe"


def test_unarmed_gate_reports_what_it_would_refuse_and_passes():
    store, _ = store_with(row("agent-a", "sid-a", "141"))
    foundry = RecordingFoundry({("fa-stage-a", "141"): definition(V1, "a")})
    report = gate.evaluate([A], store.rows, read_with(foundry), armed=False, supported=frozenset({V2}))
    assert report["decision"] == "observe" and report["refused"]


def test_armed_gate_reports_an_unclaimed_agent_without_refusing_the_deploy():
    # ADR §7 checks recorded profiles; a newly onboarded Agent has no row yet and must not block an armed deploy.
    store, _ = store_with(row("agent-a", "sid-a", "141"))
    foundry = RecordingFoundry({("fa-stage-a", "141"): definition(V1, "a")})
    report = gate.evaluate([A, B], store.rows, read_with(foundry), armed=True, supported=frozenset({V1}))
    assert report["decision"] == "pass" and report["refused"] == []
    assert report["unclaimed"] == ["agent-b"]
    assert {"agent_id": "agent-b", "agent_name": "fa-stage-b", "verdict": "unclaimed"} in report["agents"]
    store, _ = store_with()
    assert gate.evaluate([B], store.rows, read_with(foundry), armed=True, supported=frozenset({V2}))["decision"] == "pass"


def test_armed_gate_never_refuses_for_a_retired_agents_row_and_keeps_its_versions_protected():
    # A retired Agent is never invoked again: its v1 row is reported, not a reason to stop the deploy.
    store, _ = store_with(row("agent-a", "sid-a", "150", profile=V2), row("agent-b", "sid-b", "141"))
    foundry = RecordingFoundry({("fa-stage-a", "150"): definition(V2, "c")})
    refused = gate.evaluate([A], store.rows, read_with(foundry), armed=True, supported=frozenset({V2}))
    assert refused["decision"] == "refuse" and refused["refused"] == [
        {"agent_id": "agent-b", "reason": "row_profile_unsupported"}]
    report = gate.evaluate([A], store.rows, read_with(foundry), armed=True, supported=frozenset({V2}),
                           retired=("agent-b",))
    assert report["decision"] == "pass" and report["refused"] == [] and report["unserved_rows"] == []
    assert report["retired_rows"] == [{"agent_id": "agent-b", "row_profile": V1}]
    assert {"agent_id": "agent-b", "agent_name": None, "version": "141"} in report["protected"]["versions"]


def test_armed_gate_fails_closed_on_unread_rows_and_unread_definitions():
    store, repository = store_with(row("agent-a", "sid-a", "141"))
    foundry = RecordingFoundry({})
    report = gate.evaluate([A, B], store.rows, read_with(foundry), armed=True, supported=frozenset({V1}))
    assert [item["reason"] for item in report["refused"]] == ["definition_unread"]
    assert report["decision"] == "refuse"

    def unreadable():
        raise Problem(503, "TABLE_UNAVAILABLE", "synthetic outage")
    report = gate.evaluate([A], unreadable, read_with(foundry), armed=True, supported=frozenset({V1}))
    assert report["refused"] == [{"agent_id": None, "reason": "rows_unread", "code": "TABLE_UNAVAILABLE"}]
    assert report["decision"] == "refuse"


def test_an_approved_override_passes_an_armed_refusal_and_is_kept_in_the_report():
    store, _ = store_with(row("agent-a", "sid-a", "141"))
    foundry = RecordingFoundry({("fa-stage-a", "141"): definition(V1, "a")})
    override = {"release": "2026.10.20-31", "approved_by": "Neal", "approved_at": "2026-10-20T01:00:00+08:00",
                "reason": "one Agent pinned to v1 is being retired"}
    report = gate.evaluate([A], store.rows, read_with(foundry), armed=True, override=override,
                           supported=frozenset({V2}))
    assert report["decision"] == "override" and report["override"] == override


def test_the_gate_reads_binding_rows_and_definitions_only_and_never_a_session(monkeypatch):
    store, repository = store_with(row("agent-a", "sid-a", "150", "145", profile=V2,
                                       previous=[NativeBindingSession(hosted_session_id="sid-old", agent_version="141")]))
    foundry = RecordingFoundry({("fa-stage-a", v): definition(V2, d) for v, d in (("150", "c"), ("145", "b"),
                                                                                  ("141", "a"))})
    monkeypatch.setattr(behavior_agent_versions, "_agents", lambda value: (A, [(A, "configured")]))
    monkeypatch.setattr(behavior_team_registry, "retired_agent_ids", lambda store, config: ())
    monkeypatch.setattr(gate, "deploy_binding_store", lambda value: store)
    report = gate.run_gate(SimpleNamespace(runtime=SimpleNamespace(store=None), config=None),
                           lambda endpoint: foundry, armed=True)
    assert report["decision"] == "pass"
    assert {call[0] for call in foundry.calls} == {"get_version"}
    assert set(repository.calls) == {"list_prefix"}


def test_the_gate_report_lists_project_vetoed_versions_and_never_refuses_for_them(monkeypatch, capsys):
    # ADR-0009 4d (Neal 2026-10-20): an in-place drift's crash-loop only vetoes the version; the operator reads it here.
    store, repository = store_with(row("agent-a", "sid-a", "150", profile=V2))  # 2d-ii: a compatible row is v2
    vetoes = NativeVersionVetoStore(repository, clock=lambda: datetime(2026, 10, 20, 1, tzinfo=timezone.utc))
    vetoes.veto("fa-stage-a", "151")
    # One malformed row (listed before 151) is skipped and logged; it never hides the others.
    repository.create(VETO_PARTITION, "version:fa-stage-a:150", {"schema_version": "fa-native-version-veto-v0"})
    repository.calls.clear()
    foundry = RecordingFoundry({("fa-stage-a", "150"): definition(V2, "c")})
    monkeypatch.setattr(behavior_agent_versions, "_agents", lambda value: (A, [(A, "configured")]))
    monkeypatch.setattr(behavior_team_registry, "retired_agent_ids", lambda store, config: ())
    monkeypatch.setattr(gate, "deploy_binding_store", lambda value: store)
    report = gate.run_gate(SimpleNamespace(runtime=SimpleNamespace(store=None), config=None),
                           lambda endpoint: foundry, armed=True)
    assert report["decision"] == "pass" and report["refused"] == [] and set(repository.calls) == {"list_prefix"}
    assert report["vetoed_versions"] == [
        {"agent_name": "fa-stage-a", "version": "151", "vetoed_at": "2026-10-20T01:00:00+00:00"}]
    assert "vetoed_versions_unread" not in report

    def unreadable():
        raise Problem(503, "TABLE_UNAVAILABLE", "synthetic outage")
    unread = gate.evaluate([A], store.rows, read_with(foundry), armed=True, vetoes=unreadable)
    assert unread["decision"] == "pass" and unread["vetoed_versions_unread"] == "TABLE_UNAVAILABLE"

    import shared_context.behavior_server as server
    import shared_context.behavior_server_config as server_config

    class Composition:
        hosted_versions = SimpleNamespace(_foundry_for=lambda endpoint: None)

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    monkeypatch.setattr(server_config, "load_configuration", lambda path: path)
    monkeypatch.setattr(server, "build_server", lambda config: Composition())
    monkeypatch.setattr(gate, "run_gate", lambda *args, **kwargs: report)
    assert gate.main(["--config", "c.json", "--armed"]) == 0
    hints = [line for line in capsys.readouterr().out.splitlines() if line.startswith("DEPLOY_VERSION_VETOED ")]
    assert hints == ["DEPLOY_VERSION_VETOED agent=fa-stage-a version=151 vetoed_at=2026-10-20T01:00:00+00:00 "
                     "suggest=selector_rollback: consider moving the selector back"]


def test_main_exits_nonzero_only_for_an_armed_refusal(monkeypatch, capsys):
    import shared_context.behavior_server as server
    import shared_context.behavior_server_config as server_config

    class Composition:
        hosted_versions = SimpleNamespace(_foundry_for=lambda endpoint: None)

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    monkeypatch.setattr(server_config, "load_configuration", lambda path: path)
    monkeypatch.setattr(server, "build_server", lambda config: Composition())
    verdict = {}
    monkeypatch.setattr(gate, "run_gate", lambda composition, foundry_for, *, armed, override: gate._decide(
        {"gate": gate.GATE_SCHEMA, "armed": armed, "refused": verdict.get("refused", [])}, armed, override))
    verdict["refused"] = [{"agent_id": "agent-a", "reason": "row_profile_unsupported"}]
    assert gate.main(["--config", "c.json", "--armed"]) == 1
    assert gate.main(["--config", "c.json"]) == 0
    assert gate.main(["--config", "c.json", "--armed", "--override", '{"release":"r"}']) == 0
    lines = [line for line in capsys.readouterr().out.splitlines() if line.startswith("DEPLOY_COMPAT_GATE ")]
    assert [json.loads(line.split(" ", 1)[1])["decision"] for line in lines] == ["refuse", "observe", "override"]


def test_the_deploy_store_reads_the_partition_the_api_writes():
    config = SimpleNamespace(identity=SimpleNamespace(tenant_id="tenant-1", team_id="team-1"),
                             table=SimpleNamespace(scope=SimpleNamespace(partition_key="team-1")))
    composition = SimpleNamespace(config=config, clients=[object()], runtime=SimpleNamespace(
        work_policy=SimpleNamespace(policy=SimpleNamespace(template=SimpleNamespace(team_id="team-1")))))
    assert gate.deploy_binding_store(composition).partition_key == binding_store(composition).partition_key


def test_rows_reads_every_page_and_refuses_a_malformed_row():
    store, repository = store_with(*(row(f"agent-{index:03d}", f"sid-{index}", "141") for index in range(7)))
    assert [item.agent_id for item in store.rows(page=3)] == [f"agent-{index:03d}" for index in range(7)]
    repository.create(store.partition_key, "agent:agent-zzz", {"schema_version": "fa-native-binding-v1"})
    with pytest.raises(Problem) as error:
        store.rows(page=3)
    assert error.value.body["error"]["code"] == "NATIVE_BINDING_INVALID"


# ----- retention ----------------------------------------------------------------------------------------------------


def test_versions_and_images_a_binding_row_references_never_appear_in_a_cleanup_list():
    store, _ = store_with(row("agent-a", "sid-a", "150", "145",
                              previous=[NativeBindingSession(hosted_session_id="sid-older", agent_version="120"),
                                        NativeBindingSession(hosted_session_id="sid-old", agent_version="141")]))
    foundry = RecordingFoundry({("fa-stage-a", v): definition(V1, d)
                                for v, d in (("150", "c"), ("145", "b"), ("141", "a"), ("120", "9"))})
    protected = gate.evaluate([A], store.rows, read_with(foundry), armed=False)["protected"]
    every = [{"agent_name": "fa-stage-a", "version": str(number)} for number in range(100, 160)]
    deletable, kept = gate.retention_candidates(every, protected)
    # The current SID's creation and recorded versions and the previous SID's version are kept; 120 (an older
    # previous SID) is not referenced by the ADR rule.
    assert {item["version"] for item in kept} == {"150", "145", "141"}
    assert not {"150", "145", "141"} & {item["version"] for item in deletable}
    images = [IMAGE + "c" * 64, "cr.example.azurecr.io/fa-behavior-assistant-desktop:6f1e2d3c4b5a@sha256:" + "b" * 64,
              IMAGE + "9" * 64]
    deletable, kept = gate.retention_candidates(images, protected)
    assert deletable == [IMAGE + "9" * 64] and len(kept) == 2


def test_no_registry_retention_policy_sweeps_referenced_images():
    """The only registry FA's Bicep creates keeps its retention policy off; a change here needs the binding-row
    protection first (deploy/README.md#version-retention)."""
    bicep = (ROOT / "infra" / "shared-context" / "foundation.bicep").read_text(encoding="utf-8")
    registries = re.findall(r"avm/res/container-registry/registry:[^']+' = \{(.*?)\n\}", bicep, re.S)
    assert registries and all("retentionPolicyStatus: 'disabled'" in body for body in registries)
    for path in ROOT.glob("infra/**/*.bicep"):
        assert "retentionPolicyStatus: 'enabled'" not in path.read_text(encoding="utf-8"), path


def test_no_repository_script_deletes_a_version_or_an_image():
    """No version or image cleanup script exists; one added later goes through ``retention_candidates``. The only
    delete is ``behavior_agent_retire`` removing one tombstoned Agent, or one failed onboarding's leftover agent, whole
    (``agents.delete(force=True)``)."""
    pattern = re.compile(r"delete_version|acr (repository|manifest) delete|acr purge|/versions/[^\"']*\"?,? *\"?DELETE",
                         re.I)
    for folder in ("deploy", "scripts", "infra", ".github", "src/fa-shared-context-poc"):
        for directory, children, files in os.walk(ROOT / folder):
            # A local virtualenv's SDK defines delete_version; only the repository's own files count.
            children[:] = [name for name in children
                           if not name.startswith(".") and name not in {"node_modules", "__pycache__", "venv"}]
            for name in files:
                path = Path(directory) / name
                if path.suffix in {".py", ".ps1", ".sh", ".bicep", ".yml", ".yaml"}:
                    assert pattern.search(path.read_text(encoding="utf-8", errors="replace")) is None, path


# ----- CI wiring ----------------------------------------------------------------------------------------------------


def load_ci():
    spec = importlib.util.spec_from_file_location("offline_stage_ci_gate", ROOT / "deploy" / "stage" / "ci_deploy.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.STAGE = copy.deepcopy(module.STAGE)
    module.STAGE["api_environment"]["FA_NATIVE_SESSION_GUARD_ENFORCE"] = "1"  # ADR-0009 2d-ii: the switch is on
    return module


API_REF = "{registry}/fa-behavior-api@sha256:" + "d" * 64


@pytest.fixture
def ci(monkeypatch, tmp_path):
    module = load_ci()
    release = tmp_path / "release.json"
    release.write_text(json.dumps({"version": "2026.10.20-31", "milestone": "m"}), encoding="utf-8")
    module.STAGE["release_file"] = str(release)
    module.STAGE.pop("compatibility_gate_armed", None)
    jobs = []

    def run_job(api_ref, script, **kwargs):
        jobs.append(script)
        return module.JOB_STATUS, "job-execution-1"
    module.JOB_STATUS = "Failed"
    monkeypatch.setattr(module, "_run_job", run_job)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    module.jobs, module.release_path = jobs, release
    return module


def api_ref(ci):
    return API_REF.format(registry=ci.STAGE["registry"])


def test_ci_gate_stops_the_deploy_only_when_armed(ci):
    ci.gate(api_ref(ci))  # unarmed: a refusing (failed) gate run is a warning, the deploy goes on
    assert "--armed" not in ci.jobs[-1] and "native_deploy_gate import main" in ci.jobs[-1]
    ci.STAGE["compatibility_gate_armed"] = True
    with pytest.raises(SystemExit, match="compatibility gate job-execution-1 Failed"):
        ci.gate(api_ref(ci))
    assert "'--armed'" in ci.jobs[-1]
    ci.JOB_STATUS = "Succeeded"
    ci.gate(api_ref(ci))


def test_a_succeeded_gate_job_says_finished_and_puts_what_it_proves_in_the_step_summary(ci, monkeypatch, tmp_path,
                                                                                         capsys):
    # Unarmed, a Succeeded job may carry decision=observe with refusals: it is never reported as "passed".
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    ci.JOB_STATUS = "Succeeded"
    ci.gate(api_ref(ci))
    out = capsys.readouterr().out
    assert "finished" in out and "passed" not in out
    text = summary.read_text(encoding="utf-8")
    assert "decision: pass or observe" in text and "DEPLOY_COMPAT_GATE" in text and "job-execution-1" in text
    assert "DEPLOY_VERSION_VETOED" in text and "consider moving the selector back" in out
    ci.STAGE["compatibility_gate_armed"] = True
    ci.gate(api_ref(ci))
    assert "decision: pass (0 refusals)" in summary.read_text(encoding="utf-8")
    ci.JOB_STATUS = "Failed"
    with pytest.raises(SystemExit):
        ci.gate(api_ref(ci))
    assert "decision: refuse" in summary.read_text(encoding="utf-8")


def test_an_unarmed_gate_whose_job_never_finishes_does_not_block_the_deploy(ci, monkeypatch):
    def hung(api_ref, script, **kwargs):
        raise SystemExit("job execution job-execution-1 did not finish; read back before any rerun")
    monkeypatch.setattr(ci, "_run_job", hung)
    ci.gate(api_ref(ci))
    ci.STAGE["compatibility_gate_armed"] = True
    with pytest.raises(SystemExit, match="did not finish"):
        ci.gate(api_ref(ci))


JOB = {"properties": {"template": {"containers": [{"name": "behavior-controller", "env": [],
                                                    "resources": {"cpu": 0.25, "memory": "0.5Gi"}}]}}}


@pytest.fixture
def ci_arm(monkeypatch, tmp_path):
    """The real ``_run_job`` against a scripted ``rest`` (ARM); ``responses[(method, suffix)]`` answers a call."""
    module = load_ci()
    release = tmp_path / "release.json"
    release.write_text(json.dumps({"version": "2026.10.20-31"}), encoding="utf-8")
    module.STAGE["release_file"] = str(release)
    module.STAGE.pop("compatibility_gate_armed", None)
    calls, responses = [], {}

    def rest(method, path, body=None):
        calls.append((method, path.split("?")[0].rsplit("/", 1)[-1]))
        for (verb, suffix), answer in responses.items():
            if verb == method and path.split("?")[0].endswith(suffix):
                return answer() if callable(answer) else copy.deepcopy(answer)
        raise AssertionError(f"unexpected ARM call {method} {path}")
    monkeypatch.setattr(module, "rest", rest)
    monkeypatch.setattr(module.time, "sleep", lambda seconds: None)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    module.arm_calls, module.arm_responses = calls, responses
    return module


@pytest.mark.parametrize("broken", ["job_read_empty", "start_without_name", "execution_without_status"])
def test_an_unarmed_gate_survives_malformed_arm_responses_and_an_armed_one_refuses(ci_arm, capsys, broken):
    job = f"/jobs/{ci_arm.STAGE['sync_job']}"
    ci_arm.arm_responses.update({
        ("GET", job): None if broken == "job_read_empty" else JOB,
        ("POST", "/start"): {} if broken == "start_without_name" else {"name": "job-execution-1"},
        ("GET", "/executions/job-execution-1"): {"properties": {}} if broken == "execution_without_status" else {
            "properties": {"status": "Succeeded"}},
    })
    ci_arm.gate(api_ref(ci_arm))  # unarmed: TypeError/KeyError out of _run_job is a warning, not a failed step
    assert "::warning::compatibility gate did not run to the end while not armed" in capsys.readouterr().out
    ci_arm.STAGE["compatibility_gate_armed"] = True
    with pytest.raises(SystemExit, match="compatibility gate could not run"):
        ci_arm.gate(api_ref(ci_arm))


def test_an_unarmed_gate_survives_an_arm_reply_that_is_not_json(ci_arm, monkeypatch, capsys):
    real = load_ci()
    monkeypatch.setattr(real, "run", lambda *args, **kwargs: "<html>gateway timeout</html>")
    monkeypatch.setattr(ci_arm, "rest", real.rest)
    ci_arm.gate(api_ref(ci_arm))
    assert "JSONDecodeError" in capsys.readouterr().out
    ci_arm.STAGE["compatibility_gate_armed"] = True
    with pytest.raises(SystemExit, match="JSONDecodeError"):
        ci_arm.gate(api_ref(ci_arm))


def test_a_hung_gate_execution_is_stopped_after_its_own_short_budget(ci_arm, capsys):
    """The sync job's replicaTimeout is 30 min; the gate waits 3 min, stops the execution, and goes on unarmed."""
    job = f"/jobs/{ci_arm.STAGE['sync_job']}"
    ci_arm.arm_responses.update({
        ("GET", job): JOB, ("POST", "/start"): {"name": "job-execution-1"},
        ("GET", "/executions/job-execution-1"): {"properties": {"status": "Running"}},
        ("POST", "/executions/job-execution-1/stop"): None})
    ci_arm.gate(api_ref(ci_arm))
    polls = [call for call in ci_arm.arm_calls if call == ("GET", "job-execution-1")]
    assert len(polls) == ci_arm.GATE_POLL_ATTEMPTS == 18
    assert ci_arm.arm_calls[-1] == ("POST", "stop")
    out = capsys.readouterr().out
    assert "::warning::" in out and "did not finish within 180 s; stop requested" in out
    ci_arm.STAGE["compatibility_gate_armed"] = True
    ci_arm.arm_calls.clear()
    with pytest.raises(SystemExit, match="did not finish within 180 s; stop requested"):
        ci_arm.gate(api_ref(ci_arm))
    assert ci_arm.arm_calls[-1] == ("POST", "stop")
    # A failed stop is reported; the timeout stays the verdict.
    del ci_arm.arm_responses[("POST", "/executions/job-execution-1/stop")]
    with pytest.raises(SystemExit, match="stop failed"):
        ci_arm.gate(api_ref(ci_arm))
    # sync keeps its own 30-minute wait and never stops an execution.
    ci_arm.arm_calls.clear()
    with pytest.raises(SystemExit, match="did not finish; read back"):
        ci_arm._run_job(api_ref(ci_arm), "pass")
    assert len([call for call in ci_arm.arm_calls if call == ("GET", "job-execution-1")]) == 180
    assert ("POST", "stop") not in ci_arm.arm_calls


def test_a_stop_whose_reply_fails_is_read_back_once_before_it_is_reported(ci_arm):
    """The stop reached ARM but its reply was lost: one read of the execution says it stopped, not "stop failed"."""
    job = f"/jobs/{ci_arm.STAGE['sync_job']}"
    execution = ("GET", "/executions/job-execution-1")

    def stop_then_lose_the_reply():
        ci_arm.arm_responses[execution] = {"properties": {"status": "Stopped"}}
        raise SystemExit("ARM 504 gateway timeout")
    ci_arm.arm_responses.update({
        ("GET", job): JOB, ("POST", "/start"): {"name": "job-execution-1"},
        execution: {"properties": {"status": "Running"}},
        ("POST", "/executions/job-execution-1/stop"): stop_then_lose_the_reply})
    ci_arm.STAGE["compatibility_gate_armed"] = True
    with pytest.raises(SystemExit, match="did not finish within 180 s") as caught:
        ci_arm.gate(api_ref(ci_arm))
    assert "execution reads back Stopped" in str(caught.value) and "stop failed" not in str(caught.value)
    assert ci_arm.arm_calls[-2:] == [("POST", "stop"), ("GET", "job-execution-1")]


def test_ci_override_is_bound_to_this_release_and_logged(ci, monkeypatch, tmp_path, capsys):
    ci.STAGE["compatibility_gate_armed"] = True
    override = {"release": "2026.10.20-31", "approved_by": "Neal", "approved_at": "2026-10-20T01:00:00+08:00",
                "reason": "retiring agent-x"}
    ci.release_path.write_text(json.dumps({"version": "2026.10.20-31", "compatibility_gate_override": override}),
                               encoding="utf-8")
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    ci.JOB_STATUS = "Succeeded"
    ci.gate(api_ref(ci))
    assert "--override" in ci.jobs[-1] and "retiring agent-x" in ci.jobs[-1]
    assert "OVERRIDE" in summary.read_text(encoding="utf-8") and "::warning::" in capsys.readouterr().out
    # The next release keeps a stale override: meta stops before any build.
    ci.release_path.write_text(json.dumps({"version": "2026.10.21-32", "compatibility_gate_override": override}),
                               encoding="utf-8")
    with pytest.raises(SystemExit, match="one release only"):
        ci.check()
    ci.release_path.write_text(json.dumps({"version": "2026.10.20-31", "compatibility_gate_override": {
        "release": "2026.10.20-31"}}), encoding="utf-8")
    with pytest.raises(SystemExit, match="compatibility_gate_override needs"):
        ci.gate_settings()
    ci.STAGE["compatibility_gate_armed"] = "yes"
    with pytest.raises(SystemExit, match="compatibility_gate_armed"):
        ci.gate_settings()


def test_ci_gate_runs_from_the_new_api_image_only(ci):
    with pytest.raises(SystemExit, match="digest reference"):
        ci.gate(ci.STAGE["registry"] + "/fa-behavior-api:latest")
    assert ci.jobs == []


def test_workflow_runs_the_gate_before_any_environment_write():
    workflow = (ROOT / ".github" / "workflows" / "deploy-staging.yml").read_text(encoding="utf-8")
    order = [workflow.index(text) for text in ("ci_deploy.py build api", "ci_deploy.py gate", "ci_deploy.py sync",
                                               "ci_deploy.py api ", "ci_deploy.py follow")]
    assert order == sorted(order)


def test_the_checked_in_stage_profile_and_release_pass_the_gate_settings_rules():
    """The checked-in stage profile and release file go through the real ``gate_settings``: arming is a boolean and
    an override names exactly this release with its four short fields. A legitimate arming (the 2d-ii release PR)
    or one-release override keeps this green; a malformed or stale one fails it, as meta would."""
    module = load_ci()
    stage = json.loads((ROOT / "deploy" / "stage" / "stage.json").read_text(encoding="utf-8"))
    module.STAGE = stage
    module.CONFIG = ROOT / "deploy" / "stage" / "stage.json"
    armed, override = module.gate_settings()
    assert armed is stage.get("compatibility_gate_armed", False)
    if override is not None:
        assert override["release"] == module.release()
