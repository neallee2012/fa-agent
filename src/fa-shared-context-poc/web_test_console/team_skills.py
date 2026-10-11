"""Owner review of Team skills through the behavior API's /team-skills/v1 routes.

The API decides every permission; this console only proxies the owner's reads and one explicit decision at a time.
A decision ID is chosen by the page and reused for a retry, so the API's M3 decision idempotency (same ID and body
returns the original record) is what keeps a lost reply from becoming a second decision.
"""

import hashlib
import json
import re

from shared_context.m3_models import (
    OWNER_AUTHOR, ApprovalInput, ArtifactDocument, ArtifactVersion, OwnerArtifactSource, SkillDeactivationInput,
    SkillReviewEntry, SkillReviewList,
)
from shared_context.team_skill_draft_wire import OWNER_DRAFT_REQUEST_BYTES, OwnerSkillDraftInput, owner_material

from .adapter import ConsoleError, PullAdapter, require, strict_json

PREFIX = "/team-skills/v1"
MAX_READ_BYTES = 256 * 1024
MAX_WRITE_BYTES = 2048
MAX_DRAFT_BYTES = OWNER_DRAFT_REQUEST_BYTES
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
_VERSION = re.compile(r"[a-f0-9]{32}")
_RESOURCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
TEAM_SKILL_ERRORS = frozenset({
    "TEAM_SKILLS_UNAVAILABLE", "TEAM_SKILLS_OWNER_ONLY", "TEAM_SKILL_NOT_SKILL", "TEAM_SKILLS_INVALID_REQUEST",
    "TEAM_SKILL_RESOURCE_NOT_FOUND", "NOT_FOUND",
    "ETAG_REQUIRED", "ETAG_CONFLICT", "M3_DECISION_CONFLICT", "M3_STALE_PROPOSAL", "M3_ALREADY_DECIDED",
    "M3_APPROVAL_HASH_MISMATCH", "M3_APPROVAL_METADATA_MISMATCH", "M3_EFFECTIVE_VERSION_CHANGED",
    "M3_DEACTIVATION_HASH_MISMATCH", "M3_DEACTIVATION_METADATA_MISMATCH", "M3_UPLOAD_UNKNOWN",
    "M3_VERSION_NOT_FOUND", "M3_NO_EFFECTIVE_VERSION", "M3_FILE_INTEGRITY", "M3_STATE_INVALID", "M3_STATE_UNCERTAIN",
    "M3_TEAM_DENIED", "M3_IDENTITY_DENIED", "M3_HUMAN_REQUIRED", "M3_NOT_SKILL",
    "M3_SKILL_RESOURCE_DENIED", "M3_SKILL_RESOURCE_FORMAT", "M3_SKILL_RESOURCE_CHANGED",
    "SKILL_DRAFT_OUTCOME_UNKNOWN", "SKILL_DRAFT_TARGET_CONFLICT", "M3_NOT_OWNER", "M3_NATIVE_SOURCE_DENIED",
    "M3_POLICY_FORMAT", "M3_VERSION_LIMIT",
})
MESSAGES = {code: "skill 審核未通過核對；請重新讀取，不會自動重送。" for code in TEAM_SKILL_ERRORS}
MESSAGES.update({
    "TEAM_SKILLS_UNAVAILABLE": "這個 API 版本還沒有 skill 審核入口；不會改走其他入口。",
    "TEAM_SKILLS_OWNER_ONLY": "只有這個 Team 的真人主人，且目前登入具備 Behavior.Approve，才能新增、批准、拒絕或停用 skill。",
    "TEAM_SKILL_NOT_SKILL": "這不是 skill；這裡只審核 skill。",
    "TEAM_SKILL_RESPONSE_INVALID": "skill 審核的回應格式不符；不當成完成，請重新讀取。",
    "TEAM_SKILL_INPUT_INVALID": "skill 決定的內容不完整；沒有送出。",
    "TEAM_SKILL_DRAFT_INVALID": "skill 草稿不符限制，沒有送出。名稱一行、64 字內；用途一行、512 bytes 內；本文 8 KiB 內；"
                                "附檔最多 4 個、每個 8 KiB 內（中文約 2700 字），檔名只用英數和 . _ -。",
    "SKILL_DRAFT_OUTCOME_UNKNOWN": "草稿保存結果未確認；內容還在表單裡。請重新讀取審核清單；用相同內容再送一次只會對到同一份草稿，不會另建。",
    "SKILL_DRAFT_TARGET_CONFLICT": "這份內容對應的草稿已有不同內容；沒有覆寫。請改一下名稱或內容再送。",
    "M3_POLICY_FORMAT": "skill 本文需為 8 KiB 內的可見文字；沒有保存。",
    "M3_VERSION_LIMIT": "這份草稿的版本數已達上限；沒有保存。",
    "ETAG_CONFLICT": "這個 skill 剛被其他操作改過；請重新讀取後再決定，原決定編號會保留。",
    "ETAG_REQUIRED": "缺少讀取時的版本標記；請重新讀取後再決定。",
    "M3_DECISION_CONFLICT": "這個決定編號已用在另一個決定；不會覆寫。",
    "M3_STALE_PROPOSAL": "這份 skill 已有新的草稿版本；請重新查看內容後再決定。",
    "M3_ALREADY_DECIDED": "這個版本已經有決定；不能改。要修改請起草新的 skill（可在上方「新增 skill」自己寫），再用「批准並取代」換掉。",
    "M3_EFFECTIVE_VERSION_CHANGED": "目前生效的版本已改變；請重新讀取後再停用。",
    "M3_UPLOAD_UNKNOWN": "這個 skill 還有上傳未完成。助理起草的請等它核對完成；你自己寫的請用相同內容再送一次（不會另建）。",
    "M3_STATE_UNCERTAIN": "寫入結果未確認；請重新讀取核對，同一決定編號重試不會重複生效。",
})


