"""Frozen M4 synthetic fixtures; stdlib only, with the oracle kept separate from seed text."""
from __future__ import annotations

import hashlib
import json

SUITE = "m4-context-01"
POLICY_VERSION = "57f3ccdd2ff512b90ded888d8d9a2ec8"
POLICY_ARTIFACT = "m3-artifacts-01-sop-repair"
POLICY_TEXT = "APPROVED_POLICY=V3"
CONTEXT_BYTES_LIMIT = 32768
PROMPT_TOKENS_LIMIT = 8192
FIXTURE_SHA256 = "8f37192bd3c9aa93d4f7855eb5ec133ba368d136772bba96147c4cf19178c090"
CASES = {
    "short-cold": ("short", "cold"),
    "short-warm": ("short", "warm"),
    "long-warm": ("long", "warm"),
    "long-new-conversation": ("long", "new_conversation"),
    "long-resumed": ("long", "resumed"),
}
GOLD = {
    "decision_owner": ("mira", "work"),
    "report_layout": ("compact", "team_memory"),
    "private_theme": ("cobalt", "private_memory"),
    "policy_marker": (POLICY_TEXT, "policy"),
}
ALIASES = {
    "decision_owner": ("mira",),
    "report_layout": ("compact", "concise", "brief"),
    "private_theme": ("cobalt", "cobalt blue"),
    "policy_marker": (POLICY_TEXT.casefold(),),
}
PROMPT = (
    "Read the current context for Project Lyra. Return only a JSON object with "
    "decision_owner, report_layout, private_theme and policy_marker. Each field "
    "must contain value and citation. Work is authoritative over archived memory. "
    "Use only approved policy and your own private memory. Cite work as "
    "work:<work_id>@<revision>, policy as policy:<artifact_id>@<version_id>, "
    "and retrieved memory as memory:<memory_id>. Do not guess missing facts."
)


def encoded(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, indent=2) + "\n").encode("utf-8")


def fixtures() -> dict:
    short = [
        "Project Lyra uses compact report layouts. This is a continuing team preference.",
        "An archived Lyra decision named Owen as owner. It is superseded; consult the current work record.",
        "Project Lyra is a synthetic evaluation project, with no real customer data.",
    ]
    long = list(short)
    for index in range(125):
        long.append(
            f"Archive entry {index:03d} concerns synthetic project Nebula-{index:03d}, not Project Lyra. "
            "The team catalogued fictional meeting-room chairs, stationery deliveries, garden labels, "
            "training-room layouts, sample calendars and demonstration packing lists. These archived "
            "notes are distractors, not current decisions or approved operating policies. They do not "
            "change the owner of Project Lyra or any agent's private preferences."
        )
    assert len(long) == 128 and 32768 <= len(encoded(long)) <= 65536
    return {
        "schema": 1, "suite_id": SUITE,
        "seed_data": {
            "short_team": short, "long_team": long,
            "private_a": ["Agent A privately prefers the cobalt theme for Project Lyra."],
            "private_b": ["Agent B privately prefers the saffron theme for Project Lyra."],
            "current_work_progress": "Current confirmed Project Lyra decision: owner is Mira. Archived owner assignments are obsolete.",
        },
        "cases": [
            {"case_id": key, "history": history, "lifecycle": lifecycle, "actor": "agent-a", "prompt": PROMPT}
            for key, (history, lifecycle) in CASES.items()
        ],
        "oracle": {
            "answers": {key: {"value": value, "source_kind": kind} for key, (value, kind) in GOLD.items()},
            "answer_aliases": ALIASES,
            "forbidden_in_a": "saffron", "policy_version": POLICY_VERSION,
            "policy_sha256": hashlib.sha256(POLICY_TEXT.encode()).hexdigest(),
            "max_context_bytes": CONTEXT_BYTES_LIMIT, "max_model_input_tokens": PROMPT_TOKENS_LIMIT,
        },
    }
