"""Create a profile's Memory Store for private and Team memory (#306, TN-1), the store stage made by hand.

The store name is the profile's assistant_environment FA_PRIVATE_MEMORY_STORE. The definition is the one dev and stage
read back (2026-10-03): chat model gpt-4.1-mini, embedding model text-embedding-3-small, user profile and chat summary
on, no expiry. `--chat-model` names another chat deployment of the project (azd uses the chosen default model; the
store's chat model is any chat deployment per the Foundry memory docs). Memory Store is a Foundry preview API
(azure-ai-projects 2.3.0 `beta.memory_stores`).

Safety (no blind resend): an existing store is only read back, and must have this definition. Otherwise one intent file
(open mode "x") is written before the single create; an existing intent means "read back only", and an unknown create
outcome stops.

usage: python deploy/stage/new_memory_store.py --out <private dir> [--config <profile.json>] [--chat-model <name>]
"""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "shared-context"))
from deployment_config import load_config, write_x as _write_x  # noqa: E402

DEFINITION = {
    "kind": "default", "chat_model": "gpt-4.1-mini", "embedding_model": "text-embedding-3-small",
    "options": {"user_profile_enabled": True, "chat_summary_enabled": True, "default_ttl_seconds": 0},
}


def definition(chat_model=DEFINITION["chat_model"]):
    return {**DEFINITION, "chat_model": chat_model}


def store_name(profile):
    name = (profile.get("assistant_environment") or {}).get("FA_PRIVATE_MEMORY_STORE")
    if not isinstance(name, str) or not name:
        raise SystemExit("the profile's assistant_environment has no FA_PRIVATE_MEMORY_STORE; memory is off")
    return name


def matches(observed, expected):
    observed = observed or {}
    options = observed.get("options") or {}
    return (all(observed.get(key) == expected[key] for key in ("kind", "chat_model", "embedding_model"))
            and all(options.get(key) == value for key, value in expected["options"].items()))


def ensure(stores, out, name, not_found=LookupError, expected=DEFINITION):
    """(outcome, definition) of the store `name`; `stores` is the SDK's beta.memory_stores."""
    def read():
        try:
            return stores.get(name).as_dict()
        except not_found:
            return None

    current = read()
    if current is not None:
        if not matches(current.get("definition"), expected):
            raise SystemExit(f"memory store {name} exists with another definition; it is not changed here")
        return "already", current["definition"]
    intent = out / f"memory-store-{name}-intent.json"
    if intent.exists():
        raise SystemExit(f"{intent.name} exists but the store does not read back; read it again, do not create twice")
    _write_x(intent, {"at": datetime.now(timezone.utc).isoformat(), "store": name, "definition": expected,
                      "automatic_retry": False})
    from azure.ai.projects.models import MemoryStoreDefaultDefinition
    try:
        stores.create(name=name, definition=MemoryStoreDefaultDefinition(expected),
                      description="FA private and Team memory")
    except Exception as error:
        raise SystemExit(f"memory store create outcome unknown ({type(error).__name__}); read it back, do not "
                         "create again") from None
    current = read()
    if current is None or not matches(current.get("definition"), expected):
        raise SystemExit(f"memory store {name} did not read back with this definition")
    return "created", current["definition"]


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--config", help="environment profile; defaults to FA_DEPLOY_CONFIG or deploy/stage/stage.json")
    parser.add_argument("--chat-model", default=DEFINITION["chat_model"], help="a chat deployment of the project")
    args = parser.parse_args(argv)
    from azure.ai.projects import AIProjectClient
    from azure.core.exceptions import ResourceNotFoundError
    from azure.identity import AzureCliCredential

    profile = load_config(args.config, require_runtime=False)
    name = store_name(profile)
    args.out.mkdir(parents=True, exist_ok=True)
    with AzureCliCredential(subscription=profile["subscription_id"], process_timeout=30) as credential:
        with AIProjectClient(endpoint=profile["project_endpoint"], credential=credential, allow_preview=True,
                             retry_total=0, logging_enable=False) as project:
            outcome, observed = ensure(project.beta.memory_stores, args.out, name, ResourceNotFoundError,
                                       definition(args.chat_model))
    print(json.dumps({"memory_store": name, "outcome": outcome, "chat_model": observed.get("chat_model"),
                      "embedding_model": observed.get("embedding_model")}))


if __name__ == "__main__":
    main()