def _decode(model, raw):
    try:
        strict_json(raw)
        return model.model_validate_json(raw)
    except (ValueError, TypeError):
        raise ConsoleError("TEAM_SKILL_RESPONSE_INVALID", 502) from None


class TeamSkillsAdapter(PullAdapter):
    def call(self, method, path, token, *, body=None, if_match=None, params=None, limit=MAX_READ_BYTES):
        try:
            raw, headers = self.send(method, PREFIX + path, body, token, limit=limit, params=params, if_match=if_match)
        except ConsoleError as error:
            if error.code == "NO_RESULT" and error.upstream_status == 404:
                raise ConsoleError("TEAM_SKILLS_UNAVAILABLE", 409, 404) from None
            raise
        require(headers.get("cache-control") == "no-store", "TEAM_SKILL_RESPONSE_INVALID", 502)
        return raw, headers


class TeamSkills:
    def __init__(self, console):
        self.console = console
        self.adapter = TeamSkillsAdapter(console.config, console.backend)

    def _token(self, *, write=False):
        self.console.active()
        token = self.console.vault.get()
        if write:
            require(self.console.vault.allows("Behavior.Approve"), "TEAM_SKILLS_OWNER_ONLY", 403)
        return token

    def reviews(self, after=None):
        require(after is None or _ID.fullmatch(after), "TEAM_SKILL_INPUT_INVALID", 400)
        raw, _ = self.adapter.call("GET", "/reviews", self._token(), params=None if after is None else {"after": after})
        value = _decode(SkillReviewList, raw)
        require(value.after == after, "TEAM_SKILL_RESPONSE_INVALID", 502)
        return value.model_dump(mode="json")

    def _artifact(self, artifact, token):
        raw, headers = self.adapter.call("GET", "/skills/" + artifact, token)
        try:
            value = strict_json(raw)
            require(type(value) is dict and value.get("etag") == headers.get("etag"), "TEAM_SKILL_RESPONSE_INVALID", 502)
            document = ArtifactDocument.model_validate({key: item for key, item in value.items() if key != "etag"})
        except (ValueError, TypeError):
            raise ConsoleError("TEAM_SKILL_RESPONSE_INVALID", 502) from None
        require(document.artifact_id == artifact and document.kind == "skill", "TEAM_SKILL_RESPONSE_INVALID", 502)
        return document, value["etag"]

    def artifact(self, artifact):
        require(_ID.fullmatch(artifact), "TEAM_SKILL_INPUT_INVALID", 400)
        document, etag = self._artifact(artifact, self._token())
        return {"artifact": document.model_dump(mode="json"), "etag": etag}

    def version(self, artifact, version_id):
        require(_ID.fullmatch(artifact) and _VERSION.fullmatch(version_id), "TEAM_SKILL_INPUT_INVALID", 400)
        token = self._token()
        raw, _ = self.adapter.call("GET", f"/skills/{artifact}/versions/{version_id}", token)
        version = _decode(ArtifactVersion, raw)
        require(version.version_id == version_id, "TEAM_SKILL_RESPONSE_INVALID", 502)
        data, headers = self.adapter.call("GET", f"/skills/{artifact}/versions/{version_id}/content", token)
        require(headers.get("x-artifact-sha256") == version.sha256 == hashlib.sha256(data).hexdigest(),
                "TEAM_SKILL_RESPONSE_INVALID", 502)
        try:
            text = data.decode("utf-8")
        except UnicodeError:
            raise ConsoleError("TEAM_SKILL_RESPONSE_INVALID", 502) from None
        return {"version": version.model_dump(mode="json"), "text": text}

    def resource(self, artifact, version_id, name):
        require(_ID.fullmatch(artifact) and _VERSION.fullmatch(version_id) and _RESOURCE.fullmatch(name),
                "TEAM_SKILL_INPUT_INVALID", 400)
        raw, _ = self.adapter.call("GET", f"/skills/{artifact}/versions/{version_id}/resources/{name}", self._token())
        try:
            value = strict_json(raw)
        except (ValueError, TypeError):
            raise ConsoleError("TEAM_SKILL_RESPONSE_INVALID", 502) from None
        require(type(value) is dict and value.get("schema_version") == "fa-team-skill-resource-v1"
                and (value.get("artifact_id"), value.get("version_id"), value.get("name")) == (artifact, version_id, name)
                and type(value.get("text")) is str
                and value.get("sha256") == hashlib.sha256(value["text"].encode("utf-8")).hexdigest(),
                "TEAM_SKILL_RESPONSE_INVALID", 502)
        return value

    def _write(self, path, artifact, model, data):
        require(_ID.fullmatch(artifact) and type(data) is dict and type(data.get("etag")) is str
                and 0 < len(data["etag"]) <= 256, "TEAM_SKILL_INPUT_INVALID", 400)
        try:
            command = model.model_validate({key: value for key, value in data.items() if key != "etag"})
        except (ValueError, TypeError):
            raise ConsoleError("TEAM_SKILL_INPUT_INVALID", 400) from None
        token = self._token(write=True)
        raw, headers = self.adapter.call("POST", f"/skills/{artifact}/{path}", token,
                                         body=command.model_dump(mode="json"), if_match=data["etag"])
        try:
            value = strict_json(raw)
            require(type(value) is dict and value.get("etag") == headers.get("etag"), "TEAM_SKILL_RESPONSE_INVALID", 502)
            document = ArtifactDocument.model_validate({key: item for key, item in value.items() if key != "etag"})
        except (ValueError, TypeError):
            raise ConsoleError("TEAM_SKILL_RESPONSE_INVALID", 502) from None
        require(document.artifact_id == artifact and decided(document, command), "TEAM_SKILL_RESPONSE_INVALID", 502)
        return {"artifact": document.model_dump(mode="json"), "etag": value["etag"],
                "decision_id": command.decision_id, "original_request_resent": False}

    def decide(self, artifact, data):
        return self._write("decisions", artifact, ApprovalInput, data)

    def deactivate(self, artifact, data):
        return self._write("deactivations", artifact, SkillDeactivationInput, data)

    def draft(self, data):
        try:
            command = OwnerSkillDraftInput.model_validate(data)
        except (ValueError, TypeError):
            raise ConsoleError("TEAM_SKILL_DRAFT_INVALID", 400) from None
        token = self._token(write=True)
        raw, _ = self.adapter.call("POST", "/drafts", token, body=command.model_dump(mode="json"))
        try:
            value = strict_json(raw)
            require(type(value) is dict and set(value) == {"schema_version", "entry"}
                    and value["schema_version"] == "fa-team-owner-skill-draft-result-v1",
                    "TEAM_SKILL_RESPONSE_INVALID", 502)
            entry = SkillReviewEntry.model_validate_json(json.dumps(value["entry"]))
        except (ValueError, TypeError):
            raise ConsoleError("TEAM_SKILL_RESPONSE_INVALID", 502) from None
        from shared_context.behavior_native_turn_wire import material_digest
        # The draft that came back must be exactly this content, typed by the Team owner.
        require(entry.title == command.title and entry.owner == OWNER_AUTHOR
                and isinstance(entry.native_source, OwnerArtifactSource)
                and entry.native_source.submit_sha256 == material_digest(owner_material(command))
                and entry.latest_version is not None, "TEAM_SKILL_RESPONSE_INVALID", 502)
        return {"entry": entry.model_dump(mode="json"), "original_request_resent": False}


