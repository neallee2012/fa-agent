"""Parallel toolbox calls in one turn with native HITL on (stage v18, 2026-10-07).

The model sent four ``read_item`` calls at once; two failed before the toolbox was called because their HITL
``/authorize`` writes raced on the API store's single guard row (Table EGT 412 -> ``BEHAVIOR_CONFLICT``), and the
unregistered code reached the model as the generic "Authority refused ...". Offline: the real JWT/HITL API/CAS over
the fake Table, the toolbox as an httpx MockTransport and an async API transport on which a write that overlaps
another write in flight loses the guard, as on Azure Table.
"""

import asyncio
import json

import httpx
import pytest

from shared_context.behavior_assistant_boundary import AssistantModelBoundary
from shared_context.behavior_capabilities import TOOLBOX_NOT_RUN, TOOLBOX_READ_FAILED, McpToolResult
from shared_context.behavior_store import GUARD_KEY
from shared_context.errors import Problem
from shared_context.m2_copilot import ToolFailure
from shared_context.native_hitl_runtime import NativeHitlClient
from test_behavior_store import NativeEntity
from test_m3_auth import jwk_client, signing_key  # noqa: F401
from test_native_hitl import hitl  # noqa: F401
from test_native_toolbox import Toolbox
from test_native_toolbox_approval import decide, packet, permit, platform, setup, wait_pending  # noqa: F401


def advance_guard(backing):
    """Another writer's transaction committed first: the shared guard row has a new ETag."""
    key = next(key for key in backing.rows if key[2] == GUARD_KEY)
    backing.sequence += 1
    backing.rows[key] = NativeEntity(dict(backing.rows[key]), f'W/"fake-{backing.sequence}"')


class SharedGuardApi(httpx.AsyncBaseTransport):
    """The real HITL API; a POST that overlaps another POST in flight loses the store guard (EGT 412)."""

    def __init__(self, c):
        self.c, self.writing, self.overlapped, self.posts = c, 0, 0, []

    async def handle_async_request(self, request):
        content = await request.aread()
        post = request.method == "POST"
        self.writing += post
        try:
            # On the wire: the other parallel tool calls start their own requests meanwhile.
            await asyncio.sleep(0.01)
            if post:
                self.posts.append(request.url.path)
                if self.writing > 1:
                    self.overlapped += 1
                    self.c.table.before_submit = lambda: advance_guard(self.c.table.backing)
            result = self.c.client.request(request.method, request.url.path, content=content,
                                           headers=request.headers)
            return httpx.Response(result.status_code, content=result.content, headers=result.headers)
        finally:
            self.writing -= post


class LostAuthorizeReply(httpx.AsyncBaseTransport):
    """The real HITL API; /authorize commits but its reply is lost and the reconcile read is unconfirmed too."""

    def __init__(self, c):
        self.c = c

    async def handle_async_request(self, request):
        content = await request.aread()
        if request.url.path.endswith("/authorize/reconcile"):
            return httpx.Response(503, json={"error": {"code": "BEHAVIOR_COMMIT_UNKNOWN"}})
        result = self.c.client.request(request.method, request.url.path, content=content, headers=request.headers)
        if request.url.path.endswith("/authorize"):
            assert result.status_code == 200, result.text
            return httpx.Response(503, json={"error": {"code": "BEHAVIOR_COMMIT_UNKNOWN"}})
        return httpx.Response(result.status_code, content=result.content, headers=result.headers)


def boundary():
    return AssistantModelBoundary(lambda digest: None, text_reply=True,
                                  capability_results={"WebIQ-web": McpToolResult})


def read_only_toolbox():
    toolbox = Toolbox()
    for tool in toolbox.tools:
        tool["annotations"] = {"readOnlyHint": True}
    return toolbox


