"""Which catalog models an azd environment deploys and offers (docs/azd-new-environment.md, 模型與區域).

One choice drives the Foundry deployments (Bicep), the Portal menu and default (the API's FA_INSTRUCTION_MODELS and
FA_INSTRUCTION_DEFAULT_MODEL) and the memory store: FA_MODELS lists chat deployments from
src/fa-shared-context-poc/model-catalog.json and FA_DEFAULT_MODEL is one of them. The memory store uses a chosen chat
model too, so no model is required beyond the choice, except the embedding deployment memory needs. With the Cosmos DB
memory (FA_NETWORK_MODE=private, ADR-0011) the same chat model is the Hosted extraction model.

Pure and offline; fa_azd.py asks the person and reads the region's offer and quota.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

CATALOG = Path(__file__).resolve().parents[2] / "src" / "fa-shared-context-poc" / "model-catalog.json"
PREFERRED_DEFAULT = "gpt-6.1-sol"
# Environments provisioned before this choice existed deployed every catalog model, and their memory store uses this
# chat model; a store keeps the model it was created with.
LEGACY_MEMORY_CHAT_MODEL = "gpt-4.1-mini"


def catalog(path: Path = CATALOG) -> list[dict[str, Any]]:
    return json.loads(path.read_text(encoding="utf-8"))


def chat_names(entries: list[dict[str, Any]]) -> list[str]:
    """The same filter the API applies (behavior_instruction_models.InstructionModels)."""
    return [item["deployment"] for item in entries if item.get("capabilities", {}).get("chatCompletion") == "true"]


def embedding_names(entries: list[dict[str, Any]]) -> list[str]:
    return [item["deployment"] for item in entries if item.get("capabilities", {}).get("embeddings") == "true"]


def names(raw: str | None) -> tuple[str, ...]:
    return tuple(dict.fromkeys(part.strip() for part in (raw or "").split(",") if part.strip()))


def default_for(models: tuple[str, ...]) -> str:
    return PREFERRED_DEFAULT if PREFERRED_DEFAULT in models else models[0]


def before_choice(values: Mapping[str, str]) -> bool:
    """Provisioned (a Bicep output exists) but never chose: the hook writes the choice before the first provision."""
    return bool(values.get("FA_FOUNDRY_ACCOUNT")) and not values.get("FA_MODEL_DEPLOYMENTS")


def memory_on(values: Mapping[str, str]) -> bool:
    return (values.get("FA_ENABLE_MEMORY") or "true").strip().lower() != "false"


def memory_backend(values: Mapping[str, str]) -> str:
    """FA_MEMORY_BACKEND (ADR-0011): cosmos for FA_NETWORK_MODE=private with memory on, where the Foundry Memory Store
    fails; foundry otherwise (the Memory Store, or memory off). The network mode is fixed, so the backend changes only
    with the memory switch; no memory moves between the two."""
    return "cosmos" if values.get("FA_NETWORK_MODE") == "private" and memory_on(values) else "foundry"


@dataclass(frozen=True)
class Choice:
    models: tuple[str, ...]
    default: str
    memory_chat_model: str | None
    embedding: str | None

    @property
    def deployments(self) -> tuple[str, ...]:
        return self.models + ((self.embedding,) if self.embedding else ())

    def environment(self) -> dict[str, str]:
        """The azd values the hook writes back, so Bicep, the profile and later runs read one decision."""
        values = {"FA_MODELS": ",".join(self.models), "FA_DEFAULT_MODEL": self.default,
                  "FA_MODEL_DEPLOYMENTS": ",".join(self.deployments)}
        if self.memory_chat_model:
            values["FA_MEMORY_CHAT_MODEL"] = self.memory_chat_model
        return values


def parse(values: Mapping[str, str], entries: list[dict[str, Any]]) -> Choice:
    chat = chat_names(entries)
    models = names(values.get("FA_MODELS")) or tuple(chat)
    unknown = [name for name in models if name not in chat]
    if unknown:
        raise SystemExit(f"FA_MODELS names models outside the catalog's chat models ({', '.join(chat)}): "
                         + ", ".join(unknown))
    default = (values.get("FA_DEFAULT_MODEL") or "").strip() or default_for(models)
    if default not in models:
        raise SystemExit(f"FA_DEFAULT_MODEL {default} must be one of FA_MODELS ({', '.join(models)})")
    if not memory_on(values):
        return Choice(models, default, None, None)
    memory = (values.get("FA_MEMORY_CHAT_MODEL") or "").strip()
    if not memory:
        memory = LEGACY_MEMORY_CHAT_MODEL if before_choice(values) else default
    if memory not in models:
        raise SystemExit(f"the memory store uses {memory}; keep it in FA_MODELS (a store keeps its chat model)")
    embedding = embedding_names(entries)
    if len(embedding) != 1:
        raise SystemExit("the model catalog must have exactly one embedding deployment for memory")
    return Choice(models, default, memory, embedding[0])
