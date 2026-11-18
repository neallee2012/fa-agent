"""從 test_operation_rejection_cli.py 原封搬出：在乾淨的子程序裡跑一段 CLI 腳本（不帶目前程序的環境）。"""

import os
from pathlib import Path
import subprocess
import sys


def cold_cli(script):
    root = Path(__file__).resolve().parents[2]
    environment = {name: os.environ[name] for name in ("PATH", "SystemRoot", "TEMP", "TMP") if name in os.environ}
    environment.update(PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1",
                       PYTHONPATH=str(root / "src" / "fa-shared-context-poc"))
    return subprocess.run([sys.executable, "-B", "-X", "utf8", "-c", script],
                          env=environment, capture_output=True, text=True, timeout=30)