def test_parallel_never_toolbox_calls_do_not_race_their_own_approval_writes(hitl, tmp_path):
    case = setup(hitl, tmp_path, "never")
    api = SharedGuardApi(hitl)
    case.gate.client = NativeHitlClient("https://api.offline.invalid",
                                        lambda: hitl.headers(hitl.agent)["Authorization"][7:], transport=api)
    ids = [f"page-{number}" for number in range(2, 6)]

    async def check():
        web = (await case.bind())["WebIQ-web"]
        for call_id in ids:
            assert await permit(case, call_id) == (True, None)
        outputs = await asyncio.gather(*(web.handler(packet(call_id)) for call_id in ids), return_exceptions=True)
        await case.gate.close()
        return outputs
    outputs = asyncio.run(check())
    failures = [(item.public_code, item.public_message) for item in outputs if isinstance(item, ToolFailure)]
    assert failures == []
    assert all(json.loads(item.text)["text"] == "found\ncited" for item in outputs)
    # One /authorize per call, never two approval writes of this turn in flight at once.
    assert api.posts.count("/native-hitl/v1/authorize") == 4 and api.overlapped == 0
    assert len(case.toolbox.calls) == 4


def test_an_approval_write_lost_to_another_writer_says_the_call_did_not_run(hitl, tmp_path):
    case = setup(hitl, tmp_path, "never")

    async def check():
        web = (await case.bind())["WebIQ-web"]
        assert await permit(case, "page-2") == (True, None)
        hitl.table.before_submit = lambda: advance_guard(hitl.table.backing)
        with pytest.raises(ToolFailure) as lost:
            await web.handler(packet("page-2"))
        assert case.toolbox.calls == []
        # The user asks again: a new call is approved and runs once.
        assert await permit(case, "page-2-again") == (True, None)
        await web.handler(packet("page-2-again"))
        await case.gate.close()
        return lost.value
    lost = asyncio.run(check())
    assert lost.public_code == "NATIVE_HITL_NOT_STARTED"
    assert len(case.toolbox.calls) == 1
    text = boundary().public_tool_failure("WebIQ-web", lost.public_code, lost.public_message)
    assert text.startswith("[NATIVE_HITL_NOT_STARTED]") and "工具沒有執行" in text and "再做一次" in text
    assert "Authority refused" not in text and "不要重送" not in text


def test_an_approved_call_whose_authorization_reply_is_lost_closes_its_card_as_interrupted(hitl, tmp_path):
    case = setup(hitl, tmp_path, "always")

    async def check():
        web = (await case.bind())["WebIQ-web"]
        waiting = asyncio.create_task(permit(case, "approved-call"))
        view = await wait_pending(case, 1)
        assert decide(case, view, "allow_once", "owner-once").status_code == 200
        assert await waiting == (True, None)
        case.gate.client = NativeHitlClient("https://api.offline.invalid",
                                            lambda: hitl.headers(hitl.agent)["Authorization"][7:],
                                            transport=LostAuthorizeReply(hitl))
        with pytest.raises(ToolFailure) as lost:
            await web.handler(packet("approved-call"))
        closed = await case.client.read(view.approval.pending_id)
        await case.gate.close()
        return lost.value, closed
    lost, closed = asyncio.run(check())
    # The authorization exists but the tool never ran: the owner's card says interrupted, not effect unknown.
    assert lost.public_code == "NATIVE_HITL_NOT_STARTED" and case.toolbox.calls == []
    assert closed.authorization is not None and closed.status == "interrupted"


def test_other_failures_before_the_tool_runs_keep_their_own_code(hitl, tmp_path, monkeypatch):
    case = setup(hitl, tmp_path, "never")

    async def stopped(call):
        raise Problem(409, "NATIVE_EFFECT_CONFLICT", "the original turn has a stop request")

    async def check():
        web = (await case.bind())["WebIQ-web"]
        assert await permit(case, "stopping") == (True, None)
        monkeypatch.setattr(case.gate, "_authorize", stopped)
        with pytest.raises(ToolFailure) as failed:
            await web.handler(packet("stopping"))
        await case.gate.close()
        return failed.value
    failed = asyncio.run(check())
    # Only a lost approval write says "did not run, call again"; a stopped turn keeps its original refusal.
    assert failed.public_code == "NATIVE_EFFECT_CONFLICT" and case.toolbox.calls == []


