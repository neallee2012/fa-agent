"""Offline connection files from one original fresh plan, not proof of installation."""

import argparse
import json
from pathlib import Path
import sys

from shared_context.behavior_fresh_installation import FreshInstallationPlan, _check_plan
from shared_context.behavior_operations import control_key
from shared_context.behavior_server_config import load_configuration, read_document
from shared_context.behavior_work_policy import derive_work
from shared_context.errors import Problem

from .adapter import CliSettings, ConsoleError, FixedTask, canonical, sha
from .agent_connections import AgentConnection, AgentConnections
from .agent_onboarding import _create_only
from .instructions import NATIVE_LANE, InstructionConnection
from .standing import StandingConsoleConfig


def _cli_settings(executable, azure_config_dir, subscription_id):
    if executable is None and azure_config_dir is None and subscription_id is None:
        return None
    # A missing value fails CliSettings, so the three options are all-or-none.
    cli = CliSettings(executable=executable, azure_config_dir=azure_config_dir, subscription_id=subscription_id)
    file, directory = Path(executable), Path(azure_config_dir)
    # Same condition as CliTokenProvider.
    if not (file.is_absolute() and file.is_file() and directory.is_absolute() and directory.is_dir()):
        raise ValueError("cli paths must be an existing absolute file and directory")
    return cli


def export_connections(config, plan, *, backend_origin, client_id, output_directory, cli):
    _check_plan(config, plan)
    root = Path(output_directory).resolve()
    entries = []
    for target, policy in ((config.targets[0], plan.inputs.policy), (config.targets[1], plan.peer_policy)):
        seed = control_key("fresh-console", plan.installation_id, target.agent_id)
        rid = control_key("fresh-console-submit", seed)
        _, binding, _ = derive_work(policy, rid, policy.evidence_mode)
        task = FixedTask(
            task_id=seed, label=target.agent_name, binding_id=binding.binding_id, job_id=binding.job_id,
            work_id=binding.work.work_id, event_id=binding.event_id, root_request_id=binding.root_request_id,
            obligation_id=binding.obligation_id, owner_record_id=binding.owner.owner_record_id,
            route=binding.route.model_dump(), channel_version=config.delivery.channel.version,
            submit_request_id=rid, fetch_request_id=control_key("fresh-console-fetch", seed),
            ack_request_id=control_key("fresh-console-ack", seed), allowed_actions=("status",),
        )
        connection = StandingConsoleConfig(
            authorization_mode="backend-per-work", console_id=seed, backend_origin=backend_origin,
            tenant_id=config.identity.tenant_id, human_oid=config.identity.human_oid,
            client_id=client_id, audience=config.identity.audience, backend_configuration_sha256=config.sha256,
            backend_core_sha256=plan.code_sha256, cli=cli, tasks=(task,),
            standing_policy=policy, standing_policy_sha256=policy.sha256,
        )
        instructions = InstructionConnection(
            schema_version="fa-console-instructions-connection-v1", configuration_sha256=connection.sha256,
            backend_core_sha256=plan.code_sha256, state_profile=NATIVE_LANE,
        )
        directory = root / target.agent_id
        config_path, instructions_path = directory / "config.json", directory / "instructions.json"
        config_bytes, instructions_bytes = canonical(connection.model_dump(mode="json")), canonical(instructions.model_dump(mode="json"))
        _create_only(config_path, config_bytes)
        _create_only(instructions_path, instructions_bytes)
        entries.append(AgentConnection(
            agent_id=target.agent_id, config_path=str(config_path), config_sha256=sha(config_bytes),
            journal_path=str(directory / "journal.sqlite3"), instructions_path=str(instructions_path),
            instructions_sha256=sha(instructions_bytes),
        ))
    manifest = AgentConnections(default_agent_id=config.targets[0].agent_id, agents=tuple(entries))
    _create_only(root / "agents.json", canonical(manifest.model_dump(mode="json")))
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description="Offline A/B standing-v3 connection export; no Table or cloud access.")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--backend-origin", required=True)
    parser.add_argument("--client-id", required=True, help="An already approved delegated client; this does not authorize it.")
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("--cli-executable", help="Absolute az path; give all three --cli-* options or none.")
    parser.add_argument("--cli-azure-config-dir", help="Absolute AZURE_CONFIG_DIR for local console sign-in.")
    parser.add_argument("--cli-subscription-id")
    args = parser.parse_args(argv)
    try:
        manifest = export_connections(
            load_configuration(args.config), read_document(args.plan, FreshInstallationPlan),
            backend_origin=args.backend_origin, client_id=args.client_id, output_directory=args.output_directory,
            cli=_cli_settings(args.cli_executable, args.cli_azure_config_dir, args.cli_subscription_id),
        )
        print(json.dumps({"state": "connections-exported", "agents": len(manifest.agents),
                          "installation_verified": False, "table_writes": 0}, separators=(",", ":")))
        return 0
    except (Problem, ConsoleError, ValueError, OSError):
        print('{"error":{"code":"FRESH_CONSOLE_EXPORT_REFUSED"}}', file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
