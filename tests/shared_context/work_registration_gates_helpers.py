"""The refusal reader of the retired `test_work_registration_gates` that `test_work_policy_gates_read` still uses."""
import pytest

from shared_context.errors import Problem


def code(call, *args, **kwargs):
    with pytest.raises(Problem) as denied:
        call(*args, **kwargs)
    return denied.value.status, denied.value.body["error"]["code"], str(denied.value)
