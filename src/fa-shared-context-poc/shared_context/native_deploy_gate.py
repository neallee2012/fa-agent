"""ADR-0009 phase 3b: the pre-deploy compatibility gate and the version/image retention rule.

The gate runs from the **new** API image before that image serves anything (deploy/stage/ci_deploy.py ``gate``, in
the sync job, so it uses the API identity's Table and Foundry access). ``SUPPORTED_STATE_PROFILES`` imported here is
therefore exactly the new API's supported set. It reads two things only: every binding row of the Team (the Table)
and the version definitions those rows name (``get_version``). It never lists, reads, invokes, wakes or stops a
session, and it writes nothing.

M1 (expand/contract): the API may stop speaking an old wire only when no binding row still records an old profile.
So a row is refused when its recorded profile, or the profile its recorded actual version's definition declares, is
outside the new API's set. A served Agent without a row is reported (``unclaimed``), not refused: there is no
recorded profile to check (ADR §7). The gate refuses a deploy **only when armed** (ADR §7: it is connected after the v1->v2
migration, otherwise the first v2 deploy is always blocked); unarmed it reports what it would refuse and passes. An
approval-gated override (bound to one release, see deploy/README.md) passes an armed refusal and is logged.

Retention (ADR §7 "保留舊版本"): the versions a row references (the current SID's creation and recorded versions and
the previous SID's version) and their images are never deleted nor swept; ``retention_candidates`` is the guard any
cleanup list goes through. SDK-independent, so the API can import it.
"""

from __future__ import annotations

import argparse
import json
import logging
from typing import Callable, Iterable

from .errors import Problem
from .native_binding import SUPPORTED_STATE_PROFILES, NativeBindingRow, NativeBindingStore

GATE_SCHEMA = "fa-deploy-compat-gate-v1"
log = logging.getLogger(__name__)


def definition_profile(definition) -> str | None:
    """The native state profile a Hosted definition enables (``FA_NATIVE_TURNS``), None when it enables none."""
    environment = definition.get("environment_variables") if isinstance(definition, dict) else None
    value = environment.get("FA_NATIVE_TURNS") if isinstance(environment, dict) else None
    return value if isinstance(value, str) else None


def definition_image(definition) -> str | None:
    """The container image a Hosted definition runs (the field ``image_source`` replaces)."""
    if not isinstance(definition, dict):
        return None
    container = definition.get("container_configuration")
    value = container.get("image") if isinstance(container, dict) else definition.get("image")
    return value if isinstance(value, str) and value else None


def referenced_versions(row: NativeBindingRow) -> tuple[str, ...]:
    """ADR §7: the current SID's versions (created and recorded actual) and the previous SID's version."""
    versions = [row.created_agent_version, row.observed.agent_version]
    if row.previous_sessions and row.previous_sessions[-1].agent_version is not None:
        versions.append(row.previous_sessions[-1].agent_version)
    return tuple(dict.fromkeys(versions))


class _Unread(str):
    """A definition read that failed, kept as its error code."""


