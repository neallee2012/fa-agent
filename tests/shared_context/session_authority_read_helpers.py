"""從 test_session_authority_read.py 原封搬出的共用 helper，讓留存測試不依賴待刪的舊路測試檔。"""

from pathlib import Path
import pytest


@pytest.fixture
def runtime_path():
    return Path(__file__)
