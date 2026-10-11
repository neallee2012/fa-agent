"""An environment that deploys only some catalog models offers only those (FA_INSTRUCTION_MODELS, azd FA_MODELS)."""

from pathlib import Path

import pytest

from shared_context.behavior_instruction_models import InstructionModels
from shared_context.errors import Problem

CATALOG = Path(__file__).resolve().parents[2] / "src" / "fa-shared-context-poc" / "model-catalog.json"


@pytest.fixture
def catalog(monkeypatch):
    monkeypatch.setenv("FA_INSTRUCTION_MODEL_CATALOG_FILE", str(CATALOG))
    monkeypatch.delenv("FA_INSTRUCTION_MODEL_FACTS_FILE", raising=False)
    monkeypatch.delenv("FA_INSTRUCTION_MODELS", raising=False)
    return monkeypatch


def offered(models):
    return [item["id"] for item in models.public()["models"]]


def test_without_a_list_every_catalog_chat_model_is_offered(catalog):
    catalog.setenv("FA_INSTRUCTION_DEFAULT_MODEL", "gpt-6.1-sol")
    assert offered(InstructionModels.from_environment()) == ["gpt-4.1-mini", "gpt-5.4-mini", "gpt-6-luna",
                                                             "gpt-6.1-sol"]


def test_the_list_narrows_the_menu_and_refuses_other_models(catalog):
    catalog.setenv("FA_INSTRUCTION_DEFAULT_MODEL", "gpt-5.4-mini")
    catalog.setenv("FA_INSTRUCTION_MODELS", "gpt-6-luna, gpt-5.4-mini")
    models = InstructionModels.from_environment()
    assert offered(models) == ["gpt-5.4-mini", "gpt-6-luna"]
    assert models.public()["default_model"] == "gpt-5.4-mini"
    with pytest.raises(Problem, match="select a chat deployment"):
        models.select("gpt-6.1-sol")


@pytest.mark.parametrize("listed, default", [
    ("", "gpt-5.4-mini"),                         # an empty list offers nothing
    ("gpt-5.4-mini,gpt-9", "gpt-5.4-mini"),       # a name the catalog does not have
    ("gpt-5.4-mini", "gpt-6.1-sol"),              # the default is not offered
])
def test_a_list_that_cannot_serve_is_a_configuration_error(catalog, listed, default):
    catalog.setenv("FA_INSTRUCTION_DEFAULT_MODEL", default)
    catalog.setenv("FA_INSTRUCTION_MODELS", listed)
    with pytest.raises(Problem, match="original|default"):
        InstructionModels.from_environment()