def decided(document, command):
    """The returned record holds exactly this decision (a repeat of the same ID returns the original)."""
    if isinstance(command, SkillDeactivationInput):
        return any(item.model_dump(include=set(SkillDeactivationInput.model_fields)) == command.model_dump()
                   for item in document.deactivations)
    return any(item.approval is not None
               and item.approval.model_dump(include=set(ApprovalInput.model_fields)) == command.model_dump()
               for item in document.versions)


def install_team_skill_routes(app, requested_console):
    from fastapi import Depends, Request
    from starlette.concurrency import run_in_threadpool

    def control(console):
        from .standing import StandingConsoleConfig
        require(type(console.config) is StandingConsoleConfig, "TEAM_SKILLS_UNAVAILABLE")
        return TeamSkills(console)

    async def read(request, allowed=frozenset()):
        require(set(request.query_params) <= allowed and all(len(request.query_params.getlist(name)) == 1
                for name in request.query_params), "LOCAL_REQUEST_DENIED", 403)
        async for chunk in request.stream():
            require(not chunk, "LOCAL_REQUEST_DENIED", 403)

    async def body(request, limit=MAX_WRITE_BYTES, code="TEAM_SKILL_INPUT_INVALID"):
        require(not request.query_params, "LOCAL_REQUEST_DENIED", 403)
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            require(len(raw) <= limit, code, 413)
        try:
            value = strict_json(bytes(raw))
        except (ValueError, TypeError):
            raise ConsoleError(code, 400) from None
        require(type(value) is dict, code, 400)
        return value

    @app.get("/api/team-skills/reviews")
    async def reviews(request: Request, console=Depends(requested_console)):
        await read(request, frozenset({"after"}))
        return await run_in_threadpool(control(console).reviews, request.query_params.get("after"))

    @app.get("/api/team-skills/skills/{artifact}")
    async def artifact(artifact: str, request: Request, console=Depends(requested_console)):
        await read(request)
        return await run_in_threadpool(control(console).artifact, artifact)

    @app.get("/api/team-skills/skills/{artifact}/versions/{version_id}")
    async def version(artifact: str, version_id: str, request: Request, console=Depends(requested_console)):
        await read(request)
        return await run_in_threadpool(control(console).version, artifact, version_id)

    @app.get("/api/team-skills/skills/{artifact}/versions/{version_id}/resources/{name}")
    async def resource(artifact: str, version_id: str, name: str, request: Request,
                       console=Depends(requested_console)):
        await read(request)
        return await run_in_threadpool(control(console).resource, artifact, version_id, name)

    @app.post("/api/team-skills/skills/{artifact}/decisions")
    async def decide(artifact: str, request: Request, console=Depends(requested_console)):
        return await run_in_threadpool(control(console).decide, artifact, await body(request))

    @app.post("/api/team-skills/skills/{artifact}/deactivations")
    async def deactivate(artifact: str, request: Request, console=Depends(requested_console)):
        return await run_in_threadpool(control(console).deactivate, artifact, await body(request))

    @app.post("/api/team-skills/drafts")
    async def draft(request: Request, console=Depends(requested_console)):
        value = await body(request, MAX_DRAFT_BYTES, "TEAM_SKILL_DRAFT_INVALID")
        return await run_in_threadpool(control(console).draft, value)


def is_team_skill_write(path):
    return path == "/api/team-skills/drafts" or re.fullmatch(
        r"/api/team-skills/skills/[A-Za-z0-9][A-Za-z0-9_-]{0,63}/(decisions|deactivations)", path) is not None
