"""Offline stand-in for the Foundry lookup of each Agent's Hosted version of the deployed build."""

from shared_context import behavior_agent_versions
from shared_context.errors import Problem


def deployed_versions(monkeypatch, versions, *, others=False):
    """``versions``: one version for every Agent, or {agent_id: version}; a missing Agent is still following,
    or keeps its registered version when ``others`` (as a composition without a Foundry resolver does)."""
    def current(runtime, target):
        if target is None:
            return None if isinstance(versions, dict) else versions
        value = versions.get(target.agent_id) if isinstance(versions, dict) else versions
        if value is None and others:
            return target.agent_version
        if value is None:
            raise Problem(503, behavior_agent_versions.FOLLOWING, "no version of the deployed build")
        return value
    monkeypatch.setattr(behavior_agent_versions, "current_hosted_version", current)
