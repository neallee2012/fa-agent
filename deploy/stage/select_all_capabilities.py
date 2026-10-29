"""Select "all capabilities" for new conversations of the stage Agents, or of any profile's assistants (ADR-0008;
Neal 2026-10-02 22:27).

A selection is the Team owner's approval record (`selected_by` must be the owner), so an operator runs this with
Neal's login in the project-specific AZURE_CONFIG_DIR; CI never writes it. Deploys do not touch the Table, so a
selection stays across CI deploys; run this again only when the catalog changes (a new `entry_sha256`), a newly
approved Team skill should be pinned, or a stage.json settings value changes (`--keep-capabilities`). It only affects
new conversations (`applies_to=new-conversations-only`).

What "all" means is in deploy/stage/stage.json `conversation_defaults`: the prompt profile (fa-instruction-v4 from
2026-10-04, README "SP v4") and settings (the dev A/B values, plus the Responses state profile chosen on the template
page, `state_profile: foundry-responses-v1`, which a rerun must keep), plus capabilities left out on purpose
(`WebIQ-MCP` was the API secret broker, retired 2026-10-22; the exclusion stays for API images that still offer it;
stage uses the WebIQ toolbox, #415 Q9). Capabilities and skills come from the
server's own options: every offered capability except the excluded ones and those the prompt profile cannot compose
(the server's own `base_supports` rule; for example `fa-delegate-reader-v1` needs a profile that declares
`read_user_file`, which fa-instruction-v3/v4 do not, so the API would refuse the whole selection with 422
CAPABILITY_BASE_UNSUPPORTED), and every currently approved Team skill (at most 4). The "+" form pre-checks the default
Agent's (A's) current selection; this CLI never treats that UI choice as implicit approval for another Agent.

Safety (no blind resend): one intent file per Agent and request ID first (open mode "x"). Each change uses a new
request ID; rerunning with an existing one only reconciles its original outcome (`selection-reconcile` writes
nothing) and stops if this run's template differs. A selection equal to the current one is not written again. A POST
whose outcome is unknown is never repeated. Success means the read-back selection equals this run's template.

usage: PYTHONPATH=src/fa-shared-context-poc python deploy/stage/select_all_capabilities.py \
           --out <private dir> --request-id <new id per change> [--config <profile.json>] \
           [--agent <name> ...] [--dry-run] [--keep-capabilities] [--initial-template <owner-reviewed.json>]
(run from a checkout of the deployed commit: the role digest is computed from this checkout and the API refuses a
different one with 422, writing nothing)
--config selects the environment profile (default FA_DEPLOY_CONFIG, else deploy/stage/stage.json); without --agent
every `kind=assistant` Agent of that profile is selected. The profile's own `conversation_defaults` are used; a
profile without them uses the checked-in stage.json defaults (the reference new-conversation lane) and says so.
--keep-capabilities keeps each Agent's current selection (capabilities, skills, role) and applies only the
`conversation_defaults` settings, e.g. the checkpoint or state profile. An Agent that has never been selected (a
freshly seeded one) needs an explicit --initial-template selected by its owner; the authenticated /select endpoint
checks the owner's approval and exact current source references. The script never inherits a role or capabilities
from another Agent or fabricates an approved selection. Without a
selection, a new native conversation uses the completions lane, where reasoning models refuse `reasoning_effort`
(HTTP 400) — new environment lesson 2026-10-07.
"""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
from azure.identity import AzureCliCredential
from pydantic import ValidationError

from shared_context.behavior_agent_template import AgentTemplate
from shared_context.behavior_capabilities import base_supports
from shared_context.behavior_skill_prompt import assemble_prompt

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "shared-context"))
from deployment_config import config_path, load_config, write_x as _write_x  # noqa: E402

STAGE = json.loads((HERE / "stage.json").read_text(encoding="utf-8"))
DEFAULTS = STAGE["conversation_defaults"]
MAX_SKILLS = 4
TEMPLATE_KEYS = ("schema_version", "role", "settings", "skills", "unavailable_skills")


def conversation_defaults(profile):
    """(defaults, source): the profile's own new-conversation defaults, else the checked-in stage ones."""
    own = profile.get("conversation_defaults")
    if isinstance(own, dict) and isinstance(own.get("settings"), dict):
        return own, "profile"
    return DEFAULTS, "stage.json"


