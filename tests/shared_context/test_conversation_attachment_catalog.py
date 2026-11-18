"""An existing conversation never borrows the current new-conversation attachment setting."""

import pytest

from shared_context.behavior_assistant_wire import InstructionSubmit
from shared_context.behavior_attachment_catalog import AttachmentSourceCatalog
from shared_context.behavior_instruction import conversation_key
from test_behavior_assistant import assistant
from assistant_api_helpers import assistant_api
from conversation_profile_selection_helpers import configured, select, template
from instruction_flow_helpers import instruction_models, runtime_path, sdk_io
from direct_submit_helpers import submit_direct
from test_m3_auth import jwk_client, signing_key

GLOBAL = "/work-policy/v1/attachment-sources"


def submit(h, request_id):
    result = h.client.post("/work-policy/v1/submit", headers=h.headers(), content=InstructionSubmit(
        request_id=request_id, content="Hold the original settings for this conversation.",
    ).model_dump_json())
    assert result.status_code == 200, result.text
    return result.json()


def admit(h, request_id):
    return submit_direct(h, request_id, "Hold the original settings for this conversation.")[0]


def path(conversation):
    return "/work-policy/v1/conversations/" + conversation["conversation_id"] + "/attachment-sources"


@pytest.mark.parametrize("enabled_before", [False, True], ids=["old-disabled-new-enabled", "old-enabled-new-disabled"])
def test_existing_conversation_uses_its_own_attachment_setting_after_selection_changes(assistant_api, enabled_before, monkeypatch):
    h, control, _ = configured(assistant_api)
    previous = None
    if enabled_before:
        selected, _ = select(h, value=template(text_attachment_profile="fa-source-text-attachments-v1"))
        assert selected.status_code == 200
        previous = selected.json()["result"]["selection_id"]
    conversation = admit(h, "original-attachment-profile")
    old = control.records[conversation["binding_id"]].model_dump_json()
    changed, _ = select(h, request_id="change-new-conversation-setting", previous=previous,
                        value=template(**({} if enabled_before else {"text_attachment_profile": "fa-source-text-attachments-v1"})))
    assert changed.status_code == 200, changed.text
    for method in ("read_exact", "read_graph"):
        monkeypatch.setattr(h.sources, method, lambda *args, **kwargs: pytest.fail("Catalogue must not load source contents"))
    before = h.store._guard()
    control.records.clear()
    response = h.client.get(path(conversation), headers=h.headers("read-only"))
    assert response.status_code == 200, response.text
    result = AttachmentSourceCatalog.model_validate_json(response.content)
    assert result.conversation_id == conversation["conversation_id"]
    assert result.state == ("metadata" if enabled_before else "disabled")
    current = h.client.get(GLOBAL, headers=h.headers("read-only"))
    assert current.status_code == 200
    assert current.json()["state"] == ("disabled" if enabled_before else "metadata")
    assert "conversation_id" not in current.json()
    assert result.authorization is result.content_reverified is result.dependencies_reverified is False
    assert h.store._guard() == before
    assert control.records[conversation["binding_id"]].model_dump_json() == old


def test_unknown_conversation_does_not_fall_back_to_current_default(assistant_api):
    h, control, _ = configured(assistant_api)
    selected, _ = select(h, value=template(text_attachment_profile="fa-source-text-attachments-v1"))
    assert selected.status_code == 200
    before = h.store._guard()
    response = h.client.get("/work-policy/v1/conversations/not-original/attachment-sources",
                            headers=h.headers("read-only"))
    assert response.status_code == 404
    assert "entries" not in response.json() and h.store._guard() == before


@pytest.mark.parametrize("identity", ["agent", "old-m3"])
def test_conversation_catalogue_does_not_accept_other_principals(assistant_api, identity):
    h, _, _ = configured(assistant_api)
    conversation = admit(h, "not-an-agent-catalogue")
    response = h.client.get(path(conversation), headers=h.headers(identity))
    assert response.status_code == 403


def test_conversation_catalogue_rejects_extra_scope_and_revoked_read(assistant_api):
    h, control, human = configured(assistant_api)
    conversation = admit(h, "scope-catalogue")
    for suffix, body in (("?team=elsewhere", None), ("", b'{"profile":"other"}')):
        response = h.client.request("GET", path(conversation) + suffix,
                                    headers=h.headers("read-only"), content=body)
        assert response.status_code == 422
    control.revoke(human, "catalogue-current-read-revoked")
    response = h.client.get(path(conversation), headers=h.headers("read-only"))
    assert response.status_code == 403


@pytest.mark.parametrize("field", ["human_oid", "team_id", "agent_oid"])
def test_conversation_scope_cannot_be_substituted_before_metadata_read(assistant_api, field):
    h, _, _ = configured(assistant_api)
    conversation = admit(h, "original-scope-catalogue")
    h.mutate(conversation_key(conversation["conversation_id"]), **{field: "different-original-owner"})
    before = h.store._guard()
    response = h.client.get(path(conversation), headers=h.headers("read-only"))
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "CONVERSATION_SCOPE_DENIED"
    assert "entries" not in response.json() and h.store._guard() == before


def test_legacy_conversation_without_hosted_identity_requires_explicit_reset(assistant_api):
    h, _, _ = configured(assistant_api)
    conversation = admit(h, "legacy-no-hosted-catalogue")
    h.mutate(conversation_key(conversation["conversation_id"]), hosted_session_id=None)
    before = h.store._guard()
    response = h.client.get(path(conversation), headers=h.headers("read-only"))
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "CONVERSATION_SESSION_RESET_REQUIRED"
    assert h.store._guard() == before
