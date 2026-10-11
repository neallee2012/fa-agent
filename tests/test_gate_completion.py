from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import struct
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
import zlib

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "controller"))
sys.path.insert(0, str(ROOT / "src" / "fa-hosted-sandbox-poc"))

from g3_only import download_and_verify, run as run_g3, verify_png, verify_result
from poc_core import PocDispatcher, PocError
from run_poc import HostedPocClient, RESUME_HEADER, payload, run, run_succeeded, verify_io

SOURCE = "//approvedaccount.file.core.windows.net/team-poc"
MOUNT = {"mounted": True, "filesystem_type": "cifs", "source": SOURCE}


def png(pixel: bytes = b"\x00\x00\x00") -> bytes:
    def chunk(kind: bytes, value: bytes) -> bytes:
        return struct.pack(">I", len(value)) + kind + value + struct.pack(">I", zlib.crc32(kind + value))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress((b"\x00" + pixel * 2) * 2))
        + chunk(b"IEND", b"")
    )


class FakeHostedClient:
    """Local control test only: simulated CIFS/REST is never Hosted capability evidence."""

    def __init__(self, root: Path, *, start_type="resume", remount=True) -> None:
        self.http, self.credential = Mock(), Mock()
        self.calls = []
        self.created = 0
        self.start_type, self.remount = start_type, remount
        self.root = root
        self.dispatchers = {}
        self.files = {}
        self.stop_error = False
        self.download_error = False
        self.old_name_visible = False
        self.resume_source = SOURCE
        self.fail_step = None
        self.mount_mode = "application_mount"

    def create_session(self, version):
        self.created += 1
        alias = ("A", "B")[self.created - 1]
        self.dispatchers[alias] = PocDispatcher(
            home=self.root / alias, shared_root=self.root / "team",
            runtime_root=self.root / "runtime" / alias,
        )
        self.calls.append(("create", alias))
        return {"agent_session_id": alias}

    def invoke(self, alias, request):
        self.calls.append(("invoke", alias, dict(request)))
        operation = request["operation"]
        dispatcher = self.dispatchers[alias]
        with patch.dict(os.environ, {
            "POC_STORAGE_ACCOUNT_NAME": "approvedaccount",
            "POC_FILE_SHARE_NAME": "team-poc",
            "FOUNDRY_AGENT_VERSION": "6",
        }), patch("poc_core.mount_details", return_value=dict(MOUNT)), patch.object(
            dispatcher, "_mount_shared", return_value={**MOUNT, "mount_mode": self.mount_mode}
        ):
            if operation == "desktop_smoke":
                result = self.desktop(request)
            else:
                result = dispatcher.dispatch(request)["result"]
        headers = {RESUME_HEADER: "warm"}
        if operation == "probe_environment":
            result["home"] = "/mnt/agent/session"
        if operation == "status" and alias == "A":
            result["team_mount"] = (
                {"mounted": False} if self.remount else {**MOUNT, "source": self.resume_source}
            )
            headers = {} if self.start_type is None else {RESUME_HEADER: self.start_type}
        if request.get("step_id") == "g1-a-inspect-old-name" and self.old_name_visible:
            result["exists"] = True
        if request.get("step_id") == self.fail_step and self.fail_step is not None:
            raise requests.Timeout("Response lost after the write; outcome is not retried")
        return {
            "status_code": 200, "headers": headers,
            "body": {"status": "success", "runtime": {"session_id": alias}, "result": result},
        }

    def desktop(self, request):
        poc_id, run_id, nonce = (request[key] for key in ("poc_id", "run_id", "nonce"))
        remote = f"fa-poc/evidence/{poc_id}/{run_id}/g3-desktop"
        content = f"{poc_id}|{run_id}|{nonce}|GUI_COMPLETE\n".encode()
        values = {
            "before": ("before.png", png()),
            "after": ("after.png", png(b"\xff\x00\x00")),
            "browser_download": ("downloads/browser-artifact.txt", content),
        }
        artifacts = {}
        for key, (name, data) in values.items():
            self.files[f"{remote}/{name}"] = data
            artifacts[key] = {
                "path": f"/mnt/agent/session/{remote}/{name}",
                "sha256": hashlib.sha256(data).hexdigest(), "size": len(data),
            }
        artifacts["published_shared"] = None
        if request.get("publish_relative_path"):
            destination = self.root / "team" / "poc" / poc_id / run_id / "browser-artifact.txt"
            destination.write_bytes(content)
            artifacts["published_shared"] = {
                "relative_path": "browser-artifact.txt", "sha256": hashlib.sha256(content).hexdigest(),
                "size": len(content),
            }
        return {
            "completed_value": nonce, "page_ready": True, "browser_uid": 10001,
            "browser_sandbox_disabled": False, "loopback_url": "http://127.0.0.1:1234/",
            "browser_command_line": ["chromium", "--user-data-dir=private-profile", "http://127.0.0.1:1234/"],
            "display": ":98", "pids": {"xvfb": 101, "openbox": 102, "chromium": 103},
            "versions": {"xvfb": "1", "openbox": "1", "chromium": "1"},
            "cleanup": [{"pid": pid, "return_code": -15} for pid in (101, 102, 103)],
            "gui_commands": [
                ["xdotool", "windowactivate", "--sync", "200"],
                ["xdotool", "type", "--clearmodifiers", "--delay", "20", nonce],
                ["xdotool", "key", "--clearmodifiers", "Tab", "Return"],
            ],
            "artifacts": artifacts,
        }

    def download_session_file(self, session_id, relative_path, target):
        self.calls.append(("download", session_id, relative_path))
        if self.download_error:
            raise requests.HTTPError("Files API unavailable")
        target.write_bytes(self.files[relative_path])
        return {"status_code": 200}

    def get_session(self, session_id):
        raise AssertionError("G2 must not query session details to observe idle")

    def capture_session_logs(self, session_id, version, path):
        self.calls.append(("logs", session_id))
        path.write_text("data: unknown raw lifecycle format\n\n", encoding="utf-8")
        return {"bytes": path.stat().st_size, "path": path.name}

    def stop_session(self, session_id):
        self.calls.append(("stop", session_id))
        if self.stop_error and session_id == "A":
            raise requests.HTTPError("stop failed")
        return {"stopped": True}


class GateCompletionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.args = argparse.Namespace(
            output=self.root / "evidence", run_id="test-run", poc_id="poc",
            project_endpoint="https://project.invalid", agent_name="agent", agent_version="6",
            image_digest="sha256:new-image", expected_source=SOURCE, include_g2=True,
            g0_only=False, capture_logs=True, idle_seconds=330, idle_timeout_seconds=300,
        )

    def tearDown(self):
        self.directory.cleanup()

    def execute(self, client):
        original_sleep = time.sleep
        with patch("run_poc.time.sleep", side_effect=lambda seconds: original_sleep(seconds) if seconds < 1 else None) as sleep:
            result = run(self.args, client)
        self.assertEqual([call for call in client.calls if call[0] == "create"], [("create", "A"), ("create", "B")])
        self.assertEqual([call for call in client.calls if call[0] == "stop"], [("stop", "A"), ("stop", "B")])
        for alias in ("A", "B"):
            self.assertLess(client.calls.index(("logs", alias)), client.calls.index(("stop", alias)))
        self.assertTrue((self.args.output / self.args.run_id / "run-manifest.json").exists())
        return result, sleep

    def test_full_gates_application_remount_and_direct_handoff(self):
        client = FakeHostedClient(self.root)
        result, sleep = self.execute(client)
        self.assertTrue(run_succeeded(self.args, result), result["gates"])
        self.assertEqual(result["gates"]["G2"]["status"], "PASS_WITH_APPLICATION_REMOUNT")
        self.assertEqual(result["controller_binary_sha256"], result["artifacts"]["binary-v2.bin"]["sha256"])
        self.assertEqual(result["artifacts"]["binary-v2.bin"]["size"], 1024 * 1024)
        self.assertEqual([item["owner"] for item in result["owner_history"][:4]], ["A", "B", "B", "B"])
        self.assertEqual([call.args[0] for call in sleep.call_args_list if call.args[0] >= 1], [330])
        events = result["events"]
        steps = [event["request"].get("step_id") for event in events]
        self.assertLess(steps.index("g1-b-rename"), steps.index("g1-a-inspect-old-name"))
        self.assertLess(steps.index("g1-a-inspect-old-name"), steps.index("g1-a-inspect-new-name"))
        resume = result["g2"]["resume_event_index"]
        self.assertEqual(events[resume]["request"]["operation"], "status")
        self.assertEqual(events[resume]["session_alias"], "A")
        self.assertLess(steps.index("g2-b-write-v3"), resume)
        self.assertLess(resume, steps.index("g2-a-remount"))
        self.assertLess(steps.index("g2-a-remount"), steps.index("g2-a-read-v3"))
        self.assertLess(steps.index("g2-a-write-v4"), steps.index("g2-b-read-v4"))
        self.assertLess(steps.index("g2-b-read-v4"), steps.index("g3-desktop"))
        self.assertTrue(all(item["verified"] for item in result["downloads"]))

    def test_platform_auto_never_calls_a_remount(self):
        result, _ = self.execute(FakeHostedClient(self.root, remount=False))
        self.assertEqual(result["gates"]["G2"]["status"], "PASS_PLATFORM_AUTO")
        self.assertFalse(any(event["request"].get("step_id") == "g2-a-remount" for event in result["events"]))

    def test_unproven_resume_blocks_g2_but_still_completes_g3(self):
        for start_type in (None, "warm", "cold", "Resume", "unknown"):
            with self.subTest(start_type=start_type):
                self.args.run_id = f"run-{start_type}"
                result, _ = self.execute(FakeHostedClient(self.root, start_type=start_type))
                self.assertEqual(result["gates"]["G2"]["status"], "BLOCKED")
                self.assertEqual(result["gates"]["G2"]["reason_code"], "RESUME_NOT_PROVEN")
                self.assertTrue(result["g2"]["handoff_verified"])
                self.assertEqual(result["gates"]["G3"]["status"], "PASS")
                self.assertFalse(run_succeeded(self.args, result))

    def test_wrong_mount_cannot_be_platform_auto(self):
        client = FakeHostedClient(self.root, remount=False)
        client.resume_source = "//wrong/share"
        result, _ = self.execute(client)
        self.assertEqual(result["gates"]["G2"]["status"], "PARTIAL")
        self.assertFalse(any(event["request"].get("step_id") == "g2-a-read-v3" for event in result["events"]))
        self.assertEqual(result["gates"]["G3"]["status"], "PASS")

    def test_mount_race_does_not_claim_application_remount(self):
        client = FakeHostedClient(self.root)
        client.mount_mode = "already_present"
        result, _ = self.execute(client)
        self.assertEqual(result["gates"]["G2"]["status"], "PARTIAL")
        self.assertEqual(result["gates"]["G2"]["remount_mode"], "unknown")
        self.assertEqual(result["g2"]["application_mount_result"]["mount_mode"], "already_present")

    def test_g1_requires_a_independent_absence(self):
        client = FakeHostedClient(self.root)
        client.old_name_visible = True
        result, sleep = self.execute(client)
        self.assertEqual(result["gates"]["G1"]["status"], "PARTIAL")
        self.assertEqual(result["gates"]["G2"]["status"], "NOT_RUN_DEPENDENCY")
        self.assertFalse(any(call.args[0] >= 1 for call in sleep.call_args_list))
        self.assertFalse(any(call[0] == "download" for call in client.calls))

    def test_unrequested_g2_is_not_failure_or_pass(self):
        self.args.include_g2 = False
        result, sleep = self.execute(FakeHostedClient(self.root))
        self.assertEqual(result["gates"]["G2"]["status"], "NOT_RUN_OPTIONAL")
        self.assertTrue(run_succeeded(self.args, result))
        self.assertFalse(any(call.args[0] >= 1 for call in sleep.call_args_list))

    def test_files_failure_is_partial_and_never_reinvokes_desktop(self):
        client = FakeHostedClient(self.root)
        client.download_error = True
        result, _ = self.execute(client)
        self.assertEqual(result["gates"]["G3"]["status"], "PARTIAL")
        self.assertEqual(result["gates"]["G3"]["reason_code"], "GUI_OR_BROWSER_RUNTIME")
        self.assertEqual(sum(event["request"]["operation"] == "desktop_smoke" for event in result["events"]), 1)
        self.assertEqual(len(result["downloads"]), 3)
        self.assertTrue(all("error" in item for item in result["downloads"]))

    def test_ambiguous_write_timeout_is_recorded_and_never_retried(self):
        client = FakeHostedClient(self.root)
        client.fail_step = "g1-b-write-v2"
        result, _ = self.execute(client)
        self.assertEqual(result["gates"]["G1"]["status"], "PARTIAL")
        attempts = [event for event in result["events"] if event["request"].get("step_id") == client.fail_step]
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["transport_error"]["type"], "Timeout")
        self.assertNotIn("response", attempts[0])
        self.assertEqual(result["gates"]["G2"]["status"], "NOT_RUN_DEPENDENCY")

    def test_failed_stop_is_non_success_and_b_still_stops(self):
        client = FakeHostedClient(self.root)
        client.stop_error = True
        result, _ = self.execute(client)
        self.assertEqual(result["gates"]["G3"]["status"], "PASS")
        self.assertFalse(run_succeeded(self.args, result))
        self.assertIn("error", result["cleanup"][0])

    def test_invalid_idle_setting_is_blocked_without_sleep(self):
        self.args.idle_seconds = self.args.idle_timeout_seconds
        result, sleep = self.execute(FakeHostedClient(self.root))
        self.assertEqual(result["gates"]["G2"]["status"], "BLOCKED")
        self.assertEqual(result["gates"]["G3"]["status"], "PASS")
        self.assertFalse(any(call.args[0] >= 1 for call in sleep.call_args_list))

    def test_g3_only_uses_the_same_verification_with_one_session(self):
        client = FakeHostedClient(self.root)
        result = run_g3(self.args, client)
        self.assertEqual(result["status"], "PASS", result)
        self.assertEqual(result["shared_publish"], "NOT_RUN_DEPENDENCY")
        self.assertEqual(client.created, 1)
        self.assertEqual([call for call in client.calls if call[0] == "stop"], [("stop", "A")])
        self.assertLess(client.calls.index(("logs", "A")), client.calls.index(("stop", "A")))
        self.assertFalse(any(
            call[0] == "invoke" and call[2]["operation"] == "mount_shared" for call in client.calls
        ))


