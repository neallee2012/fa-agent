from __future__ import annotations

import argparse
import json
import base64
from contextlib import ExitStack
import errno
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, Mock, patch

import sys

AGENT_ROOT = Path(__file__).resolve().parents[1] / "src" / "fa-hosted-sandbox-poc"
sys.path.insert(0, str(AGENT_ROOT))

from poc_core import PocDispatcher, PocError, safe_path  # noqa: E402
from smb_oauth import SmbOAuthError  # noqa: E402

# Captured from vnet-g0-20260913T0414Z/A-console.sse.
MOUNT_ERROR_13_STDERR = (
    "mount error(13): Permission denied\n"
    "Refer to the mount.cifs(8) manual page (e.g. man mount.cifs) and kernel log messages (dmesg)\n"
)


class SafePathTests(unittest.TestCase):
    def test_accepts_nested_relative_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(
                safe_path(root, "folder/file.txt"),
                root / "folder" / "file.txt",
            )

    def test_rejects_parent_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(PocError):
                safe_path(Path(directory), "../secret.txt")

    def test_rejects_absolute_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(PocError):
                safe_path(Path(directory), "/etc/passwd")

    def test_rejects_symlink_target(self) -> None:
        if not hasattr(os, "symlink"):
            self.skipTest("symlink is unavailable")
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside_directory:
            root = Path(directory)
            outside = Path(outside_directory)
            link = root / "link"
            try:
                os.symlink(outside, link, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"symlink creation is unavailable: {exc}")
            with self.assertRaises(PocError):
                safe_path(root, "link/file.txt")


class DispatcherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        base = Path(self.temporary.name)
        self.home = base / "home"
        self.shared = base / "team"
        self.runtime = base / "runtime"
        self.dispatcher = PocDispatcher(
            home=self.home,
            shared_root=self.shared,
            runtime_root=self.runtime,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def request(operation: str, **values: object) -> dict[str, object]:
        return {
            "schema_version": "1.0",
            "poc_id": "poc-1",
            "run_id": "run-1",
            "operation": operation,
            **values,
        }

    def test_rejects_unknown_operation(self) -> None:
        with self.assertRaises(PocError) as context:
            self.dispatcher.dispatch(self.request("shell"))
        self.assertEqual(context.exception.reason_code, "INVALID_REQUEST")

    def test_desktop_publish_requires_the_approved_mount(self) -> None:
        with patch("poc_core.mount_details", return_value={"mounted": False}):
            with patch("poc_core.run_desktop_smoke") as desktop:
                with self.assertRaises(PocError):
                    self.dispatcher.dispatch(
                        self.request("desktop_smoke", step_id="desktop",
                                     nonce="unique", publish_relative_path="result.txt")
                    )
                desktop.assert_not_called()

    def test_wrong_cifs_share_cannot_be_used_for_shared_io(self) -> None:
        with patch.dict(os.environ, {
            "POC_STORAGE_ACCOUNT_NAME": "approvedaccount",
            "POC_FILE_SHARE_NAME": "team-poc",
        }):
            with patch("poc_core.mount_details", return_value={
                "mounted": True, "filesystem_type": "cifs",
                "source": "//wrongaccount.file.core.windows.net/team-poc",
            }):
                with self.assertRaises(PocError):
                    self.dispatcher._require_shared_mount()

    def test_mount_requires_fixed_environment(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(PocError) as context:
                self.dispatcher.dispatch(
                    self.request("mount_shared", step_id="mount")
                )
        self.assertEqual(context.exception.reason_code, "ENVIRONMENT_OR_APPROVAL")

    def test_legacy_core_only_image_does_not_import_smb_oauth(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for filename in ("poc_core.py", "desktop_smoke.py"):
                shutil.copyfile(AGENT_ROOT / filename, root / filename)
            script = """
import importlib.util, os, pathlib, sys
from unittest.mock import patch
sys.path.insert(0, sys.argv[1])
assert importlib.util.find_spec("smb_oauth") is None
import poc_core
root = pathlib.Path(sys.argv[1])
dispatcher = poc_core.PocDispatcher(home=root / "home", shared_root=root / "team")
request = {"schema_version": "1.0", "poc_id": "poc-1", "run_id": "run-1"}
for operation in ("probe_environment", "status"):
    assert dispatcher.dispatch({**request, "operation": operation})["status"] == "success"
with patch.dict(os.environ, {"POC_STORAGE_ACCOUNT_NAME": "testaccount", "POC_FILE_SHARE_NAME": "team-poc"}):
    with patch.object(poc_core, "mount_details", return_value={
        "mounted": True, "filesystem_type": "cifs",
        "source": "//testaccount.file.core.windows.net/team-poc",
    }):
        response = dispatcher.dispatch({**request, "operation": "mount_shared", "step_id": "legacy-mount"})
        assert response["result"]["mount_mode"] == "already_present"
        assert "auth_mode" not in response["result"]
        assert "auth_mode" not in response
assert "smb_oauth" not in sys.modules
"""
            completed = subprocess.run(
                [sys.executable, "-I", "-B", "-c", script, str(root)],
                cwd=root, capture_output=True, text=True, timeout=20, check=False,
                env={key: value for key, value in os.environ.items() if not key.startswith("POC_")},
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_key_vault_url_is_validated_before_secret_lookup(self) -> None:
        with patch.dict(
            os.environ,
            {
                "POC_STORAGE_ACCOUNT_NAME": "storageaccount",
                "POC_FILE_SHARE_NAME": "team-poc",
                "POC_KEY_VAULT_URL": "http://not-a-vault.example",
            },
            clear=True,
        ):
            with self.assertRaises(PocError) as context:
                self.dispatcher.dispatch(
                    self.request("mount_shared", step_id="mount")
                )
        self.assertEqual(context.exception.reason_code, "INVALID_CONFIGURATION")

    def test_idempotent_replay_returns_existing_result(self) -> None:
        calls = 0

        def execute() -> dict[str, object]:
            nonlocal calls
            calls += 1
            return {"value": 1}

        request = self.request("mount_shared", step_id="mount")
        first = self.dispatcher._idempotent("poc-1", "run-1", "mount", request, execute)
        second = self.dispatcher._idempotent(
            "poc-1", "run-1", "mount", request, execute
        )
        self.assertEqual(calls, 1)
        self.assertEqual(first, {"value": 1})
        self.assertEqual(second, {"value": 1, "idempotent_replay": True})

    def test_idempotency_conflict_is_explicit(self) -> None:
        first = self.request("mount_shared", step_id="mount")
        second = self.request("mount_shared", step_id="mount", changed=True)
        self.dispatcher._idempotent(
            "poc-1", "run-1", "mount", first, lambda: {"value": 1}
        )
        with self.assertRaises(PocError) as context:
            self.dispatcher._idempotent(
                "poc-1", "run-1", "mount", second, lambda: {"value": 2}
            )
        self.assertEqual(context.exception.reason_code, "STEP_CONFLICT")

    def test_state_file_contains_no_request_secret(self) -> None:
        request = self.request("mount_shared", step_id="mount")
        self.dispatcher._idempotent(
            "poc-1", "run-1", "mount", request, lambda: {"mounted": True}
        )
        state_file = self.home / "fa-poc" / "state" / "poc-1" / "run-1" / "mount.json"
        record = json.loads(state_file.read_text(encoding="utf-8"))
        self.assertEqual(record["result"], {"mounted": True})
        self.assertNotIn("request", record)


class MountFailureTests(unittest.TestCase):
    def dispatch_failure(self, stderr: str) -> PocError:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runner = Mock(return_value=subprocess.CompletedProcess(
                args=["mount"], returncode=32, stdout="", stderr=stderr,
            ))
            dispatcher = PocDispatcher(
                home=root / "home", shared_root=root / "team",
                runtime_root=root / "runtime", command_runner=runner,
            )
            with (
                patch.dict(os.environ, {
                    "POC_STORAGE_ACCOUNT_NAME": "testaccount",
                    "POC_FILE_SHARE_NAME": "team-poc",
                    "POC_STORAGE_ACCOUNT_KEY": "synthetic-test-key",
                }, clear=True),
                patch("poc_core.mount_details", return_value={"mounted": False}),
                patch("poc_core.socket.getaddrinfo", return_value=[
                    (2, 1, 6, "", ("10.0.0.85", 445)),
                ]),
                patch("poc_core.socket.create_connection"),
                patch.object(os, "fchmod", create=True) as chmod,
                patch.object(os, "getuid", return_value=0, create=True),
                patch.object(os, "getgid", return_value=0, create=True),
            ):
                with self.assertRaises(PocError) as caught:
                    dispatcher.dispatch(DispatcherTests.request("mount_shared", step_id="mount"))
                chmod.assert_called_once_with(ANY, 0o600)
            runner.assert_called_once()
            command = runner.call_args.args[0]
            options = command[-1].split(",")
            self.assertIn("vers=3.1.1", options)
            self.assertIn("seal", options)
            self.assertNotIn("synthetic-test-key", json.dumps(command))
            credential_path = Path(next(option.split("=", 1)[1] for option in options if option.startswith("credentials=")))
            self.assertFalse(credential_path.exists())
            return caught.exception

    def test_errno_13_from_hosted_sse_does_not_claim_kernel_denial(self) -> None:
        error = self.dispatch_failure(MOUNT_ERROR_13_STDERR)
        body = error.as_dict()
        self.assertEqual(error.status_code, 400)
        self.assertEqual(body["status"], "error")
        self.assertEqual(body["reason_code"], "MOUNT_ACCESS_DENIED_UNCLASSIFIED")
        self.assertEqual(body["details"]["mount_errno"], 13)
        self.assertIs(type(body["details"]["mount_errno"]), int)
        self.assertEqual(body["details"]["denial_origin"], "unknown")
        self.assertEqual(body["details"]["return_code"], 32)
        self.assertEqual(body["details"]["stderr"], MOUNT_ERROR_13_STDERR)

    def test_errno_13_takes_precedence_over_permission_wording(self) -> None:
        error = self.dispatch_failure("mount error(13): Operation not permitted\n")
        self.assertEqual(error.reason_code, "MOUNT_ACCESS_DENIED_UNCLASSIFIED")
        self.assertEqual(error.details["mount_errno"], 13)
        self.assertEqual(error.details["denial_origin"], "unknown")

    def test_errno_1_keeps_the_existing_privilege_category(self) -> None:
        error = self.dispatch_failure("mount error(1): Operation not permitted\n")
        self.assertEqual(error.reason_code, "MOUNT_PRIVILEGE_OR_KERNEL")
        self.assertEqual(error.details["mount_errno"], 1)
        self.assertEqual(error.details["denial_origin"], "unknown")
        self.assertEqual(error.status_code, 400)

    def test_permission_denied_without_errno_does_not_invent_an_origin(self) -> None:
        error = self.dispatch_failure("mount.cifs: Permission denied\n")
        body = json.loads(json.dumps(error.as_dict()))
        self.assertEqual(body["reason_code"], "MOUNT_ACCESS_DENIED_UNCLASSIFIED")
        self.assertIsNone(body["details"]["mount_errno"])
        self.assertEqual(body["details"]["denial_origin"], "unknown")
        self.assertEqual(body["details"]["return_code"], 32)


class MountDiagnosticsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.key = base64.b64encode(bytes(range(64))).decode("ascii")
        self.stack.enter_context(patch.dict(os.environ, {
            "POC_STORAGE_ACCOUNT_NAME": "testaccount",
            "POC_FILE_SHARE_NAME": "team-poc",
            "POC_STORAGE_ACCOUNT_KEY": self.key,
            "POC_MOUNT_DIAGNOSTICS": "1",
        }, clear=True))
        self.stack.enter_context(patch("poc_core.mount_details", return_value={"mounted": False}))
        self.stack.enter_context(patch("poc_core.socket.getaddrinfo", return_value=[
            (2, 1, 6, "", ("10.0.0.85", 445)),
        ]))
        self.stack.enter_context(patch("poc_core.socket.create_connection"))
        for name in ("getuid", "geteuid", "getgid"):
            self.stack.enter_context(patch.object(os, name, return_value=0, create=True))
        self.stack.enter_context(patch.object(os, "fchmod", create=True))
        self.credential_mode = 0o100600
        original_stat = Path.stat

        def linux_stat(path, *args, **kwargs):
            metadata = original_stat(path, *args, **kwargs)
            if path.suffix == ".cred":
                fields = list(metadata)
                fields[0], fields[4], fields[5] = self.credential_mode, 0, 0
                return os.stat_result(fields)
            return metadata

        self.stack.enter_context(patch.object(Path, "stat", linux_stat))
        self.runner = Mock(return_value=subprocess.CompletedProcess(
            args=["mount"], returncode=32, stdout="", stderr=MOUNT_ERROR_13_STDERR,
        ))
        self.dispatcher = PocDispatcher(
            home=self.root / "home", shared_root=self.root / "team",
            runtime_root=self.root / "runtime", command_runner=self.runner,
        )

    def failure(self) -> PocError:
        with self.assertRaises(PocError) as caught:
            self.dispatcher.dispatch(DispatcherTests.request("mount_shared", step_id="mount"))
        return caught.exception

    def test_invalid_key_is_rejected_without_mount_or_kernel_commands(self) -> None:
        os.environ["POC_STORAGE_ACCOUNT_KEY"] = f'"{self.key}"'
        error = self.failure()
        self.assertEqual(error.reason_code, "INVALID_CONFIGURATION")
        diagnostics = error.details["diagnostics"]
        self.assertEqual(diagnostics["credential_source"], "environment")
        self.assertTrue(diagnostics["credential_format"]["has_quotes"])
        self.assertFalse(diagnostics["credential_format"]["valid_64_byte_base64"])
        self.assertEqual(error.details["denial_origin"], "unknown")
        self.assertIsNone(error.details["mount_errno"])
        self.assertNotIn(self.key, json.dumps(error.as_dict()))
        self.runner.assert_not_called()

    def test_failure_preserves_safe_file_checks_and_only_new_relevant_kernel_lines(self) -> None:
        snapshots = iter([
            "[1.000000] CIFS: old mount failure\n[2.000000] unrelated baseline\n",
            f"[1.000000] CIFS: old mount failure\n[3.000000] CIFS: denied {self.key}\n"
            "[4.000000] unrelated host message\n"
            '[5.000000] audit: apparmor="DENIED" operation="mount" name="/team"\n',
        ])

        def execute(command, **kwargs):
            if command[0] == "dmesg":
                self.assertEqual(kwargs["timeout"], 3)
                self.assertNotIn("POC_STORAGE_ACCOUNT_KEY", kwargs["env"])
                return subprocess.CompletedProcess(command, 0, next(snapshots), "")
            credential_path = Path(command[-1].split(",")[0].split("=", 1)[1])
            self.assertEqual(credential_path.read_text(), f"username=testaccount\npassword={self.key}\n")
            return subprocess.CompletedProcess(command, 32, self.key, MOUNT_ERROR_13_STDERR + self.key)

        self.runner.side_effect = execute
        error = self.failure()
        diagnostics = error.details["diagnostics"]
        self.assertEqual(error.reason_code, "MOUNT_ACCESS_DENIED_UNCLASSIFIED")
        self.assertEqual(diagnostics["credential_source"], "environment")
        self.assertIsNone(diagnostics["secret_version"])
        self.assertTrue(diagnostics["credential_format"]["valid_64_byte_base64"])
        file = diagnostics["credential_file"]
        self.assertEqual(file["owner_uid"], 0)
        self.assertEqual(file["mode"], "0600")
        self.assertTrue(file["readable_by_process"])
        self.assertTrue(file["content_matches_expected"])
        self.assertTrue(file["removed"])
        kernel = diagnostics["kernel"]
        self.assertEqual(kernel["status"], "available")
        self.assertEqual(len(kernel["messages"]), 2)
        serialized = json.dumps(error.as_dict())
        self.assertNotIn(self.key, serialized)
        self.assertNotIn("old mount failure", serialized)
        self.assertNotIn("unrelated", serialized)
        self.assertIn("<REDACTED>", serialized)
        self.assertEqual([call.args[0][0] for call in self.runner.call_args_list], ["dmesg", "mount", "dmesg"])
        self.assertFalse(list((self.root / "runtime").glob("*.cred")))

    def test_diagnostics_default_off_preserves_the_original_mount_path(self) -> None:
        os.environ.pop("POC_MOUNT_DIAGNOSTICS")
        os.environ["POC_STORAGE_ACCOUNT_KEY"] = "original-unvalidated-fixture"
        error = self.failure()
        self.assertNotIn("diagnostics", error.details)
        self.runner.assert_called_once()
        self.assertEqual(self.runner.call_args.args[0][0], "mount")

    def test_bookworm_dmesg_command_contract_uses_default_boot_timestamps(self) -> None:
        # Documentation-backed argv fixture, not an executed Linux/Hosted binary:
        # https://manpages.debian.org/bookworm/util-linux/dmesg.1.en.html
        parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False, exit_on_error=False)
        parser.add_argument("--kernel", action="store_true")
        parser.add_argument("--syslog", action="store_true")
        parser.add_argument("--buffer-size", type=int)
        parser.add_argument("--color", choices=("auto", "never", "always"))
        parser.add_argument("--nopager", action="store_true")
        parser.add_argument("--time-format", choices=("ctime", "reltime", "delta", "iso"))
        with self.assertRaises(argparse.ArgumentError):
            parser.parse_args(["--time-format=raw"])
        snapshots = iter([
            "[    1.000000] CIFS: old event\n",
            "[    1.000000] CIFS: old event\n[    2.000001] CIFS: new denial\n",
        ])

        def execute(command, **kwargs):
            if command[0] == "dmesg":
                try:
                    options = parser.parse_args(command[1:])
                except argparse.ArgumentError:
                    return subprocess.CompletedProcess(command, 1, "", "dmesg: unknown time format: raw\n")
                self.assertTrue(options.kernel)
                self.assertTrue(options.syslog)
                self.assertTrue(options.nopager)
                self.assertEqual(options.color, "never")
                self.assertEqual(options.buffer_size, 65536)
                self.assertIsNone(options.time_format)
                self.assertEqual(kwargs["timeout"], 3)
                return subprocess.CompletedProcess(command, 0, next(snapshots), "")
            return subprocess.CompletedProcess(command, 32, "", MOUNT_ERROR_13_STDERR)

        self.runner.side_effect = execute
        error = self.failure()
        kernel = error.details["diagnostics"]["kernel"]
        self.assertEqual(kernel["status"], "available", kernel)
        self.assertEqual(kernel["messages"], ["[    2.000001] CIFS: new denial"])
        self.assertEqual(kernel["before"]["cursor_us"], 1000000)
        self.assertEqual(kernel["after"]["cursor_us"], 2000001)
        self.assertEqual(error.details["mount_errno"], 13)
        self.assertEqual([call.args[0][0] for call in self.runner.call_args_list], ["dmesg", "mount", "dmesg"])

    def test_dmesg_argument_error_is_not_a_kernel_access_denial(self) -> None:
        def execute(command, **kwargs):
            if command[0] == "dmesg":
                return subprocess.CompletedProcess(command, 1, "", "dmesg: unknown time format: raw\n")
            return subprocess.CompletedProcess(command, 32, "", MOUNT_ERROR_13_STDERR)

        self.runner.side_effect = execute
        error = self.failure()
        kernel = error.details["diagnostics"]["kernel"]
        self.assertEqual(kernel["status"], "unavailable")
        for phase in ("before", "after"):
            self.assertEqual(kernel[phase]["return_code"], 1)
            self.assertEqual(kernel[phase]["stderr"], "dmesg: unknown time format: raw\n")
            self.assertIsNone(kernel[phase]["errno"])
            self.assertNotIn("errno_source", kernel[phase])
        self.assertEqual(error.reason_code, "MOUNT_ACCESS_DENIED_UNCLASSIFIED")
        self.assertEqual(error.details["mount_errno"], 13)
        self.assertEqual(error.details["denial_origin"], "unknown")

    def test_malformed_keys_are_rejected_without_repair_or_secret_disclosure(self) -> None:
        cases = [
            (self.key + "\r\n", "has_crlf"),
            (" " + self.key, "has_whitespace"),
            ("'" + self.key + "'", "has_quotes"),
            (base64.b64encode(bytes(32)).decode(), "valid_base64"),
        ]
        for value, flag in cases:
            with self.subTest(flag=flag):
                os.environ["POC_STORAGE_ACCOUNT_KEY"] = value
                error = self.failure()
                self.assertEqual(error.reason_code, "INVALID_CONFIGURATION")
                checks = error.details["diagnostics"]["credential_format"]
                self.assertTrue(checks[flag])
                self.assertFalse(checks["valid_64_byte_base64"])
                self.assertNotIn(self.key, json.dumps(error.as_dict()))
        self.runner.assert_not_called()

    def test_kernel_permission_denial_does_not_change_mount_errno_or_origin(self) -> None:
        def execute(command, **kwargs):
            if command[0] == "dmesg":
                return subprocess.CompletedProcess(
                    command, 1, "", "dmesg: read kernel buffer failed: Operation not permitted\n",
                )
            return subprocess.CompletedProcess(command, 32, "", MOUNT_ERROR_13_STDERR)

        self.runner.side_effect = execute
        error = self.failure()
        self.assertEqual(error.reason_code, "MOUNT_ACCESS_DENIED_UNCLASSIFIED")
        self.assertEqual(error.details["mount_errno"], 13)
        self.assertEqual(error.details["denial_origin"], "unknown")
        kernel = error.details["diagnostics"]["kernel"]
        self.assertEqual(kernel["status"], "unavailable")
        self.assertEqual(kernel["before"]["errno"], errno.EPERM)
        self.assertEqual(kernel["before"]["errno_source"], "dmesg_stderr")
        self.assertEqual(kernel["after"]["errno"], errno.EPERM)
        self.assertEqual(kernel["messages"], [])

    def test_kernel_os_error_and_timeout_are_unavailable_without_raw_exception(self) -> None:
        for failure in (
            PermissionError(errno.EACCES, self.key),
            FileNotFoundError(errno.ENOENT, self.key),
            subprocess.TimeoutExpired(["dmesg"], 3, output=self.key, stderr=self.key),
        ):
            with self.subTest(failure=type(failure).__name__):
                def execute(command, **kwargs):
                    if command[0] == "dmesg":
                        raise failure
                    return subprocess.CompletedProcess(command, 32, "", MOUNT_ERROR_13_STDERR)
                self.runner.side_effect = execute
                error = self.failure()
                kernel = error.details["diagnostics"]["kernel"]
                self.assertEqual(kernel["status"], "unavailable")
                self.assertEqual(kernel["before"]["errno"], getattr(failure, "errno", None))
                self.assertNotIn(self.key, json.dumps(error.as_dict()))
                self.assertTrue(error.details["diagnostics"]["credential_file"]["removed"])

    def test_success_persists_diagnostics_even_when_kernel_logs_are_unavailable(self) -> None:
        mounted = False
        approved = {
            "mounted": True, "filesystem_type": "cifs",
            "source": "//testaccount.file.core.windows.net/team-poc",
        }

        def execute(command, **kwargs):
            nonlocal mounted
            if command[0] == "dmesg":
                raise PermissionError(errno.EPERM, "kernel access unavailable")
            mounted = True
            return subprocess.CompletedProcess(command, 0, "", "")

        self.runner.side_effect = execute
        with patch("poc_core.mount_details", side_effect=lambda target: approved.copy() if mounted else {"mounted": False}):
            response = self.dispatcher.dispatch(DispatcherTests.request("mount_shared", step_id="mount"))
        self.assertEqual(response["status"], "success")
        diagnostics = response["result"]["diagnostics"]
        self.assertEqual(diagnostics["kernel"]["status"], "unavailable")
        self.assertTrue(diagnostics["credential_file"]["removed"])
        state = self.root / "home" / "fa-poc" / "state" / "poc-1" / "run-1" / "mount.json"
        saved = state.read_text(encoding="utf-8")
        self.assertEqual(json.loads(saved)["result"]["diagnostics"], diagnostics)
        self.assertNotIn(self.key, saved)

    def test_key_vault_source_records_only_the_returned_secret_version(self) -> None:
        os.environ.pop("POC_STORAGE_ACCOUNT_KEY")
        os.environ["POC_KEY_VAULT_URL"] = "https://test-vault.vault.azure.net"
        secret_client = Mock()
        secret_client.get_secret.return_value = SimpleNamespace(
            value=self.key, properties=SimpleNamespace(version="0123456789abcdef" * 2),
        )
        client_type = Mock(return_value=secret_client)
        with (
            patch("azure.identity.DefaultAzureCredential"),
            patch.dict(sys.modules, {"azure.keyvault.secrets": SimpleNamespace(SecretClient=client_type)}),
        ):
            error = self.failure()
        diagnostics = error.details["diagnostics"]
        self.assertEqual(diagnostics["credential_source"], "key_vault")
        self.assertEqual(diagnostics["secret_version"], "0123456789abcdef" * 2)
        self.assertTrue(diagnostics["credential_format"]["valid_64_byte_base64"])
        secret_client.get_secret.assert_called_once_with("azure-files-account-key")
        self.assertNotIn(self.key, json.dumps(error.as_dict()))

    def test_environment_key_does_not_fall_back_to_key_vault(self) -> None:
        os.environ["POC_KEY_VAULT_URL"] = "https://test-vault.vault.azure.net"
        with patch("azure.identity.DefaultAzureCredential") as credential:
            error = self.failure()
        credential.assert_not_called()
        self.assertEqual(error.details["diagnostics"]["credential_source"], "environment")

    def test_key_vault_failure_does_not_expose_raw_sdk_exception(self) -> None:
        os.environ.pop("POC_STORAGE_ACCOUNT_KEY")
        os.environ["POC_KEY_VAULT_URL"] = "https://test-vault.vault.azure.net"
        client = Mock()
        client.get_secret.side_effect = RuntimeError(f"unexpected SDK error containing {self.key}")
        with (
            patch("azure.identity.DefaultAzureCredential"),
            patch.dict(sys.modules, {"azure.keyvault.secrets": SimpleNamespace(SecretClient=Mock(return_value=client))}),
        ):
            error = self.failure()
        self.assertEqual(error.reason_code, "AUTHENTICATION_OR_SHARE_ACL")
        self.assertEqual(error.details["diagnostics"]["credential_source"], "key_vault")
        self.assertNotIn(self.key, json.dumps(error.as_dict()))
        self.runner.assert_not_called()

    def test_nul_in_key_vault_value_is_rejected_without_writing_credentials(self) -> None:
        os.environ.pop("POC_STORAGE_ACCOUNT_KEY")
        os.environ["POC_KEY_VAULT_URL"] = "https://test-vault.vault.azure.net"
        client = Mock()
        client.get_secret.return_value = SimpleNamespace(
            value=self.key + "\x00", properties=SimpleNamespace(version="test-version"),
        )
        with (
            patch("azure.identity.DefaultAzureCredential"),
            patch.dict(sys.modules, {"azure.keyvault.secrets": SimpleNamespace(SecretClient=Mock(return_value=client))}),
        ):
            error = self.failure()
        self.assertEqual(error.reason_code, "INVALID_CONFIGURATION")
        checks = error.details["diagnostics"]["credential_format"]
        self.assertTrue(checks["has_nul"])
        self.assertFalse(checks["valid_64_byte_base64"])
        self.assertNotIn(self.key, json.dumps(error.as_dict()))
        self.runner.assert_not_called()
        self.assertFalse((self.root / "runtime").exists())

    def test_unreadable_credentials_fail_before_mount_and_are_removed(self) -> None:
        original_open = Path.open

        def open_file(path, mode="r", *args, **kwargs):
            if path.suffix == ".cred" and mode == "rb":
                raise PermissionError(errno.EACCES, self.key)
            return original_open(path, mode, *args, **kwargs)

        with patch.object(Path, "open", open_file):
            error = self.failure()
        file = error.details["diagnostics"]["credential_file"]
        self.assertFalse(file["readable_by_process"])
        self.assertFalse(file["content_matches_expected"])
        self.assertEqual(file["errno"], errno.EACCES)
        self.assertTrue(file["removed"])
        self.assertNotIn(self.key, json.dumps(error.as_dict()))
        self.runner.assert_not_called()

    def test_unexpected_credentials_mode_or_file_type_is_not_used(self) -> None:
        for mode in (0o100644, 0o120777):
            with self.subTest(mode=mode):
                self.credential_mode = mode
                error = self.failure()
                self.assertEqual(error.reason_code, "INVALID_CONFIGURATION")
                self.assertTrue(error.details["diagnostics"]["credential_file"]["removed"])
        self.runner.assert_not_called()

    def test_kernel_messages_are_bounded_and_credential_dumps_are_excluded(self) -> None:
        snapshots = iter([
            "[1.000000] baseline\n",
            "".join(f"[2.{index:06d}] CIFS: {'x' * 100} {self.key}\n" for index in range(1000))
            + f"[3.000000] CIFS: password={self.key}\n[4.000000] CIFS: mount data {self.key}\n",
        ])

        def execute(command, **kwargs):
            if command[0] == "dmesg":
                self.assertIn("--buffer-size=65536", command)
                self.assertNotIn("--read-clear", command)
                self.assertNotIn("--clear", command)
                self.assertNotIn("--follow", command)
                return subprocess.CompletedProcess(command, 0, next(snapshots), "")
            return subprocess.CompletedProcess(command, 32, "", MOUNT_ERROR_13_STDERR)

        self.runner.side_effect = execute
        error = self.failure()
        kernel = error.details["diagnostics"]["kernel"]
        self.assertEqual(kernel["status"], "partial")
        self.assertLessEqual(len(kernel["messages"]), 16)
        self.assertLessEqual(sum(len(line.encode("utf-8")) for line in kernel["messages"]), 8192)
        self.assertNotIn(self.key, json.dumps(error.as_dict()))
        self.assertNotIn("password=", json.dumps(kernel))
        self.assertNotIn("mount data", json.dumps(kernel))

    def test_unknown_kernel_timestamp_format_is_not_treated_as_new_evidence(self) -> None:
        def execute(command, **kwargs):
            if command[0] == "dmesg":
                return subprocess.CompletedProcess(command, 0, "CIFS: unknown time format\n", "")
            return subprocess.CompletedProcess(command, 32, "", MOUNT_ERROR_13_STDERR)

        self.runner.side_effect = execute
        error = self.failure()
        kernel = error.details["diagnostics"]["kernel"]
        self.assertEqual(kernel["status"], "unavailable")
        self.assertEqual(kernel["reason"], "kernel_timestamp_unavailable")
        self.assertEqual(kernel["messages"], [])

    def test_mount_timeout_retains_diagnostics_and_cleans_credentials(self) -> None:
        def execute(command, **kwargs):
            if command[0] == "dmesg":
                return subprocess.CompletedProcess(command, 0, "[1.000000] baseline\n", "")
            raise subprocess.TimeoutExpired(command, 30, output=self.key, stderr=self.key)

        self.runner.side_effect = execute
        error = self.failure()
        self.assertEqual(error.reason_code, "MOUNT_EXECUTION_FAILED")
        self.assertTrue(error.details["diagnostics"]["credential_file"]["removed"])
        self.assertNotIn(self.key, json.dumps(error.as_dict()))
        self.assertEqual([call.args[0][0] for call in self.runner.call_args_list], ["dmesg", "mount", "dmesg"])

    def test_mount_errno_survives_output_limits_without_secret_disclosure(self) -> None:
        def execute(command, **kwargs):
            if command[0] == "dmesg":
                return subprocess.CompletedProcess(command, 0, "[1.000000] baseline\n", "")
            return subprocess.CompletedProcess(
                command, 32, self.key + "x" * 5000,
                "x" * 5000 + self.key + "\n" + MOUNT_ERROR_13_STDERR,
            )

        self.runner.side_effect = execute
        error = self.failure()
        self.assertEqual(error.details["mount_errno"], 13)
        self.assertEqual(error.reason_code, "MOUNT_ACCESS_DENIED_UNCLASSIFIED")
        self.assertLessEqual(len(error.details["stdout"].encode()), 4096)
        self.assertLessEqual(len(error.details["stderr"].encode()), 4096)
        self.assertTrue(error.details["diagnostics"]["mount_output_truncated"]["stderr"])
        self.assertNotIn(self.key, json.dumps(error.as_dict()))


class ManagedIdentityMountTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        directory = self.stack.enter_context(tempfile.TemporaryDirectory())
        root = Path(directory)
        self.environment = {
            "POC_SMB_AUTH_MODE": "managed_identity",
            "POC_STORAGE_ACCOUNT_NAME": "testaccount",
            "POC_FILE_SHARE_NAME": "team-poc",
            "POC_EXPECTED_MI_OBJECT_ID": "00000000-0000-0000-0000-000000000011",
            "POC_EXPECTED_MI_TENANT_ID": "00000000-0000-0000-0000-000000000013",
            "POC_STORAGE_ACCOUNT_KEY": "never-read-this-key",
            "POC_KEY_VAULT_URL": "https://never-read.vault.azure.net",
        }
        self.stack.enter_context(patch.dict(os.environ, self.environment, clear=True))
        self.stack.enter_context(patch.object(os, "getuid", return_value=0, create=True))
        self.stack.enter_context(patch.object(os, "getgid", return_value=0, create=True))
        self.stack.enter_context(patch("poc_core.socket.getaddrinfo", return_value=[
            (2, 1, 6, "", ("10.0.0.85", 445)),
        ]))
        self.stack.enter_context(patch("poc_core.socket.create_connection"))
        self.key_reader = self.stack.enter_context(patch.object(
            PocDispatcher, "_read_storage_key", side_effect=AssertionError("no key fallback"),
        ))
        self.credential_file = self.stack.enter_context(patch(
            "poc_core.tempfile.mkstemp", side_effect=AssertionError("no credentials file"),
        ))
        self.oauth = Mock()
        self.oauth.ensure.return_value = {
            "credential_source": "hosted_managed_identity", "credential_uid": 10002,
            "claims": {"aud": "https://storage.azure.com",
                       "oid": self.environment["POC_EXPECTED_MI_OBJECT_ID"],
                       "tid": self.environment["POC_EXPECTED_MI_TENANT_ID"]},
            "cache": {"present": True, "owner_uid": 10002, "mode": "0600"},
        }
        self.factory = Mock(return_value=self.oauth)
        self.mounted = False
        self.source = "//testaccount.file.core.windows.net/team-poc"
        self.options = ["sec=krb5", "cruid=10002", "vers=3.1.1", "seal"]
        self.stack.enter_context(patch("poc_core.mount_details", side_effect=self.mount_info))
        self.runner = Mock(side_effect=self.mount_command)
        self.dispatcher = PocDispatcher(
            home=root / "home", shared_root=root / "team", runtime_root=root / "runtime",
            command_runner=self.runner, oauth_factory=self.factory,
        )

    def mount_info(self, target):
        return {
            "mounted": self.mounted, "target": str(target), "filesystem_type": "cifs",
            "source": self.source, "super_options": list(self.options),
        }

    def mount_command(self, command, **kwargs):
        self.mounted = True
        return subprocess.CompletedProcess(command, 0, "", "")

    def invoke(self, operation="mount_shared", step_id="mount", **kwargs):
        return self.dispatcher.dispatch(DispatcherTests.request(operation, step_id=step_id, **kwargs))["result"]

    def test_mi_mount_uses_kerberos_cruid_and_never_reads_key_or_creates_password_file(self):
        result = self.invoke()
        self.assertEqual(result["auth_mode"], "managed_identity")
        self.assertEqual(result["mount_mode"], "application_mount")
        self.factory.assert_called_once_with(
            "testaccount", self.environment["POC_EXPECTED_MI_OBJECT_ID"],
            self.environment["POC_EXPECTED_MI_TENANT_ID"],
        )
        self.oauth.ensure.assert_called_once_with(force=True)
        self.runner.assert_called_once()
        command = self.runner.call_args.args[0]
        options = command[-1].split(",")
        for expected in ("sec=krb5", "cruid=10002", "vers=3.1.1", "seal"):
            self.assertIn(expected, options)
        self.assertFalse(any(o.startswith(("username=", "password=", "credentials=")) for o in options))
        self.assertEqual(self.runner.call_args.kwargs["timeout"], 30)
        self.assertEqual(self.runner.call_args.kwargs["env"]["KRB5CCNAME"], "FILE:/tmp/krb5cc_10002")
        self.assertEqual(set(self.runner.call_args.kwargs["env"]), {"PATH", "LC_ALL", "KRB5CCNAME"})
        self.assertNotIn("never-read-this-key", json.dumps(command) + json.dumps(result))
        self.key_reader.assert_not_called()
        self.credential_file.assert_not_called()

    def test_successful_mi_mount_exposes_mode_on_envelope_and_result(self):
        for step, expected_mount_mode in (
            ("new-mount", "application_mount"), ("existing-mount", "already_present"),
        ):
            with self.subTest(step=step):
                response = self.dispatcher.dispatch(DispatcherTests.request("mount_shared", step_id=step))
                self.assertEqual(response["auth_mode"], "managed_identity")
                self.assertEqual(response["result"]["auth_mode"], "managed_identity")
                self.assertEqual(response["result"]["mount_mode"], expected_mount_mode)

    def test_legacy_mount_replay_cannot_be_promoted_to_mi_evidence(self):
        request = DispatcherTests.request("mount_shared", step_id="legacy-replay")
        self.dispatcher._idempotent(
            "poc-1", "run-1", "legacy-replay", request,
            lambda: {"mounted": True, "mount_mode": "application_mount"},
        )
        with self.assertRaises(PocError) as caught:
            self.dispatcher.dispatch(request)
        self.assertEqual(caught.exception.reason_code, "SMB_AUTH_MODE_UNVERIFIED")
        self.factory.assert_not_called()
        self.runner.assert_not_called()

    def test_authentication_failure_is_not_kernel_denial_or_key_fallback(self):
        self.oauth.ensure.side_effect = SmbOAuthError("SMB_NATIVE_SET_FAILED", return_code=7)
        with self.assertRaises(PocError) as caught:
            self.invoke()
        self.assertEqual(caught.exception.reason_code, "SMB_NATIVE_SET_FAILED")
        self.runner.assert_not_called()
        self.key_reader.assert_not_called()

    def test_native_failure_keeps_post_failure_connectivity_diagnostics(self):
        self.oauth.ensure.side_effect = SmbOAuthError("SMB_NATIVE_SET_FAILED", return_code=-1)
        proof = {"scope": "unauthenticated_connectivity_only", "probes": []}
        with patch.dict(os.environ, {"POC_MI_NETWORK_DIAGNOSTICS": "1"}), \
             patch("smb_diagnostics.network_probe", create=True, return_value=proof) as probe:
            with self.assertRaises(PocError) as caught:
                self.invoke()
        self.assertEqual(caught.exception.details["diagnostics"]["network"], proof)
        self.assertFalse(caught.exception.details["mount_attempted"])
        probe.assert_called_once()
        self.runner.assert_not_called()
        self.key_reader.assert_not_called()

    def test_errno_13_stays_unclassified_and_sensitive_mount_output_is_not_archived(self):
        self.runner.side_effect = None
        self.runner.return_value = subprocess.CompletedProcess(
            ["mount"], 32, "synthetic-sensitive-output",
            MOUNT_ERROR_13_STDERR + "synthetic-sensitive-output",
        )
        with self.assertRaises(PocError) as caught:
            self.invoke()
        error = caught.exception
        self.assertEqual(error.reason_code, "MOUNT_ACCESS_DENIED_UNCLASSIFIED")
        self.assertEqual(error.details["mount_errno"], 13)
        self.assertEqual(error.details["denial_origin"], "unknown")
        self.assertNotIn("synthetic-sensitive-output", json.dumps(error.as_dict()))
        self.oauth.invalidate.assert_called_once()
        self.runner.assert_called_once()

    def test_mi_diagnostics_capture_kernel_and_upcall_before_credential_cleanup(self):
        self.runner.side_effect = None
        self.runner.return_value = subprocess.CompletedProcess(
            ["mount"], 32, "do-not-persist", "mount error(2): No such file or directory\ndo-not-persist",
        )
        snapshots = [
            {"status": "available", "cursor_us": 1, "records": [], "read_limit_reached": False},
            {"status": "available", "cursor_us": 2, "records": [
                (2, {"category": "mount_failed", "code": -2}),
            ], "read_limit_reached": False},
        ]
        with patch.dict(os.environ, {"POC_MI_MOUNT_DIAGNOSTICS": "1"}), \
             patch("smb_diagnostics.begin", return_value={"status": "ready"}), \
             patch("smb_diagnostics.finish", return_value={"status": "available", "events": []}) as finish, \
             patch.object(self.dispatcher, "_kernel_snapshot", side_effect=snapshots) as kernel:
            with self.assertRaises(PocError) as caught:
                self.invoke()
        details = caught.exception.details["diagnostics"]
        self.assertEqual(details["kernel"]["messages"], [{"category": "mount_failed", "code": -2}])
        self.assertEqual(details["mount_error"], {"errno": 2, "reason": "ENOENT"})
        self.assertEqual(details["upcall"]["status"], "available")
        self.assertNotIn("do-not-persist", json.dumps(caught.exception.as_dict()))
        self.assertEqual(kernel.call_count, 2)
        self.assertTrue(all(call.kwargs["categories_only"] for call in kernel.call_args_list))
        finish.assert_called_once()
        self.oauth.invalidate.assert_called_once()

    def test_false_mount_success_and_wrong_source_or_options_are_not_passes(self):
        for failure in ("missing", "wrong_source", "account_key", "wrong_cruid", "unsealed"):
            def mount(command, **kwargs):
                self.mounted = failure != "missing"
                if failure == "wrong_source":
                    self.source = "//otheraccount.file.core.windows.net/team-poc"
                if failure == "account_key":
                    self.options = ["sec=ntlmssp", "cruid=10002", "vers=3.1.1", "seal"]
                if failure == "wrong_cruid":
                    self.options = ["sec=krb5", "cruid=0", "vers=3.1.1", "seal"]
                if failure == "unsealed":
                    self.options = ["sec=krb5", "cruid=10002", "vers=3.1.1"]
                return subprocess.CompletedProcess(command, 0, "", "")

            self.mounted = False
            self.source = "//testaccount.file.core.windows.net/team-poc"
            self.options = ["sec=krb5", "cruid=10002", "vers=3.1.1", "seal"]
            self.runner.side_effect = mount
            with self.subTest(failure=failure), self.assertRaises(PocError):
                self.invoke(step_id=failure)
        self.assertEqual(self.oauth.invalidate.call_count, 5)

    def test_probe_and_status_are_observational_resume_recovery_is_explicit(self):
        self.invoke("probe_environment")
        self.invoke("status")
        self.factory.assert_not_called()
        self.oauth.ensure.assert_not_called()
        result = self.invoke("resume_recover", step_id="resume")
        self.assertFalse(result["mount_before_recovery"]["mounted"])
        self.assertEqual(result["mount_mode"], "application_mount")
        self.assertEqual(result["platform_resume_evidence"], "not_observed_by_this_operation")
        self.oauth.ensure.assert_called_once_with(force=True)
        self.runner.assert_called_once()
        self.invoke("resume_recover", step_id="resume-existing")
        self.assertEqual(self.oauth.ensure.call_count, 2)
        self.oauth.ensure.assert_called_with(force=True)
        self.runner.assert_called_once()

    def test_shared_io_refreshes_but_does_not_remount_or_replay_writes(self):
        self.mounted = True
        self.invoke("shared_io_step", step_id="write", action="write_text",
                    relative_path="hello.txt", content="hello")
        self.oauth.ensure.assert_called_once_with()
        replay = self.invoke("shared_io_step", step_id="write", action="write_text",
                             relative_path="hello.txt", content="hello")
        self.assertTrue(replay["idempotent_replay"])
        self.oauth.ensure.assert_called_once()
        self.runner.assert_not_called()
        self.invoke("shared_io_step", step_id="read", action="read_text", relative_path="hello.txt")
        self.assertEqual(self.oauth.ensure.call_count, 2)

    def test_existing_account_key_mount_cannot_be_claimed_as_mi(self):
        self.mounted = True
        self.options = ["sec=ntlmssp", "vers=3.1.1", "seal"]
        with self.assertRaises(PocError) as caught:
            self.invoke()
        self.assertEqual(caught.exception.reason_code, "SMB_MOUNT_OPTIONS_UNVERIFIED")
        self.oauth.ensure.assert_not_called()
        self.runner.assert_not_called()

    def test_cleanup_failure_is_explicit_on_mount_failure(self):
        self.runner.side_effect = subprocess.TimeoutExpired(["mount"], 30, output="sensitive")
        self.oauth.invalidate.side_effect = SmbOAuthError("SMB_CACHE_CLEANUP_FAILED")
        with self.assertRaises(PocError) as caught:
            self.invoke()
        self.assertEqual(caught.exception.details["credential_cleanup"]["status"], "failed")
        self.assertNotIn("sensitive", json.dumps(caught.exception.as_dict()))
        self.runner.assert_called_once()

    def test_unknown_auth_mode_is_rejected_before_effects(self):
        for mode in ("", "auto", "MANAGED_IDENTITY"):
            with patch.dict(os.environ, {"POC_SMB_AUTH_MODE": mode}):
                with self.subTest(mode=mode), self.assertRaises(PocError) as caught:
                    self.invoke()
                self.assertEqual(caught.exception.reason_code, "INVALID_CONFIGURATION")
        self.runner.assert_not_called()
        self.factory.assert_not_called()

    def test_missing_expected_identity_is_rejected_before_native_or_mount(self):
        self.dispatcher.oauth_factory = None
        for key in ("POC_EXPECTED_MI_OBJECT_ID", "POC_EXPECTED_MI_TENANT_ID"):
            with patch.dict(os.environ, {key: ""}):
                with self.subTest(key=key), self.assertRaises(PocError) as caught:
                    self.invoke()
                self.assertEqual(caught.exception.reason_code, "SMB_AUTH_CONFIG_INVALID")
        self.oauth.ensure.assert_not_called()
        self.runner.assert_not_called()


if __name__ == "__main__":
    unittest.main()
