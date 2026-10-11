"""Template references and human-selected new-conversation settings; never grants."""

from pydantic import TypeAdapter

from shared_context.behavior_agent_template import (
    AgentTemplate, MAX_TEMPLATE_BYTES, MAX_TEMPLATE_OPTIONS_BYTES, TemplateInspection, TemplateOptions,
)
from shared_context.behavior_profile_selection import (
    ConversationProfileReceipt, CurrentConversationProfile, SelectConversationProfile, selection_key,
)
from shared_context.behavior_operations import control_key
from shared_context.behavior_system_prompt_view import (
    MAX_SYSTEM_PROMPT_VIEW_BYTES, CurrentSystemPrompt, check_current_view,
)
from shared_context.source_materials import digest

from .adapter import ConsoleError, PullAdapter, canonical, require, sha, strict_json

PATH = "/work-policy/v1/agent-template"
MAX_SELECTION_BYTES = MAX_TEMPLATE_BYTES + 512
SELECTION_PHASE = "template-select"
RECONCILE_PHASE = "template-selection-reconcile"
_current = TypeAdapter(CurrentConversationProfile)
# The API raises these before its transaction writes anything (it reads the original receipt first and decides once),
# so a 4xx refusal with one of them means the selection was not applied.
REJECTED_BEFORE_WRITE = frozenset({
    "PROFILE_SELECTION_CONFLICT", "PROFILE_SELECTION_SOURCES_CHANGED", "PROFILE_SELECTION_DENIED",
    "PROFILE_SELECTION_INVALID", "PROFILE_SELECTION_UNSUPPORTED",
})
# Whatever code the adapter kept: /select answers these statuses only before its one control transaction commits
# (sign-in, body, permission and selection checks). 409/412 may be a racing commit; 429 and 5xx stay unknown.
PRE_WRITE_STATUSES = frozenset({400, 401, 403, 404, 413, 415, 422})


def rejected_before_write(code, upstream_status):
    return (code in REJECTED_BEFORE_WRITE and upstream_status in {403, 409, 422}) or upstream_status in PRE_WRITE_STATUSES


def parse_template(raw: bytes, *, code: str, status: int) -> AgentTemplate:
    require(0 < len(raw) <= MAX_TEMPLATE_BYTES, code, status)
    try:
        strict_json(raw)
        return AgentTemplate.model_validate_json(raw)
    except (ValueError, TypeError):
        raise ConsoleError(code, status) from None


class TemplateProfileUnsupported(ConsoleError):
    """The API has no versioned template for this Agent (no prompt profile and no selection).

    Callers still see TEMPLATE_UNAVAILABLE; only lane routing tells it apart, because such an Agent was never
    server-selected for another lane and keeps its original one.
    """


def _send(adapter, method, path, body, token):
    try:
        return adapter.send(method, path, body, token, limit=MAX_TEMPLATE_BYTES)
    except ConsoleError as error:
        if error.code == "AGENT_TEMPLATE_PROFILE_UNSUPPORTED":
            raise TemplateProfileUnsupported("TEMPLATE_UNAVAILABLE", 409, error.upstream_status) from None
        if error.upstream_status in {404, 409}:
            raise ConsoleError("TEMPLATE_UNAVAILABLE", 409, error.upstream_status) from None
        raise


def download(adapter: PullAdapter, token: str) -> bytes:
    raw, headers = _send(adapter, "GET", PATH, None, token)
    require(headers.get("cache-control") == "no-store"
            and headers.get("x-content-sha256") == sha(raw)
            and headers.get("content-type", "").split(";")[0] == "application/json",
            "TEMPLATE_RESPONSE_INVALID", 502)
    parse_template(raw, code="TEMPLATE_RESPONSE_INVALID", status=502)
    return raw


