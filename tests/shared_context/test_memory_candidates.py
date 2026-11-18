"""MEM-01 suggestions stay private and inactive until the original turn is saved."""



from shared_context.behavior_capabilities import CapabilityGrant
from shared_context.behavior_history_prompt import HISTORY_PROFILE
from legacy_turn_rows_helpers import read_startup
from legacy_turn_rows_helpers import stored_turn
from instruction_flow_helpers import instruction_models, runtime_path
from test_m3_auth import jwk_client, signing_key


def test_candidate_capability_adds_only_its_tool_and_preserves_the_base_prefix():
    from shared_context.behavior_capabilities import CapabilityCapture, available_grants
    from shared_context.behavior_capability_prompt import assemble_capabilities, session_needs
    from shared_context.behavior_skill_prompt import assemble_prompt
    from shared_context.memory_candidates import CANDIDATE_CAPABILITY

    grants = available_grants(None, ("fa-memory-candidate-v1",))
    grant = grants["fa-memory-candidate-v1"]
    assert grant.entry_sha256 == CANDIDATE_CAPABILITY.entry_sha256
    assert grant.tools == ("stage_memory_candidate",) and grant.vision is False
    capture = CapabilityCapture(head_selection_id="selected-candidate", grants=(grant,))
    base = assemble_prompt(HISTORY_PROFILE)
    prompt = assemble_capabilities(HISTORY_PROFILE, capture)
    assert prompt.system_message.startswith(base.system_message)
    assert prompt.record.base_prefix_sha256 == base.record.prefix_sha256
    assert "does not create an effective memory" in prompt.system_message
    assert "original answer is saved successfully" in prompt.system_message
    assert "Observe before acting" not in prompt.system_message
    needs = session_needs(capture)
    assert needs.custom_tools == ("stage_memory_candidate",)
    assert not needs.vision and not needs.image_tools and not needs.mcp_servers
    assert not needs.builtin_tools and not needs.custom_agents


def test_candidate_composes_with_delegate_without_giving_the_child_tools():
    from shared_context.behavior_capabilities import CapabilityCapture, available_grants
    from shared_context.behavior_capability_prompt import assemble_capabilities, session_needs

    grants = available_grants(None, ("fa-delegate-v1", "fa-memory-candidate-v1"))
    capture = CapabilityCapture(head_selection_id="candidate-and-delegate",
                                grants=tuple(grants[name] for name in sorted(grants)))
    needs = session_needs(capture)
    assert needs.custom_tools == ("stage_memory_candidate",)
    assert needs.builtin_tools == ("task",)
    assert len(needs.custom_agents) == 1 and needs.custom_agents[0]["tools"] == []
    assert {"task", "stage_memory_candidate"} <= set(
        assemble_capabilities(HISTORY_PROFILE, capture).record.declared_capabilities)


def stage(turn, command):
    from shared_context.memory_candidates import stage_candidate

    principal = turn.h.verifier(turn.h.client.build_request("GET", "/", headers=turn.h.headers("agent")))
    return stage_candidate(turn.h.assistant, turn.session, command, principal)


def test_human_candidate_lookup_uses_current_read_and_cannot_activate(stored_turn, signing_key):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from shared_context.behavior_api import _install_error_handlers
    from shared_context.memory_candidate_api import install_memory_candidate_routes
    from test_m3_auth import token

    app = FastAPI()
    _install_error_handlers(app)
    # Entered before the stored turn forbids sockets: the Windows event loop opens its self-pipe at start.
    with TestClient(app) as client:
        c = stored_turn("startup-memory-candidate")
        install_memory_candidate_routes(app, c.h.runtime, c.h.composition.verifier)
        identity = c.h.config.identity
        path = "/behavior/v1/bindings/" + c.turn.binding.binding_id + "/memory-candidate"
        human = c.h.headers(signing_key)
        agent = {"Authorization": "Bearer " + token(signing_key, {
            "oid": c.h.config.bindings[0].agent_oid, "idtyp": "app", "roles": ["Behavior.Agent"]}, remove=("scp",))}
        stranger = {"Authorization": "Bearer " + token(signing_key, {
            "oid": identity.agents[0].oid, "appid": identity.admin_client_id, "scp": "Behavior.Read"})}
        backing = c.h.table.backing
        before = (dict(backing.rows), backing.sequence)
        response = client.get(path, headers=human)
        assert response.status_code == 200, response.text
        assert response.json()["state"] == "pending_review"
        assert response.json()["text"] == "startup-reader"
        for headers in (agent, stranger):
            assert client.get(path, headers=headers).status_code == 403
        assert client.get(path + "?activate=true", headers=human).status_code == 422
        assert client.post(path, json={}, headers=human).status_code == 405
        assert (dict(backing.rows), backing.sequence) == before
    assert c.sessions.calls == []


def test_staged_candidate_record_survives_the_installed_startup_readers(stored_turn):
    from shared_context.memory_candidates import CANDIDATE_CAPABILITY, MemoryCandidates

    # The old turn ran on a history profile granting this builtin and staged one candidate ("durable_fact",
    # quote "startup-reader") before its reply was saved; candidate_id is what staging returned.
    c = stored_turn("startup-memory-candidate")
    builtin = CANDIDATE_CAPABILITY
    grant = CapabilityGrant(entry_id=builtin.entry_id, entry_sha256=builtin.entry_sha256,
                            kind="fa-builtin", tools=builtin.tools, vision=False)
    c.h.runtime.capability_grants = {builtin.entry_id: grant}
    staged = c.turn.candidate_id
    inventory = read_startup(c)
    assert [key for key, row in inventory.rows.items()
            if row.payload["data"].get("kind") == "memory_turn_candidate"] == [staged]
    assert inventory.rows[staged].payload["data"]["kind"] == "memory_turn_candidate"
    candidate = MemoryCandidates(c.h.runtime).read(c.turn.binding.binding_id, c.human)
    assert candidate.text == "startup-reader" and candidate.candidate_id == staged
    assert c.sessions.calls == []
