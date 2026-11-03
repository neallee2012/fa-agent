"""conftest's Windows read cache around the pinned core's local State store: a hit parses like the file, and a file
replaced from outside is read again."""
import json
import os
from pathlib import Path

import pytest
from azure.ai.agentserver.core.storage._local_state import LocalStateStoreBackend

pytestmark = pytest.mark.skipif(os.name != "nt", reason="conftest installs the cache on Windows only")


def test_a_hit_round_trips_like_the_file_and_an_outside_replace_is_read_again(tmp_path, monkeypatch):
    path = tmp_path / "store.json"
    store = LocalStateStoreBackend(path, name="cache", user_isolation=False, item_ttl_seconds=0, description=None,
                                   tags={})
    opened, read_text = [], Path.read_text

    def counted(self, *args, **kwargs):
        opened.append(self)
        return read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", counted)
    with store._lock:
        store._write({"range": (0, 5), "count": 1})
        assert store._read() == {"range": [0, 5], "count": 1}
        assert opened == []
        outside = tmp_path / "outside.json"
        outside.write_text(json.dumps({"range": [0, 5], "count": 2}), encoding="utf-8")
        os.replace(outside, path)
        assert store._read() == {"count": 2, "range": [0, 5]}
        assert opened == [path]
