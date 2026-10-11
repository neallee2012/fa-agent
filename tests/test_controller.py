from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "controller"))
from g3_only import verify_result  # noqa: E402
from run_poc import run, verify_io  # noqa: E402


class ControllerTests(unittest.TestCase):
    def test_shared_read_must_match_controller_bytes(self) -> None:
        digest = hashlib.sha256(b"v1").hexdigest()
        artifacts = {}
        verify_io(
            {"action": "write_text", "relative_path": "x.txt", "content": "v1"},
            {"sha256": digest, "size": 2}, artifacts,
        )
        with self.assertRaisesRegex(RuntimeError, "response bytes"):
            verify_io(
                {"action": "read_text", "relative_path": "x.txt"},
                {"sha256": digest, "size": 2, "content": "v2"}, artifacts,
            )

    def test_private_marker_leak_is_not_a_pass(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "Private marker"):
            verify_io({"action": "check_private_marker_absent"}, {"absent": False}, {})

    def test_second_session_create_failure_stops_the_first(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            args = argparse.Namespace(
                output=Path(directory), run_id="test-run", poc_id="poc",
                project_endpoint="https://project.invalid", agent_name="agent",
                agent_version="5", image_digest="sha256:test",
                expected_source="//approved.file.core.windows.net/team",
            )
            with patch("run_poc.HostedPocClient") as client_type:
                client = client_type.return_value
                client.create_session.side_effect = [
                    {"agent_session_id": "session-a"}, requests.HTTPError("create B failed"),
                ]
                client.stop_session.return_value = {"stopped": True}
                client.capture_session_logs.return_value = {"bytes": 0}
                result = run(args)
                client.stop_session.assert_called_once_with("session-a")
                client.invoke.assert_not_called()
            saved = json.loads((Path(directory) / "test-run" / "run-manifest.json").read_text())
            self.assertEqual(saved["sessions"], {"A": "session-a"})
            self.assertEqual(result["gates"]["G0"]["status"], "BLOCKED")
            self.assertEqual(result["gates"]["G1"]["status"], "NOT_RUN_DEPENDENCY")

    def test_g0_failure_never_runs_desktop_and_stops_both_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            args = argparse.Namespace(
                output=Path(directory), run_id="test-run", poc_id="poc",
                project_endpoint="https://project.invalid", agent_name="agent",
                agent_version="5", image_digest="sha256:test",
                expected_source="//approved.file.core.windows.net/team",
            )
            with patch("run_poc.HostedPocClient") as client_type:
                client = client_type.return_value
                client.create_session.side_effect = [
                    {"agent_session_id": "session-a"}, {"agent_session_id": "session-b"},
                ]
                client.invoke.return_value = {"status_code": 400, "body": {"error": "blocked"}}
                client.stop_session.return_value = {"stopped": True}
                client.capture_session_logs.return_value = {"bytes": 0}
                result = run(args)
                self.assertEqual(client.invoke.call_count, 1)
                self.assertEqual(client.stop_session.call_count, 2)
                self.assertEqual(result["gates"]["G3"]["status"], "NOT_RUN_DEPENDENCY")

    def test_headless_variants_cannot_pass_g3(self) -> None:
        result = {
            "completed_value": "nonce", "page_ready": True, "browser_uid": 10001,
            "browser_sandbox_disabled": False, "loopback_url": "http://127.0.0.1:1234/",
            "browser_command_line": ["chromium", "--headless=new"],
            "cleanup": [{"return_code": 0}] * 3,
        }
        with self.assertRaisesRegex(RuntimeError, "headed"):
            verify_result(result, "nonce")


if __name__ == "__main__":
    unittest.main()
