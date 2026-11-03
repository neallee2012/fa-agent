"""Local fast lane: tests/shared_context minus the node ids Linux CI deselects (ci-quarantine.txt and
known-failing-on-main.txt, parsed as .github/workflows/shared-context-tests.yml does). Run from anywhere:
``python -X utf8 tests/shared_context/fast_lane.py -q -p no:cacheprovider [more pytest args]``; relative paths in
the extra args resolve from the repository root. In-process ``pytest.main``: Windows cannot take 462 ``--deselect``
flags on one command line."""
import os
import sys
from pathlib import Path

import pytest

here = Path(__file__).resolve().parent
ids = [id_ for name in ("ci-quarantine.txt", "known-failing-on-main.txt")
       for id_ in (line.split("#", 1)[0].strip() for line in (here / name).read_text(encoding="utf-8").splitlines()) if id_]
# The listed ids are relative to the repository root; from any other cwd pytest would silently match none of them.
os.chdir(here.parents[1])
print(f"fast_lane: deselecting {len(ids)} ids", flush=True)
sys.exit(pytest.main(["tests/shared_context", *(f"--deselect={id_}" for id_ in ids), *sys.argv[1:]]))
