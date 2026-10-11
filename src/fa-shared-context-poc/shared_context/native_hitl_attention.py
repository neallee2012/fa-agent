"""Owner-only discovery and original-turn grouping; neither a new approval authority nor a turn dispatcher."""

import base64
import binascii
import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone

from pydantic import TypeAdapter

from .behavior_native_turn_wire import NativeRead, NativeTurnAddress, material_digest, native_turn_root
from .errors import Problem
from .native_hitl_wire import (
    AttentionCursor, NativeHitlAttentionOrigin, NativeHitlTeamAttentionEntry, NativeHitlTeamAttentionPage,
)

log = logging.getLogger(__name__)
_CURSOR = TypeAdapter(AttentionCursor)
# An open wait was written within its TTL, and the Table server stamps every write. This margin is a generous
# allowance for the clock difference between the API host and the Table server.
CLOCK_MARGIN = timedelta(minutes=5)


def _since(value):
    since = datetime.fromisoformat(value)
    if since.utcoffset() != timedelta(0) or since.isoformat() != value:
        raise ValueError("discovery window is not an original UTC bound")
    return since


def _cursor(value, team, now):
    """The Table continuation and the scan's fixed window; a cursor without a window resumes an unfiltered scan."""
    if value is None:
        return None, None
    try:
        _CURSOR.validate_python(value, strict=True)
        decoded = json.loads(base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True))
        if (set(decoded) not in ({"team", "after"}, {"team", "after", "since"}) or decoded["team"] != team
                or not isinstance(decoded["after"], dict) or not decoded["after"]
                or any(not isinstance(key, str) or not isinstance(item, (str, type(None)))
                       for key, item in decoded["after"].items())):
            raise ValueError("discovery cursor has another scope")
        since = _since(decoded["since"]) if "since" in decoded else None
        if since is not None and since > now:
            raise ValueError("discovery window starts in the future")
        return decoded["after"], since
    except (ValueError, TypeError, KeyError, binascii.Error):
        raise Problem(422, "NATIVE_HITL_INVALID_REQUEST", "use this Team's original discovery cursor") from None


def _next_cursor(token, team, since, now):
    if token is None:
        return None
    try:
        payload = {"team": team, "after": token}
        if since is not None:
            payload["since"] = since.isoformat()
        value = base64.urlsafe_b64encode(json.dumps(
            payload, separators=(",", ":"), allow_nan=False).encode("utf-8")).decode("ascii").rstrip("=")
        _cursor(value, team, now)
        return _CURSOR.validate_python(value, strict=True)
    except (ValueError, TypeError, Problem):
        raise Problem(503, "NATIVE_HITL_RECORD_INVALID", "the original discovery cursor is unavailable") from None


def owner_origin_reader(composition, principal):
    from .behavior_native_turn_api import NativeTurnGateway, configured_native_turns
    from .behavior_team_registry import resolve_agent_scope

    owner = getattr(composition, "_agent_message_owner", None) or composition
    cache = {}

    def address_scope(address):
        scope = resolve_agent_scope(owner.runtime.store, owner.config, address.agent_id)
        if (str(scope.tenant_id), scope.team_id) != (principal.tenant_id, principal.team):
            raise Problem(403, "NATIVE_STATE_SCOPE", "the original source belongs to another Team")

    def read_turn(address):
        address_scope(address)
        key = (address.agent_id, address.conversation_id, address.request_id)
        if key not in cache:
            try:
                gateway = NativeTurnGateway(owner.for_agent(address.agent_id), enabled=configured_native_turns())
                receipt, _response = gateway._reading(principal, lambda _control, client: gateway._invoke(
                    client, NativeRead(request_id=address.request_id)))
                if (receipt.agent_id, receipt.conversation_id, receipt.request_id) != key:
                    raise Problem(403, "NATIVE_STATE_SCOPE", "the source read differs from its original turn")
                cache[key] = receipt
            except Problem as error:
                cache[key] = error
        if isinstance(cache[key], Problem):
            raise cache[key]
        return cache[key]

    def origin(call):
        address = NativeTurnAddress(
            agent_id=call.agent_id, conversation_id=call.conversation_id, request_id=call.request_id)
        try:
            receipt = read_turn(address)
            root = native_turn_root(receipt)
            source = receipt.source
            sender = None if source is None else source.sender
            if source is not None:
                expected_root = material_digest({
                    "kind": "fa-native-peer-root-v1", "tenant": principal.tenant_id,
                    "team": principal.team, "origin": root.model_dump(mode="json"),
                })
                if source.recipient_agent_id != address.agent_id or source.root_id != expected_root:
                    raise Problem(403, "NATIVE_STATE_SCOPE", "the saved relay source differs from its original root")
                if native_turn_root(read_turn(sender)) != root:
                    raise Problem(403, "NATIVE_STATE_SCOPE", "the sender does not share this original root")
            original_root = read_turn(root)
            if native_turn_root(original_root) != root:
                raise Problem(403, "NATIVE_STATE_SCOPE", "the root does not identify its original request")
            return NativeHitlAttentionOrigin(state="verified", root=root, sender=sender)
        except Problem:
            log.warning("NATIVE_HITL_ORIGIN_UNAVAILABLE reason=original_source_unconfirmed")
            return NativeHitlAttentionOrigin()

    return origin


