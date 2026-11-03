"""Cold-import boundary check (AGENTS.md rule 9): one fresh interpreter imports a whole module list."""

import json
import subprocess
import sys
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[2] / "src" / "fa-shared-context-poc"

_CHILD = r"""
import importlib, importlib.abc, json, sys
plan = json.load(sys.stdin)
sys.path.insert(0, plan["source"])
current, banned, found = None, (), {}

def hit(name):
    return any(name == prefix or name.startswith(prefix + ".") for prefix in banned)

class Ban(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if hit(name):
            found.setdefault(current, []).append(name)
            raise ModuleNotFoundError("banned import: " + name, name=name)

sys.meta_path.insert(0, Ban())
if plan["offline"]:
    def offline(event, args):
        if event == "socket.connect":
            found.setdefault(current, []).append("socket.connect")
            raise ConnectionError("cold import opened a network connection")
    sys.addaudithook(offline)
for current, banned in plan["cases"]:
    banned = tuple(banned)
    # A banned package an earlier module already loaded would be a cache hit, so this module's check would be vacuous.
    early = sorted(name for name in sys.modules if hit(name))
    if early:
        found.setdefault(current, []).append("already loaded before this check: " + ", ".join(early))
        continue
    try:
        importlib.import_module(current)
    except Exception as error:
        found.setdefault(current, []).append(type(error).__name__ + ": " + str(error))
    found.setdefault(current, []).extend(sorted(name for name in sys.modules if hit(name)))
    if not found[current]:
        del found[current]
print(json.dumps(found))
"""


def import_violations(cases, *, offline=False):
    """Imports each ``(module, banned prefixes)`` in order in one ``-I`` interpreter; returns ``{module: problems}``."""
    plan = {"source": str(SOURCE), "offline": offline, "cases": [[module, list(banned)] for module, banned in cases]}
    result = subprocess.run([sys.executable, "-I", "-X", "utf8", "-B", "-c", _CHILD], input=json.dumps(plan),
                            capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout.splitlines()[-1])