@pytest.mark.parametrize("hitl_on", [True, False])
@pytest.mark.parametrize("status", [503, 403])
def test_a_failed_read_only_toolbox_call_tells_the_model_it_may_call_again(hitl, tmp_path, hitl_on, status):
    toolbox = read_only_toolbox()
    toolbox.reply = lambda body: httpx.Response(status)
    case = setup(hitl, tmp_path, "never", hitl_on=hitl_on, toolbox=toolbox)

    async def check():
        web = (await case.bind())["WebIQ-web"]
        if hitl_on:
            assert await permit(case, "read-1") == (True, None)
        with pytest.raises(ToolFailure) as failed:
            await web.handler(packet("read-1"))
        if not hitl_on:
            # The same SDK call id replayed is never sent again; only a new call is.
            with pytest.raises(ToolFailure) as replayed:
                await web.handler(packet("read-1"))
            assert replayed.value.public_code == TOOLBOX_READ_FAILED
        await case.gate.close()
        return failed.value
    failed = asyncio.run(check())
    assert failed.public_code == TOOLBOX_READ_FAILED and len(toolbox.calls) == 1
    text = boundary().public_tool_failure("WebIQ-web", failed.public_code, failed.public_message)
    assert text.startswith("Tool call failed [NATIVE_TOOLBOX_READ_FAILED].") and "only reads data" in text
    assert "Authority refused" not in text


@pytest.mark.parametrize("hitl_on", [True, False])
def test_a_tool_not_marked_read_only_keeps_unknown_and_says_not_run_only_when_refused(hitl, tmp_path, hitl_on):
    toolbox = Toolbox()
    case = setup(hitl, tmp_path, "never", hitl_on=hitl_on, toolbox=toolbox)
    replies = {"unknown": lambda body: httpx.Response(503), "refused": lambda body: httpx.Response(422)}

    async def check():
        web = (await case.bind())["WebIQ-web"]
        codes = {}
        for name, reply in replies.items():
            toolbox.reply = reply
            if hitl_on:
                assert await permit(case, name) == (True, None)
            with pytest.raises(ToolFailure) as failed:
                await web.handler(packet(name))
            codes[name] = failed.value.public_code
        await case.gate.close()
        return codes
    codes = asyncio.run(check())
    # With HITL on or off, an unknown outcome of an effectful tool keeps the toolbox's own fixed text.
    assert codes == {"unknown": "NATIVE_TOOLBOX_OUTCOME_UNKNOWN", "refused": TOOLBOX_NOT_RUN}
    not_run = boundary().public_tool_failure("WebIQ-web", TOOLBOX_NOT_RUN, "")
    assert not_run.startswith("Tool call failed [NATIVE_TOOLBOX_NOT_RUN].") and "nothing was executed" in not_run
    unknown = boundary().public_tool_failure("WebIQ-web", "NATIVE_TOOLBOX_OUTCOME_UNKNOWN", "")
    assert unknown == (
        "Tool call failed [NATIVE_TOOLBOX_OUTCOME_UNKNOWN]. The result of this call is unknown: it may or may not have "
        "taken effect, and it was not sent again. Do not call this tool again for the same action in this turn, and "
        "do not say it succeeded or failed. Tell the user the result is uncertain and was not resent, and ask them to "
        "check the service before deciding whether to do it again.")


def test_toolbox_retry_guidance_is_shown_only_for_an_mcp_tool():
    other = AssistantModelBoundary(lambda digest: None, text_reply=True,
                                   capability_results={"read_work_context": dict})
    text = other.public_tool_failure("read_work_context", TOOLBOX_READ_FAILED, "")
    assert text is None or not text.startswith("Tool call failed")
