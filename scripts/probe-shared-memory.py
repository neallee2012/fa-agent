# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "azure-ai-projects==2.3.0",
#   "azure-identity==1.25.3",
#   "httpx==0.28.1",
# ]
# ///
"""Run one operator-only extraction/search probe, never a Hosted acceptance test."""

import argparse
import json
import os
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path
from uuid import uuid4

from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import MemorySearchOptions
from azure.core.exceptions import AzureError
from azure.identity import AzureCliCredential


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--store", required=True)
    parser.add_argument("--subscription", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--resume-from", type=Path, help="Search a completed, not-yet-searched update")
    args = parser.parse_args()
    if version("azure-ai-projects") != "2.3.0":
        raise RuntimeError("This probe requires azure-ai-projects==2.3.0")
    if not os.environ.get("AZURE_CONFIG_DIR"):
        raise RuntimeError("Set the isolated AZURE_CONFIG_DIR before running")
    if args.output.exists():
        raise FileExistsError(args.output)
    if not args.output.parent.is_dir():
        raise FileNotFoundError(args.output.parent)

    scope = f"compat-{uuid4().hex}"
    evidence = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "endpoint": args.endpoint,
        "store": args.store,
        "scope": scope,
        "status": "STARTED",
        "update_attempts": 0,
        "search_attempts": 0,
        "boundary": "Operator-only single-scope probe; not Hosted or cross-agent acceptance.",
    }
    if args.resume_from:
        previous = json.loads(args.resume_from.read_text(encoding="utf-8"))
        if (
            previous["endpoint"] != args.endpoint
            or previous["store"] != args.store
            or previous.get("update_status") != "completed"
            or previous.get("update_attempts") != 1
            or previous.get("search_attempts") != 0
            or not previous["scope"].startswith("compat-")
        ):
            raise ValueError("Resume requires a matching completed update with no search attempt")
        scope = previous["scope"]
        evidence.update({
            "scope": scope,
            "update_attempts": 1,
            "update_id": previous["update_id"],
            "update_status": previous["update_status"],
            "operations": previous["operations"],
            "update_usage": previous["update_usage"],
            "resume_from": str(args.resume_from),
            "previous_probe_status": previous["status"],
            "updates_submitted_this_process": 0,
        })

    def save() -> None:
        args.output.write_text(json.dumps(evidence, indent=2), encoding="utf-8")

    save()
    try:
        with AzureCliCredential(subscription=args.subscription) as credential:
            with AIProjectClient(
                endpoint=args.endpoint,
                credential=credential,
                allow_preview=True,
                retry_total=0,
                connection_timeout=10,
                read_timeout=30,
            ) as project:
                if not args.resume_from:
                    evidence["update_attempts"] = 1
                    evidence["updates_submitted_this_process"] = 1
                    save()
                    poller = project.beta.memory_stores.begin_update_memories(
                        name=args.store,
                        scope=scope,
                        items=[{
                            "type": "message",
                            "role": "user",
                            "content": (
                                "For this synthetic POC, our team's preferred dashboard "
                                "theme is sepia. This is a stable team preference."
                            ),
                        }],
                        update_delay=0,
                        polling_interval=5,
                    )
                    evidence["update_id"] = poller.update_id
                    save()
                    print(json.dumps({"scope": scope, "update_id": poller.update_id}), flush=True)
                    poller.wait(timeout=180)
                    evidence["update_status"] = poller.status()
                    if not poller.done():
                        raise TimeoutError(
                            "CLIENT_WAIT_TIMEOUT: server processing may continue; "
                            "retain update_id and do not resubmit."
                        )
                    if poller.status() != "completed":
                        raise RuntimeError(f"UPDATE_NOT_COMPLETED: {poller.status()}")

                    updated = poller.result(timeout=0)
                    evidence["operations"] = [{
                        "kind": operation.kind,
                        "memory_id": operation.memory_item.memory_id,
                        "scope": operation.memory_item.scope,
                        "content": operation.memory_item.content,
                    } for operation in updated.memory_operations]
                    evidence["update_usage"] = {
                        key: getattr(updated.usage, key)
                        for key in ("embedding_tokens", "input_tokens", "output_tokens", "total_tokens")
                    }
                written_ids = {
                    operation["memory_id"]
                    for operation in evidence["operations"]
                    if operation["kind"] in ("created", "updated")
                    and operation["scope"] == scope
                }
                save()
                if not written_ids:
                    raise RuntimeError("NO_EXTRACTED_MEMORY: no search submitted")

                evidence["search_attempts"] = 1
                save()
                found = project.beta.memory_stores.search_memories(
                    name=args.store,
                    scope=scope,
                    items=[{
                        "type": "message",
                        "role": "user",
                        "content": "What is our team's preferred dashboard theme?",
                    }],
                    options=MemorySearchOptions(max_memories=5),
                )
                evidence["search_id"] = found.search_id
                evidence["search_usage"] = {
                    key: getattr(found.usage, key)
                    for key in ("embedding_tokens", "input_tokens", "output_tokens", "total_tokens")
                }
                evidence["memories"] = [{
                    "memory_id": hit.memory_item.memory_id,
                    "scope": hit.memory_item.scope,
                    "content": hit.memory_item.content,
                } for hit in found.memories]
                matched = any(
                    hit.memory_item.scope == scope
                    and hit.memory_item.memory_id in written_ids
                    and "sepia" in hit.memory_item.content.casefold()
                    for hit in found.memories
                )
                if not matched:
                    raise RuntimeError("RECALL_NOT_CONFIRMED: do not auto-retry")
                evidence["status"] = "PASS_SINGLE_SCOPE_OPERATOR_MEMORY_PROBE"
    except (AzureError, RuntimeError, TimeoutError) as error:
        evidence["status"] = "PROBE_NOT_PASSED"
        evidence["error_type"] = type(error).__name__
        evidence["error"] = str(error)
        raise
    finally:
        evidence["finished_at"] = datetime.now(timezone.utc).isoformat()
        save()
        print(json.dumps(evidence, indent=2), flush=True)


if __name__ == "__main__":
    main()
