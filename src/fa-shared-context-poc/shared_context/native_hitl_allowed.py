"""Owner-only log of executed managed calls, read from existing create-only rows; no new index or row."""

import base64
import binascii
import json
from datetime import timedelta, timezone

from pydantic import TypeAdapter

from .behavior_operations import _parse
from .errors import Problem
from .native_hitl import _Authorization, _Intent
from .native_hitl_attention import _since
from .native_hitl_audit_wire import NativeHitlAllowedEntry, NativeHitlAllowedPage, allowed_by
from .native_hitl_wire import NATIVE_HITL_HTTP_BYTES, AttentionCursor, NativeHitlAuthorize, hitl_key, pending_id

_CURSOR = TypeAdapter(AttentionCursor)
DEFAULT_WINDOW = timedelta(hours=24)
SCAN_ROWS = 20
# One action can be about NATIVE_HITL_BODY_BYTES, so a page stops early and resumes inside the same scan page.
PAGE_BYTES = NATIVE_HITL_HTTP_BYTES - 32 * 1024


def _invalid(message):
    return Problem(409, "NATIVE_HITL_RECORD_INVALID", message)


def _encode(team, since, after, last):
    payload = {"team": team, "since": since.isoformat(), "after": after, "last": last}
    value = base64.urlsafe_b64encode(json.dumps(
        payload, separators=(",", ":"), allow_nan=False).encode("utf-8")).decode("ascii").rstrip("=")
    try:
        return _CURSOR.validate_python(value, strict=True)
    except ValueError:
        raise Problem(503, "NATIVE_HITL_RECORD_INVALID", "the allowed-log cursor is unavailable") from None


def _decode(value, team, now):
    """(Table continuation or None, last emitted row in that page or None, the scan's fixed window)."""
    try:
        _CURSOR.validate_python(value, strict=True)
        decoded = json.loads(base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True))
        after, last = decoded["after"], decoded["last"]
        if (set(decoded) != {"team", "since", "after", "last"} or decoded["team"] != team
                or after is None and last is None
                or after is not None and (not isinstance(after, dict) or not after or any(
                    not isinstance(key, str) or not isinstance(item, (str, type(None))) for key, item in after.items()))
                or last is not None and not isinstance(last, str)):
            raise ValueError("allowed-log cursor has another scope")
        since = _since(decoded["since"])
        if since > now:
            raise ValueError("allowed-log window starts in the future")
        return after, last, since
    except (ValueError, TypeError, KeyError, binascii.Error):
        raise Problem(422, "NATIVE_HITL_INVALID_REQUEST", "use this Team's original allowed-log cursor") from None


def _entry(service, principal, identifier, record):
    value = _parse(record.payload["data"], _Authorization)
    authorization = value.authorization
    call = authorization.call
    if identifier != authorization.authorization_id or identifier != hitl_key(
            "authorization", principal.tenant_id, principal.team, pending_id(principal.tenant_id, principal.team, call)):
        raise _invalid("the authorization key does not identify its original call")
    row = service.store.read_record(value.intent_id)
    if row is None:
        raise _invalid("the authorization's original intent is missing")
    intent = service._load(row, value.intent_id, _Intent)
    command = intent.command
    if (intent.intent_id != value.intent_id or (intent.tenant_id, intent.team_id) != (principal.tenant_id, principal.team)
            or intent.principal_kind != "agent" or not isinstance(command, NativeHitlAuthorize)
            or (command.action.call, command.action_digest, command.review, command.approval, command.effect_token)
            != (call, authorization.action_digest, authorization.review, authorization.approval,
                authorization.effect_token)):
        raise _invalid("the authorization differs from its original claim")
    return NativeHitlAllowedEntry(
        authorization_id=identifier, intent_id=intent.intent_id, authorized_at=intent.created_at,
        action=command.action, review=command.review, approval=command.approval,
        by=allowed_by(command.review, command.approval))


def read_team_allowed(service, principal, *, since=None, cursor=None):
    service._authorize(principal, owner=True)
    now = service._now()
    if cursor is not None:
        if since is not None:
            raise Problem(422, "NATIVE_HITL_INVALID_REQUEST", "a continuation keeps its original window")
        after, last, window = _decode(cursor, principal.team, now)
    else:
        try:
            window = (now - DEFAULT_WINDOW).astimezone(timezone.utc) if since is None else _since(since)
        except (ValueError, TypeError):
            raise Problem(422, "NATIVE_HITL_INVALID_REQUEST", "the window is one original UTC time") from None
        if window > now:
            raise Problem(422, "NATIVE_HITL_INVALID_REQUEST", "the window starts in the future")
        after, last = None, None
    rows, token = service.store.read_record_page(limit=SCAN_ROWS, continuation=after, modified_since=window)
    identifiers = [identifier for identifier, _ in rows]
    if identifiers != sorted(set(identifiers)):
        # Resuming inside a page skips rows up to the last one shown, which needs the Table's RowKey order.
        raise Problem(503, "NATIVE_HITL_RECORD_INVALID", "the scan page is not in its original key order")
    entries, size, resume = [], 0, None
    for identifier, record in rows:
        data = record.payload["data"]
        if (last is not None and identifier <= last or data.get("kind") != "native_hitl_authorization"
                or (data.get("tenant_id"), data.get("team_id")) != (principal.tenant_id, principal.team)):
            continue
        entry = _entry(service, principal, identifier, record)
        weight = len(entry.model_dump_json().encode("utf-8"))
        if entries and size + weight > PAGE_BYTES:
            resume = entries[-1].authorization_id
            break
        entries.append(entry)
        size += weight
    service._authorize(principal, owner=True)
    # Keep skipping up to the last shown row: a re-read page can shift when rows land before it.
    next_cursor = (_encode(principal.team, window, after, resume) if resume is not None
                   else None if token is None else _encode(principal.team, window, token, last))
    return NativeHitlAllowedPage(team_id=principal.team, since=window, entries=tuple(entries),
                                 next_cursor=next_cursor, observed_at=service._now())
