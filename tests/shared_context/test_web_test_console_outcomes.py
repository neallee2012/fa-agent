"""Optional outcome wire through the real progress reader; no runtime or model."""

import copy

import httpx
import pytest

from shared_context.behavior_skill_draft import SkillDraftOutcome
from shared_context.behavior_skill_prompt import assemble_prompt
from test_behavior_assistant import assistant
from test_instruction_flow import instruction_models, runtime_path
from test_m3_auth import jwk_client, signing_key
from test_web_test_console_attachments import draft, picker, submit


@pytest.mark.parametrize("stage", [None, "create_unknown", "created", "upload_unknown", "saved"])
def test_skill_draft_outcome_wire_does_not_block_original_conversation_progress(picker, stage):
    c = picker
    original = draft(c)
    accepted = submit(c)
    assert accepted.status_code == 200, accepted.text
    cid = accepted.json()["task"]["admission"]["conversation_id"]
    outcome = None if stage is None else SkillDraftOutcome(
        schema_version="fa-skill-draft-outcome-v1",
        receipt_id="a" * 64, intent_sha256="b" * 64, stage=stage,
    )
    wire_turns = []

    def wire(request, response):
        if request.url.path != f"/work-policy/v1/conversations/{cid}" or response.status_code != 200:
            return response
        body = response.json()
        if outcome is not None:
            body["turns"][0]["skill_draft_outcome"] = outcome.model_dump(mode="json")
            body["turns"][0]["prompt_assembly"] = assemble_prompt("fa-instruction-history-skill-draft-v1").record.model_dump(mode="json")
        wire_turns.append(body["turns"][0])
        return httpx.Response(200, json=body, headers={"Cache-Control": "no-store"})

    c.mutate = wire
    rows, journal = copy.deepcopy(c.h.transport.backing.rows), c.journal.read_bytes()
    c.calls.clear()
    observed = c.browser.ui.get("/api/instructions/progress", params={"thread_id": original["thread_id"]})
    assert observed.status_code == 200, observed.text
    state = observed.json()
    task = next(item for item in state["tasks"] if item["task_id"] == original["request_id"])
    assert task["progress"]["data"] == wire_turns[-1]
    if outcome is None:
        assert "skill_draft_outcome" not in task["progress"]["data"]
    else:
        assert task["progress"]["data"]["skill_draft_outcome"] == outcome.model_dump(mode="json")
        stored = c.browser.app.state.console.instructions.projections[cid].turns[0]
        assert type(stored.skill_draft_outcome) is SkillDraftOutcome
    assert state["composer"]["conversation"]["conversation_id"] == cid
    assert task["submission"]["state"] == "CONFIRMED"
    assert task["acknowledgment"]["state"] == "NOT_SENT"
    assert c.calls == [("GET", f"/work-policy/v1/conversations/{cid}", b"")]
    assert c.h.transport.backing.rows == rows and c.journal.read_bytes() == journal


def test_invalid_skill_draft_wire_preserves_the_original_readable_projection(picker):
    c = picker
    original = draft(c)
    accepted = submit(c).json()["task"]
    cid = accepted["admission"]["conversation_id"]
    url, params = "/api/instructions/progress", {"thread_id": original["thread_id"]}
    assert c.browser.ui.get(url, params=params).status_code == 200
    controller = c.browser.app.state.console.instructions
    retained = controller.projections[cid].model_dump_json()
    rows, journal = copy.deepcopy(c.h.transport.backing.rows), c.journal.read_bytes()
    valid = {"schema_version": "fa-skill-draft-outcome-v1", "receipt_id": "a" * 64,
             "intent_sha256": "b" * 64, "stage": "saved"}
    for malformed in ({**valid, "stage": "future"}, {**valid, "approved": True},
                      {**valid, "intent_sha256": "not-a-hash"}, False):
        def wire(request, response):
            if request.url.path != f"/work-policy/v1/conversations/{cid}" or response.status_code != 200:
                return response
            body = response.json()
            body["turns"][0]["skill_draft_outcome"] = malformed
            return httpx.Response(200, json=body, headers={"Cache-Control": "no-store"})

        c.mutate = wire
        invalid = c.browser.ui.get(url, params=params)
        assert invalid.status_code == 502 and invalid.json()["error"]["code"] == "CONVERSATION_INVALID"
        assert controller.projections[cid].model_dump_json() == retained
    c.mutate = lambda request, response: response
    assert c.browser.ui.get(url, params=params).status_code == 200
    assert c.h.transport.backing.rows == rows and c.journal.read_bytes() == journal
