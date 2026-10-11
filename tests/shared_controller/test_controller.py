from __future__ import annotations

import base64
import copy
import hashlib
import importlib.util
import json
import os
import shutil
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "validate-shared-hosted.py"
SPEC = importlib.util.spec_from_file_location("shared_hosted_controller", SCRIPT)
controller = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = controller
SPEC.loader.exec_module(controller)


def raw(body, status=200, prefix=""):
    return prefix + f"HTTP/1.1 {status} Synthetic\r\nContent-Type: application/json\r\n\r\n" + json.dumps(body)


class FakeClock:
    def __init__(self):
        self.value = 0

    def __call__(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


class FakeWorld:
    """Fake CLI and data-plane model only: never launches azd or touches Azure."""

    def __init__(self):
        self.calls = []
        self.state = None
        self.memories = {}
        self.operations = {}
        self.sessions = {"session-a": "active", "session-b": "active"}
        self.boots = {"session-a": 1, "session-b": 1}
        self.number = 0
        self.fail_create = False
        self.storage_status = 403
        self.wrong_runtime = False
        self.after_create = None

    def search(self, scope):
        self.number += 1
        memories = [{"memory_item": {"content": f"Fixture {fixture['marker']} uses {fixture['value']}."}}
                    for fixture in self.memories.get(scope, [])]
        return {"scope": scope, "native": {"search_id": "search-" + str(self.number), "memories": memories}}

    def replace(self, changes):
        self.state.update(changes)
        self.state["revision"] += 1
        self.state["etag"] = f'W/"revision-{self.state["revision"]}"'
        return copy.deepcopy(self.state)

    def api(self, actor, payload):
        method, path, body = payload["method"], payload["path"], payload.get("body", {})
        if path.startswith("/teams/other-"):
            return 403, {"error": {"code": "CROSS_TEAM_DENIED"}}
        if "scope" in body or "owner" in body:
            return 422, {"error": {"code": "INVALID_REQUEST"}}
        if "/memory/" in path:
            private = "/memory/private/agent-a" in path
            scope = "private:team-one:agent-a" if private else "team:team-one"
            if private and actor == "B":
                return 403, {"error": {"code": "PRIVATE_TARGET_DENIED"}}
            if "/operations/" in path:
                operation = path.rsplit("/", 1)[-1]
                stored = self.operations[operation]
                if actor == "B" and stored["scope"].startswith("private:"):
                    return 403, {"error": {"code": "PRIVATE_TARGET_DENIED"}}
                return 200, {"operation_id": operation, "scope": stored["scope"], "native": {
                    "update_id": stored["update_id"], "status": "completed",
                    "result": {"memory_operations": [{"kind": "created"}], "usage": {"total_tokens": 10}},
                }}
            if path.endswith("/search"):
                return 200, self.search(scope)
            operation = uuid4().hex
            update_id = "update-" + operation
            self.memories.setdefault(scope, []).append(body)
            self.operations[operation] = {"scope": scope, "update_id": update_id}
            return 202, {"operation_id": operation, "scope": scope, "native": {
                "update_id": update_id, "status": "queued",
            }}
        if path.endswith("/context"):
            return 200, {
                "actor": "agent-" + actor.lower(), "team": "team-one", "work": copy.deepcopy(self.state),
                "role_instruction": "Synthetic server configured role.",
                "team_memory": self.search("team:team-one"),
                "private_memory": self.search("private:team-one:agent-" + actor.lower()),
            }
        if method == "POST" and not path.endswith(("/handoff", "/publish")):
            if self.fail_create:
                return 503, {"error": {"code": "synthetic failure"}}
            if self.state is not None:
                return 409, {"error": {"code": "ALREADY_EXISTS"}}
            text = body["artifact_text"]
            self.state = {
                "work_id": path.rsplit("/", 1)[-1], "team": "team-one", "owner": "agent-a",
                "goal": body["goal"], "progress": "", "status": "draft", "revision": 1, "etag": 'W/"revision-1"',
                "artifact": {"text": text, "sha256": hashlib.sha256(text.encode()).hexdigest(), "version": 1},
            }
            if self.after_create:
                self.after_create()
            return 201, copy.deepcopy(self.state)
        if method == "GET":
            return 200, copy.deepcopy(self.state)
        if self.state["status"] == "published":
            return 409, {"error": {"code": "PUBLISHED_IMMUTABLE"}}
        if self.state["owner"] != "agent-" + actor.lower():
            return 403, {"error": {"code": "NOT_OWNER"}}
        if "etag" not in payload:
            return 428, {"error": {"code": "ETAG_REQUIRED"}}
        if payload["etag"] != self.state["etag"]:
            return 412, {"error": {"code": "ETAG_CONFLICT"}}
        if path.endswith("/handoff"):
            return 200, self.replace({"owner": body["to_actor"]})
        if path.endswith("/publish"):
            return 200, self.replace({"status": "published", "published_by": "agent-" + actor.lower()})
        changes = copy.deepcopy(body)
        if "artifact_text" in changes:
            text = changes.pop("artifact_text")
            changes["artifact"] = {
                "text": text, "sha256": hashlib.sha256(text.encode()).hexdigest(),
                "version": self.state["artifact"]["version"] + 1,
            }
        return 200, self.replace(changes)

    def __call__(self, args, cwd, env, timeout):
        self.calls.append({"args": args, "cwd": cwd, "env": env, "timeout": timeout})
        if args[3] == "sessions":
            action, session = args[4:6]
            if action == "stop":
                self.sessions[session] = "idle"
                return controller.CommandResult(0, '{"status":"accepted"}')
            if action == "show":
                return controller.CommandResult(0, json.dumps({"id": session, "status": self.sessions[session]}))
            raise AssertionError("forbidden session operation: " + action)
        assert args[3] == "invoke"
        assert args[4] == "--agent-endpoint"
        agent = args[5].split("/agents/")[1].split("/")[0]
        actor = "A" if agent == "fa-context-a" else "B"
        session = args[args.index("--session-id") + 1]
        if self.sessions[session] == "idle":
            self.sessions[session] = "active"
            self.boots[session] += 1
        request_file = args[args.index("--input-file") + 1]
        payload = json.loads(Path(request_file).read_text(encoding="utf-8"))
        operation = payload["operation"]
        result = {"operation": operation, "runtime": {
            "pid": 100 if actor == "A" else 101,
            "process_boot_marker": f"{actor}-boot-{self.boots[session]}",
            "home_marker": actor + "-home", "fixed_vm_validated": False,
            "session_id": "wrong-session" if self.wrong_runtime else session, "invocation_id": uuid4().hex,
        }}
        if operation == "api":
            status, content = self.api(actor, payload)
            result["outcome"] = {"http_status": status, "content": content, "truncated": False}
        elif operation == "api_race":
            attempts = []
            for update in payload["updates"]:
                status, content = self.api(actor, {
                    "method": "PATCH", "path": payload["path"], "etag": payload["etag"], "body": update,
                })
                attempts.append({"http_status": status, "content": content})
            result["outcome"] = {"attempts": attempts, "concurrency": 2, "same_client_identity": True}
        elif operation in ("direct_storage", "direct_memory"):
            status = self.storage_status if operation == "direct_storage" else 403
            result["outcome"] = {
                "http_status": status, "access_denied": status in (401, 403),
                "unexpected_success": status == 200, "content": {"code": "synthetic-access-denied"},
            }
        elif operation == "gui":
            artifacts = {}
            for name in ("before", "after"):
                data = b"\x89PNG\r\n\x1a\nfake-" + name.encode()
                artifacts[name] = {"png_base64": base64.b64encode(data).decode(),
                                   "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
            text = f"shared-context-poc|{payload['run_id']}|{payload['nonce']}|GUI_COMPLETE\n"
            artifacts["browser_download"] = {"text": text, "size": len(text.encode()),
                                             "sha256": hashlib.sha256(text.encode()).hexdigest()}
            result["outcome"] = {"probe_status": "passed", "browser_uid": 10001,
                                 "browser_uid_fields": [10001] * 4, "browser_sandbox_disabled": False,
                                 "browser_command_line": ["chromium", "--no-first-run", "http://127.0.0.1:8000/"],
                                 "page_ready": True, "completed_value": payload["nonce"],
                                 "cleanup": {"success": True, "server_stopped": True, "files_removed": True, "errors": []},
                                 "artifacts": artifacts}
        elif operation != "runtime":
            raise AssertionError("unknown invocation operation")
        return controller.CommandResult(0, raw(result), "synthetic CLI stderr")


class ParserTests(unittest.TestCase):
    def test_azd_error_trailer_preserves_http_failure_and_application_error(self):
        body = {"error": {"code": "ENVIRONMENT_HOME_NOT_WRITABLE", "errno": 13}}
        trailer = (
            "ERROR: POST https://synthetic.services.ai.azure.com/api/projects/poc/agents/a/"
            "endpoint/protocols/invocations?api-version=v1&agent_session_id=fixture "
            "failed with HTTP 503: 503 Service Unavailable\n"
        )
        status, _, actual = controller.parse_raw_http(raw(body, 503) + trailer)
        self.assertEqual(status, 503)
        self.assertEqual(actual, body)
        for packet in (raw(body, 200) + trailer, raw(body, 403) + trailer,
                       raw(body, 503) + trailer + '{"unexpected":true}'):
            with self.subTest(packet=packet), self.assertRaises(ValueError):
                controller.parse_raw_http(packet)

    def test_extension_update_notice_after_complete_json_is_not_http_body(self):
        notice = (
            "WARNING: A new version of extension 'Foundry agents (Beta)' is available: "
            "1.0.0-beta.13 -> 1.0.0-beta.15\n"
            "  \u2022 To update: azd extension update azure.ai.agents\n"
            "  \u2022 To update all: azd extension update --all\n"
        )
        status, _, body = controller.parse_raw_http(raw({"operation": "runtime"}) + notice)
        self.assertEqual((status, body), (200, {"operation": "runtime"}))
        self.assertEqual(controller.parse_show('{"status":"idle"}' + notice), {"status": "idle"})

    def test_unknown_trailing_data_or_second_json_remains_an_error(self):
        for tail in ('{"other":true}', "WARNING: arbitrary failure", "garbage"):
            with self.subTest(tail=tail), self.assertRaises(ValueError):
                controller.parse_raw_http(raw({"operation": "runtime"}) + tail)
            with self.subTest(tail=tail), self.assertRaises(ValueError):
                controller.parse_show('{"status":"idle"}' + tail)

    def test_http_status_headers_prefix_and_continue(self):
        status, headers, body = controller.parse_raw_http(raw({"operation": "runtime"}, prefix=(
            "\x1b[32mLocal azd progress\x1b[0m\r\nHTTP/1.1 100 Continue\r\n\r\n"
        )))
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "application/json")
        self.assertEqual(body, {"operation": "runtime"})
        self.assertEqual(controller.parse_raw_http("HTTP/1.1 403 Forbidden\n\n{}")[0], 403)

    def test_reject_non_http_nonobject_or_truncated_json(self):
        for output in ('{"fake":true}', "HTTP/1.1 200 OK\n\n[]", "HTTP/1.1 200 OK\n\n{", "No response"):
            with self.subTest(output=output), self.assertRaises(ValueError):
                controller.parse_raw_http(output)

    def test_redact_request_credentials_and_output_tokens(self):
        text = 'Authorization: Bearer synthetic\n{"access_token":"opaque-secret","message":"Bearer eyJabc.def.ghi"}'
        redacted = controller.redact(text)
        self.assertNotIn("synthetic", redacted)
        self.assertNotIn("opaque-secret", redacted)
        self.assertNotIn("eyJabc", redacted)
        for payload in ({"Authorization": "opaque"}, {"body": {"access_token": "opaque"}}, {"body": "Bearer opaque"}):
            with self.assertRaises(ValueError):
                controller.reject_request_secrets(payload)

    def test_idle_is_explicit_not_stopped_or_unknown(self):
        self.assertEqual(controller.show_state(controller.parse_show('progress\n{"properties":{"state":"Idle"}}')), "idle")
        self.assertEqual(controller.show_state({"status": {"state": "Active"}}), "active")
        self.assertNotEqual(controller.show_state({"status": "Stopped"}), "idle")
        self.assertIsNone(controller.show_state({"provisioningState": "Succeeded"}))


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parent / (".controller-fixture-" + uuid4().hex)
        self.root.mkdir()
        self.addCleanup(lambda: shutil.rmtree(self.root) if self.root.exists() else None)
        self.clock = FakeClock()
        self.world = FakeWorld()

    def make(self, gui=False, **kwargs):
        for actor in ("a", "b"):
            kwargs.setdefault("endpoint_" + actor, (
                "https://synthetic.services.ai.azure.com/api/projects/synthetic"
                f"/agents/fa-context-{actor}/endpoint/protocols/invocations?api-version=v1"
            ))
        config = controller.Config(self.root, "synthetic-env", "session-a", "session-b",
                                   self.root / "evidence", gui=gui, **kwargs)
        return controller.Controller(config, self.world, self.clock, self.clock.sleep)

    def test_invalid_or_mismatched_endpoints_fail_before_any_command(self):
        valid = (
            "https://synthetic.services.ai.azure.com/api/projects/synthetic"
            "/agents/fa-context-a/endpoint/protocols/invocations?api-version=v1"
        )
        invalid = [
            valid.replace("https:", "http:"),
            valid.replace("https://", "https://user@"),
            valid.replace(".azure.com", ".azure.com.example.org"),
            valid.replace("fa-context-a", "fa-context-b"),
            valid.replace("/invocations?", "/responses?"),
            valid.replace("/projects/synthetic/", "/projects/different/"),
            valid + "&extra=true",
            valid + "#fragment",
        ]
        for endpoint in invalid:
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                self.make(endpoint_a=endpoint)
        self.assertEqual(self.world.calls, [])

    def test_transport_logs_preserve_crlf_without_windows_expansion(self):
        instance = self.make()
        output = "HTTP/2.0 503 Service Unavailable\r\nContent-Type: application/json\r\n\r\n{}\r\n"
        errors = "synthetic CLI message\r\n"
        instance.runner = lambda *args: controller.CommandResult(0, stdout=output, stderr=errors)
        instance.command("line-endings", ["synthetic"])
        call = instance.report["commands"][-1]
        self.assertEqual((instance.directory / call["stdout"]).read_bytes(), output.encode())
        self.assertEqual((instance.directory / call["stderr"]).read_bytes(), errors.encode())

    def test_full_fake_flow_with_gui_no_cloud(self):
        instance = self.make(gui=True)
        self.assertEqual(instance.run(), 0, instance.report["gates"])
        self.assertTrue(all(gate["status"] == "PASS" for gate in instance.report["gates"].values()))
        self.assertEqual(self.world.sessions, {"session-a": "idle", "session-b": "idle"})
        self.assertEqual(instance.report["memory_usage"]["accepted_updates"], 3)
        self.assertEqual(instance.report["memory_usage"]["completed_updates"], 3)
        self.assertEqual(instance.report["memory_usage"]["accepted_searches"], 8)
        self.assertEqual(instance.report["memory_usage"]["total_accepted_updates_including_parent"], 4)
        self.assertEqual(instance.report["memory_usage"]["total_accepted_searches_including_parent"], 9)
        self.assertLessEqual(instance.report["memory_usage"]["update_attempt_upper_bound"], 8)
        self.assertLessEqual(instance.report["memory_usage"]["search_attempt_upper_bound"], 32)
        self.assertEqual(instance.report["latest_work"]["status"], "published")
        self.assertFalse(instance.report["fixed_vm_validated"])
        self.assertFalse(instance.report["gates"]["direct_storage_a"]["separate_rbac_proof"])
        self.assertTrue((instance.directory / "gui-before.png").is_file())
        for record in instance.report["commands"]:
            self.assertTrue((instance.directory / record["stdout"]).exists())
            self.assertTrue((instance.directory / record["stderr"]).exists())
        self.assertTrue((instance.directory / "report.json").exists())

    def test_all_commands_bind_service_session_project_env_and_inherit_login(self):
        with patch.dict(os.environ, {"AZURE_CONFIG_DIR": "synthetic-login-dir", "AZURE_DEV_USER_AGENT": "original"}):
            instance = self.make()
            self.assertEqual(instance.run(), 0)
            self.assertEqual(os.environ["AZURE_DEV_USER_AGENT"], "original")
        for call in self.world.calls:
            args = call["args"]
            self.assertEqual(args[:3], ["azd", "ai", "agent"])
            self.assertEqual(args[args.index("-C") + 1], str(self.root.resolve()))
            self.assertEqual(args[args.index("-e") + 1], "synthetic-env")
            self.assertEqual(call["env"]["AZURE_CONFIG_DIR"], "synthetic-login-dir")
            self.assertEqual(call["env"]["AZURE_DEV_USER_AGENT"], "microsoft_foundry_skill")
            self.assertLessEqual(call["timeout"], 180)
            self.assertGreater(call["timeout"], 0)
            if args[3] == "invoke":
                self.assertEqual(args[4], "--agent-endpoint")
                self.assertNotIn("fa-context-a", args)
                self.assertNotIn("fa-context-b", args)
                self.assertNotIn("--version", args)
                self.assertNotIn("--protocol", args)
                actor = "a" if args[args.index("--session-id") + 1] == "session-a" else "b"
                self.assertEqual(args[5], getattr(instance.config, "endpoint_" + actor))
                self.assertIn(args[args.index("--session-id") + 1], ("session-a", "session-b"))
                self.assertTrue(Path(args[args.index("--input-file") + 1]).is_absolute())
                self.assertEqual(args[args.index("--output") + 1], "raw")
            else:
                self.assertIn(args[4], ("stop", "show"))
                self.assertIn("--agent-name", args)
            self.assertNotIn("--new-session", args)
        self.assertEqual(instance.report["gates"]["gui"]["status"], "NOT_RUN")

    def test_failure_preserves_evidence_marks_dependents_not_run_and_stops_both(self):
        self.world.fail_create = True
        instance = self.make()
        self.assertEqual(instance.run(), 1)
        self.assertEqual(instance.report["gates"]["shared_state"]["status"], "FAIL")
        self.assertEqual(instance.report["gates"]["same_etag_cas"]["status"], "NOT_RUN")
        self.assertEqual(instance.report["gates"]["memory_extraction"]["status"], "NOT_RUN")
        self.assertEqual(instance.report["gates"]["cleanup_a"]["status"], "PASS")
        self.assertEqual(instance.report["gates"]["cleanup_b"]["status"], "PASS")
        payloads = [json.loads((instance.directory / call["request"]).read_text()) for call in instance.report["commands"]
                    if "request" in call]
        creates = [p for p in payloads if p.get("method") == "POST" and "/works/" in p.get("path", "")]
        self.assertEqual(len(creates), 1)

    def test_time_budget_reserves_cleanup_and_starts_no_more_functional_calls(self):
        instance = self.make(max_seconds=1000)
        self.world.after_create = lambda: setattr(self.clock, "value", 761)
        self.assertEqual(instance.run(), 1)
        self.assertIn("BudgetExceeded", instance.report["gates"]["shared_state"]["reason"])
        self.assertEqual(instance.report["gates"]["cleanup_a"]["status"], "PASS")
        self.assertEqual(instance.report["gates"]["cleanup_b"]["status"], "PASS")
        self.assertEqual(sum(call["args"][3] == "invoke" for call in self.world.calls), 3)

    def test_direct_storage_404_never_counts_as_denial(self):
        self.world.storage_status = 404
        instance = self.make()
        self.assertEqual(instance.run(), 1)
        self.assertEqual(instance.report["gates"]["direct_storage_a"]["status"], "FAIL")
        self.assertEqual(instance.report["gates"]["direct_memory_a"]["status"], "PASS")
        self.assertEqual(instance.report["gates"]["cleanup_b"]["status"], "PASS")

    def test_wrong_runtime_session_cannot_pass(self):
        self.world.wrong_runtime = True
        instance = self.make()
        self.assertEqual(instance.run(), 1)
        self.assertEqual(instance.report["gates"]["runtime_binding"]["status"], "FAIL")
        self.assertEqual(instance.report["gates"]["shared_state"]["status"], "NOT_RUN")
        self.assertEqual(instance.report["gates"]["cleanup_b"]["status"], "PASS")

    def test_memory_budget_checks_before_starting_call(self):
        instance = self.make()
        instance.report["memory_usage"]["update_attempt_upper_bound"] = 8
        with self.assertRaises(controller.BudgetExceeded):
            instance.invoke("A", {"operation": "api", "method": "POST", "path": "/teams/team-one/memory/team",
                                  "body": {"marker": "synthetic", "value": "amber"}}, "over-budget")
        self.assertEqual(self.world.calls, [])

    def test_rejected_secret_request_never_saved_or_launched(self):
        instance = self.make()
        with self.assertRaises(ValueError):
            instance.invoke("A", {"operation": "api", "Authorization": "opaque"}, "bad-secret")
        self.assertEqual(self.world.calls, [])
        self.assertEqual(list(instance.directory.glob("*.request.json")), [])

    def test_runner_failure_is_retained_and_cleanup_still_runs(self):
        original = self.world

        def failed_once(args, cwd, env, timeout):
            if args[3] == "invoke":
                return controller.CommandResult(-1, stderr="Authorization: Bearer synthetic-secret", timed_out=True)
            return original(args, cwd, env, timeout)

        instance = self.make()
        instance.runner = failed_once
        self.assertEqual(instance.run(), 1)
        self.assertEqual(instance.report["gates"]["cleanup_a"]["status"], "PASS")
        self.assertEqual(instance.report["gates"]["cleanup_b"]["status"], "PASS")
        text = "\n".join(path.read_text(encoding="utf-8") for path in instance.directory.glob("*.txt"))
        self.assertNotIn("synthetic-secret", text)
        self.assertIn("[REDACTED]", text)

    def test_interrupt_stops_functional_flow_but_finally_stops_both_sessions(self):
        original = self.world

        def interrupted(args, cwd, env, timeout):
            if args[3] == "invoke":
                return controller.CommandResult(-1, stderr="synthetic interrupt", interrupted=True)
            return original(args, cwd, env, timeout)

        instance = self.make()
        instance.runner = interrupted
        self.assertEqual(instance.run(), 1)
        self.assertTrue(instance.report["interrupted"])
        self.assertEqual(instance.report["gates"]["direct_storage_a"]["status"], "NOT_RUN")
        self.assertEqual(instance.report["gates"]["cleanup_a"]["status"], "PASS")
        self.assertEqual(instance.report["gates"]["cleanup_b"]["status"], "PASS")

    def test_known_native_acceptance_counted_even_when_cli_exit_is_nonzero(self):
        instance = self.make()
        operation = {"operation": "api", "outcome": {"http_status": 202, "content": {
            "operation_id": uuid4().hex, "native": {"update_id": "synthetic-native-id", "status": "queued"},
        }}}
        instance.runner = lambda *_: controller.CommandResult(1, raw(operation), "CLI failed after response")
        with self.assertRaises(controller.CheckFailed):
            instance.invoke("A", {"operation": "api", "method": "POST", "path": "/teams/team-one/memory/team",
                                  "body": {"marker": "fixture", "value": "amber"}}, "accepted-before-cli-failure")
        self.assertEqual(instance.report["memory_usage"]["accepted_updates"], 1)
        self.assertEqual(instance.report["memory_usage"]["update_attempt_upper_bound"], 1)

    def test_home_pid_and_fixed_vm_fields_are_not_shared_context_prerequisites(self):
        instance = self.make()
        original = self.world

        def no_home_metadata(args, cwd, env, timeout):
            result = original(args, cwd, env, timeout)
            if args[3] == "invoke":
                status, _, body = controller.parse_raw_http(result.stdout)
                for field in ("home_marker", "pid", "fixed_vm_validated"):
                    body["runtime"].pop(field, None)
                result.stdout = raw(body, status)
            return result

        instance.runner = no_home_metadata
        self.assertEqual(instance.run(), 0, instance.report["gates"])
        self.assertIsNone(instance.report["gates"]["idle_resume"]["home_marker_retained"])
        self.assertFalse(instance.report["gates"]["idle_resume"]["home_persistence_is_gate"])
        self.assertFalse(instance.report["fixed_vm_validated"])

    def test_outer_500_at_runtime_or_first_mutation_stops_later_mutations_and_cleans_up(self):
        for fail_runtime in (True, False):
            with self.subTest(fail_runtime=fail_runtime):
                world = FakeWorld()
                instance = self.make()

                def outer_failure(args, cwd, env, timeout):
                    if args[3] == "invoke":
                        payload = json.loads(Path(args[args.index("--input-file") + 1]).read_text())
                        should_fail = payload["operation"] == "runtime" if fail_runtime else (
                            payload["operation"] == "api" and payload.get("method") == "POST"
                            and "/works/" in payload.get("path", "")
                        )
                        if should_fail:
                            return controller.CommandResult(0, raw({
                                "error": {"code": "HOSTED_FAILURE", "type": "PermissionError"},
                            }, 500))
                    return world(args, cwd, env, timeout)

                instance.runner = outer_failure
                self.assertEqual(instance.run(), 1)
                payloads = [json.loads((instance.directory / call["request"]).read_text())
                            for call in instance.report["commands"] if "request" in call]
                mutations = [payload for payload in payloads if payload["operation"] == "api_race"
                             or (payload["operation"] == "api" and payload.get("method") in ("POST", "PATCH"))]
                self.assertEqual(len(mutations), 0 if fail_runtime else 1)
                self.assertEqual(instance.report["gates"]["same_etag_cas"]["status"], "NOT_RUN")
                self.assertEqual(instance.report["gates"]["cleanup_a"]["status"], "PASS")
                self.assertEqual(instance.report["gates"]["cleanup_b"]["status"], "PASS")
                self.assertIn(500, [call.get("outer_http_status") for call in instance.report["commands"]])


if __name__ == "__main__":
    unittest.main()
