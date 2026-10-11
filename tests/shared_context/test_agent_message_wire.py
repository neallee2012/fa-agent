import pytest
from pydantic import ValidationError

from shared_context.behavior_agent_messages import AgentMessage, TeamReference
from shared_context.behavior_agent_message_wire import AgentMessageSend, PeerTeamReference, PublicAgentEntry


def test_production_refs_require_explicit_kind_without_changing_old_contract_bytes():
    fields = {"visibility": "team", "tenant_id": "tenant-one", "team_id": "team-one",
              "resource_id": "source-one", "version": "1", "sha256": "a" * 64}
    original = TeamReference(**fields)
    assert original.model_dump(mode="json") == fields
    message = AgentMessage(message_id="original-message", recipient_agent_id="agent-b",
                           kind="handoff", content="Shared source.", refs=(original,))
    with pytest.raises(ValidationError):
        AgentMessageSend(message=message)
    explicit = PeerTeamReference(**fields, resource_kind="source")
    sent = AgentMessageSend(message=message.model_copy(update={"refs": (explicit,)}))
    assert sent.message.refs[0].resource_kind == "source"
    for kind in ("private", "url", "unknown"):
        with pytest.raises(ValidationError):
            PeerTeamReference(**fields, resource_kind=kind)


@pytest.mark.parametrize("field", ["source_kind", "sender_agent_id", "root_request_id", "hop"])
def test_message_payload_cannot_supply_server_authority_fields(field):
    with pytest.raises(ValidationError):
        AgentMessageSend.model_validate_json(
            '{"request_id":"agent-message-send","message":{"message_id":"one","recipient_agent_id":"agent-b",'
            '"kind":"handoff","content":"Data only.","' + field + '":"forged"}}')


@pytest.mark.parametrize("prefix", ["/agents/agent-a", "https://example.invalid/agents/agent-b", ""])
def test_public_directory_cannot_redirect_the_selected_agent(prefix):
    with pytest.raises(ValidationError):
        PublicAgentEntry(agent_id="agent-b", display_name="B", api_prefix=prefix, state="ready")


@pytest.mark.parametrize(("kind", "version"), [
    ("source", "01"), ("source", "a" * 32), ("artifact", "1"), ("artifact", "A" * 32),
])
def test_explicit_resource_kind_requires_its_canonical_version(kind, version):
    with pytest.raises(ValidationError):
        PeerTeamReference(resource_kind=kind, tenant_id="tenant-one", team_id="team-one",
                          resource_id="source-one", version=version, sha256="a" * 64)


def test_hosted_wire_import_does_not_require_api_only_packages():
    import subprocess
    import sys

    script = """
import importlib.abc
import sys
class ApiOnlyPackages(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'fastapi', 'starlette', 'mcp'}:
            raise ModuleNotFoundError('API-only package reached by Hosted wire: ' + fullname)
sys.meta_path.insert(0, ApiOnlyPackages())
from shared_context.behavior_agent_message_wire import AgentMessage, AgentMessageRead, PeerTeamReference
print('hosted-wire-ok')
"""
    result = subprocess.run([sys.executable, "-X", "utf8", "-c", script], capture_output=True,
                            text=True, encoding="utf-8", timeout=30, check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "hosted-wire-ok"
