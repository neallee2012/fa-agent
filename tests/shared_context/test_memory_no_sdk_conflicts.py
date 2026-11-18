"""A scheduled public observer update is not evidence that reviewed knowledge changed."""

import json

import pytest

from shared_context.behavior_wire import SdkNoStart, sdk_refusal_allowed
from instruction_flow_helpers import instruction_models, runtime_path
from test_m3_auth import jwk_client, signing_key
from memory_no_sdk_helpers import PROFILE_V2
from test_memory_no_sdk_wire import literal_disposition


def test_generic_source_comparison_code_cannot_assert_a_v2_no_sdk_disposition():
    assert not sdk_refusal_allowed(PROFILE_V2, "ASSISTANT_SOURCE_CHANGED")
    value = literal_disposition("v2")
    value["gate"]["refusal_code"] = "ASSISTANT_SOURCE_CHANGED"
    with pytest.raises(ValueError):
        SdkNoStart.model_validate_json(json.dumps(value))
    assert sdk_refusal_allowed(PROFILE_V2, "WORK_MEMORY_SOURCE_CHANGED")
    assert sdk_refusal_allowed("fa-sdk-start-gate-v1", "SKILL_SOURCE_CHANGED")
