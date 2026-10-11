"""Original-project deployment facts, not a fixed model or invented token window."""
import json
import os
from pathlib import Path
from typing import Annotated

from pydantic import Field

from .behavior_wire import WireModel
from .behavior_assistant_wire import ModelDeployment
from .errors import Problem
from .behavior_model_capacity import DeclaredModelLimits, ModelCapacitySnapshot


class InstructionModel(WireModel):
    id: ModelDeployment
    display_name: str
    context_window_tokens: Annotated[int, Field(gt=0)] | None = None
    max_output_tokens: Annotated[int, Field(gt=0)] | None = None
    input_microusd_per_million: Annotated[int, Field(ge=0)] | None = None
    output_microusd_per_million: Annotated[int, Field(ge=0)] | None = None
    supports_reasoning_effort: bool | None = None
    supports_reasoning_summary: bool | None = Field(default=None, exclude_if=lambda value:value is None)
    max_prompt_tokens: Annotated[int, Field(gt=0)] | None = Field(default=None, exclude_if=lambda value: value is None)
    supports_image_input: bool | None = Field(default=None, exclude_if=lambda value: value is None)

    def capacity_snapshot(self):
        try:
            limits = DeclaredModelLimits(context_window_tokens=self.context_window_tokens,
                max_prompt_tokens=self.max_prompt_tokens, max_output_tokens=self.max_output_tokens)
            return ModelCapacitySnapshot.from_declared(self.id, limits)
        except ValueError:
            raise Problem(503, "MODEL_CAPACITY_INVALID", "selected server-declared model capacity is invalid or contradictory") from None

    def public(self):
        # Only an explicit image fact adds the key, so the response for every other deployment stays unchanged.
        return {"id":self.id,"display_name":self.display_name,
            "context_window_tokens":self.context_window_tokens,"max_output_tokens":self.max_output_tokens,
            "reasoning_summary_available":self.supports_reasoning_summary is True,
            **({"supports_image_input": True} if self.supports_image_input is True else {})}


class InstructionModels:
    def __init__(self, catalog, facts, default):
        self.models={}
        for entry in catalog:
            if entry.get("provisioning")!="Succeeded" or entry.get("capabilities",{}).get("chatCompletion")!="true":
                continue
            name=entry["deployment"]
            if name in self.models:
                raise Problem(503,"INSTRUCTION_MODEL_CONFIGURATION","duplicate deployment in original project catalog")
            self.models[name]=InstructionModel(id=name,display_name=name,**facts.get(name,{}))
        if not self.models or default not in self.models:
            raise Problem(503,"INSTRUCTION_MODEL_CONFIGURATION","configured default must be an existing chat deployment")
        self.default=default

    def select(self, name):
        selected=self.models.get(self.default if name is None else name)
        if selected is None:
            raise Problem(422,"INSTRUCTION_MODEL_UNAVAILABLE","select a chat deployment from this original project")
        return selected

    def capacity_snapshot(self, name):
        return self.select(name).capacity_snapshot()

    def public(self):
        return {"default_model":self.default,"models":[self.models[k].public() for k in sorted(self.models)]}

    @classmethod
    def from_environment(cls):
        path=os.environ.get("FA_INSTRUCTION_MODEL_CATALOG_FILE")
        default=os.environ.get("FA_INSTRUCTION_DEFAULT_MODEL")
        if not path or not default:
            raise Problem(503,"INSTRUCTION_MODEL_CONFIGURATION","original project deployment catalog and default are required")
        try:
            catalog=json.loads(Path(path).read_text(encoding="utf-8-sig"))
            fact_path=os.environ.get("FA_INSTRUCTION_MODEL_FACTS_FILE")
            facts={} if fact_path is None else json.loads(Path(fact_path).read_text(encoding="utf-8-sig"))
            if type(catalog) is not list or type(facts) is not dict:
                raise ValueError()
            # An environment that deploys only some catalog models offers only those (azd FA_MODELS).
            offered=os.environ.get("FA_INSTRUCTION_MODELS")
            if offered is not None:
                names={name.strip() for name in offered.split(",") if name.strip()}
                if not names or names-{entry["deployment"] for entry in catalog}:
                    raise ValueError()
                catalog=[entry for entry in catalog if entry["deployment"] in names]
            return cls(catalog,facts,default)
        except (OSError,ValueError,KeyError,TypeError):
            raise Problem(503,"INSTRUCTION_MODEL_CONFIGURATION","original deployment facts are unavailable or invalid") from None
