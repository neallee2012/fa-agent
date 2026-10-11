import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1] / "src" / "fa-hosted-sandbox-poc"
sys.path.insert(0, str(ROOT))

import smb_diagnostics as diagnostics


class SmbDiagnosticsTests(unittest.TestCase):
    def test_kernel_output_is_numeric_categories_not_raw_credentials(self):
        cases = (
            ("CIFS: VFS: cifs_mount failed w/return code = -2", "mount_failed", -2),
            ("CIFS: VFS: host Send error in SessSetup = -126", "session_setup_failed", -126),
            ("CIFS: VFS: Verify user has a krb5 ticket and keyutils is installed",
             "check_ticket_and_keyutils", None),
            ("CIFS: Status code returned 0xc0000022 STATUS_ACCESS_DENIED",
             "server_status", "0xc0000022"),
        )
        for line, category, code in cases:
            with self.subTest(line=line):
                result = diagnostics.kernel_message(line + " secret=synthetic-do-not-persist")
                self.assertEqual(result["category"], category)
                self.assertEqual(result.get("code"), code)
                self.assertNotIn("synthetic", json.dumps(result))
        for line in (
            "CIFS SPNEGO reply blob: 123456", "CIFS mount data password=not-for-evidence",
            "unrelated line", "CIFS unknown message eyJhbGciOiJIUzI1NiJ9.test.token",
        ):
            self.assertIsNone(diagnostics.kernel_message(line))

    def test_mount_stderr_keeps_only_errno_and_fixed_reason(self):
        result = diagnostics.mount_message(
            "mount error(2): No such file or directory\nAuthorization: Bearer do-not-persist"
        )
        self.assertEqual(result, {"errno": 2, "reason": "ENOENT"})
        self.assertEqual(diagnostics.mount_message("unknown sensitive data"), {"reason": "unclassified"})

    def test_metadata_never_reads_file_contents(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cache"
            path.write_text("do-not-persist", encoding="utf-8")
            with patch.object(Path, "read_bytes", side_effect=AssertionError("no credential reads")):
                result = diagnostics.path_metadata(path)
            self.assertTrue(result["present"])
            self.assertTrue(result["regular_file"])
            self.assertNotIn("do-not-persist", json.dumps(result))
            self.assertFalse(diagnostics.path_metadata(path.parent / "missing")["present"])

    def test_curl_metadata_never_returns_raw_output(self):
        completed = subprocess.CompletedProcess([], 28, "000||0.000010|0.000000|0.000000|3.001234")
        result = diagnostics.curl_metadata(completed)
        self.assertEqual(result["return_code"], 28)
        self.assertEqual(result["http_status"], 0)
        self.assertIsNone(result["remote_ip"])
        self.assertEqual(result["total_seconds"], 3.001234)
        completed.stdout = "400|10.0.0.85|0.0001|0.0002|0.0003|0.0004"
        self.assertEqual(diagnostics.curl_metadata(completed)["remote_ip"], "10.0.0.85")
        completed.stdout += "\nsynthetic-secret"
        self.assertEqual(diagnostics.curl_metadata(completed)["status"], "invalid_metrics")
        self.assertNotIn("synthetic", json.dumps(diagnostics.curl_metadata(completed)))

    def test_network_child_is_bounded_and_keeps_errors_numeric(self):
        with patch.object(diagnostics.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 7, "do-not-persist")
            self.assertEqual(diagnostics.network_probe(), {"status": "probe_failed", "return_code": 7})
            self.assertEqual(run.call_args.kwargs["timeout"], 26)
            self.assertEqual(run.call_args.kwargs["stderr"], subprocess.DEVNULL)
            self.assertEqual(run.call_args.args[0][-1], "--network-probe")

    def test_key_service_distinguishes_local_keys_from_missing_callout(self):
        library = Mock()
        library.keyctl_join_session_keyring.return_value = 10
        library.add_key.return_value = 11
        library.keyctl_unlink.return_value = 0
        library.request_key.return_value = -1
        with patch("ctypes.CDLL", return_value=library), patch("ctypes.get_errno", return_value=2):
            result = diagnostics.key_service_sample()
        library.keyctl_join_session_keyring.assert_called_once_with(None)
        library.keyctl_unlink.assert_called_once_with(11, 10)
        self.assertEqual(library.request_key.call_args.args[-1], 10)
        self.assertTrue(library.request_key.call_args.args[1].startswith(b"fa-poc-callout:"))
        self.assertEqual(result["local_add"]["status"], "success")
        self.assertEqual(result["request_key"], {"status": "failed", "errno": 2})
        library.keyctl_read.assert_not_called()
        library.request_key.return_value = 12

        def read_own_payload(key, buffer, size):
            self.assertEqual(key, 12)
            buffer.raw = b"fa-poc-probe\n".ljust(size, b"\0")
            return 13

        library.keyctl_read.side_effect = read_own_payload
        with patch("ctypes.CDLL", return_value=library):
            result = diagnostics.key_service_sample()
        self.assertTrue(result["request_key"]["payload_matches"])
        self.assertEqual(result["request_key"]["unlink_return_code"], 0)

    @unittest.skipUnless(os.name == "posix", "Linux request-key filesystem contract")
    def test_wrapper_records_entry_exit_without_command_output(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(diagnostics, "LOG_ROOT", Path(directory) / "diag"):
                diagnostics.begin(Path(directory))
                with patch.object(diagnostics, "process_metadata", return_value={"uid": 0}), \
                     patch.object(diagnostics.subprocess, "run") as run:
                    run.return_value = subprocess.CompletedProcess([], 7)
                    self.assertEqual(diagnostics.upcall("123"), 7)
                events = diagnostics.finish()["events"]
                self.assertEqual([event["event"] for event in events], ["entry", "exit"])
                self.assertEqual(events[-1]["return_code"], 7)
                self.assertEqual(run.call_args.args[0], [diagnostics.UPCALL, "123"])
                self.assertEqual(run.call_args.kwargs["stdout"], subprocess.DEVNULL)
                self.assertEqual(run.call_args.kwargs["stderr"], subprocess.DEVNULL)

    def test_image_uses_existing_immutable_dependencies(self):
        image = (ROOT / "Dockerfile.smb-diagnostics").read_text()
        self.assertIn("@sha256:10b6f915a0f425c93e7acd056d536bd704cdb49e4059693f94ebff08364e95b9", image)
        self.assertIn("ENV POC_MI_MOUNT_DIAGNOSTICS=1", image)
        self.assertIn("smb_diagnostics.py --self-check", image)
        self.assertNotIn("apt-get", image)
        self.assertNotIn("pip install", image)


if __name__ == "__main__":
    unittest.main()