def assistant_names(profile):
    return [agent["name"] for agent in profile["agents"] if agent["kind"] == "assistant"]


def template(options, defaults=None):
    defaults = DEFAULTS if defaults is None else defaults
    profile = defaults["prompt_profile"]
    role = assemble_prompt(profile).record
    capabilities = sorted(({"entry_id": item["entry_id"], "entry_sha256": item["entry_sha256"]}
                           for item in options["capabilities"]
                           if item["entry_id"] not in defaults["excluded_capabilities"]
                           and base_supports(item["entry_id"], profile)), key=lambda item: item["entry_id"])
    skills = sorted(options["skills"], key=lambda item: item["name"])
    if len(skills) > MAX_SKILLS or options.get("more_skills"):
        raise SystemExit(f"more than {MAX_SKILLS} approved Team skills; the owner chooses which to pin")
    return {"schema_version": "fa-agent-template-v1",
            "role": {"profile": role.profile, "prefix_sha256": role.prefix_sha256,
                     "capability_catalog_version": role.capability_catalog_version},
            "settings": {"sdk_version": "1.0.13", "runtime_version": "1.0.83", "mode": "empty",
                         **defaults["settings"], "capabilities": capabilities},
            "skills": skills, "unavailable_skills": []}


def keep_current(current, initial_template=None, defaults=None):
    """The owner's current choice with only the conversation_defaults settings applied; capabilities, skills, role kept.

    For a settings-only change (2026-10-06: checkpoint profile manual → auto): "all" can exceed the 12-capability limit
    once Team MCP servers are offered, and the owner's own capability choice must not be widened or narrowed here.
    An unselected Agent requires a template explicitly supplied by its owner; scoped Agents may not export a default."""
    defaults = DEFAULTS if defaults is None else defaults
    if current.get("selection_id") is not None:
        if not isinstance(current.get("template"), dict):
            raise SystemExit("the current selection has no readable template; do not replace it with an initial template")
        base = current["template"]
    elif isinstance(initial_template, dict) and all(key in initial_template for key in TEMPLATE_KEYS):
        try:
            base = AgentTemplate.model_validate_json(json.dumps(initial_template)).model_dump(mode="json")
        except ValidationError:
            raise SystemExit("--initial-template must contain a valid fa-agent-template-v1 template") from None
    else:
        raise SystemExit("--keep-capabilities for an unselected Agent requires --initial-template with an "
                         "owner-reviewed fa-agent-template-v1 JSON file")
    wanted = json.loads(json.dumps(base))
    wanted["settings"].update(defaults["settings"])
    return wanted


def load_initial_template(path):
    try:
        raw = Path(path).read_bytes()
        return AgentTemplate.model_validate_json(raw).model_dump(mode="json")
    except OSError as error:
        raise SystemExit(f"cannot read initial template {path}: {error}") from None
    except ValidationError:
        raise SystemExit(f"{path} is not a valid fa-agent-template-v1 template") from None


def _same(left, right):
    return (AgentTemplate.model_validate_json(json.dumps(left))
            == AgentTemplate.model_validate_json(json.dumps(right)))