def inspect(adapter: PullAdapter, token: str, raw: bytes) -> TemplateInspection:
    imported = parse_template(raw, code="TEMPLATE_INVALID", status=400)
    response, headers = _send(adapter, "POST", PATH + "/inspect", imported.model_dump(mode="json"), token)
    require(headers.get("cache-control") == "no-store", "TEMPLATE_RESPONSE_INVALID", 502)
    try:
        strict_json(response)
        result = TemplateInspection.model_validate_json(response)
    except (ValueError, TypeError):
        raise ConsoleError("TEMPLATE_RESPONSE_INVALID", 502) from None
    names = tuple(sorted((*[item.name for item in imported.skills], *imported.unavailable_skills)))
    require(result.template_sha256 == sha(imported.model_dump_json().encode("utf-8"))
            and tuple(item.name for item in result.skills) == names
            and result.references_match == (result.role_matches and result.settings_match
                and result.selection_matches and all(item.status == "matches" for item in result.skills)),
            "TEMPLATE_RESPONSE_INVALID", 502)
    return result


def template_hash(template):
    return sha(template.model_dump_json().encode("utf-8"))


def options(adapter: PullAdapter, token: str) -> TemplateOptions:
    """Effective Team skills and available capabilities to add; references only, never a selection."""
    try:
        raw, headers = adapter.send("GET", PATH + "/options", None, token, limit=MAX_TEMPLATE_OPTIONS_BYTES)
    except ConsoleError as error:
        if error.upstream_status in {404, 409}:
            raise ConsoleError("TEMPLATE_UNAVAILABLE", 409, error.upstream_status) from None
        raise
    require(headers.get("cache-control") == "no-store"
            and headers.get("content-type", "").split(";")[0] == "application/json",
            "TEMPLATE_RESPONSE_INVALID", 502)
    try:
        strict_json(raw)
        return TemplateOptions.model_validate_json(raw)
    except (ValueError, TypeError):
        raise ConsoleError("TEMPLATE_RESPONSE_INVALID", 502) from None


def system_prompt(adapter: PullAdapter, token: str) -> CurrentSystemPrompt:
    """The API's rebuilt system prompt for new conversations; checked here, never composed here."""
    try:
        raw, headers = adapter.send("GET", PATH + "/system-prompt", None, token, limit=MAX_SYSTEM_PROMPT_VIEW_BYTES)
    except ConsoleError as error:
        if error.upstream_status == 404:
            raise ConsoleError("SYSTEM_PROMPT_UNAVAILABLE", 409, 404) from None
        raise
    require(headers.get("cache-control") == "no-store"
            and headers.get("content-type", "").split(";")[0] == "application/json",
            "SYSTEM_PROMPT_RESPONSE_INVALID", 502)
    try:
        strict_json(raw)
        return check_current_view(CurrentSystemPrompt.model_validate_json(raw))
    except (ValueError, TypeError):
        raise ConsoleError("SYSTEM_PROMPT_RESPONSE_INVALID", 502) from None


def intent_key(command):
    return "template-selection-" + sha(canonical({
        "expected_selection_id": command.expected_selection_id,
        "template_sha256": template_hash(command.template),
    }))


def parse_selection(raw):
    require(0 < len(raw) <= MAX_SELECTION_BYTES, "TEMPLATE_SELECTION_INVALID", 400)
    try:
        strict_json(raw)
        return SelectConversationProfile.model_validate_json(raw)
    except (ValueError, TypeError):
        raise ConsoleError("TEMPLATE_SELECTION_INVALID", 400) from None


def _selection_send(adapter, method, suffix, body, token):
    try:
        raw, headers = adapter.send(method, PATH + suffix, body, token, limit=MAX_SELECTION_BYTES)
    except ConsoleError as error:
        if error.upstream_status == 404:
            raise ConsoleError("TEMPLATE_SELECTION_UNAVAILABLE", 409, 404) from None
        raise
    require(headers.get("cache-control") == "no-store"
            and headers.get("content-type", "").split(";")[0] == "application/json",
            "TEMPLATE_SELECTION_RESPONSE_INVALID", 502)
    return raw


