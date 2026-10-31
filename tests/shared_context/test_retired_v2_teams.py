"""The v2 multi-channel Teams installation is gone; what it left behind refuses instead of passing silently."""
import json

import pytest
from pydantic import ValidationError

from shared_context.behavior_runtime import RuntimeBinding
from shared_context.behavior_server_config import ServerConfigurationDocument, load_configuration, synthetic_configuration
from shared_context.errors import Problem


def test_a_v2_configuration_document_refuses_by_name():
    raw = synthetic_configuration().model_dump(mode="json")
    raw.update(schema_version="fa-behavior-server-v2", channels={})
    with pytest.raises(Problem) as refused:
        load_configuration(environ={"BEHAVIOR_SERVER_CONFIG_JSON": json.dumps(raw)})
    assert (refused.value.status, refused.value.body["error"]["code"]) == (503, "BEHAVIOR_CONFIGURATION")


def test_a_v1_configuration_document_still_parses():
    config = synthetic_configuration()
    parsed = ServerConfigurationDocument.model_validate_json(json.dumps(config.model_dump(mode="json")))
    assert parsed.sha256 == config.sha256


def test_a_stored_msteams_binding_refuses_at_parse():
    binding = synthetic_configuration().bindings[0].model_dump(mode="json")
    binding["route"]["channel_id"] = "msteams"
    with pytest.raises(ValidationError, match="validation error"):
        RuntimeBinding.model_validate_json(json.dumps(binding))