def evaluate(agents: Iterable, rows: Callable[[], Iterable[NativeBindingRow]],
             read_definition: Callable[[object, str], dict], *, armed: bool, override: dict | None = None,
             supported: frozenset[str] = SUPPORTED_STATE_PROFILES, retired: Iterable[str] = ()) -> dict:
    """The gate's report. ``agents`` are the served targets (``agent_id``, ``agent_name``, ``project_endpoint``);
    ``rows()`` reads every binding row; ``read_definition(target, version)`` is one ``get_version`` read and returns
    the version's definition; ``retired`` are tombstoned Agent IDs, whose rows no turn reaches. No other I/O."""
    agents = list(agents)
    by_id = {target.agent_id: target for target in agents}
    retired = frozenset(retired)
    report = {"gate": GATE_SCHEMA, "armed": armed, "supported": sorted(supported), "agents": [],
              "unserved_rows": [], "retired_rows": [], "unclaimed": [], "refused": [],
              "protected": {"versions": [], "images": []}}
    try:
        found = {row.agent_id: row for row in rows()}
    except Exception as error:  # noqa: BLE001 - an unreadable Table proves nothing: refused when armed
        code = error.body["error"]["code"] if isinstance(error, Problem) else type(error).__name__
        report["refused"].append({"agent_id": None, "reason": "rows_unread", "code": code})
        return _decide(report, armed, override)
    definitions = {}

    def definition(target, version):
        key = (target.agent_id, version)
        if key not in definitions:
            try:
                definitions[key] = read_definition(target, version)
            except Exception as error:  # noqa: BLE001 - unread is reported, and refused when it decides compatibility
                code = error.body["error"]["code"] if isinstance(error, Problem) else type(error).__name__
                definitions[key] = _Unread(code)
        return definitions[key]

    for target in agents:
        row = found.get(target.agent_id)
        entry = {"agent_id": target.agent_id, "agent_name": target.agent_name}
        if row is None:
            # Reported, never refused: ADR §7 checks the profiles binding rows record, and a row-less Agent records
            # none (a newly onboarded Agent, or one no API has observed since phase 1). Its first observation claims
            # the row with the profile its version declares (``NativeBindingStore.claim``, 2d-i). Whether its session is the gen-0
            # SID the selected version derives cannot be told without ``get_session``, which the gate never calls
            # (``native_session_id`` also derives a version-less SID); the 2d-ii gate list claims or retires these
            # Agents before arming (ADR-0009 M1).
            entry["verdict"] = "unclaimed"
            report["agents"].append(entry)
            report["unclaimed"].append(target.agent_id)
            continue
        version = row.observed.agent_version
        current = definition(target, version)
        declared = None if isinstance(current, _Unread) else definition_profile(current)
        entry.update(hosted_session_id=row.hosted_session_id, version=version, row_profile=row.state_profile,
                     definition_profile=declared)
        if row.state_profile not in supported:
            entry["verdict"] = "row_profile_unsupported"
        elif isinstance(current, _Unread):
            entry.update(verdict="definition_unread", code=str(current))
        elif declared not in supported:
            entry["verdict"] = "definition_profile_unsupported"
        else:
            entry["verdict"] = "compatible"
        report["agents"].append(entry)
        if entry["verdict"] != "compatible":
            report["refused"].append({"agent_id": target.agent_id, "reason": entry["verdict"]})
        for referenced in referenced_versions(row):
            report["protected"]["versions"].append(
                {"agent_id": target.agent_id, "agent_name": target.agent_name, "version": referenced})
            image = definition_image(definition(target, referenced))
            if image is not None and image not in report["protected"]["images"]:
                report["protected"]["images"].append(image)
    for agent_id, row in sorted(found.items()):
        if agent_id in by_id:
            continue
        if agent_id in retired:
            # A retired Agent is never invoked again, so its recorded profile cannot block a deploy; its versions
            # stay protected here (removing them is the separate, approved retire --delete-foundry step).
            report["retired_rows"].append({"agent_id": agent_id, "row_profile": row.state_profile})
            for referenced in referenced_versions(row):
                report["protected"]["versions"].append({"agent_id": agent_id, "agent_name": None, "version": referenced})
            continue
        # A row of an Agent this API no longer serves: no turn reaches its session, so only its recorded profile
        # is checked; its versions stay protected (their definitions are not readable without a served target).
        report["unserved_rows"].append({"agent_id": agent_id, "row_profile": row.state_profile})
        if row.state_profile not in supported:
            report["refused"].append({"agent_id": agent_id, "reason": "row_profile_unsupported"})
        for referenced in referenced_versions(row):
            report["protected"]["versions"].append({"agent_id": agent_id, "agent_name": None, "version": referenced})
    return _decide(report, armed, override)


def _decide(report, armed, override):
    if not report["refused"]:
        report["decision"] = "pass"
    elif not armed:
        report["decision"] = "observe"
    elif override is not None:
        report.update(decision="override", override=override)
    else:
        report["decision"] = "refuse"
    return report