def read_team_attention(service, principal, *, cursor=None, limit=20, resolve_origin):
    service._authorize(principal, owner=True)
    if type(limit) is not int or not 1 <= limit <= 20:
        raise Problem(422, "NATIVE_HITL_INVALID_REQUEST", "Team attention requires a bounded page")
    now = service._now()
    after, since = _cursor(cursor, principal.team, now)
    if cursor is None:
        # Older rows are past every wait created under the longest TTL; continuation pages keep this same bound.
        since = (now - timedelta(seconds=max(service.wait_seconds.values())) - CLOCK_MARGIN).astimezone(timezone.utc)
    rows, token = service.store.read_record_page(limit=limit, continuation=after, modified_since=since)
    entries = []
    for identifier, record in rows:
        data = record.payload["data"]
        if (data.get("tenant_id"), data.get("team_id")) != (principal.tenant_id, principal.team):
            continue
        kind = data.get("kind")
        if kind == "native_hitl_question":
            view = service.read_question(principal, identifier)
        elif kind == "native_hitl_consent":
            view = service.read_consent(principal, identifier)
        elif kind == "native_hitl_pending" or (
                kind == "native_hitl_packed_record" and data.get("record_kind") == "native_hitl_pending"):
            view = service.read(principal, identifier)
        else:
            continue
        item = view.attention
        if item is None:
            continue
        call = (item.approval if item.type == "approval_required" else
                item.question if item.type == "question_required" else item.consent).call
        scope = service.resolve_agent(call.agent_id)
        if (str(scope.tenant_id), scope.team_id) != (principal.tenant_id, principal.team):
            raise Problem(403, "NATIVE_HITL_SCOPE_DENIED", "this pending call belongs to another Agent Team")
        origin = resolve_origin(call)
        if service._now() < item.expires_at:
            entries.append(NativeHitlTeamAttentionEntry(item=item, origin=origin))
    service._authorize(principal, owner=True)
    return NativeHitlTeamAttentionPage(
        team_id=principal.team, items=tuple(entries), next_cursor=_next_cursor(token, principal.team, since, now),
        observed_at=service._now())


def approval_code(pending_id):
    """Six digits derived from one approval card's ID: the handle a person types back (Teams relay), not a secret."""
    return "%06d" % (int(hashlib.sha256(pending_id.encode("utf-8")).hexdigest(), 16) % 1_000_000)


def find_approval_ids(service, principal, code):
    """IDs of this Team's approval cards, decided or not, inside the longest wait window whose code matches.

    A read-only scan like Team attention, but it keeps decided cards so a repeated command can read its own result."""
    service._authorize(principal)
    now = service._now()
    since = (now - timedelta(seconds=max(service.wait_seconds.values())) - CLOCK_MARGIN).astimezone(timezone.utc)
    matches, after = [], None
    while True:
        rows, after = service.store.read_record_page(limit=20, continuation=after, modified_since=since)
        for identifier, record in rows:
            data = record.payload["data"]
            if ((data.get("tenant_id"), data.get("team_id")) == (principal.tenant_id, principal.team)
                    and (data.get("kind") == "native_hitl_pending" or (
                        data.get("kind") == "native_hitl_packed_record"
                        and data.get("record_kind") == "native_hitl_pending"))
                    and approval_code(identifier) == code):
                matches.append(identifier)
        if after is None:
            return matches
