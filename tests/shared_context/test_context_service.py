"""ContextService authorize/get/create, the part still used by ArtifactService and the registry operator."""

from __future__ import annotations

import pytest

from conftest import A
from shared_context.auth import Actor
from shared_context.errors import Problem
from shared_context.models import CreateWork


def test_create_is_not_upsert(system, work):
    service = system[0]
    with pytest.raises(Problem) as exc:
        service.create(A, A.team, "task-1", CreateWork(goal="replacement"))
    assert exc.value.status == 409
    assert service.get(A, A.team, "task-1").response() == work


@pytest.mark.parametrize("call", [
    lambda service: service.get(A, "team-two", "task-1"),
    lambda service: service.create(A, "team-two", "task-1", CreateWork(goal="bad")),
], ids=["get", "create"])
def test_forbidden_team_precedes_storage_or_memory(system, call):
    with pytest.raises(Problem) as exc:
        call(system[0])
    assert exc.value.status == 403
    assert system[1].data == {} and system[2].calls == []


def test_unregistered_internal_identity_cannot_use_service(system):
    actor = Actor("dddddddd-dddd-dddd-dddd-dddddddddddd", "intruder", "team-one")
    with pytest.raises(Problem) as exc:
        system[0].get(actor, "team-one", "task-1")
    assert exc.value.status == 403
