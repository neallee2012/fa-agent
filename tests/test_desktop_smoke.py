from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

AGENT_ROOT = Path(__file__).resolve().parents[1] / "src" / "fa-hosted-sandbox-poc"
sys.path.insert(0, str(AGENT_ROOT))

from desktop_smoke import _send_gui_input, _verify_completion  # noqa: E402


class DesktopSmokeTests(unittest.TestCase):
    def test_type_does_not_consume_following_key_commands(self) -> None:
        with patch(
            "desktop_smoke.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0, "", ""),
        ) as run:
            _send_gui_input("1234", "unique-nonce", {"DISPLAY": ":91"})
        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(
            commands,
            [
                ["xdotool", "windowactivate", "--sync", "1234",
                 "windowfocus", "--sync", "1234"],
                ["xdotool", "type", "--clearmodifiers", "--delay", "20", "unique-nonce"],
                ["xdotool", "key", "--clearmodifiers", "Tab", "Return"],
            ],
        )
        for call in run.call_args_list:
            self.assertTrue(call.kwargs["check"])
            self.assertLessEqual(call.kwargs["timeout"], 15)

    def test_gui_result_must_match_nonce_and_download_bytes(self) -> None:
        _verify_completion("nonce", "nonce", b"expected\n", b"expected\n")
        with self.assertRaisesRegex(RuntimeError, "nonce"):
            _verify_completion("nonce", "nonce key Tab key Return", b"x", b"x")
        with self.assertRaisesRegex(RuntimeError, "download"):
            _verify_completion("nonce", "nonce", b"wrong", b"expected")


if __name__ == "__main__":
    unittest.main()