def _digest(reference: str) -> str:
    """A digest reference names one manifest whatever registry path or tag spells it."""
    return reference.rsplit("@", 1)[-1] if "@sha256:" in reference else reference


def retention_candidates(candidates: Iterable, protected: dict) -> tuple[list, list]:
    """(deletable, kept): the guard every version or image cleanup list goes through (ADR §7). ``candidates`` are
    ``{"agent_name", "version"}`` items or image references; anything a binding row references is kept. Deleting
    what remains still needs its own approval."""
    versions = {(item["agent_name"], item["version"]) for item in protected["versions"]}
    agent_ids = {(item["agent_id"], item["version"]) for item in protected["versions"]}
    images = set(protected["images"])
    deletable, kept = [], []
    for item in candidates:
        if isinstance(item, str):
            held = item in images or _digest(item) in {_digest(image) for image in images}
        else:
            held = ((item.get("agent_name"), item.get("version")) in versions
                    or (item.get("agent_id"), item.get("version")) in agent_ids)
        (kept if held else deletable).append(item)
    return deletable, kept


def deploy_binding_store(composition) -> NativeBindingStore:
    """The Team's binding store without ``installation()`` (which starts workers): the same partition the API's
    ``binding_store`` uses, since the configuration binds every Team binding to the identity's Team."""
    from .storage import TableRepository
    config = composition.config
    store = NativeBindingStore(TableRepository(composition.clients[0]), tenant_id=str(config.identity.tenant_id),
                               team_id=config.identity.team_id)
    if store.partition_key == config.table.scope.partition_key:
        raise Problem(503, "NATIVE_BINDING_UNAVAILABLE", "binding rows cannot share the legacy authority inventory")
    return store


def run_gate(composition, foundry_for, *, armed: bool, override: dict | None = None) -> dict:
    from .behavior_agent_versions import _agents
    from .behavior_team_registry import retired_agent_ids
    _, agents = _agents(composition)
    retired = retired_agent_ids(composition.runtime.store, composition.config)
    store = deploy_binding_store(composition)

    def read_definition(target, version):
        details = foundry_for(target.project_endpoint).get_version(target.agent_name, version)
        if not isinstance(details, dict) or details.get("version") != version:
            raise Problem(503, "AGENT_VERSION_UNREADABLE", "the version definition could not be read")
        return details.get("definition")

    return evaluate([target for target, _ in agents], store.rows, read_definition, armed=armed, override=override,
                    retired=retired)


def main(argv=None):
    from .behavior_server import build_server
    from .behavior_server_config import load_configuration
    parser = argparse.ArgumentParser(prog="deploy-compat-gate", description=(
        "Read every binding row and the version definitions they name; refuse (exit 1) when armed and a recorded "
        "profile is not supported by this image. Never calls a session."))
    parser.add_argument("--config", required=True)
    parser.add_argument("--armed", action="store_true")
    parser.add_argument("--override", help="JSON of the approved one-release override (release, approved_by, reason)")
    args = parser.parse_args(argv)
    override = None if args.override is None else json.loads(args.override)
    try:
        with build_server(load_configuration(args.config)) as composition:
            report = run_gate(composition, composition.hosted_versions._foundry_for, armed=args.armed,
                              override=override)
    except Problem as error:
        report = _decide({"gate": GATE_SCHEMA, "armed": args.armed, "refused": [
            {"agent_id": None, "reason": "gate_unavailable", "code": error.body["error"]["code"]}]},
            args.armed, override)
    except (ValueError, OSError):
        report = _decide({"gate": GATE_SCHEMA, "armed": args.armed, "refused": [
            {"agent_id": None, "reason": "gate_unavailable", "code": "DEPLOY_GATE_CONFIGURATION_INVALID"}]},
            args.armed, override)
    line = json.dumps(report, sort_keys=True, ensure_ascii=False)
    (log.warning if report["decision"] in {"refuse", "override"} else log.info)("DEPLOY_COMPAT_GATE %s", line)
    print(f"DEPLOY_COMPAT_GATE {line}")
    return 1 if report["decision"] == "refuse" else 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(main())
