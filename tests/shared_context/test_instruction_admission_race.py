"""Deterministic signed admission interleavings; no SDK, model or external network."""

from concurrent.futures import ThreadPoolExecutor
import copy
from threading import Event
from types import SimpleNamespace

import httpx
import pytest

from shared_context.behavior_assistant_wire import InstructionSubmit
from shared_context.behavior_migration import MigrationStore
from shared_context.behavior_operations import control_key, _row
from shared_context.behavior_work_policy import CostLedger, WorkPolicyControl, derive_work_binding
from shared_context.errors import Problem
from assistant_api_helpers import assistant_api
from instruction_flow_helpers import instruction_models
from test_m3_auth import jwk_client, signing_key
from test_web_test_console_attachments import draft, picker, submit as console_submit
from test_web_test_console_frozen_interface import open_ui
from test_work_cost_policy import test_original_scope_and_spent_high_water_cannot_regress as check_high_water
from web_test_console.adapter import canonical, sha


def test_original_budget_scope_and_high_water_without_runtime(assistant_api):
    check_high_water(assistant_api)