def read_selection(adapter, token):
    raw = _selection_send(adapter, "GET", "/selection", None, token)
    try:
        strict_json(raw)
        result = _current.validate_json(raw)
    except (ValueError, TypeError):
        raise ConsoleError("TEMPLATE_SELECTION_RESPONSE_INVALID", 502) from None
    if result.selection_id is not None:
        require(result.template_sha256 == template_hash(result.template),
                "TEMPLATE_SELECTION_RESPONSE_INVALID", 502)
    return result


def checked_receipt(config, command, raw):
    try:
        strict_json(raw)
        receipt = ConversationProfileReceipt.model_validate_json(raw)
    except (ValueError, TypeError):
        raise ConsoleError("TEMPLATE_SELECTION_RESPONSE_INVALID", 502) from None
    policy = config.standing_policy
    identifier = control_key("conversation-profile-id", *policy.scope_ids, command.request_id)
    require(receipt.result.selection_id == identifier
            and receipt.result.template_sha256 == template_hash(command.template)
            and receipt.control_request_id == selection_key(policy.authority_domain, identifier, policy.agent_id)
            and receipt.control_request_digest == digest(command.model_dump(mode="json")),
            "TEMPLATE_SELECTION_RESPONSE_INVALID", 502)
    return receipt


def select(adapter, token, command, *, reconcile=False):
    raw = _selection_send(adapter, "POST", "/selection-reconcile" if reconcile else "/select",
                          command.model_dump(mode="json"), token)
    return checked_receipt(adapter.config, command, raw)


def original(config, key, entry):
    require(entry is not None, "TEMPLATE_SELECTION_ORIGINAL_REQUIRED")
    try:
        intent = entry["intent"]
        body = canonical(intent["body"])
        command = SelectConversationProfile.model_validate_json(body)
        require(intent["config_sha256"] == config.sha256
                and intent["method"] == "POST" and intent["path"] == PATH + "/select"
                and body == canonical(command.model_dump(mode="json"))
                and intent["body_sha256"] == sha(body)
                and intent["template_sha256"] == template_hash(command.template)
                and key == intent_key(command),
                "TEMPLATE_SELECTION_ORIGINAL_INVALID", 502)
    except (KeyError, ValueError, TypeError):
        raise ConsoleError("TEMPLATE_SELECTION_ORIGINAL_INVALID", 502) from None
    return command


def history(config, journal):
    result = []
    for key, entry in journal.entries(SELECTION_PHASE):
        command = original(config, key, entry)
        require(entry["state"] in {"UNKNOWN", "CONFIRMED", "REJECTED"}, "TEMPLATE_SELECTION_ORIGINAL_INVALID", 502)
        # Read-time classification: older journals recorded these refusals as UNKNOWN; the journal is not rewritten.
        state = ("REJECTED" if entry["state"] != "CONFIRMED"
                 and rejected_before_write(entry.get("code"), entry.get("upstream_status")) else entry["state"])
        receipt = None
        if entry["state"] == "CONFIRMED":
            receipt = checked_receipt(config, command, canonical(entry["receipt"])).model_dump(mode="json")
        reconciliation = journal.get(key, RECONCILE_PHASE)
        reconciled_receipt = None
        if reconciliation is not None:
            require(reconciliation["state"] in {"UNKNOWN", "CONFIRMED"}
                    and reconciliation["intent"]["method"] == "POST"
                    and reconciliation["intent"]["path"] == PATH + "/selection-reconcile"
                    and reconciliation["intent"]["original_body_sha256"] == entry["intent"]["body_sha256"],
                    "TEMPLATE_SELECTION_ORIGINAL_INVALID", 502)
        if reconciliation is not None and reconciliation["state"] == "CONFIRMED":
            reconciled_receipt = checked_receipt(
                config, command, canonical(reconciliation["receipt"]),
            ).model_dump(mode="json")
        result.append({
            "request_id": command.request_id, "expected_selection_id": command.expected_selection_id,
            "template_sha256": entry["intent"]["template_sha256"], "state": state,
            "code": entry.get("code"), "created_at": entry["intent"]["created_at"],
            "receipt": receipt, "reconciliation": reconciled_receipt,
            "historical": True, "original_request_resent": False,
        })
    return result
