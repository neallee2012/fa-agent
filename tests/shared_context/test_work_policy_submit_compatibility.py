"""The shared HTTP route preserves legacy bytes without restricting new instructions."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from shared_context.behavior_assistant_wire import AssistantSubmit, InstructionSubmit, Task
from shared_context.behavior_work_policy import derive_work
from shared_context.behavior_work_policy_api import install_work_policy_routes
from assistant_api_helpers import assistant_api
from instruction_flow_helpers import instruction_models
from test_m3_auth import jwk_client, signing_key
from work_policy_lifecycle_helpers import enabled


@pytest.fixture
def runtime_path():
    return Path(__file__)  # Admission only: no runtime, provider or model is started.


def route(assistant_api):
    h, control, human, _ = enabled(assistant_api)
    install_work_policy_routes(h.app, SimpleNamespace(
        runtime=h.runtime, verifier=h.verifier, installation=lambda: None,
    ))
    return h, control, human, []


def post(h, action, command):
    return h.client.post("/work-policy/v1/" + action, content=command.model_dump_json(), headers=h.headers())
