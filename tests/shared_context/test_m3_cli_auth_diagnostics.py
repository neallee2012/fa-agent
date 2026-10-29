"""Real credential failure paths remain offline and emit no private diagnostics."""

import json

import pytest

from test_operation_rejection_cli import cold_cli


@pytest.mark.parametrize("namespace", ["azure", "msal"])
@pytest.mark.parametrize("root_interception", [False, True])
def test_cold_late_child_control_distinguishes_root_interception(namespace, root_interception):
    script = r'''
import json, logging
from shared_context.human_cli_logging import private_auth_logging

assert not logging.getLogger().handlers
root = logging.getLogger(NAMESPACE)
before = (root.handlers, root.propagate, root.disabled)
name = NAMESPACE + ".cold_late_child"
assert name not in logging.root.manager.loggerDict
with private_auth_logging():
    if not INTERCEPT:
        root.handlers = before[0]
        root.propagate = before[1]
    logging.getLogger(name).warning("M3_COLD_LATE_CHILD_CANARY")
assert (root.handlers, root.propagate, root.disabled) == before
print(json.dumps({"late_child_created": name in logging.root.manager.loggerDict,
                  "root_handlers": len(logging.getLogger().handlers)}))
'''
    result = cold_cli("NAMESPACE = " + repr(namespace) + "\nINTERCEPT = " + repr(root_interception) + "\n" + script)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"late_child_created": True, "root_handlers": 0}
    if root_interception:
        assert result.stderr == ""
    else:
        assert result.stderr == "M3_COLD_LATE_CHILD_CANARY\n"
