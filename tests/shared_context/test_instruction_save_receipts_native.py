"""One fixed SDK/offline-provider response-loss control; no Hosted/cloud acceptance."""

from copilot import CopilotClient
from copilot.session import CopilotSession

from test_behavior_assistant import assistant
from test_instruction_flow import instruction_models
from test_instruction_save_receipts import save_case, completed_response_loss, sha
from test_m2_copilot import runtime_path
from test_m3_auth import jwk_client, signing_key


def test_fixed_sdk_save_ack_loss_has_one_original_send_model_and_graceful_stop(assistant, monkeypatch):
    clients, sends, prompts, stops, forced, versions = set(), [], [], [], [], []
    original_start, original_send = CopilotClient.start, CopilotSession.send
    original_stop, original_force = CopilotClient.stop, CopilotClient.force_stop
    original_status = CopilotClient.get_status

    async def start(client, *args, **kwargs):
        result = await original_start(client, *args, **kwargs)
        clients.add(id(client))
        return result

    async def send(session, prompt, **kwargs):
        sends.append(session.session_id)
        prompts.append(sha(prompt.encode("utf-8")))
        return await original_send(session, prompt, **kwargs)

    async def stop(client, *args, **kwargs):
        process = client._cli_process
        result = await original_stop(client, *args, **kwargs)
        assert process is not None and process.poll() is not None
        stops.append({"pid": process.pid, "exit_code": process.returncode})
        return result

    async def force(client, *args, **kwargs):
        forced.append(True)
        return await original_force(client, *args, **kwargs)

    async def status(client, *args, **kwargs):
        result = await original_status(client, *args, **kwargs)
        versions.append(result.version)
        return result

    monkeypatch.setattr(CopilotClient, "start", start)
    monkeypatch.setattr(CopilotSession, "send", send)
    monkeypatch.setattr(CopilotClient, "stop", stop)
    monkeypatch.setattr(CopilotClient, "force_stop", force)
    monkeypatch.setattr(CopilotClient, "get_status", status)

    def observed():
        return {
            "kind": "fixed_native_sdk_offline_provider",
            "send_count": len(sends), "session_ids": sends, "client_count": len(clients),
            "prompt_sha256": prompts,
            "stop_returns": len(stops), "force_stop_calls": len(forced),
            "stops": stops, "runtime_versions": versions,
        }

    with save_case(assistant, monkeypatch, "after-commit", observed) as case:
        completed_response_loss(case, "succeeded")
        assert versions and set(versions) == {"1.0.83"}
        assert len(stops) == 1
        assert case.boundary.save_body["sdk_stop"]["pid"] == stops[0]["pid"]
        assert case.boundary.save_body["sdk_stop"]["exit_code"] == stops[0]["exit_code"]
        assert case.boundary.save_body["sdk_stop"]["graceful"] is True
        case.boundary.complete = True
