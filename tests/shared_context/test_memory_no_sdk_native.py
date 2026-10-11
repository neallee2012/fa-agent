"""One fixed-SDK memory-v2 normal-entry control; no paid provider or Hosted process."""

from test_sdk_no_start_native import (
    assistant, instruction_models, jwk_client, runtime_path, signing_key,
    test_fixed_sdk_constructs_only_after_durable_entry_intent_and_keeps_real_stop as verify_normal_entry,
)


def test_fixed_sdk_memory_v2_keeps_entry_before_constructor_and_real_stop(assistant, monkeypatch):
    verify_normal_entry(assistant, monkeypatch, profile="fa-sdk-start-gate-v2")
