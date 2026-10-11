from __future__ import annotations

import argparse
import io
import json
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "controller"))
from private_controller import DEFAULT_IMAGE, check_private_dns, definition, ensure_version, main, request_json
from run_poc import HostedPocClient, run


class PrivateControllerTests(unittest.TestCase):
    def test_auth_selection_preserves_legacy_and_excludes_key_settings_from_mi(self) -> None:
        args = argparse.Namespace(
            image=DEFAULT_IMAGE, storage_account="stfaexampledemo",
            share="team-poc", vault_url="https://kv-faexample-de.vault.azure.net",
            secret_name="azure-files-account-key", smb_auth_mode="account_key",
            expected_runtime_principal="00000000-0000-0000-0000-000000000011",
            tenant_id="00000000-0000-0000-0000-000000000013",
        )
        legacy = definition(args)["environment_variables"]
        self.assertEqual(definition(args)["protocol_versions"], [
            {"protocol": "invocations", "version": "2.0.0"},
            {"protocol": "responses", "version": "2.0.0"},
        ])
        self.assertEqual(legacy, {
            "POC_STORAGE_ACCOUNT_NAME": args.storage_account,
            "POC_FILE_SHARE_NAME": args.share,
            "POC_KEY_VAULT_URL": args.vault_url,
            "POC_STORAGE_KEY_SECRET_NAME": args.secret_name,
        })
        args.smb_auth_mode = "managed_identity"
        managed = definition(args)["environment_variables"]
        self.assertEqual(managed, {
            "POC_STORAGE_ACCOUNT_NAME": args.storage_account,
            "POC_FILE_SHARE_NAME": args.share,
            "POC_SMB_AUTH_MODE": "managed_identity",
            "POC_EXPECTED_MI_OBJECT_ID": args.expected_runtime_principal,
            "POC_EXPECTED_MI_TENANT_ID": args.tenant_id,
        })
        args.expected_runtime_principal = ""
        with self.assertRaisesRegex(ValueError, "UUIDs"):
            definition(args)
        args.smb_auth_mode = "try_keys_if_mi_fails"
        with self.assertRaisesRegex(ValueError, "Unsupported"):
            definition(args)

    def test_powershell_transport_passes_validated_auth_mode(self) -> None:
        script = (Path(__file__).resolve().parents[1] / "scripts" / "invoke-private-controller.ps1").read_text()
        self.assertIn("[ValidateSet('account_key', 'managed_identity')]", script)
        self.assertIn("[string]$SmbAuthMode = 'account_key'", script)
        self.assertIn("--smb-auth-mode '$SmbAuthMode'", script)

    def test_g3_mode_reuses_mi_client_and_propagates_failure(self) -> None:
        expected = definition(argparse.Namespace(
            image=DEFAULT_IMAGE, storage_account="stfaexampledemo",
            share="team-poc", vault_url="https://kv-faexample-de.vault.azure.net",
            secret_name="azure-files-account-key", smb_auth_mode="account_key",
        ))
        for status in ("PASS", "FAIL"):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                argv = [
                    "private_controller.py", "g3", "--project-endpoint",
                    "https://example.services.ai.azure.com/api/projects/poc",
                    "--agent-version", "1", "--output", directory,
                ]
                with (
                    patch("sys.argv", argv),
                    patch("sys.stdout", new_callable=io.StringIO),
                    patch("private_controller.check_private_dns", return_value={}),
                    patch("private_controller.ManagedIdentityCredential"),
                    patch("private_controller.HostedPocClient") as client_type,
                    patch("private_controller.request_json", return_value={
                        "status": "active", "definition": expected,
                    }),
                    patch("private_controller.run") as shared_runner,
                    patch("g3_only.run", return_value={
                        "status": status, "shared_publish": "NOT_RUN_DEPENDENCY", "cleanup": {},
                    }) as g3_runner,
                ):
                    if status == "FAIL":
                        with self.assertRaises(SystemExit) as raised:
                            main()
                        self.assertEqual(raised.exception.code, 1)
                    else:
                        main()
                shared_runner.assert_not_called()
                self.assertIs(g3_runner.call_args.kwargs["client"], client_type.return_value)
                self.assertEqual(g3_runner.call_args.args[0].image_digest, DEFAULT_IMAGE.split("@")[1])
                client_type.return_value.credential.close.assert_called_once()

    def test_api_failure_preserves_body_without_authorization_header(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = Mock()
            client.project_endpoint = "https://example.services.ai.azure.com/api/projects/poc"
            client.agent_name = "agent"
            client.output_dir = Path(directory)
            client._headers.return_value = {"Authorization": "Bearer test-only-token"}
            response = requests.Response()
            response.status_code = 403
            response._content = b'{"error":{"code":"AccessDenied"}}'
            client.http.request.return_value = response
            with self.assertRaises(requests.HTTPError):
                request_json(client, "GET", "/versions")
            saved = (Path(directory) / "last-api-error.json").read_text()
        self.assertNotIn("test-only-token", saved)
        self.assertEqual(json.loads(saved)["status_code"], 403)
        self.assertIn("AccessDenied", json.loads(saved)["body"])

    def test_all_mode_enables_complete_shared_runner(self) -> None:
        expected = definition(argparse.Namespace(
            image=DEFAULT_IMAGE, storage_account="stfaexampledemo",
            share="team-poc", vault_url="https://kv-faexample-de.vault.azure.net",
            secret_name="azure-files-account-key", smb_auth_mode="account_key",
        ))
        with tempfile.TemporaryDirectory() as directory:
            argv = [
                "private_controller.py", "all", "--project-endpoint",
                "https://example.services.ai.azure.com/api/projects/poc",
                "--agent-version", "1", "--output", directory,
            ]
            with (
                patch("sys.argv", argv),
                patch("sys.stdout", new_callable=io.StringIO),
                patch("private_controller.check_private_dns", return_value={}),
                patch("private_controller.ManagedIdentityCredential"),
                patch("private_controller.HostedPocClient"),
                patch("private_controller.request_json", return_value={
                    "status": "active", "definition": expected,
                }),
                patch("private_controller.run", return_value={"gates": {}, "cleanup": []}) as runner,
                patch("private_controller.run_succeeded", return_value=True) as succeeded,
            ):
                main()
        args = runner.call_args.args[0]
        self.assertFalse(args.g0_only)
        self.assertTrue(args.include_g2)
        self.assertTrue(args.capture_logs)
        self.assertEqual(args.idle_timeout_seconds, 300)
        self.assertEqual(args.idle_seconds, 330)
        succeeded.assert_called_once()

    def test_managed_identity_is_used_without_cli_fallback(self) -> None:
        credential = Mock()
        with patch("run_poc.AzureCliCredential") as cli:
            client = HostedPocClient("https://project.invalid", "agent", Path("."), credential)
        self.assertIs(client.credential, credential)
        cli.assert_not_called()
        client.http.close()

    def test_dns_rejects_mixed_public_private_results(self) -> None:
        records = [
            (socket.AF_INET, 1, 6, "", ("192.168.81.4", 443)),
            (socket.AF_INET, 1, 6, "", ("20.1.2.3", 443)),
        ]
        with patch("private_controller.socket.getaddrinfo", return_value=records):
            with self.assertRaisesRegex(RuntimeError, "RFC1918"):
                check_private_dns(["account.services.ai.azure.com"])

    def test_mutable_image_tag_is_rejected(self) -> None:
        args = argparse.Namespace(image=DEFAULT_IMAGE.split("@")[0] + ":latest")
        with self.assertRaisesRegex(ValueError, "immutable"):
            definition(args)

    def test_existing_matching_version_is_not_recreated(self) -> None:
        expected = {"kind": "hosted"}
        version = {"version": "1", "status": "active", "definition": expected}
        with tempfile.TemporaryDirectory() as directory:
            with patch("private_controller.request_json", return_value={"data": [version]}) as request:
                result = ensure_version(Mock(), expected, Path(directory) / "version.json")
        self.assertEqual(result, version)
        request.assert_called_once()
        self.assertEqual(request.call_args.args[1], "GET")

    def _run_g0_mount_case(self, mode="account_key", response_mode=None) -> tuple:
        with tempfile.TemporaryDirectory() as directory:
            args = argparse.Namespace(
                output=Path(directory), run_id="test", poc_id="poc",
                project_endpoint="https://project.invalid", agent_name="agent",
                agent_version="5", image_digest="sha256:test", expected_source="//approved/team",
                g0_only=True, capture_logs=True, smb_auth_mode=mode,
            )
            client = Mock()
            client.create_session.side_effect = [
                {"agent_session_id": "A"}, {"agent_session_id": "B"},
            ]
            client.invoke.side_effect = [
                {
                    "status_code": 200,
                    "body": {
                        "status": "success", "runtime": {"session_id": alias}, "result": result,
                    },
                }
                for alias in ("A", "B")
                for result in (
                    {"agent": {"version": "5"}},
                    {
                        "mounted": True, "filesystem_type": "cifs", "source": "//approved/team",
                        "auth_mode": response_mode,
                    },
                )
            ]
            client.capture_session_logs.return_value = {"bytes": 10}
            client.stop_session.return_value = {"stopped": True}
            result = run(args, client)
            saved = json.loads((Path(directory) / "test" / "run-manifest.json").read_text())
        return result, saved, client

    def test_g0_only_stops_after_two_real_mount_results(self) -> None:
        result, saved, client = self._run_g0_mount_case()
        self.assertEqual(result["gates"]["G0"]["status"], "PASS")
        self.assertEqual(result["gates"]["G1"]["status"], "NOT_RUN_DEPENDENCY")
        self.assertEqual(client.invoke.call_count, 4)
        self.assertEqual(client.stop_session.call_count, 2)
        self.assertEqual(len(saved["session_logs"]), 2)
        calls = [item[0] for item in client.method_calls]
        self.assertLess(calls.index("capture_session_logs"), calls.index("stop_session"))

    def test_mi_g0_requires_explicit_mi_mount_evidence(self) -> None:
        for response_mode in (None, "account_key", "managed_identity"):
            with self.subTest(response_mode=response_mode):
                result, saved, client = self._run_g0_mount_case("managed_identity", response_mode)
                expected = "PASS" if response_mode == "managed_identity" else "BLOCKED"
                self.assertEqual(result["gates"]["G0"]["status"], expected)
                self.assertEqual(saved["smb_auth_mode"], "managed_identity")
                self.assertEqual(client.stop_session.call_count, 2)

    def test_log_capture_failure_still_stops_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            args = argparse.Namespace(
                output=Path(directory), run_id="failed", poc_id="poc",
                project_endpoint="https://project.invalid", agent_name="agent",
                agent_version="5", image_digest="sha256:test", expected_source="//approved/team",
                g0_only=True, capture_logs=True,
            )
            client = Mock()
            client.create_session.side_effect = [{"agent_session_id": "A"}, {"agent_session_id": "B"}]
            client.invoke.return_value = {"status_code": 400, "body": {"error": "blocked"}}
            client.capture_session_logs.side_effect = OSError("disk full")
            client.stop_session.return_value = {"stopped": True}
            result = run(args, client)
        self.assertEqual(result["gates"]["G0"]["status"], "BLOCKED")
        self.assertEqual(client.stop_session.call_count, 2)
        self.assertIn("error", result["session_logs"]["A"])


if __name__ == "__main__":
    unittest.main()