class GateBoundaryTests(unittest.TestCase):
    def test_files_rest_uses_mi_and_home_relative_query_without_azd(self):
        credential = Mock()
        credential.get_token.return_value.token = "test-token"
        with tempfile.TemporaryDirectory() as directory, patch("run_poc.requests.Session") as session:
            client = HostedPocClient("https://project.invalid", "agent", Path(directory), credential)
            response = session.return_value.get.return_value.__enter__.return_value
            response.status_code = 200
            response.iter_content.return_value = [b"content"]
            target = Path(directory) / "artifact"
            result = client.download_session_file("A", "fa-poc/evidence/run/before.png", target)
            self.assertEqual(target.read_bytes(), b"content")
            self.assertEqual(result["bytes"], 7)
            args, kwargs = session.return_value.get.call_args
            self.assertEqual(args[0], "https://project.invalid/agents/agent/endpoint/sessions/A/files/content")
            self.assertEqual(kwargs["params"], {"api-version": "v1", "path": "fa-poc/evidence/run/before.png"})
            self.assertFalse(kwargs["allow_redirects"])
            credential.get_token.assert_called_once_with("https://ai.azure.com/.default")
            for path in ("/team/file", "../secret", "x\\y", "https://other/file", "a//b"):
                with self.subTest(path=path), self.assertRaises(ValueError):
                    client.download_session_file("A", path, target)
            self.assertEqual(session.return_value.get.call_count, 1)

    def test_invocation_requests_official_latency_headers_once(self):
        credential = Mock()
        with patch("run_poc.requests.Session") as session:
            client = HostedPocClient("https://project.invalid", "agent", Path("."), credential)
            response = session.return_value.post.return_value
            response.status_code = 200
            response.headers = {"X-Ms-Debug-Latency-Session-Start-Type": "resume"}
            response.json.return_value = {"status": "success"}
            result = client.invoke("A", {"operation": "status"})
            session.return_value.post.assert_called_once()
            kwargs = session.return_value.post.call_args.kwargs
            self.assertEqual(kwargs["headers"]["x-ms-debug-latency-enabled"], "true")
            self.assertFalse(kwargs["allow_redirects"])
            self.assertEqual(result["headers"], dict(response.headers))

    def test_png_verification_rejects_corruption_and_truncation(self):
        self.assertEqual(verify_png(png()), (2, 2))
        for value in (b"not a PNG", png()[:-1], png()[:-8] + b"corrupt!"):
            with self.subTest(value=value[:10]), self.assertRaises(RuntimeError):
                verify_png(value)

    def test_g3_cleanup_and_gui_input_must_be_this_run(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = FakeHostedClient(root)
            (root / "team" / "poc" / "poc" / "run").mkdir(parents=True)
            for mutation in ("cleanup", "gui_commands", "browser_uid", "browser_command_line"):
                result = client.desktop(payload("poc", "run", "desktop_smoke", nonce="nonce"))
                if mutation == "cleanup":
                    result[mutation][0]["pid"] = 999
                elif mutation == "gui_commands":
                    result[mutation][1][-1] = "other-nonce"
                elif mutation == "browser_uid":
                    result[mutation] = None
                else:
                    result[mutation] = []
                with self.subTest(mutation=mutation), self.assertRaises(RuntimeError):
                    verify_result(result, "nonce")

    def test_desktop_dispatch_adds_byte_counts_to_all_private_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = root / "image.png"
            image.write_bytes(png())
            dispatcher = PocDispatcher(home=root / "home", shared_root=root / "team", runtime_root=root / "runtime")
            with patch("poc_core.run_desktop_smoke", return_value={
                "artifacts": {key: {"path": str(image)} for key in ("before", "after", "browser_download")},
            }):
                result = dispatcher.dispatch(payload(
                    "poc", "run", "desktop_smoke", step_id="desktop", nonce="nonce",
                ))["result"]
            self.assertTrue(all(item["size"] == image.stat().st_size for item in result["artifacts"].values()))

    def test_private_artifact_path_and_hash_are_verified(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = FakeHostedClient(root)
            (root / "team" / "poc" / "poc" / "run").mkdir(parents=True)
            result = client.desktop(payload("poc", "run", "desktop_smoke", nonce="nonce"))
            verify_result(result, "nonce")
            values = dict(
                home="/mnt/agent/session", poc_id="poc", run_id="run", nonce="nonce",
                run_dir=root, downloads=[], save=lambda: None,
            )
            result["artifacts"]["before"]["path"] = "/team/before.png"
            with self.assertRaisesRegex(RuntimeError, "outside this run"):
                download_and_verify(client, "A", result, **values)
            self.assertEqual(client.calls, [])
            result["artifacts"]["before"]["path"] = "/mnt/agent/session/fa-poc/evidence/poc/run/g3-desktop/before.png"
            result["artifacts"]["before"]["sha256"] = "wrong"
            with self.assertRaisesRegex(RuntimeError, "hash differs"):
                download_and_verify(client, "A", result, **values)

    def test_matching_hash_cannot_hide_invalid_or_identical_pngs_or_wrong_download(self):
        for mutation in ("invalid_png", "identical_pngs", "wrong_download"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                client = FakeHostedClient(root)
                result = client.desktop(payload("poc", "run", "desktop_smoke", nonce="nonce"))
                key = "browser_download" if mutation == "wrong_download" else "after"
                data = {"invalid_png": b"not PNG", "identical_pngs": png(), "wrong_download": b"other run"}[mutation]
                artifact = result["artifacts"][key]
                client.files[artifact["path"].removeprefix("/mnt/agent/session/")] = data
                artifact.update(sha256=hashlib.sha256(data).hexdigest(), size=len(data))
                with self.assertRaises(RuntimeError):
                    download_and_verify(
                        client, "A", result, home="/mnt/agent/session", poc_id="poc", run_id="run",
                        nonce="nonce", run_dir=root, downloads=[], save=lambda: None,
                    )

    def test_inspect_requires_approved_cifs_and_reports_real_absence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dispatcher = PocDispatcher(home=root / "home", shared_root=root / "team", runtime_root=root / "runtime")
            request = payload(
                "poc", "run", "shared_io_step", step_id="inspect-missing", action="inspect",
                relative_path="missing.txt",
            )
            with patch.dict(os.environ, {
                "POC_STORAGE_ACCOUNT_NAME": "approvedaccount", "POC_FILE_SHARE_NAME": "team-poc",
            }), patch("poc_core.mount_details", return_value=MOUNT):
                result = dispatcher.dispatch(request)["result"]
                self.assertIs(result["exists"], False)
                self.assertEqual(result["team_mount"]["source"], SOURCE)
                self.assertFalse((root / "team" / "poc").exists())
                write = payload(
                    "poc", "run", "shared_io_step", step_id="write", action="write_text",
                    relative_path="present.txt", content="content",
                )
                written = dispatcher.dispatch(write)["result"]
                present = dispatcher.dispatch({
                    **request, "step_id": "inspect-present", "relative_path": "present.txt",
                })["result"]
                self.assertIs(present["exists"], True)
                self.assertEqual(present["sha256"], written["sha256"])
                for unsafe in ("../escape", "/absolute", "nested/../../escape"):
                    with self.subTest(unsafe=unsafe), self.assertRaises(PocError):
                        dispatcher.dispatch({**request, "step_id": "unsafe", "relative_path": unsafe})
            for invalid in ({"mounted": False}, {**MOUNT, "source": "//wrong/share"}, {**MOUNT, "filesystem_type": "ext4"}):
                with patch("poc_core.mount_details", return_value=invalid), self.assertRaises(PocError):
                    dispatcher.dispatch({**request, "step_id": "wrong-mount"})

    def test_inspect_cannot_treat_dangling_symlink_as_absent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dispatcher = PocDispatcher(home=root / "home", shared_root=root / "team", runtime_root=root / "runtime")
            with patch.dict(os.environ, {
                "POC_STORAGE_ACCOUNT_NAME": "approvedaccount", "POC_FILE_SHARE_NAME": "team-poc",
            }), patch("poc_core.mount_details", return_value=MOUNT), patch.object(
                Path, "is_symlink", return_value=True
            ), self.assertRaises(PocError):
                dispatcher.dispatch(payload(
                    "poc", "run", "shared_io_step", step_id="inspect-link", action="inspect",
                    relative_path="link",
                ))

    def test_inspect_hash_and_size_are_both_required(self):
        request = {"action": "inspect", "relative_path": "new", "expected_exists": True}
        for result in (
            {"exists": True, "is_file": True, "sha256": "hash", "size": 2},
            {"exists": False}, {"exists": True, "is_file": False},
        ):
            with self.subTest(result=result), self.assertRaises(RuntimeError):
                verify_io(request, result, {"new": {"sha256": "hash", "size": 1}})


if __name__ == "__main__":
    unittest.main()