def select(client, headers, out, agent, request_id, dry_run, keep=False, defaults=None, initial_template=None):
    """(result, wanted template); wanted is what the current selection must equal afterwards."""
    base = f"/agents/{agent['agent_id']}/work-policy/v1/agent-template"
    intent = out / f"select-{agent['name']}-{request_id}-intent.json"
    if intent.exists():
        saved = json.loads(intent.read_text(encoding="utf-8"))
        saved_body = saved.get("body") if isinstance(saved, dict) else None
        if (not isinstance(saved_body, dict) or saved_body.get("request_id") != f"{request_id}-{agent['name']}"
                or not isinstance(saved_body.get("template"), dict)):
            raise SystemExit(f"{agent['name']}: existing intent is invalid; do not send a replacement request")
        try:
            wanted = AgentTemplate.model_validate_json(
                json.dumps(saved_body["template"])).model_dump(mode="json")
        except ValidationError:
            raise SystemExit(f"{agent['name']}: existing intent has an invalid template; do not resend") from None
        reconciled = client.post(base + "/selection-reconcile", json=saved_body, headers=headers)
        return {"agent": agent["name"], "outcome": "intent-exists-reconciled",
                "reconcile_status": reconciled.status_code}, wanted
    current = client.get(base + "/selection", headers=headers).raise_for_status().json()
    if not keep:
        options = client.get(base + "/options", headers=headers).raise_for_status().json()
    wanted = keep_current(current, initial_template, defaults) if keep else template(options, defaults)
    if current.get("selection_id") is not None and _same(current["template"], wanted):
        return {"agent": agent["name"], "outcome": "already-selected"}, wanted
    body = {"request_id": f"{request_id}-{agent['name']}", "expected_selection_id": current.get("selection_id"),
            "template": wanted}
    if dry_run:
        before = (current.get("template") or initial_template or {}).get("settings", {})
        changed = sorted(key for key in set(before) | set(wanted["settings"])
                         if before.get(key) != wanted["settings"].get(key))
        return {"agent": agent["name"], "outcome": "dry-run", "would_replace": current.get("selection_id"),
                "changed_settings": changed,
                "capabilities": [item["entry_id"] for item in wanted["settings"].get("capabilities", [])],
                "skills": [item["name"] for item in wanted.get("skills", [])]}, None
    _write_x(intent, {"at": datetime.now(timezone.utc).isoformat(), "body": body, "automatic_retry": False})
    try:
        response = client.post(base + "/select", json=body, headers=headers)
        outcome = f"http_{response.status_code}"
    except httpx.HTTPError as error:
        outcome = f"unknown:{type(error).__name__}"
    return {"agent": agent["name"], "outcome": outcome}, wanted


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--request-id", required=True)
    parser.add_argument("--config", help="deployment profile (default: FA_DEPLOY_CONFIG, else deploy/stage/stage.json)")
    parser.add_argument("--agent", action="append", default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--keep-capabilities", action="store_true",
                        help="keep each Agent's selected (or explicitly supplied) capabilities, skills and role; apply only the "
                             "conversation_defaults settings")
    parser.add_argument("--initial-template", type=Path,
                        help="owner-reviewed fa-agent-template-v1 JSON used only when --keep-capabilities finds no selection")
    args = parser.parse_args()
    if args.initial_template is not None and not args.keep_capabilities:
        parser.error("--initial-template requires --keep-capabilities")
    initial_template = None if args.initial_template is None else load_initial_template(args.initial_template)
    profile = load_config(args.config)
    defaults, source = conversation_defaults(profile)
    agents = {agent["name"]: agent for agent in profile["agents"]}
    names = args.agent or assistant_names(profile)
    unknown = [name for name in names if name not in agents]
    if unknown:
        raise SystemExit(f"not Agents of {config_path(args.config)}: {', '.join(unknown)}")
    print(json.dumps({"config": str(config_path(args.config)), "environment": profile["environment"],
                      "conversation_defaults": source, "agents": names}))
    args.out.mkdir(parents=True, exist_ok=True)
    with AzureCliCredential(subscription=profile["subscription_id"], process_timeout=30) as credential:
        token = credential.get_token(profile["api_audience"] + "/.default").token
    headers = {"Authorization": "Bearer " + token}
    with httpx.Client(base_url=profile["api_url"], timeout=120, trust_env=False, follow_redirects=False) as client:
        for name in names:
            result, wanted = select(client, headers, args.out, agents[name], args.request_id, args.dry_run,
                                    args.keep_capabilities, defaults, initial_template)
            read = client.get(f"/agents/{agents[name]['agent_id']}/work-policy/v1/agent-template/selection",
                              headers=headers)
            current = read.json() if read.status_code == 200 else {}
            # Success is the read-back selection equal to this run's template, not only a 200.
            applied = wanted is not None and current.get("selection_id") is not None and _same(current["template"], wanted)
            result.update(selection_id=current.get("selection_id"), applies_to=current.get("applies_to"), applied=applied)
            print(json.dumps(result))
            if result["outcome"] != "dry-run" and not applied:
                raise SystemExit(f"{name}: the current selection is not this template; reconcile with the same "
                                 "request ID first, use a new one only after that shows nothing was written")
    del token


if __name__ == "__main__":
    main()
