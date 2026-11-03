from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone

from .auth import Actor, AuthSettings
from .errors import Problem
from .memory import Memory
from .models import CreateWork, Handoff, ID_PATTERN
from .storage import Record, Repository


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def artifact(text: str, version: int) -> dict:
    return {"text": text, "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(), "version": version}


class ContextService:
    def __init__(self, repository: Repository, memory: Memory, auth: AuthSettings):
        self.repository, self.memory, self.auth = repository, memory, auth

    def authorize(self, actor: Actor, team: str) -> None:
        if not re.fullmatch(ID_PATTERN, team):
            raise Problem(422, "INVALID_ID", "invalid team ID")
        if self.auth.actors.get(actor.oid) != actor:
            raise Problem(403, "ACTOR_NOT_ALLOWED", "identity is not in the server allowlist")
        if actor.team != team:
            raise Problem(403, "CROSS_TEAM_DENIED", "cross-team access is forbidden")

    def work_row(self, work: str) -> str:
        if not re.fullmatch(ID_PATTERN, work):
            raise Problem(422, "INVALID_ID", "invalid work ID")
        return f"work:{work}"

    def get(self, actor: Actor, team: str, work: str) -> Record:
        self.authorize(actor, team)
        return self.repository.get(team, self.work_row(work))

    def create(self, actor: Actor, team: str, work: str, body: CreateWork) -> Record:
        self.authorize(actor, team)
        payload = {
            "work_id": work, "team": team, "owner": actor.actor, "revision": 1,
            "status": "draft", "goal": body.goal, "progress": body.progress,
            "artifact": artifact(body.artifact_text, 1), "handoff": None,
            "published_at": None, "published_by": None, "updated_at": now(),
        }
        return self.repository.create(team, self.work_row(work), payload)

    def editable(self, actor: Actor, team: str, work: str, etag: str | None) -> Record:
        record = self.get(actor, team, work)
        if record.payload["status"] == "published":
            raise Problem(409, "PUBLISHED_IMMUTABLE", "published work cannot be changed")
        if record.payload["owner"] != actor.actor:
            raise Problem(403, "NOT_OWNER", "only the current editor can mutate or hand off work")
        if not etag:
            raise Problem(428, "ETAG_REQUIRED", "If-Match is required")
        if len(etag) > 256 or etag == "*" or "," in etag or etag != record.etag:
            raise Problem(412, "ETAG_CONFLICT", "supply the exact ETag from GET")
        return record

    def save(self, team: str, work: str, old: Record, changes: dict) -> Record:
        payload = {**old.payload, **changes, "revision": old.payload["revision"] + 1, "updated_at": now()}
        return self.repository.replace(team, self.work_row(work), payload, old.etag)

    def handoff(self, actor: Actor, team: str, work: str, etag: str | None, body: Handoff) -> Record:
        old = self.editable(actor, team, work, etag)
        if body.to_actor == actor.actor:
            raise Problem(422, "INVALID_HANDOFF", "handoff must select a different editor")
        if not any(a.team == team and a.actor == body.to_actor for a in self.auth.actors.values()):
            raise Problem(403, "HANDOFF_TARGET_DENIED", "target must be an allowlisted teammate")
        return self.save(team, work, old, {
            "owner": body.to_actor,
            "handoff": {"from_actor": actor.actor, "to_actor": body.to_actor, "at": now()},
        })

    def publish(self, actor: Actor, team: str, work: str, etag: str | None) -> Record:
        old = self.editable(actor, team, work, etag)
        return self.save(team, work, old, {
            "status": "published", "published_at": now(), "published_by": actor.actor,
        })
